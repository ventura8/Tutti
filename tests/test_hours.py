"""Recurring blocked hours (`tutti devices hours`, a machine's limit at 0 %): a window across midnight, the hand-over before
it starts, and nothing at all by default. Times are in a fixed zone so the tests read the same everywhere."""
import shutil
import tempfile
import unittest
from datetime import datetime

from tutti import controls, hours, share, tz

Z = 'Europe/Bucharest'


def at(text):
    return tz.to_utc(datetime.strptime(text, '%Y-%m-%d %H:%M'), Z).timestamp()


class WindowTest(unittest.TestCase):
    def test_a_window_across_midnight_belongs_to_the_day_it_starts_on(self):
        w = hours.window('22-07', None, at('2026-11-17 02:00'), zone=Z)   # Tuesday 02:00: Monday's night
        self.assertEqual(w, (at('2026-11-16 22:00'), at('2026-11-17 07:00')))
        self.assertIsNone(hours.window('22-07', None, at('2026-11-17 12:00'), zone=Z))
        self.assertIsNone(hours.window('22-07', ['tue'], at('2026-11-17 02:00'), zone=Z))   # Monday's night is not listed
        self.assertEqual(hours.window('22-07,12-13', None, at('2026-11-17 12:30'), zone=Z)[0], at('2026-11-17 12:00'))

    def test_the_lead_time_catches_a_window_about_to_start(self):
        self.assertIsNone(hours.window('22-07', None, at('2026-11-16 21:00'), lead=30 * 60, zone=Z))
        self.assertEqual(hours.window('22-07', None, at('2026-11-16 21:40'), lead=30 * 60, zone=Z)[0], at('2026-11-16 22:00'))

    def test_bad_input_says_the_form(self):
        with self.assertRaisesRegex(ValueError, 'HH-HH'):
            hours.parse('22:00-07:00')
        self.assertEqual(hours.parse_days('mon-fri'), ['mon', 'tue', 'wed', 'thu', 'fri'])
        self.assertEqual(hours.parse_days('sat,sun'), ['sat', 'sun'])
        self.assertEqual(hours.parse_days('fri-mon'), ['mon', 'fri', 'sat', 'sun'])
        with self.assertRaisesRegex(ValueError, 'not a day'):
            hours.parse_days('weekend')
        self.assertEqual(hours.errors('devices.p', None), [])
        self.assertTrue(hours.errors('devices.p', {'window': '7-9'}))


class BlockedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.old = tz._CHOSEN.get('zone')
        tz._CHOSEN['zone'] = Z
        self.addCleanup(tz._CHOSEN.__setitem__, 'zone', self.old)

    def raw(self):
        machines = {'pc': {'host': 'pc', 'limit': {'window': '09-17', 'pct': 0}},
                    'nuc': {'host': 'nuc', 'limit': {'window': '09-17', 'pct': 50}}}
        return {'devices': {'tv': {'serial': 'x', 'hours': {'window': '22-07'}}, 'phone': {'serial': 'y'}}, 'machines': machines}

    def test_no_hours_by_default_and_blocked_only_inside_the_window(self):
        raw = controls.blocked_hours(self.raw(), at('2026-11-16 20:00'))
        self.assertTrue(all('paused' not in e for e in raw['devices'].values()))
        raw = controls.blocked_hours(self.raw(), at('2026-11-16 23:00'))
        self.assertTrue(raw['devices']['tv']['paused']['blocked'])
        self.assertNotIn('paused', raw['devices']['phone'])   # no hours set: never blocked
        self.assertIn('free again at 07:00', controls.paused_note(raw['devices']['tv']))

    def test_a_machine_at_0_percent_is_not_used_at_all_and_one_at_50_still_is(self):
        raw = controls.blocked_hours(self.raw(), at('2026-11-16 10:00'))   # a Monday
        self.assertTrue(raw['machines']['pc']['paused']['blocked'])
        self.assertNotIn('paused', raw['machines']['nuc'])
        self.assertEqual(share.limit_errors('pc', {'window': '09-17', 'pct': 0}), [])

    def test_work_is_handed_over_once_when_the_window_starts_not_before(self):
        cfg = {'project': {'state': self.tmp}, **controls.blocked_hours(self.raw(), at('2026-11-16 21:40'))}
        stopped = []
        self.assertEqual(controls.blocked_round(cfg, at('2026-11-16 21:40'), lambda k, n: stopped.append(n)), [])   # lead: none
        self.assertTrue(cfg['devices']['tv']['paused']['blocked'])   # but no new work already
        cfg = {'project': {'state': self.tmp}, **controls.blocked_hours(self.raw(), at('2026-11-16 22:00'))}
        controls.blocked_round(cfg, at('2026-11-16 22:00'), lambda k, n: stopped.append(n))
        controls.blocked_round(cfg, at('2026-11-16 23:00'), lambda k, n: stopped.append(n))
        self.assertEqual(stopped, ['tv'])


if __name__ == '__main__':
    unittest.main()
