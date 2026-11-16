"""Quiet mode (tutti/quietmode.py) and GNOME Wayland idle detection (tutti/share.py): a person at the laptop is seen, Tutti's
own jobs are calmed or stopped by exact pid, commands are wrapped, `tutti quiet status` answers by exit code."""
import os
import shutil
import signal
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

from tutti import quietmode, share


def runner(answers):
    def run(argv, **kw):
        for key, out in answers.items():
            if key in ' '.join(argv):
                return SimpleNamespace(returncode=0, stdout=out)
        return SimpleNamespace(returncode=1, stdout='')
    return run


SESSIONS = {'list-sessions': '2 1000 me seat0 6475 user tty2 no -\n',
            'show-session 2': 'Type=wayland\nIdleHint=no\nIdleSinceHint=0\nLockedHint=no\n'}


class IdleTest(unittest.TestCase):
    def setUp(self):
        real = shutil.which
        self.addCleanup(setattr, shutil, 'which', real)
        shutil.which = lambda n: '/usr/bin/' + n if n in ('gdbus', 'loginctl') else None

    def test_wayland_gnome_idle_read_from_mutter(self):
        run = runner({**SESSIONS, 'GetIdletime': '(uint64 42000,)\n'})
        self.assertEqual(share.idle_seconds(run), 42.0)

    def test_gnome_without_hint_and_no_mutter_is_unknown(self):
        self.assertIsNone(share.idle_seconds(runner(SESSIONS)))

    def test_locked_screen_reads_away(self):
        run = runner({**SESSIONS, 'show-session 2': 'Type=wayland\nLockedHint=yes\n', 'GetIdletime': '(uint64 1,)'})
        self.assertGreater(share.idle_seconds(run), 3600)
        self.assertTrue(share.screen_locked(run))

    def test_person_takes_the_whole_share(self):
        self.assertEqual(share.pct({}, None, None, idle_s=10)[0], 0)


class QuietTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cfg = {'project': {'state': self.tmp}, 'machines': {'laptop': {'transport': 'local'}}}

    def test_status_person_and_memory(self):
        st = quietmode.status(self.cfg, lambda: 5.0, lambda: 2.0)
        self.assertTrue(st['quiet'] and st['person'] and st['low_memory'])
        free = quietmode.status(self.cfg, lambda: 3600.0, lambda: 9.0)
        self.assertFalse(free['quiet'])
        self.assertTrue(quietmode.status(self.cfg, lambda: None, lambda: 1.0)['low_memory'])

    def test_wrap_only_while_quiet(self):
        def which(n):
            return '/usr/bin/' + n
        self.assertEqual(quietmode.wrap(['make'], self.cfg, {'quiet': False}), ['make'])
        argv = quietmode.wrap(['make'], self.cfg, {'quiet': True}, which)
        self.assertEqual(argv[:3], ['systemd-run', '--user', '--scope'])
        self.assertIn('CPUQuota=200%', argv)
        self.assertEqual(argv[-1], 'make')

    def test_guard_stops_and_continues_exact_pids(self):
        sent = []

        def kill(pid, sig):
            sent.append((pid, sig))

        def run(*a, **k):
            return None
        st = {'quiet': True, 'person': True, 'low_memory': True}
        quietmode.guard_round(self.cfg, st, {101, 102}, kill, run, unload=lambda c: ['m'])
        self.assertEqual(sorted(sent), [(101, signal.SIGSTOP), (102, signal.SIGSTOP)])
        sent.clear()
        acts = quietmode.guard_round(self.cfg, {'quiet': False, 'person': False, 'low_memory': False}, {101, 102}, kill, run,
                                     unload=lambda c: [])
        self.assertEqual(sorted(sent), [(101, signal.SIGCONT), (102, signal.SIGCONT)])
        self.assertTrue(any('continued' in a for a in acts))

    def test_own_pids_follow_the_pid_file_tree(self):
        d = os.path.join(self.tmp, 'quiet')
        os.makedirs(d)
        child = subprocess.Popen(['sleep', '30'])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        with open(os.path.join(d, '1-1.log.pid'), 'w') as f:
            f.write(str(os.getpid()))
        self.assertIn(child.pid, quietmode.own_pids(self.cfg))
        with open(os.path.join(d, '1-1.log.rc'), 'w') as f:
            f.write('0')
        self.assertEqual(quietmode.own_pids(self.cfg), set())

    def test_wait_free(self):
        seq = iter([True, False])
        ok = quietmode.wait_free(self.cfg, None, 1, lambda s: None, status_fn=lambda c: {'quiet': next(seq)})
        self.assertTrue(ok)
        self.assertFalse(quietmode.wait_free(self.cfg, 0, 1, lambda s: None, lambda: 5, lambda c: {'quiet': True}))


if __name__ == '__main__':
    unittest.main()
