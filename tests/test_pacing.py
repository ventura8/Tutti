import os
import tempfile
import unittest
from datetime import timedelta

from tutti import pacing

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
        half = start + (reset - timedelta(hours=3) - start) / 2

        def verdict(now, used, share=1.0):
            return pacing.pace(now, start, reset, used, 3, share)['verdict']
        self.assertEqual(verdict(half, 50), 'on pace')
        self.assertEqual(verdict(half, 80), 'ahead')
        self.assertEqual(verdict(half, 20), 'behind')
        self.assertEqual(verdict(half, 100), 'spent')
        self.assertEqual(verdict(reset - timedelta(hours=1), 70), 'late')
        self.assertEqual(verdict(half, 25, share=0.5), 'on pace')
        self.assertEqual(verdict(half, 50, share=0.5), 'spent')

    def test_target_is_the_reset_minus_the_lead(self):
        p = pacing.pace(T('2026-01-05T00:00Z'), T('2026-01-03T02:00Z'), T('2026-01-10T02:00Z'), 10, lead_hours=3)
        self.assertEqual(p['target'], T('2026-01-09T23:00Z'))

    def test_log_and_report(self):
        accounts = {'claude': {'window': '7d', 'resets': '2026-01-03T02:00Z', 'source': 'the usage page', 'share': 0.5},
                    'agy': {'source': 'the account page'}}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'usage.jsonl')
            pacing.append_log(path, 'claude', 90, T('2026-01-02T00:00Z'))   # the window before: says nothing about this one
            text = pacing.report(accounts, pacing.read_log(path), T('2026-01-06T12:00Z'))
            self.assertIn('no reading', text)
            self.assertIn('tutti usage log claude', text)
            self.assertIn('accounts.agy.resets', text)
            pacing.append_log(path, 'claude', 20, T('2026-01-05T00:00Z'))
            pacing.append_log(path, 'claude', 52, T('2026-01-06T11:00Z'))
            text = pacing.report(accounts, pacing.read_log(path), T('2026-01-06T12:00Z'))
            self.assertIn('52 %', text)
            self.assertIn("this session's part is 50 %", text)
            self.assertIn('on pace', text)


if __name__ == '__main__':
    unittest.main()
