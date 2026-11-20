import os
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

from tutti import autopilot, pacing, state

# These tests read times in UTC: the host's detected zone is pinned so they pass anywhere (tests/test_tz.py covers zones)
_PIN = mock.patch('tutti.tz.host_zone', return_value='UTC')


def setUpModule():
    _PIN.start()


def tearDownModule():
    _PIN.stop()

T = pacing.parse_time


class PacingTest(unittest.TestCase):
    def test_window_rolls_forward_and_back_from_any_anchor(self):
        self.assertEqual(pacing.window_at(T('2026-01-03T02:00Z'), '7d', T('2026-01-20T00:00Z')),
                         (T('2026-01-17T02:00Z'), T('2026-01-24T02:00Z')))
        self.assertEqual(pacing.window_at(T('2026-01-24T02:00Z'), '7d', T('2026-01-05T00:00Z')),
                         (T('2026-01-03T02:00Z'), T('2026-01-10T02:00Z')))

    def test_a_reset_instant_starts_the_new_window(self):
        start, reset = pacing.window_at(T('2026-01-03T02:00Z'), '7d', T('2026-01-10T02:00Z'))
        self.assertEqual((start, reset), (T('2026-01-10T02:00Z'), T('2026-01-17T02:00Z')))

    def test_monthly_keeps_the_day_and_clamps_short_months(self):
        self.assertEqual(pacing.window_at(T('2026-01-31T00:00Z'), 'monthly', T('2026-02-10T00:00Z')),
                         (T('2026-01-31T00:00Z'), T('2026-02-28T00:00Z')))

    def test_bad_window_says_what_to_write(self):
        with self.assertRaises(ValueError) as ctx:
            pacing.shift(T('2026-01-01T00:00Z'), 'fortnight', 1)
        self.assertIn('monthly', str(ctx.exception))

    def test_verdicts(self):
        start, reset = T('2026-01-03T02:00Z'), T('2026-01-10T02:00Z')
        half = start + (reset - timedelta(hours=1) - start) / 2

        def verdict(now, used, share=1.0):
            return pacing.pace(now, start, reset, used, 1, share)['verdict']
        self.assertEqual(verdict(half, 50), 'on pace')
        self.assertEqual(verdict(half, 80), 'ahead')
        self.assertEqual(verdict(half, 20), 'behind')
        self.assertEqual(verdict(half, 100), 'spent')
        self.assertEqual(verdict(reset - timedelta(hours=0.5), 70), 'late')
        self.assertEqual(verdict(half, 25, share=0.5), 'on pace')
        self.assertEqual(verdict(half, 50, share=0.5), 'spent')

    def test_target_defaults_to_one_hour_before_the_reset(self):
        p = pacing.pace(T('2026-01-05T00:00Z'), T('2026-01-03T02:00Z'), T('2026-01-10T02:00Z'), 10)
        self.assertEqual(p['target'], T('2026-01-10T01:00Z'))

    def test_target_is_the_reset_minus_the_lead(self):
        p = pacing.pace(T('2026-01-05T00:00Z'), T('2026-01-03T02:00Z'), T('2026-01-10T02:00Z'), 10, lead_hours=3)
        self.assertEqual(p['target'], T('2026-01-09T23:00Z'))

    def test_log_and_report(self):
        accounts = {'claude': {'window': '7d', 'resets': '2026-01-03T02:00Z', 'source': 'the usage page', 'share': 0.5},
                    'agy': {'source': 'the account page'}}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'usage.jsonl')
            pacing.append_log(path, 'claude', 90, T('2026-01-02T00:00Z'))   # the window before: a reset passed since
            text = pacing.report(accounts, pacing.read_log(path), T('2026-01-06T12:00Z'))
            self.assertIn('claude   0 %', text)                       # the new window starts from 0 at once: work goes on
            self.assertIn('behind', text)
            self.assertIn('accounts.agy.resets', text)
            self.assertIn('Sat 10 Jan 01:00 (UTC)', text)  # spend by: reset minus the default 1 h lead
            pacing.append_log(path, 'claude', 20, T('2026-01-05T00:00Z'))
            pacing.append_log(path, 'claude', 52, T('2026-01-06T11:00Z'))
            text = pacing.report(accounts, pacing.read_log(path), T('2026-01-06T12:00Z'))
            self.assertIn('52 %', text)
            self.assertIn("this session's part is 50 %", text)
            self.assertIn('on pace', text)

    def test_report_spend_by_honours_per_account_lead_hours(self):
        accounts = {'claude': {'window': '7d', 'resets': '2026-01-03T02:00Z', 'lead_hours': 3, 'source': 'the usage page'}}
        text = pacing.report(accounts, [], T('2026-01-06T12:00Z'))
        self.assertIn('Fri 09 Jan 23:00 (UTC)', text)  # reset 02:00 minus 3 h
        self.assertNotIn('Sat 10 Jan 01:00 (UTC)', text)




