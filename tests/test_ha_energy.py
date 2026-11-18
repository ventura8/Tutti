import unittest
from unittest.mock import patch
from tutti import ha_energy

class TestHaEnergy(unittest.TestCase):
    @patch('tutti.homeassistant.get_energy_usage')
    def test_probe_all(self, mock_get_energy_usage):
        cfg = {'home_assistant': {'energy_entities': {'builder': 'sensor.builder_plug_power'}}}
        mock_get_energy_usage.return_value = 15.5
        results = ha_energy.probe_all(cfg)
        self.assertEqual(results, {'builder': 15.5})
        mock_get_energy_usage.assert_called_with(cfg, 'sensor.builder_plug_power')

if __name__ == '__main__':
    unittest.main()
