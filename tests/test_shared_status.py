"""One status for a worker in the CLI and the dashboard (the "nuc worker unavailable" report, 0.1.64): a GPU tool's error text
no longer turns a machine offline, the Local models cards follow the workers' own probe, and a config edit reaches a dashboard
that started before it."""
import os
import subprocess
import tempfile
import time
import unittest

from tutti import config, dashboard, energy, remote, routing, workers

NVIDIA_FAILED = ("NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver. Make sure that the latest NVIDIA "
                 'driver is installed and running.')


class ProbeLine(unittest.TestCase):
    def test_a_gpu_tools_error_text_leaves_the_machine_reachable_with_no_gpu_reading(self):
        p = routing.parse_probe(f'3 5.20 16 0 58050 {NVIDIA_FAILED} 22.6')
        self.assertTrue(p.reachable)
        self.assertEqual((p.free, p.cores, p.gpu_temp, p.mem_gb), (3, 16, None, 22.6))

    def test_the_fields_after_the_gpu_text_keep_their_places(self):
        p = routing.parse_probe(f'1 0.5 8 0 50000 {NVIDIA_FAILED} 9.5 120')
        self.assertEqual((p.reachable, p.gpu_temp, p.mem_gb, p.idle_s), (True, None, 9.5, 120.0))
        self.assertEqual(routing.parse_probe('1 0.5 8 0 50000 - 9.5 120').idle_s, 120.0)

    def test_a_broken_line_is_still_unreachable(self):
        self.assertFalse(routing.parse_probe('ssh: connect to host nuc port 22: Connection refused').reachable)
        self.assertFalse(routing.parse_probe('').reachable)

    def test_the_gpu_reading_keeps_numbers_only(self):
        with tempfile.TemporaryDirectory() as d:
            smi = os.path.join(d, 'nvidia-smi')
            with open(smi, 'w', encoding='utf8') as f:
                f.write(f'#!/bin/sh\necho "{NVIDIA_FAILED}"\nexit 9\n')
            os.chmod(smi, 0o755)
            out = subprocess.run(['bash', '-c', remote.GPU_TEMP + 'echo "[$g]"'], capture_output=True, text=True,
                                 env={**os.environ, 'PATH': f'{d}:/usr/bin:/bin'}, check=True).stdout
        self.assertNotIn('NVIDIA', out)


def cfg_with_nuc():
    raw = {'machines': {'nuc': {'host': 'nuc.example'}},
           'workers': {'nuc': {'kind': 'ollama', 'model': 'm:latest', 'host': '127.0.0.1:11437', 'machine': 'nuc'}},
           'fleet': {'enabled': False}}
    return config.normalise(raw)


class SharedStatus(unittest.TestCase):
    def test_the_cli_and_the_dashboard_ask_the_same_function(self):
        cfg = cfg_with_nuc()
        calls = []

        def fake(w):
            calls.append(w['host'])
            return True, 'm:latest at 127.0.0.1:11437'
        self.assertEqual(workers.statuses(cfg, fake), {'nuc': (True, 'm:latest at 127.0.0.1:11437')})
        self.assertIn('nuc          ollama  ready', workers.listing(cfg, fake))
        self.assertEqual(calls, ['127.0.0.1:11437', '127.0.0.1:11437'])   # the worker's host (its tunnel), never the machine's

    def test_a_ready_worker_keeps_its_card_available_when_the_machines_ssh_probe_fails(self):
        status, why = energy.worker_status(('offline', 'ssh did not answer'), ['nuc'], {'nuc': (True, 'ok')}, None)
        self.assertEqual(status, 'available')
        self.assertIn('nuc answers', why)
        self.assertIn('ssh did not answer', why)

    def test_a_worker_that_does_not_answer_makes_the_card_offline_with_its_own_reason(self):
        status, why = energy.worker_status(('available', ''), ['nuc'], {'nuc': (False, 'start its tunnel')}, {'gpu': 'x'})
        self.assertEqual((status, why), ('offline', 'its model server does not answer (nuc: start its tunnel)'))

    def test_without_worker_probes_the_machine_row_decides_as_before(self):
        self.assertEqual(energy.worker_status(('available', ''), ['nuc'], None, None)[0], 'offline')
        self.assertEqual(energy.worker_status(('thermal', 'CPU 99 C'), ['nuc'], {'nuc': (True, 'ok')}, {})[0], 'thermal')


class FollowsConfig(unittest.TestCase):
    def test_a_worker_added_after_the_dashboard_started_is_probed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'tutti.config.yaml')
            base = f'project: {{state: {d}/state, worktrees: {d}}}\nmachines: {{here: {{host: local}}}}\nfleet: {{enabled: false}}\n'
            with open(path, 'w', encoding='utf8') as f:
                f.write(base)
            c = dashboard.Collector(config.load(path))
            self.assertIsNone(c.follow_config())
            self.assertNotIn('nuc', c.cfg['workers'])
            with open(path, 'w', encoding='utf8') as f:
                f.write(base + "workers: {nuc: {kind: ollama, model: 'm:latest', host: '127.0.0.1:11437'}}\n")
            os.utime(path, (time.time() + 5, time.time() + 5))
            self.assertIsNone(c.follow_config())
            self.assertIn('nuc', c.cfg['workers'])

    def test_a_broken_edit_keeps_the_last_good_config_and_says_why(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'tutti.config.yaml')
            with open(path, 'w', encoding='utf8') as f:
                f.write(f'project: {{state: {d}/state, worktrees: {d}}}\nmachines: {{here: {{host: local}}}}\n')
            c = dashboard.Collector(config.load(path))
            self.assertIsNone(c.follow_config())
            with open(path, 'w', encoding='utf8') as f:
                f.write('machines: [not, a, map]\n')
            os.utime(path, (time.time() + 5, time.time() + 5))
            why = c.follow_config()
            self.assertIn('does not load', why)
            self.assertIn('here', c.cfg['machines'])
            self.assertEqual(c.follow_config(), why)   # said at every probe until the file loads again

    def test_an_edit_before_the_first_probe_is_seen(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'tutti.config.yaml')
            base = f'project: {{state: {d}/state, worktrees: {d}}}\nmachines: {{here: {{host: local}}}}\nfleet: {{enabled: false}}\n'
            with open(path, 'w', encoding='utf8') as f:
                f.write(base)
            c = dashboard.Collector(config.load(path))
            with open(path, 'w', encoding='utf8') as f:
                f.write(base + "workers: {nuc: {kind: ollama, model: 'm:latest', host: '127.0.0.1:11437'}}\n")
            os.utime(path, (time.time() + 5, time.time() + 5))
            self.assertIsNone(c.follow_config())
            self.assertIn('nuc', c.cfg['workers'])


if __name__ == '__main__':
    unittest.main()
