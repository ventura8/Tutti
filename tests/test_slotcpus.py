"""A slot's CPU budget: the arithmetic, the shell line a run starts with, the docker pass-through and the config keys."""
import os
import shlex
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone

from tutti import config, remote, share, slotcpus, tz

M = {'name': 'pc', 'transport': 'local', 'slots': 2, 'runner': {'kind': 'shell', 'memory_gb': 12, 'cpus': 8, 'env': {}, 'args': []},
     'heat': {}, 'cores': 28}


SLOT = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'slot.sh')
NAMES = ('TUTTI_SLOT_CPUS', 'PLAYWRIGHT_WORKERS', 'VITEST_MAX_THREADS', 'VITEST_MIN_THREADS', 'MAKEFLAGS')


def evaluate(machine, env=None, now=None, temp_c=None, zone='UTC'):
    """Run scripts/slot.sh the way remote.run() does, with a fake lock folder, a fake thermal file and a fake clock, and print
    the variables the command sees: no machine is touched."""
    with tempfile.TemporaryDirectory() as d:
        thermal = os.path.join(d, 'temp')
        if temp_c is not None:
            with open(thermal, 'w') as f:
                f.write(f'{temp_c * 1000}\n')
        prefix = remote.slot_prefix({**machine, 'min_free_gb': 0, 'lock_prefix': '/tmp/x-'}, zone)
        words = dict(w.split('=', 1) for w in shlex.split(prefix))
        words.pop('SLOT_LOCKS')
        e = {**os.environ, **words, 'SLOTS': '1', 'SLOT_LOCK_PREFIX': os.path.join(d, 's-'), 'MIN_FREE_GB': '0',
             'THERMAL_GLOB': thermal, 'NVIDIA_SMI': 'none', **(env or {})}
        for n in NAMES:
            if n not in (env or {}):
                e.pop(n, None)
        if now is not None:
            e['SLOT_NOW'] = str(int(now.timestamp()))
        out = subprocess.run(['bash', SLOT, 'bash', '-c', 'echo "' + '|'.join(f'${n}' for n in NAMES) + '"'], capture_output=True,
                             text=True, env=e, check=True, timeout=60)
        return out.stdout.strip().split('|')


class BudgetTest(unittest.TestCase):
    def test_cpus_are_shared_among_the_slots(self):
        self.assertEqual(slotcpus.budget(28, 2), 14)
        self.assertEqual(slotcpus.budget(28, 3), 9)

    def test_a_limited_window_scales_it_and_it_is_never_below_one(self):
        self.assertEqual(slotcpus.budget(28, 2, pct=40), 5)
        self.assertEqual(slotcpus.budget(2, 4), 1)

    def test_an_override_wins(self):
        self.assertEqual(slotcpus.budget(28, 2, override=3), 3)


