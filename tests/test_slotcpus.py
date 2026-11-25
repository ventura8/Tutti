"""A slot's CPU budget: the arithmetic, the shell line a run starts with, the docker pass-through and the config keys."""
import os
import shlex
import subprocess
import unittest
from datetime import datetime, timezone

from tutti import config, remote, slotcpus

M = {'name': 'pc', 'transport': 'local', 'slots': 2, 'runner': {'kind': 'shell', 'memory_gb': 12, 'cpus': 8, 'env': {}, 'args': []},
     'heat': {}, 'cores': 28}


def evaluate(machine, env=None, now=None):
    """Run the export line in bash and print the variables: no machine is touched, the line only reads nproc and /sys."""
    names = ('TUTTI_SLOT_CPUS', 'PLAYWRIGHT_WORKERS', 'VITEST_MAX_THREADS', 'VITEST_MIN_THREADS', 'MAKEFLAGS')
    script = slotcpus.export_line(machine, now) + 'echo "' + '|'.join(f'${n}' for n in names) + '"'
    out = subprocess.run(['bash', '-c', script], capture_output=True, text=True, env={**os.environ, **(env or {})}, check=True)
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


class ExportLineTest(unittest.TestCase):
    def test_every_knob_is_set_from_the_budget(self):
        self.assertEqual(evaluate(M), ['14', '14', '14', '1', '-j14'])

    def test_a_knob_the_caller_set_is_kept(self):
        self.assertEqual(evaluate(M, {'PLAYWRIGHT_WORKERS': '3', 'MAKEFLAGS': '-j2'}), ['14', '3', '14', '1', '-j2'])

    def test_slot_cpus_overrides_the_computed_value(self):
        self.assertEqual(evaluate({**M, 'slot_cpus': 4})[:2], ['4', '4'])

    def test_the_machine_cpus_are_read_when_not_configured(self):
        self.assertGreaterEqual(int(evaluate({k: v for k, v in M.items() if k != 'cores'})[0]), 1)

    def test_a_docker_runner_cap_bounds_it(self):
        m = {**M, 'slots': 1, 'runner': {**M['runner'], 'kind': 'docker', 'cpus': 6}}
        self.assertEqual(evaluate(m)[0], '6')

    def test_limited_hours_apply_their_share(self):
        m = {**M, 'limit': {'window': '00-24', 'pct': 40}}
        inside = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(evaluate(m, now=inside)[0], '5')
        self.assertEqual(evaluate({k: v for k, v in m.items() if k != 'limit'}, now=inside)[0], '14')

    def test_a_hot_machine_takes_its_throttle_cpus(self):
        hot = {**M, 'slots': 1, 'heat': {'cap_c': 0, 'throttle_cpus': 2}}   # a 0 C cap: any reading, even none, is not below it...
        cold = {**M, 'slots': 1, 'heat': {'cap_c': 400}}                     # ...and no reading is ever 400 C
        self.assertEqual(evaluate(hot)[0], '2' if os.path.exists('/sys/class/thermal/thermal_zone0/temp') else '28')
        self.assertEqual(evaluate(cold)[0], '28')

    def test_the_line_is_one_shell_word_list_that_parses(self):
        shlex.split(slotcpus.export_line(M))


class RunnerTest(unittest.TestCase):
    docker = {**M, 'runner': {'kind': 'docker', 'image': 'img:1', 'memory_gb': 12, 'cpus': 8, 'env': {}, 'args': []}}

    def test_docker_gets_the_budget_and_the_knobs_passed_in(self):
        line = remote.runner_command(self.docker, 't', 'npx playwright test', cpu_env=True)
        for name in ('TUTTI_SLOT_CPUS', 'PLAYWRIGHT_WORKERS', 'VITEST_MAX_THREADS', 'GOMAXPROCS', 'MAKEFLAGS'):
            self.assertIn(f'-e {name} ', line)

    def test_docker_gets_nothing_extra_when_it_is_off(self):
        self.assertNotIn('PLAYWRIGHT_WORKERS', remote.runner_command(self.docker, 't', 'x'))

    def test_run_exports_before_the_slot_and_off_means_nothing(self):
        cfg = {'test': {'slot_cpu_env': True, 'prepare': []}, 'sync': {'exclude': [], 'ensure_dirs': []}}
        seen = []
        orig = remote.sh
        remote.sh = lambda m, script, **k: seen.append(script) or subprocess.CompletedProcess([], 0)
        self.addCleanup(setattr, remote, 'sh', orig)
        m = {**M, 'min_free_gb': 0, 'lock_prefix': '/tmp/x-', 'name': 'pc'}
        remote.run(cfg, m, 't', '.', 'true')
        remote.run({**cfg, 'test': {**cfg['test'], 'slot_cpu_env': False}}, m, 't', '.', 'true')
        self.assertIn('export TUTTI_SLOT_CPUS=', seen[0])
        self.assertLess(seen[0].index('TUTTI_SLOT_CPUS'), seen[0].index('slot.sh'))
        self.assertNotIn('TUTTI_SLOT_CPUS', seen[1])


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
