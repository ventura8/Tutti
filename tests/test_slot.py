"""scripts/slot.sh with temp lock files and a fake thermal zone: a run waiting in the queue re-reads the heat before it starts."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from tests.flaky import flaky
from tutti import remote

try:
    import fcntl
except ImportError:          # Windows: slot.sh runs in WSL or Git Bash there, this test only where flock(2) is
    fcntl = None

SLOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts', 'slot.sh')


@unittest.skipUnless(fcntl and shutil.which('flock'), 'needs flock(1) and fcntl, as on the Linux machines slot.sh runs on')
class SlotHeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.zone = os.path.join(self.tmp.name, 'temp')
        self.out = os.path.join(self.tmp.name, 'started')

    def heat(self, millidegrees):
        with open(self.zone, 'w') as f:
            f.write(f'{millidegrees}\n')

    def start(self, **env):
        locks = ' '.join(os.path.join(self.tmp.name, f'{i}.lock') for i in (1, 2))
        env = {**os.environ, 'SLOT_LOCKS': locks, 'MIN_FREE_GB': '0', 'SLOT_POLL': '0.1', 'THERMAL_GLOB': self.zone,
               'NVIDIA_SMI': 'none', **env}
        p = subprocess.Popen(['bash', SLOT, 'touch', self.out], env=env, stderr=subprocess.PIPE, text=True)
        self.addCleanup(p.stderr.close)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def started_within(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if os.path.exists(self.out):
                return True
            time.sleep(0.05)
        return False

    @flaky
    def test_a_waiting_run_starts_only_once_the_machine_cools(self):
        self.heat(99000)
        p = self.start(HEAT_STOP_C='98')
        self.assertFalse(self.started_within(0.8))
        self.heat(60000)
        self.assertTrue(self.started_within(5))
        self.assertEqual(p.wait(5), 0)
        self.assertIn('too hot: 99 C', p.stderr.read())

    @flaky
    def test_hot_caps_the_slots_but_a_cool_machine_runs_at_once(self):
        self.heat(90000)
        with open(os.path.join(self.tmp.name, '1.lock'), 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)              # another run holds a slot
            self.start(HEAT_CAP_C='85', HOT_SLOTS='1')
            self.assertFalse(self.started_within(0.8))   # at 90 C only one run at a time
        self.assertTrue(self.started_within(5))           # that run ended: this one starts

    @flaky
    def test_a_hot_gpu_holds_a_run_too(self):
        self.heat(40000)
        smi = os.path.join(self.tmp.name, 'smi')
        with open(smi, 'w') as f:
            f.write('#!/bin/sh\necho 99.20\n')
        os.chmod(smi, 0o755)
        p = self.start(HEAT_STOP_C='98', NVIDIA_SMI=smi)
        self.assertFalse(self.started_within(0.8))
        with open(smi, 'w') as f:
            f.write('#!/bin/sh\necho 50\n')
        self.assertTrue(self.started_within(5))
        self.assertEqual(p.wait(5), 0)

    @flaky
    def test_without_heat_limits_or_a_readable_zone_it_runs(self):
        self.start()
        self.assertTrue(self.started_within(5))
        os.remove(self.out)
        self.start(HEAT_STOP_C='98', THERMAL_GLOB=os.path.join(self.tmp.name, 'none*'))
        self.assertTrue(self.started_within(5))

    def test_the_machine_heat_reaches_slot_sh(self):
        m = {'min_free_gb': 6, 'slots': 2, 'heat': {'cap_c': 85, 'stop_c': 98, 'hot_slots': 1}, 'lock_prefix': '/tmp/x-'}
        line = remote.slot_prefix(m)
        self.assertIn('HEAT_STOP_C=98 HEAT_CAP_C=85 HOT_SLOTS=1', line)
        self.assertNotIn('HEAT', remote.slot_prefix({**m, 'heat': {}}))



@unittest.skipUnless(fcntl and shutil.which('flock'), 'needs flock(1) and fcntl')
class SlotSharedTest(unittest.TestCase):
    """A household's shared machine: a lock another login made (here: read-only for us) is opened read-only and still locks."""

    def test_a_read_only_lock_file_still_takes_the_slot_and_a_missing_one_is_made(self):
        with tempfile.TemporaryDirectory() as d:
            ro, new, out = os.path.join(d, '1.lock'), os.path.join(d, '2.lock'), os.path.join(d, 'ran')
            open(ro, 'w').close()
            os.chmod(ro, 0o444)
            env = {**os.environ, 'SLOT_LOCKS': ro, 'MIN_FREE_GB': '0', 'SLOT_SHARED': '1', 'NVIDIA_SMI': 'none',
                   'THERMAL_GLOB': os.path.join(d, 'none')}
            self.assertEqual(subprocess.run(['bash', SLOT, 'touch', out], env=env, capture_output=True).returncode, 0)
            self.assertTrue(os.path.exists(out))
            env['SLOT_LOCKS'] = new
            self.assertEqual(subprocess.run(['bash', SLOT, 'true'], env=env, capture_output=True).returncode, 0)
            self.assertEqual(os.stat(new).st_mode & 0o777, 0o666)


    def test_a_planted_link_is_never_followed_and_the_run_keeps_the_callers_umask(self):
        # exploit (audit 2026-11, H5): a dangling link in the shared lock folder made a file in the victim's home, and the
        # whole run went with umask 0 (every file it made writable by every login)
        with tempfile.TemporaryDirectory() as d:
            target, lock, out = os.path.join(d, 'victim-bashrc'), os.path.join(d, '1.lock'), os.path.join(d, 'made')
            os.symlink(target, lock)
            env = {**os.environ, 'SLOT_LOCKS': lock, 'MIN_FREE_GB': '0', 'SLOT_SHARED': '1', 'NVIDIA_SMI': 'none',
                   'THERMAL_GLOB': os.path.join(d, 'none')}
            res = subprocess.run(['bash', SLOT, 'true'], env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 2)
            self.assertIn('symbolic link', res.stderr)
            self.assertFalse(os.path.exists(target))
            os.remove(lock)
            res = subprocess.run(['bash', '-c', f'umask 077; bash {SLOT} touch {out}'], env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)   # the caller's umask, not 0
            self.assertEqual(os.stat(lock).st_mode & 0o777, 0o666)  # only the lock is readable by every login

if __name__ == '__main__':
    unittest.main()
