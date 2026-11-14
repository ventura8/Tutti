"""The owner's real day (tutti/ownerday.py): a heavy-burn day, a light day, an early start and a late stop, and the learned
profile following repeated early starts. Times are UTC with the owner at offset 0; the plan is Mon-Fri 09-17, 1 point/h."""
import unittest
from datetime import UTC, datetime, timedelta

from tutti import owner, ownerday, planner

MON = datetime(2026, 1, 5, tzinfo=UTC)   # a Monday
OWNER = {'schedule': {'days': ['mon', 'tue', 'wed', 'thu', 'fri'], 'window': '09-17', 'hours': 8, 'rate': 1.0, 'utc_offset': 0},
         'mode': 'auto', 'accounts': ['claude'], 'away_after_hours': 3}


def readings(start, rates, base=0.0, used=0.0):
    """Hourly readings from `start`: rates[h] points in hour h (plus the workers' `base`)."""
    out, u = [{'at': start.isoformat(), 'used': used}], used
    for h, r in enumerate(rates):
        u += r + base
        out.append({'at': (start + timedelta(hours=h + 1)).isoformat(), 'used': round(u, 3)})
    return out


def day(early=0, late=0, rate=1.0, hours_now=24):
    """A day's hourly owner rates: work from 9 - early to 17 + late at `rate`, cut at `hours_now`."""
    return [rate if 9 - early <= h < 17 + late else 0.0 for h in range(24)][:hours_now]


class OwnerDay(unittest.TestCase):
    def test_a_heavy_burn_day_cuts_the_workers_and_warns_before_it_runs_out(self):
        now = MON + timedelta(hours=12)
        rs = readings(MON, day(rate=4.0, hours_now=12))
        ctx = owner.context(OWNER, rs, now)
        self.assertGreater(ctx['today']['factor'], 3)
        self.assertIn('burning', ctx['today_line'])
        s = {'share': 1.0, 'used': 40.0, 'target': (MON + timedelta(days=4, hours=23)).isoformat(),
             'reset': (MON + timedelta(days=5)).isoformat()}
        heavy = planner.plan_single(s, {}, now, owner=ctx)
        calm = planner.plan_single(s, {}, now, owner=owner.context(OWNER, readings(MON, day(hours_now=12)), now))
        self.assertEqual(heavy['tasks'], 0)                     # workers back off on this account
        self.assertGreater(calm['left'], heavy['left'])
        self.assertIsNotNone(heavy['run_out'])                  # even with no worker it runs out: warned early
        self.assertIsNone(calm['run_out'])

    def test_a_light_day_frees_the_reserve_hour_by_hour(self):
        target = MON + timedelta(days=4, hours=23)
        s = {'share': 1.0, 'used': 20.0, 'target': target.isoformat(), 'reset': (target + timedelta(hours=1)).isoformat()}
        left = []
        for h in (9, 12):
            now = MON + timedelta(hours=h)
            ctx = owner.context(OWNER, readings(MON, day(rate=0.0, hours_now=h)), now)
            left.append(planner.plan_single(s, {}, now, owner=ctx)['left'])
        self.assertAlmostEqual(left[1] - left[0], 3.0, places=1)   # the three idle owner hours went to the workers by midday
        end = planner.plan_single({**s, 'used': 99.0 - 4 * 8}, {}, target - timedelta(hours=1), owner=owner.context(OWNER, [], target))
        self.assertGreater(end['tasks'] + end['left'], 0)           # what is left is still spent by the spend-by time

    def test_an_early_start_moves_todays_reserve_and_a_late_stop_keeps_it(self):
        early = owner.context(OWNER, readings(MON, day(early=2, hours_now=8)), MON + timedelta(hours=8))
        self.assertEqual(early['today']['start'], 7)
        self.assertGreater(early['reserve'][0][7], 0)
        self.assertIn('started 07:00 (planned 09:00)', early['today_line'])
        late = owner.context(OWNER, readings(MON, day(late=2, hours_now=18)), MON + timedelta(hours=18, minutes=10))
        self.assertTrue(late['today'].get('late'))
        self.assertGreater(late['reserve'][0][18], 0)
        stopped = owner.context(OWNER, readings(MON, day(late=-3, hours_now=16)), MON + timedelta(hours=16))
        self.assertEqual(stopped['today']['stop'], 14)
        self.assertEqual(stopped['reserve'][0][15], 0.0)          # released after the real stop

    def test_the_profile_learns_repeated_early_starts(self):
        rs, used = [], 0.0
        for d in range(5):
            part = readings(MON + timedelta(days=d), day(early=3, rate=2.0), used=used)
            used = part[-1]['used']
            rs += part
        ctx = owner.context(OWNER, rs, MON + timedelta(days=5, hours=1))
        self.assertTrue(ctx['learned'])
        self.assertGreater(ctx['planned_reserve'][0][6], 0)      # Monday's plan now starts at 06:00
        self.assertEqual(ownerday.line({'planned_start': None}), 'Today: no owner hours planned')


if __name__ == '__main__':
    unittest.main()