class ToleranceTest(unittest.TestCase):
    def test_early_in_the_window_ten_against_six_is_ahead(self):
        start = pacing.parse_time('2026-11-14T02:00Z')
        reset = start + pacing.timedelta(days=7)
        now = start + (reset - pacing.timedelta(hours=1) - start) * 0.06
        p = pacing.pace(now, start, reset, 10)
        self.assertAlmostEqual(p['expected'], 6, places=3)
        self.assertEqual((p['verdict'], p['tolerance']), ('ahead', 1.5))
        self.assertEqual(pacing.pace(now, start, reset, 7)['verdict'], 'on pace')

    def test_the_band_is_one_point_at_least_and_five_at_most(self):
        self.assertEqual((pacing.tolerance(0), pacing.tolerance(80)), (1.0, 5))

    def test_the_report_row_shows_the_math(self):
        acc = {'claude': {'window': '7d', 'resets': '2026-01-03T02:00Z', 'share': 0.5}}
        entries = [{'at': '2026-01-06T00:00Z', 'account': 'claude', 'used': 50, 'note': ''}]
        out = pacing.report(acc, entries, pacing.parse_time('2026-01-06T02:00Z'))
        self.assertIn('the line is the whole account', out)
        self.assertIn('50 % vs', out)


class NeedsReadingTest(unittest.TestCase):
    """Automatic accounts report reader failures without asking the person for a manual reading."""

    def test_only_where_nothing_reads_it_or_the_reading_is_old(self):
        now = pacing.parse_time('2026-01-06T12:00Z')
        fresh, old = now - timedelta(minutes=30), now - timedelta(hours=3)
        self.assertTrue(pacing.needs_reading({}, fresh, now))                                  # read by hand (Cursor)
        self.assertFalse(pacing.needs_reading({'reader': {'command': ['x']}}, fresh, now))     # agy: read automatically
        self.assertFalse(pacing.needs_reading({'reader': {'command': ['x']}}, old, now))
        self.assertFalse(pacing.needs_reading({'reading_file': '/r.json'}, None, now))
        self.assertFalse(pacing.needs_reading({'reading_file': '/r.json', 'stale_hours': 4}, old, now))