class SlotBudgetTest(unittest.TestCase):
    def test_every_knob_is_set_from_the_budget(self):
        self.assertEqual(evaluate({**M, 'slots': 2}), ['14', '14', '14', '1', '-j14'])

    def test_a_knob_the_caller_set_is_kept(self):
        self.assertEqual(evaluate(M, {'PLAYWRIGHT_WORKERS': '3', 'MAKEFLAGS': '-j2'}), ['14', '3', '14', '1', '-j2'])
        self.assertEqual(evaluate(M, {'TUTTI_SLOT_CPUS': '5'})[:2], ['5', '5'])

    def test_slot_cpus_overrides_the_computed_value(self):
        self.assertEqual(evaluate({**M, 'slot_cpus': 4})[:2], ['4', '4'])

    def test_the_machine_cpus_are_read_when_not_configured(self):
        self.assertGreaterEqual(int(evaluate({k: v for k, v in M.items() if k != 'cores'})[0]), 1)

    def test_a_docker_runner_cap_bounds_it(self):
        m = {**M, 'slots': 1, 'runner': {**M['runner'], 'kind': 'docker', 'cpus': 6}}
        self.assertEqual(evaluate(m)[0], '6')

    def test_limited_hours_apply_their_share_at_the_start(self):
        m = {**M, 'limit': {'window': '10-14', 'pct': 40}}
        for hour, want in ((12, '5'), (9, '14'), (14, '14')):
            with self.subTest(hour=hour):
                self.assertEqual(evaluate(m, now=datetime(2026, 11, 25, hour, 0, tzinfo=timezone.utc))[0], want)
        self.assertEqual(evaluate({k: v for k, v in M.items() if k != 'limit'})[0], '14')

    def test_the_window_reads_the_hours_in_the_zone_and_the_days(self):
        m = {**M, 'limit': {'window': '18-01', 'days': ['wed'], 'pct': 50}}
        def at(day, hour):   # 25 Nov 2026 is a Wednesday
            return datetime(2026, 11, day, hour, 30, tzinfo=timezone.utc)

        self.assertEqual(evaluate(m, now=at(25, 19))[0], '7')
        self.assertEqual(evaluate(m, now=at(26, 0))[0], '7')      # past midnight: still Wednesday's window
        self.assertEqual(evaluate(m, now=at(26, 19))[0], '14')    # Thursday
        self.assertEqual(evaluate(m, now=at(25, 12))[0], '14')
        self.assertEqual(evaluate(m, now=at(25, 12), zone='Asia/Tokyo')[0], '7')   # 21:30 there

    def test_the_bash_window_agrees_with_share_limited(self):
        m = {**M, 'slots': 1, 'limit': {'window': '22-06', 'days': ['mon', 'fri'], 'pct': 25}}
        for day in range(23, 30):
            for hour in (0, 5, 6, 12, 21, 22, 23):
                at = datetime(2026, 11, day, hour, 15, tzinfo=timezone.utc)
                want = str(max(1, int(28 * (share.limited(m, at) or 100) / 100)))
                with self.subTest(at=at):
                    self.assertEqual(evaluate(m, now=at, zone=tz.zone_name())[0], want)

    def test_a_hot_machine_takes_its_throttle_cpus_when_the_run_starts(self):
        m = {**M, 'slots': 1, 'heat': {'cap_c': 85, 'throttle_cpus': 2}}
        self.assertEqual(evaluate(m, temp_c=90)[0], '2')
        self.assertEqual(evaluate(m, temp_c=84)[0], '28')
        self.assertEqual(evaluate(m)[0], '28')   # no reading: not hot

    def test_the_budget_is_worked_out_after_the_lock_not_before(self):
        with tempfile.TemporaryDirectory() as d:
            m = {**M, 'slots': 1, 'heat': {'cap_c': 85, 'throttle_cpus': 2}}
            thermal = os.path.join(d, 'temp')
            with open(thermal, 'w') as f:
                f.write('50000\n')
            words = dict(w.split('=', 1) for w in shlex.split(remote.slot_prefix({**m, 'min_free_gb': 0, 'lock_prefix': '/tmp/x-'}, 'UTC')))
            words.pop('SLOT_LOCKS')
            e = {**os.environ, **words, 'SLOTS': '1', 'SLOT_LOCK_PREFIX': os.path.join(d, 's-'), 'MIN_FREE_GB': '0',
                 'THERMAL_GLOB': thermal, 'NVIDIA_SMI': 'none', 'SLOT_POLL': '0.2'}
            e.pop('TUTTI_SLOT_CPUS', None)
            holder = subprocess.Popen(['bash', SLOT, 'sleep', '2'], env=e)
            self.addCleanup(holder.wait)
            for _ in range(50):
                if os.path.exists(os.path.join(d, 's-1.lock.who')):
                    break
                time.sleep(0.1)
            waiter = subprocess.Popen(['bash', SLOT, 'bash', '-c', 'echo $TUTTI_SLOT_CPUS'], env=e, stdout=subprocess.PIPE, text=True)
            time.sleep(0.5)   # queued while the machine is cool...
            with open(thermal, 'w') as f:
                f.write('95000\n')   # ...and hot by the time the slot frees
            out, _ = waiter.communicate(timeout=30)
            self.assertEqual(out.strip(), '2')

    def test_nothing_is_set_when_it_is_off(self):
        self.assertNotIn('SLOT_CPU', remote.slot_prefix({**M, 'min_free_gb': 0, 'lock_prefix': '/tmp/x-'}))

    def test_the_inputs_are_one_shell_word_list_that_parses(self):
        words = shlex.split(slotcpus.slot_env({**M, 'limit': {'window': '18-01', 'pct': 60}, 'heat': {'cap_c': 85}}))
        self.assertIn('SLOT_CPU_WINDOW=18-01', words)
        self.assertIn('SLOT_CPU_THROTTLE=1', words)


