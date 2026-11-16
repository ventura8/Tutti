"""CPU and GPU temperatures per machine: the probe line, a WSL machine's Windows reading, and heat routing on the hotter."""
import unittest
from unittest import mock

from tutti import remote, routing, state


class TempsTest(unittest.TestCase):
    def setUp(self):
        remote._win_temps.clear()

    def test_the_probe_line_carries_the_gpu(self):
        p = routing.parse_probe('1 0.5 8 0 71000 58')
        self.assertEqual((p.temp, p.gpu_temp, p.hottest()), (71.0, 58.0, (71.0, 'CPU')))
        self.assertEqual(routing.parse_probe('1 0.5 8 0 0 43.20').hottest(), (43.2, 'GPU'))
        self.assertEqual(routing.parse_probe('1 0.5 8 0 0 -').hottest(), (None, ''))
        self.assertIsNone(routing.parse_probe('1 0.5 8 0 0 junk').gpu_temp)

    def test_a_hot_gpu_refuses_a_run(self):
        m = {'slots': 2, 'heat': {'cap_c': 85, 'stop_c': 98}}
        self.assertIn('too hot: GPU 99 C', routing.unfit(m, routing.parse_probe('2 0.1 8 0 40000 99')))
        self.assertEqual(routing.unfit(m, routing.parse_probe('2 0.1 8 0 40000 60')), '')

    def test_windows_temperature_or_an_honest_not_readable(self):
        self.assertEqual(remote.parse_win_temp('S LibreHardwareMonitor\nT 61.5\n'), (61.5, ''))
        t, why = remote.parse_win_temp('S ACPI\nT 16.9\n')
        self.assertIsNone(t)
        self.assertIn("ACPI reports 16.9 C, a placeholder", why)
        self.assertIn('no temperature sensor', remote.parse_win_temp('')[1])

    def test_a_wsl_machine_without_zones_asks_windows_once_a_while(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv[-1][:20])
            return mock.Mock(returncode=0, stdout='S ACPI\nT 55\n')
        m = {'name': 'pc', 'host': 'pc.example'}
        with mock.patch.object(remote.subprocess, 'run', fake_run):
            kw = {'lhm_fn': lambda mm, t: (None, None), 'ensure_fn': lambda mm: None}
            self.assertEqual(remote.windows_temp(m, clock=lambda: 0, **kw), (55.0, ''))
            self.assertEqual(remote.windows_temp(m, clock=lambda: 30, **kw), (55.0, ''))
        self.assertEqual(len(calls), 1)

    def test_the_probe_fills_a_wsl_cpu_from_windows(self):
        m = {'name': 'pc', 'host': 'pc.example', 'transport': 'wsl', 'wsl_helper': '/x', 'yield_file': '~/y', 'slots': 1,
             'lock_prefix': '/tmp/x-'}
        with mock.patch.object(remote, 'sh', return_value=mock.Mock(returncode=0, stdout='1 0.2 16 0 0 43\n')):
            p = remote.probe(m, windows_fn=lambda mm: (None, 'CPU not readable: placeholder'))
        self.assertEqual((p.temp, p.gpu_temp, p.temp_note), (None, 43.0, 'CPU not readable: placeholder'))
        row = state.machine_state({**m, 'min_free_gb': 6}, p, 1)
        self.assertEqual((row['temp_c'], row['gpu_temp_c'], row['temp_note']), (None, 43.0, 'CPU not readable: placeholder'))

    def test_the_probe_script_asks_nvidia_smi_also_where_wsl_keeps_it(self):
        script = remote.probe_script({'yield_file': '~/y', 'slots': 1, 'lock_prefix': '/tmp/x-'})
        self.assertIn('/usr/lib/wsl/lib/nvidia-smi --query-gpu=temperature.gpu', script)
        self.assertIn('${g:--}', script)


if __name__ == '__main__':
    unittest.main()


class SafeByDefault(unittest.TestCase):
    def test_a_machine_with_no_heat_block_is_refused_at_the_default_stop(self):
        from tutti import config, routing
        cfg = config.normalise({'machines': {'lap': {'host': 'local', 'slots': 2}}})
        lap = cfg['machines']['lap']
        self.assertEqual(lap['heat'], config.HEAT_DEFAULT)
        hot = routing.Probe(True, 2, 1.0, 16, False, 95.0)
        self.assertIn('too hot', routing.unfit(lap, hot))
        gpu = routing.Probe(True, 1, 1.0, 16, False, 60.0, gpu_temp=96.0)   # the GPU counts too
        self.assertIn('GPU', routing.unfit(lap, gpu))
        self.assertEqual(routing.unfit(lap, routing.Probe(True, 2, 1.0, 16, False, 70.0)), '')

    def test_an_own_heat_block_is_kept(self):
        from tutti import config
        cfg = config.normalise({'machines': {'pc': {'host': 'pc.example', 'heat': {'cap_c': 80, 'stop_c': 90}}}})
        self.assertEqual(cfg['machines']['pc']['heat'], {'cap_c': 80, 'stop_c': 90})
