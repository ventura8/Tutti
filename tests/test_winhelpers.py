"""Windows helpers: presence from the console session's own input, and LibreHardwareMonitor's CPU reading."""
import json
import unittest
from unittest import mock

from tutti import remote, winhelpers


def lhm(temp='56,3 °C', power='87,6 W'):
    cpu = {'Text': 'AMD Ryzen', 'Children': [
        {'Text': 'Core (Tctl/Tdie)', 'Type': 'Temperature', 'SensorId': '/amdcpu/0/temperature/2', 'Value': temp},
        {'Text': 'CCD1 (Tdie)', 'Type': 'Temperature', 'SensorId': '/amdcpu/0/temperature/3', 'Value': '99,0 °C'},
        {'Text': 'Package', 'Type': 'Power', 'SensorId': '/amdcpu/0/power/0', 'Value': power},
        {'Text': 'Core #1 (SMU)', 'Type': 'Power', 'SensorId': '/amdcpu/0/power/1', 'Value': '3,0 W'}]}
    gpu = {'Text': 'GPU Hot Spot', 'Type': 'Temperature', 'SensorId': '/gpu-nvidia/0/temperature/0', 'Value': '70,0 °C'}
    return json.dumps({'Children': [{'Children': [cpu, gpu]}]})


class WinHelpersTest(unittest.TestCase):
    def setUp(self):
        remote._win_temps.clear()
        remote._win_idle.clear()
        winhelpers._done.clear()

    def test_lhm_cpu_temperature_and_package_power_in_any_locale(self):
        self.assertEqual(winhelpers.parse_lhm(lhm()), (56.3, 87.6))
        self.assertEqual(winhelpers.parse_lhm(lhm('61.5 °C', '40.0 W')), (61.5, 40.0))
        intel = json.dumps({'Children': [{'Text': 'CPU Package', 'Type': 'Power', 'SensorId': '/intelcpu/0/power/0', 'Value': '65,0 W'},
                                         {'Text': 'CPU Package', 'Type': 'Temperature', 'SensorId': '/intelcpu/0/temperature/8',
                                          'Value': '70,0 °C'}]})
        self.assertEqual(winhelpers.parse_lhm(intel), (70.0, 65.0))
        self.assertEqual(winhelpers.parse_lhm(''), (None, None))
        self.assertEqual(winhelpers.parse_lhm(json.dumps({'Children': []})), (None, None))

    def test_windows_temp_prefers_lhm_and_keeps_its_watts(self):
        m = {'name': 'pc', 'host': 'pc.example'}
        ensured = []
        self.assertEqual(remote.windows_temp(m, clock=lambda: 0, lhm_fn=lambda mm, t: (58.9, 86.2), ensure_fn=ensured.append),
                         (58.9, ''))
        self.assertEqual((remote.windows_cpu_w(m), ensured), (86.2, []))
        remote._win_temps.clear()
        remote.windows_temp(m, clock=lambda: 500, lhm_fn=lambda mm, t: (58.0, None), ensure_fn=ensured.append)
        self.assertEqual(remote._win_watts['pc'], 86.2)   # a read without watts keeps the last good one

    def test_no_lhm_installs_it_and_falls_back(self):
        m = {'name': 'pc2', 'host': 'pc.example'}
        ensured = []
        with mock.patch.object(remote.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='S ACPI\nT 16.9\n')):
            t, why = remote.windows_temp(m, clock=lambda: 0, lhm_fn=lambda mm, t: (None, None), ensure_fn=ensured.append)
        self.assertIsNone(t)
        self.assertIn('placeholder', why)
        self.assertEqual(ensured, [m])

    def test_idle_file_locked_stale_or_missing(self):
        self.assertEqual(winhelpers.parse_idle('2515 0 1000', 1030), 2515.0)
        self.assertIsNone(winhelpers.parse_idle('3 1 1000', 1030))           # locked: nobody at the keyboard
        self.assertIs(winhelpers.parse_idle('3 0 1000', 2000), False)        # stale: the helper stopped
        self.assertIs(winhelpers.parse_idle('The system cannot find the file specified.', 0), False)
        # tolerates minor Windows clock skew without falling back to query user
        self.assertEqual(winhelpers.parse_idle('2515 0 1000', 1250), 2515.0)  # Windows 250s behind
        self.assertEqual(winhelpers.parse_idle('2515 0 1250', 1000), 2515.0)  # Windows 250s ahead
        self.assertIs(winhelpers.parse_idle('2515 0 1500', 1000), False)       # ahead beyond skew limit

    def test_presence_tolerates_clock_skew_without_fallback(self):
        m = {'name': 'pc', 'host': 'pc.example'}
        calls = []

        def run(argv, **kw):
            calls.append(argv[-1])
            return mock.Mock(returncode=0, stdout='2515 0 1000\n')
        # Windows clock is 200s behind host clock (1200 vs 1000): should not fall back to query user
        self.assertEqual(remote.windows_idle(m, clock=lambda: 1200, run=run, ensure_fn=calls.append), 2515.0)
        self.assertEqual(calls, [winhelpers.READ_IDLE])

    def test_presence_comes_from_the_console_helper_not_query_user(self):
        m = {'name': 'pc', 'host': 'pc.example'}
        calls = []

        def run(argv, **kw):
            calls.append(argv[-1])
            return mock.Mock(returncode=0, stdout='2515 0 1000\n')
        self.assertEqual(remote.windows_idle(m, clock=lambda: 1010, run=run, ensure_fn=calls.append), 2515.0)
        self.assertEqual(calls, [winhelpers.READ_IDLE])

    def test_without_the_helper_it_is_installed_and_query_user_answers_meanwhile(self):
        m = {'name': 'pc', 'host': 'pc.example'}
        ensured = []
        out = {winhelpers.READ_IDLE: '', 'query user': ' ventu  console  1  Active  1:05  12.11.2026 19:23\n'}

        def run(argv, **kw):
            return mock.Mock(returncode=0, stdout=out[argv[-1]])
        self.assertEqual(remote.windows_idle(m, clock=lambda: 0, run=run, ensure_fn=ensured.append), 3900.0)
        self.assertEqual(ensured, [m])

    def test_ensure_sends_the_setup_on_stdin_once_an_hour(self):
        m = {'name': 'pc', 'host': 'pc.example'}
        sent = []

        def run(argv, **kw):
            sent.append((argv[-1], kw['input']))
            return mock.Mock(returncode=0)
        self.assertEqual(winhelpers.ensure(m, run=run, clock=lambda: 10_000), 0)
        self.assertIsNone(winhelpers.ensure(m, run=run, clock=lambda: 10_100))
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][0].endswith('-Command -'))
        self.assertTrue(sent[0][1].startswith('iex ') and sent[0][1].count('\n') == 1)
        self.assertIn('Tutti sensors', winhelpers.SETUP)
        self.assertIn('key="listenerIp" value="127.0.0.1"', winhelpers.LHM_CONFIG)   # the sensor server is never on the LAN
        failing = mock.Mock(return_value=mock.Mock(returncode=1))
        self.assertIsNone(winhelpers.ensure({'name': 'y', 'host': 'h'}, run=failing, clock=lambda: 10_000))
        self.assertIsNone(winhelpers.ensure({'name': 'y', 'host': 'h'}, run=failing, clock=lambda: 10_000 + winhelpers.RETRY))
        self.assertEqual(failing.call_count, 2)   # a failure is retried after RETRY s, not an hour
        self.assertIsNone(winhelpers.ensure({'name': 'x', 'host': 'h'}, run=mock.Mock(side_effect=OSError), clock=lambda: 10_000))

    def test_setup_pins_lhm_version_and_verifies_hash(self):
        self.assertEqual(winhelpers.LHM_VERSION, '0.9.6')
        self.assertIn(f'--version {winhelpers.LHM_VERSION}', winhelpers.SETUP)
        self.assertIn(winhelpers.LHM_HASH, winhelpers.SETUP)
        self.assertIn('--disable-interactivity', winhelpers.SETUP)
        self.assertIn('LibreHardwareMonitor hash mismatch', winhelpers.SETUP)
        self.assertIn(f'winget pin add --id LibreHardwareMonitor.LibreHardwareMonitor --version {winhelpers.LHM_VERSION}', winhelpers.SETUP)
        self.assertIn(f'winget version {winhelpers.LHM_VERSION} not found or failed', winhelpers.SETUP)

    def test_ensure_logs_output_lines(self):
        m = {'name': 'pc', 'host': 'pc.example'}
        out = f'winget version {winhelpers.LHM_VERSION} not found or failed\nno LibreHardwareMonitor: winget and choco failed\n'
        with mock.patch.object(winhelpers.log, 'info') as log_info:
            winhelpers.ensure(m, run=lambda *a, **k: mock.Mock(returncode=0, stdout=out), clock=lambda: 10_000)
            log_info.assert_any_call('%s: %s', 'pc', f'winget version {winhelpers.LHM_VERSION} not found or failed')
            log_info.assert_any_call('%s: %s', 'pc', 'no LibreHardwareMonitor: winget and choco failed')


if __name__ == '__main__':
    unittest.main()