class RunnerTest(unittest.TestCase):
    docker = {**M, 'runner': {'kind': 'docker', 'image': 'img:1', 'memory_gb': 12, 'cpus': 8, 'env': {}, 'args': []}}

    def test_docker_gets_the_budget_and_the_knobs_passed_in(self):
        line = remote.runner_command(self.docker, 't', 'npx playwright test', cpu_env=True)
        for name in ('TUTTI_SLOT_CPUS', 'PLAYWRIGHT_WORKERS', 'VITEST_MAX_THREADS', 'GOMAXPROCS', 'MAKEFLAGS'):
            self.assertIn(f'-e {name} ', line)

    def test_docker_gets_nothing_extra_when_it_is_off(self):
        self.assertNotIn('PLAYWRIGHT_WORKERS', remote.runner_command(self.docker, 't', 'x'))

    def test_run_hands_the_inputs_to_the_slot_and_off_means_nothing(self):
        cfg = {'test': {'slot_cpu_env': True, 'prepare': []}, 'sync': {'exclude': [], 'ensure_dirs': []}}
        seen = []
        orig = remote.sh
        remote.sh = lambda m, script, **k: seen.append(script) or subprocess.CompletedProcess([], 0)
        self.addCleanup(setattr, remote, 'sh', orig)
        m = {**M, 'min_free_gb': 0, 'lock_prefix': '/tmp/x-', 'name': 'pc'}
        remote.run(cfg, m, 't', '.', 'true')
        remote.run({**cfg, 'test': {**cfg['test'], 'slot_cpu_env': False}}, m, 't', '.', 'true')
        self.assertIn('SLOT_CPU_KNOBS=', seen[0])
        self.assertNotIn('export TUTTI_SLOT_CPUS', seen[0])   # worked out by slot.sh, after its lock
        self.assertLess(seen[0].index('SLOT_CPU_KNOBS'), seen[0].index('slot.sh'))
        self.assertNotIn('SLOT_CPU', seen[1])


class ConfigTest(unittest.TestCase):
    def test_bad_values_are_named(self):
        self.assertEqual(slotcpus.errors('pc', {}), [])
        self.assertEqual(slotcpus.errors('pc', {'slot_cpus': 4, 'cores': 28}), [])
        for key, bad in (('slot_cpus', 0), ('slot_cpus', '4'), ('slot_cpus', True), ('cores', -1)):
            with self.subTest(key=key, bad=bad):
                self.assertIn(f'machines.pc.{key} is {bad!r}', slotcpus.errors('pc', {key: bad})[0])

    def test_the_test_key_defaults_on_and_must_be_a_switch(self):
        self.assertIs(config.DEFAULTS['test']['slot_cpu_env'], True)
        cfg = {'test': {'prepare': [], 'slot_cpu_env': 'yes'}}
        self.assertIn('test.slot_cpu_env is', ' '.join(config._test_errors(cfg)))
        self.assertEqual(config._test_errors({'test': {'prepare': [], 'slot_cpu_env': False}}), [])


if __name__ == '__main__':
    unittest.main()
