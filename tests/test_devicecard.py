"""One card per device (tutti/devicecard.py): one status that agrees with the idle watchdog and the pause switches, plain
durations, and temperatures labelled by sensor (real readings from a Google TV Streamer, a Pixel Tablet and a Pixel 9 Pro)."""
import unittest

from tutti import devicecard

TV = {'name': 'googletv', 'kind': 'tv', 'status': 'ready', 'reason': '', 'battery_present': 0, 'battery_c': 2.5, 'temp_soc': 38.442}
TABLET = {'name': 'pixeltablet', 'status': 'ready', 'reason': 'ready: takes the next run', 'battery_present': 1, 'battery_c': 27,
          'temp_skin': 30.9323, 'temp_soc': 41}
PHONE = {'name': 'pixel9pro', 'status': 'ready', 'reason': '', 'battery_present': 1, 'battery_c': 33.4, 'temp_skin': 33.5435}


class Card(unittest.TestCase):
    def test_plain_minutes(self):
        got = [devicecard.plain_minutes(m) for m in (12, 60, 95, 2298, 2880)]
        self.assertEqual(got, ['12 min', '1 h', '1 h 35 min', '1 day 14 h', '2 days'])

    def test_temperatures_by_sensor(self):
        self.assertEqual(devicecard.temps(TV), ['SoC 38 °C'])   # no battery: its 2.5 °C is never shown
        self.assertEqual(devicecard.temps(TABLET), ['skin 31 °C', 'battery 27 °C'])
        self.assertEqual(devicecard.temps(PHONE), ['skin 34 °C', 'battery 33.4 °C'])
        self.assertEqual(devicecard.temps({'name': 'x'}), [])

    def test_a_blocked_runner_makes_the_card_blocked_not_ready(self):
        w = {'kind': 'android', 'name': 'pixeltablet', 'state': 'blocked', 'reason': 'runner w5090 yield flag up', 'minutes': 2298,
             'what': 'RoRadio has 3 e2e shards pending', 'fix': {'action': 'dispatch_now', 'label': 'Check again now'}}
        out = {'devices': {'items': [dict(TABLET)]}, 'controls': {'paused': []},
               'dispatch': {'warnings': [w], 'reroute': ['pixel9pro away (paused or away): 1 shard moved to pixeltablet']}}
        c = devicecard.attach({}, out)['devices']['items'][0]['card']
        self.assertEqual((c['status'], c['tone'], c['why']), ('blocked', 'bad', 'runner w5090 yield flag up'))
        self.assertEqual(c['action']['label'], 'Check again now')
        self.assertEqual(c['waiting'], 'RoRadio has 3 e2e shards pending')

    def test_a_paused_device_offers_resume_and_carries_its_reroute_line(self):
        out = {'devices': {'items': [dict(PHONE)]},
               'controls': {'paused': [{'kind': 'device', 'name': 'pixel9pro', 'from': 1, 'until': None, 'window': 'active'}]},
               'dispatch': {'warnings': [], 'reroute': ['pixel9pro away (paused or away): 1 shard moved to pixeltablet']}}
        c = devicecard.attach({}, out)['devices']['items'][0]['card']
        self.assertEqual((c['status'], c['action']['action'], c['moved']), ('away', 'resume', '1 shard moved to pixeltablet'))

    def test_idle_while_dispatching_says_so_in_plain_words(self):
        c = devicecard.card(dict(PHONE), warning={'state': 'dispatching', 'minutes': 95, 'what': 'x'})
        self.assertEqual(c['status'], 'idle')
        self.assertIn('1 h 35 min', c['why'])


if __name__ == '__main__':
    unittest.main()
