"""The reaper of Tutti's stray helper processes (tutti/procs.py) on a fake /proc, and the helpers' singletons."""
import os
import signal
import subprocess
import tempfile
import unittest
from unittest import mock

from tutti import prep, procs, selfheal

UID = os.getuid()


class FakeProc:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def add(self, pid, argv, env, ppid=1, uid=UID):
        d = os.path.join(self.root, str(pid))
        os.makedirs(d)
        with open(os.path.join(d, 'status'), 'w', encoding='utf8') as f:
            f.write(f'Name:\tx\nPPid:\t{ppid}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n')
        with open(os.path.join(d, 'cmdline'), 'wb') as f:
            f.write(b'\0'.join(a.encode() for a in argv) + b'\0')
        with open(os.path.join(d, 'environ'), 'wb') as f:
            f.write(b'\0'.join(f'{k}={v}'.encode() for k, v in env.items()) + b'\0')


LOOP = 'while :; do live=; for c in "$HOME"/.tutti/devices/pixel/users/claim-*; do :; done; rm -f x/../awake.pid; done'


class ReaperTest(unittest.TestCase):
    def setUp(self):
        self.p = FakeProc()
        self.addCleanup(self.p.tmp.cleanup)
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        pid_patch = mock.patch('os.getpid', return_value=999999)
        pid_patch.start()
        self.addCleanup(pid_patch.stop)

    def test_strays_are_stopped_by_exact_pid_and_logged_and_owned_helpers_stay(self):
        h = self.home.name
        os.makedirs(os.path.join(h, '.tutti', 'devices', 'pixel'))
        with open(os.path.join(h, '.tutti', 'devices', 'pixel', 'awake.pid'), 'w', encoding='utf8') as f:
            f.write('11\n')
        with open(os.path.join(h, 'tunnel-big.pid'), 'w', encoding='utf8') as f:
            f.write('21\n')
        self.p.add(5, ['python3', 'bin/tutti', 'dashboard'], {'HOME': h})
        self.p.add(10, ['bash', '-c', LOOP], {'HOME': '/gone/home'})                          # a test's home removed
        self.p.add(11, ['bash', '-c', LOOP], {'HOME': h, 'TUTTI_PROC': 'claim:pixel'})        # the device's one loop
        self.p.add(12, ['bash', '-c', LOOP], {'HOME': h})                                     # a second loop
        self.p.add(13, ['bash', '-c', LOOP], {'HOME': h, 'TUTTI_TEST_ROOT': '/gone/root'})    # its test run is over
        self.p.add(20, ['bash', '-c', 'loop "$@"', 'loop', 'a', 'b', 'c'], {'HOME': h, 'TUTTI_STATE': '/gone'})
        self.p.add(21, ['bash', '-c', 'loop "$@"', 'loop', 'a', 'b', 'c'], {'HOME': h, 'TUTTI_STATE': h, 'TUTTI_PROC': 'tunnel:big'})
        self.p.add(22, ['bash', '-c', 'loop "$@"', 'loop', 'a', 'b', 'c'], {'HOME': h, 'TUTTI_STATE': h, 'TUTTI_PROC': 'tunnel:other'})
        self.p.add(30, ['systemd-inhibit', '--who=tutti', 'sleep', 'infinity'], {'HOME': h}, ppid=1)   # orphaned
        self.p.add(31, ['systemd-inhibit', '--who=tutti', 'tail'], {'HOME': h}, ppid=5)                # its dashboard lives
        self.p.add(32, ['systemd-inhibit', '--who=tutti'], {'HOME': h, 'TUTTI_PROC': 'inhibit', 'TUTTI_PROC_OWNER': '99'}, ppid=5)
        self.p.add(40, ['bash', '-c', LOOP], {'HOME': '/gone'}, uid=UID + 1)                   # another user's: never touched
        self.p.add(41, ['sleep', '5'], {'HOME': '/gone'})                                      # not a Tutti helper
        old = os.path.getmtime(self.p.root) - 3600
        for n in os.listdir(self.p.root):
            os.utime(os.path.join(self.p.root, n), (old, old))   # none of them just started
        self.p.add(23, ['bash', '-c', 'loop "$@"', 'loop', 'a', 'b', 'c'], {'HOME': h, 'TUTTI_STATE': h, 'TUTTI_PROC': 'tunnel:x'})
        killed, logged = [], []
        lines = procs.reap(logged.append, proc=self.p.root, kill=lambda pid, sig: killed.append((pid, sig)))
        self.assertEqual(sorted(pid for pid, _ in killed), [10, 12, 13, 20, 22, 30, 32])
        self.assertTrue(all(sig == signal.SIGTERM for _, sig in killed))
        self.assertEqual(logged, lines)
        # 23: a second loop of tunnel x, but it just started (its pid file may be on its way): left for the next round
        self.assertIn('stopped claim 10: its home folder /gone/home is gone', lines)
        self.assertTrue(any(ln.startswith('stopped inhibit 30: its Tutti process 1 is gone') for ln in lines))
        self.assertEqual(procs.reap(logged.append, proc=self.p.root, dry=True, kill=None)[0][:10], 'would stop')

    def test_a_refused_or_finished_kill_is_said_and_never_raised(self):
        self.p.add(10, ['bash', '-c', LOOP], {'HOME': '/gone'})
        self.p.add(11, ['bash', '-c', LOOP], {'HOME': '/gone2'})

        def kill(pid, sig):
            raise PermissionError if pid == 10 else ProcessLookupError
        lines = procs.reap(lambda ln: None, proc=self.p.root, kill=kill)
        self.assertEqual(lines, ['could not stop claim 10 (not allowed): its home folder /gone is gone'])
        self.assertIsNone(procs.info(999, self.p.root))
        self.assertIsNone(procs.kind({'argv': ['bash'], 'env': {}}))

    def test_tests_start_no_helper_and_the_self_heal_round_never_reaps_in_tests(self):
        self.assertFalse(procs.daemons_allowed())   # tests/run.py sets TUTTI_NO_DAEMONS
        self.assertTrue(procs.daemons_allowed({}))
        self.assertEqual(selfheal.check_strays(selfheal.Ctx({})), [])
        ctx = selfheal.Ctx({})
        ctx.reap = lambda log: ['stopped claim 1: x']
        self.assertEqual(selfheal.check_strays(ctx), ['stopped claim 1: x'])

    def test_the_lease_loop_is_one_per_device_ends_without_its_folder_and_carries_the_marker(self):
        dev = {'name': 'pixel', 'serial': 'R5'}
        self.assertNotIn('nohup', prep.lease_script(dev, 'c', 5, 'me'))   # tests: no background loop at all
        with mock.patch.dict(os.environ, {'TUTTI_NO_DAEMONS': ''}):
            s = prep.lease_script(dev, 'c', 5, 'me')
        self.assertIn('TUTTI_PROC=claim:pixel', s)
        self.assertIn('= "$$" ] || break', s)    # a loop awake.pid does not name ends
        self.assertIn('|| break 2', s)           # and one whose folder is gone never spins


class TunnelScriptTest(unittest.TestCase):
    def test_tests_start_no_tunnel_loop(self):
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts', 'tunnel.sh')
            r = subprocess.run([script, 'start', 'w', '127.0.0.1:1', 'test.invalid', '127.0.0.1:2'], capture_output=True, text=True,
                               env={**os.environ, 'TUTTI_STATE': d, 'TUTTI_NO_DAEMONS': '1'}, timeout=10)
            self.assertIn('not started', r.stdout)
            self.assertFalse(os.path.exists(os.path.join(d, 'tunnel-w.pid')))
