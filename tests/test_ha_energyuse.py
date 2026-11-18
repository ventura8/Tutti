import unittest
import time
from unittest.mock import patch
from tutti import ha_energyuse

class TestHaEnergyUse(unittest.TestCase):
    @patch('tutti.homeassistant.get_energy_usage')
    def test_record_and_view(self, mock_get_energy_usage):
        cfg = {'home_assistant': {'energy_entities': {'builder': 'sensor.plug_power'}},
               'state': '/tmp'} # This needs to be a valid state dir for Tutti
        # Need to mock config.state_dir to return /tmp
        with patch('tutti.config.state_dir', return_value='/tmp'):
            mock_get_energy_usage.return_value = 15.5
            
            now = time.time()
            data = ha_energyuse.record(cfg, now=now)
            self.assertEqual(data['builder'][-1][1], 15.5)
            
            view_data = ha_energyuse.view(cfg, now=now)
            self.assertEqual(view_data['builder'], 15.5)

if __name__ == '__main__':
    unittest.main()
