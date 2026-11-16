"""The planner spends a window's unused room while its weekly limit is at or behind its pace line (use it or lose it),
and holds it when the weekly limit is ahead of its pace."""
import unittest
from datetime import datetime, timedelta, timezone

from tutti import planner

UTC = timezone.utc
NOW = datetime(2026, 11, 16, 16, 25, tzinfo=UTC)


def case(weekly_used, expected):
    own = {'name': 'agy-gemini', 'used': 74.7, 'window_start': (NOW - timedelta(hours=4, minutes=38)).isoformat(),
           'reset': (NOW + timedelta(minutes=22)).isoformat(), 'target': (NOW + timedelta(minutes=22)).isoformat()}
    start = NOW - timedelta(hours=4, minutes=38)
    lim = {'name': 'agy-gemini-weekly', 'share': 1.0, 'used': weekly_used, 'expected': expected, 'verdict': 'on pace',
           'readings': [{'at': (start - timedelta(minutes=5)).isoformat(), 'used': weekly_used - 21.9},
                        {'at': NOW.isoformat(), 'used': weekly_used}],
           'window_start': (NOW - timedelta(days=2, hours=10)).isoformat(),
           'target': (NOW + timedelta(days=4, hours=20)).isoformat(), 'reset': (NOW + timedelta(days=4, hours=21)).isoformat()}
    return planner.plan_limited(own, lim, {}, {}, [], None, NOW)


class PaceTest(unittest.TestCase):
    def test_behind_pace_weekly_spends_the_window_room_near_its_reset(self):
        p = case(27.5, 31.3)
        self.assertGreater(p['tasks'], 0)
        self.assertNotIn('reach 100 %', planner._proj(p))

    def test_ahead_of_pace_weekly_holds_the_window(self):
        self.assertEqual(case(45.0, 31.3)['tasks'], 0)


if __name__ == '__main__':
    unittest.main()
