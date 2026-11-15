"""A day with its own owner hours (pacing.dates, `tutti owner window <date> <HH-HH>`): the reserve follows that day's window,
a vacation that ends before it hands over to it, and bad input is refused with the fix. Pure, plus a temp state folder."""
import tempfile
import unittest
from datetime import datetime, timezone

from tutti import owner

SCHED = {'days': ['mon', 'tue', 'wed', 'thu', 'fri'], 'hours': 8, 'window': '10-18', 'rate': 1.0, 'utc_offset': None}


def ctx(mode, dates, now):
    return owner.context({'schedule': SCHED, 'mode': mode, 'zone': 'Europe/Bucharest', 'dates': dates}, [], now)


class DatedWindow(unittest.TestCase):
    def test_the_day_keeps_its_own_hours_and_the_rest_runs_free(self):
        now = datetime(2026, 11, 16, 6, tzinfo=timezone.utc)                  # Mon 16 Nov, 08:00 in Bucharest
        end = datetime(2026, 11, 16, 22, 30, tzinfo=timezone.utc)             # 00:30 on the 17th
        c = ctx('auto', {'2026-11-16': '20-24'}, now)
        self.assertEqual(owner.reserve_until(c, now, end), 4.0)               # 20-24 only, not the usual 10-18
        noon = datetime(2026, 11, 16, 10, tzinfo=timezone.utc)
        self.assertEqual(owner.reserve_until(c, now, noon), 0.0)              # its usual hours are free for the agents
        plain = ctx('auto', {}, now)
        self.assertEqual(owner.reserve_until(plain, now, end), 8.0)

    def test_a_vacation_that_ends_hands_over_to_the_dated_window(self):
        now = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)                 # still on vacation until the 16th
        c = ctx('vacation-until-2026-11-16', {'2026-11-16': '20-24'}, now)
        self.assertEqual(c['mode'], 'vacation')
        end = datetime(2026, 11, 16, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(owner.reserve_until(c, now, end), 4.0)               # nothing on the 15th; the 16th's 20-24
        self.assertEqual(owner.reserve_days(c, now, end), ['Mon 16'])
        later = datetime(2026, 11, 16, 7, tzinfo=timezone.utc)
        self.assertEqual(ctx('vacation-until-2026-11-16', {'2026-11-16': '20-24'}, later)['mode'], 'auto')

    def test_off_and_bad_input(self):
        now = datetime(2026, 11, 16, 6, tzinfo=timezone.utc)
        c = ctx('auto', {'2026-11-16': 'off'}, now)
        self.assertEqual(owner.reserve_until(c, now, datetime(2026, 11, 16, 22, tzinfo=timezone.utc)), 0.0)
        self.assertIn('HH-HH', owner.date_errors({'2026-11-16': '8pm'})[0])
        self.assertIn('a mapping of dates', owner.date_errors(['2026-11-16'])[0])   # a list or a string, not a crash
        self.assertIn('a mapping of dates', owner.date_errors('2026-11-16')[0])
        self.assertEqual(owner.date_errors(None), [])
        self.assertIn('a date such as', owner.date_errors({'16 Nov': '20-24'})[0])
        self.assertIn('a date such as', owner.date_errors({'2026-13-40': '20-24'})[0])

    def test_the_dashboard_and_cli_change_saves_and_takes_back(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(owner.change_date(folder, {'date': '2026-11-16', 'window': '20-24'})[0], 200)
            self.assertEqual(owner.read(folder)['dates'], {'2026-11-16': '20-24'})
            self.assertEqual(owner.change_date(folder, {'date': '2026-11-16', 'window': 'late'})[0], 400)
            owner.change_date(folder, {'date': '2026-11-16', 'window': None})
            self.assertNotIn('dates', owner.read(folder))


class Review(unittest.TestCase):
    def test_a_window_past_midnight_is_refused_with_the_fix(self):
        self.assertIn('next day its own window', owner.date_errors({'2026-11-16': '22-02'})[0])

    def test_a_config_day_can_be_taken_back_and_a_yaml_date_key_counts(self):
        import datetime as dt

        from tutti import config
        with tempfile.TemporaryDirectory() as folder:
            cfg = config.normalise({'project': {'state': folder}, 'pacing': {'dates': {dt.date(2026, 11, 16): '20-24'}}})
            self.assertEqual(owner.owner_of(cfg, folder)['dates'], {'2026-11-16': '20-24'})   # an unquoted YAML date key
            self.assertEqual(owner.change(cfg, folder, {'date': '2026-11-16', 'window': None})[0], 200)
            self.assertEqual(owner.owner_of(cfg, folder)['dates'], {})                      # taken back over the config
