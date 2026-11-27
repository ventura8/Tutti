import tempfile
import unittest
from unittest.mock import patch

from tutti import ha_energyuse


class TestHaEnergyUse(unittest.TestCase):
    @patch('tutti.homeassistant.get_energy_usage', return_value=15.5)
    def test_records_live_watts_and_retains_samples(self, _read):
        with tempfile.TemporaryDirectory() as folder:
            cfg = {'home_assistant': {'energy_entities': {'builder': 'sensor.plug_power'}}, 'project': {'state': folder}}
            self.assertEqual(ha_energyuse.record(cfg, now=10), {'builder': 15.5})
            self.assertEqual(ha_energyuse.load(cfg), {'builder': [[10, 15.5]]})

    @patch('tutti.homeassistant.get_energy_usage', return_value=None)
    def test_missing_meter_has_no_live_value(self, _read):
        cfg = {'home_assistant': {'energy_entities': {'builder': 'sensor.plug_power'}}}
        self.assertEqual(ha_energyuse.record(cfg), {})

    @patch('tutti.energy.tariff', return_value={'price': 2, 'currency': 'EUR', 'periods': []})
    def test_cost_integrates_watts_over_the_task(self, _tariff):
        self.assertEqual(ha_energyuse.cost({}, [(0, 100), (3600, 300)]), {'kwh': 0.2, 'cost': 0.4, 'currency': 'EUR'})