class AutomaticFreshnessTest(unittest.TestCase):
    def setUp(self):
        self.now = pacing.parse_time('2026-01-06T12:00Z')
        self.account = {'reader': {'command': ['example-reader']}, 'window': '7d', 'resets': '2026-01-10T02:00Z'}

    def test_missing_and_old_automatic_readings_are_stale_without_manual_prompt(self):
        for entries in ([], [{'account': 'api', 'at': '2026-01-06T09:00Z', 'used': 20, 'source': 'reader'}]):
            state = pacing.account_state('api', self.account, entries, self.now)
            self.assertTrue(state['stale'])
            self.assertTrue(state['auto'])
            self.assertFalse(state['needs_reading'])
            self.assertIn('automatic reader', state['remind'])
            self.assertNotIn('usage log', state['remind'])
        self.assertNotIn('usage log', pacing.account_state('api', self.account, [], self.now)['action'])

    def test_fresh_automatic_reading_is_not_stale_and_implicit_readers_are_detected(self):
        entry = {'account': 'codex', 'at': '2026-01-06T11:30Z', 'used': 20, 'source': 'reader'}
        state = pacing.account_state('codex', {'window': '7d', 'resets': '2026-01-10T02:00Z'}, [entry], self.now)
        self.assertFalse(state['stale'])
        self.assertTrue(state['auto'])
        self.assertFalse(state['needs_reading'])
        entry['at'] = '2026-01-06T08:00Z'
        self.assertTrue(pacing.account_state('codex', {'window': '7d', 'resets': '2026-01-10T02:00Z'}, [entry], self.now)['stale'])

    def test_automatic_missing_reading_is_visible_even_without_reset_configuration(self):
        state = pacing.account_state('api', {'reader': {'command': ['example-reader']}}, [], self.now)
        self.assertEqual(state['state'], 'no reset')
        self.assertTrue(state['stale'])
        self.assertTrue(state['auto'])
        self.assertFalse(state['needs_reading'])

    def test_future_or_estimated_reading_does_not_hide_stale_actual(self):
        future = {'account': 'api', 'at': '2026-01-06T13:00Z', 'used': 20, 'source': 'reader'}
        result = pacing.account_state('api', self.account, [future], self.now)
        self.assertTrue(result['stale'])
        self.assertIn('future', result['remind'])
        old = {'account': 'api', 'at': '2026-01-06T08:00Z', 'used': 10, 'source': 'reader'}
        estimate = {'account': 'api', 'at': '2026-01-06T11:59Z', 'used': 11, 'source': 'estimate'}
        result = pacing.account_state('api', self.account, [old, estimate], self.now)
        self.assertTrue(result['stale'])
        self.assertFalse(result['needs_reading'])

    def test_stale_and_future_linked_windows_block_panic_rescue_until_fresh(self):
        now = self.now
        common = {'reader': {'command': ['example-reader']}, 'window': '7d', 'resets': '2026-01-10T02:00Z'}
        configured = {'cursor': {**common, 'worker': 'cursor', 'limits': ['weekly']},
                      'weekly': dict(common), 'claude': {'worker': 'claude'}}
        cfg = {'accounts': configured, 'workers': {'cursor': {'kind': 'cli'}, 'claude': {'kind': 'cli'},
                                                    'local': {'kind': 'ollama'}},
               '_main_worker': 'local', '_now': now, '_account_limits': {'cursor': ['weekly']}, 'autopilot': {}}
        own_entry = {'account': 'cursor', 'at': (now - timedelta(minutes=10)).isoformat(), 'used': 96, 'source': 'reader'}
        for offset, expected in ((timedelta(hours=-3), False), (timedelta(minutes=10), False),
                                 (timedelta(minutes=-10), True)):
            with self.subTest(offset=offset):
                limit_entry = {'account': 'weekly', 'at': (now + offset).isoformat(), 'used': 40, 'source': 'reader'}
                own = pacing.account_state('cursor', configured['cursor'], [own_entry, limit_entry], now, accounts=configured)
                self.assertEqual(own['limits'][0]['name'], 'weekly')
                state.with_readers({'accounts': [own]}, {'cursor': {'problem': None, 'used': 96}, 'weekly': {'problem': None, 'used': 40}})
                states = {'cursor': own, 'claude': {'panic': True, 'used': 96}}
                self.assertEqual(autopilot.panic_rescue(cfg, 'cursor', own, states), expected)

    def test_zero_manual_reminder_does_not_hide_automatic_failure(self):
        self.assertFalse(pacing.staleness('manual', {'stale_hours': 0}, None, self.now)[0])
        self.assertTrue(pacing.staleness('api', {**self.account, 'stale_hours': 0}, None, self.now)[0])

    def test_automatic_stale_hours_zero_still_ages(self):
        old = self.now - timedelta(hours=pacing.STALE_HOURS + 1)
        stale, msg = pacing.staleness('api', {**self.account, 'stale_hours': 0}, old, self.now)
        self.assertTrue(stale)
        self.assertIn('old', msg)
        fresh = self.now - timedelta(minutes=5)
        self.assertFalse(pacing.staleness('api', {**self.account, 'stale_hours': 0}, fresh, self.now)[0])


if __name__ == '__main__':
    unittest.main()
