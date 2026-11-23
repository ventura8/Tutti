import unittest
from unittest.mock import MagicMock, patch

from tutti import host_power


class TestHostPower(unittest.TestCase):
    @patch('subprocess.run')
    def test_windows(self, mock_run):
        mock_run.return_value = MagicMock(stdout='Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e (Balanced)')
        with patch('tutti.host_power.get_platform', return_value='windows'):
            result = host_power.get_power_mode()
            self.assertEqual(result['source'], 'powercfg')
            self.assertIn('Balanced', result['mode'])

    @patch('subprocess.run')
    def test_macos(self, mock_run):
        # mock stdout for lowpowermode 0 and AC Power
        mock_run.return_value = MagicMock(stdout='0\nAC')
        with patch('tutti.host_power.get_platform', return_value='darwin'):
            result = host_power.get_power_mode()
            self.assertEqual(result['mode'], 'LPM=Off, AC')
            self.assertEqual(result['source'], 'pmset')
        mock_run.return_value = MagicMock(stdout='1\nBattery')
        with patch('tutti.host_power.get_platform', return_value='darwin'):
            result = host_power.get_power_mode()
            self.assertEqual(result['mode'], 'LPM=On, Battery')
            self.assertEqual(result['source'], 'pmset')

    @patch('subprocess.run')
    def test_linux(self, mock_run):
        # Setup for powerprofilesctl
        mock_run.return_value = MagicMock(stdout='balanced')
        with patch('tutti.host_power.get_platform', return_value='linux'):
            result = host_power.get_power_mode()
            self.assertEqual(result['mode'], 'balanced')
            self.assertEqual(result['source'], 'linux_power_utils')

    @patch('subprocess.run')
    @patch('shutil.which')
    def test_android(self, mock_which, mock_run):
        mock_which.return_value = '/usr/bin/adb'
        mock_run.return_value = MagicMock(stdout='AC powered: true\nBattery saver: false')

        # We need to construct a mock machine object
        machine = {'transport': 'android'}

        result = host_power.get_power_mode(machine)
        self.assertEqual(result['mode'], 'AC')
        self.assertEqual(result['source'], 'adb')

    @patch('subprocess.run', side_effect=OSError('boom'))
    def test_command_failure_returns_unknown(self, _mock_run):
        with patch('tutti.host_power.get_platform', return_value='linux'):
            result = host_power.get_power_mode()
            self.assertEqual(result['mode'], 'unknown')
            self.assertEqual(result['source'], 'linux_power_utils')

    @patch('shutil.which', return_value=None)
    def test_android_without_adb(self, _mock_which):
        result = host_power.get_power_mode({'transport': 'android'})
        self.assertEqual(result['mode'], 'adb not found')
        self.assertEqual(result['source'], 'adb')


