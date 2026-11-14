"""The owner's own prompting in the planner (tutti/owner.py): week-long hour-by-hour simulations of the main agent's weekly
account, with the owner prompting and workers spending what the planner gives them. Every scenario must end the window at
95 % or more, and in the owner's hours the account must never be projected to reach its limit (used + the reserve still
kept stays under 100). Pure: no clock, no files except a temp state folder."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tutti import config, dashboard, owner, planner

UTC = timezone.utc
SCHED = {'days': ['mon', 'tue', 'wed', 'thu', 'fri'], 'hours': 8, 'window': '01-14', 'rate': 1.0, 'utc_offset': 0}


def present_weekdays(hours=range(1, 9), rate=1.5):
    """The owner works Mon-Fri in `hours` at `rate` points per hour."""
    return lambda t: rate if t.weekday() < 5 and t.hour in hours else 0.0


def simulate(reset, burn, mode='auto', sched=None):
    """(end %, [(t, used, kept) in owner hours]) of one weekly window ending at `reset`. The week before it, the same owner
    and an even worker load are in the readings (what the profile learns from). Each hour: the owner burns `burn(t)`, the
    workers spend what the planner lets them (owner.spendable of its `left`, an hour's part of it)."""
    start, target = reset - timedelta(days=7), reset - timedelta(hours=1)
    history, used, t = [], 0.0, start - timedelta(days=7)
    while t < start:
        used += burn(t) + 0.3
        history.append({'at': (t + timedelta(hours=1)).isoformat(), 'used': used})
        t += timedelta(hours=1)
    o = {'accounts': ['claude'], 'schedule': {**SCHED, **(sched or {})}, 'mode': mode, 'away_after_hours': 3}
    readings, log, used = [{'at': start.isoformat(), 'used': 0.0}], [], 0.0
    while t < target:
        ctx = owner.context(o, history + readings, t)
        s = {'name': 'claude', 'state': 'ok', 'target': target.isoformat(), 'reset': reset.isoformat(), 'share': 1.0, 'used': used,
             'room_tasks': None}
        p = planner.plan_single(s, {'task_pct': 1}, t, owner=ctx)
        hours = (target - t).total_seconds() / 3600
        workers = owner.spendable(p['left'], t, target) / max(1.0, min(owner.BLOCK, hours))
        if burn(t):
            log.append((t, used, owner.reserve_until(ctx, t, target)))
        used = min(100.0, used + burn(t) + workers)
        readings.append({'at': (t + timedelta(hours=1)).isoformat(), 'used': used})
        t += timedelta(hours=1)
    return used, log


class SimulationTest(unittest.TestCase):
    def check(self, end, log):
        self.assertGreaterEqual(end, 95.0)
        for t, used, kept in log:
            self.assertLess(used + kept, 100.0 + 1e-6, f'the owner would hit the limit at {t}')

    def test_a_full_week_of_five_owner_days_and_a_weekend(self):
        self.check(*simulate(datetime(2026, 11, 16, 2, tzinfo=UTC), present_weekdays()))   # resets Monday 02:00

    def test_a_reset_on_saturday(self):
        self.check(*simulate(datetime(2026, 11, 21, 2, tzinfo=UTC), present_weekdays()))

    def test_a_reset_on_wednesday_keeps_the_days_left_before_it(self):
        reset = datetime(2026, 11, 18, 2, tzinfo=UTC)
        end, log = simulate(reset, present_weekdays())
        self.check(end, log)
        monday = [kept for t, _, kept in log if t.weekday() == 0 and t.hour == 1]
        self.assertGreater(monday[0], 0)                  # on Monday, Monday's and Tuesday's owner hours are still kept

    def test_away_for_a_day_lets_the_workers_take_it(self):
        burn = present_weekdays()
        self.check(*simulate(datetime(2026, 11, 21, 2, tzinfo=UTC), lambda t: 0.0 if t.weekday() == 2 else burn(t)))

    def test_a_two_week_vacation_spends_it_all(self):
        reset = datetime(2026, 11, 21, 2, tzinfo=UTC)
        end, log = simulate(reset, lambda t: 0.0, mode=f'vacation-until-{(reset + timedelta(days=7)).date()}')
        self.check(end, log)

    def test_an_owner_who_starts_late_in_the_seeded_window(self):
        self.check(*simulate(datetime(2026, 11, 21, 2, tzinfo=UTC), present_weekdays(hours=range(6, 14))))

    def test_mostly_agents_an_hour_a_day(self):
        self.check(*simulate(datetime(2026, 11, 21, 2, tzinfo=UTC), present_weekdays(hours=range(9, 10), rate=3.0)))

    def test_the_weekend_reserves_nothing(self):
        o = {'accounts': ['claude'], 'schedule': SCHED, 'mode': 'auto', 'away_after_hours': 3}
        sat = datetime(2026, 11, 21, 3, tzinfo=UTC)
        ctx = owner.context(o, [], sat)
        self.assertEqual(owner.reserve_until(ctx, sat, sat + timedelta(hours=40)), 0.0)
        self.assertEqual(owner.reserve_until(ctx, sat, sat + timedelta(hours=56)), 8.0)   # Monday 01-09: hours 46 to 54


class ProfileTest(unittest.TestCase):
    def test_a_learned_window_wider_than_the_hours_keeps_the_busiest(self):
        row = [0.0] * 24
        for h in range(6, 20):
            row[h] = 2.0 if 10 <= h < 18 else 1.0
        self.assertEqual(owner.busiest(row, 8), set(range(10, 18)))

    def test_the_seed_until_enough_history_and_the_workers_baseline_taken_off(self):
        self.assertFalse(owner.profile_of(SCHED, [], 0)['learned'])
        t0 = datetime(2026, 11, 9, tzinfo=UTC)   # a Monday
        used, rs = 0.0, []
        for i in range(24 * 5):
            t = t0 + timedelta(hours=i)
            used += 0.5 + (2.0 if 3 <= t.hour < 6 else 0.0)
            rs.append({'at': (t + timedelta(hours=1)).isoformat(), 'used': used})
        p = owner.profile_of(SCHED, rs, 0)
        self.assertTrue(p['learned'])
        self.assertAlmostEqual(p['reserve'][1][4], 2.0, places=1)   # Tuesday 04:00: the owner's 2, not the workers' 0.5
        self.assertEqual(p['reserve'][1][12], 0.0)

    def test_a_scattered_peak_keeps_the_stretch_that_holds_most(self):
        row = [0.0] * 24
        row[2], row[3], row[4], row[20] = 1.0, 1.0, 1.0, 2.5
        self.assertEqual(owner.busiest(row, 3), {2, 3, 4})

    def test_an_impossible_vacation_date_is_refused_and_a_bad_saved_mode_is_auto(self):
        self.assertFalse(owner.valid_mode('vacation-until-2026-13-45'))
        reserve = [[0.0] * 24 for _ in range(7)]
        self.assertEqual(owner.effective('vacation-until-2026-13-45', reserve, [], datetime(2026, 11, 16, tzinfo=UTC), 0, 3)[0], 'auto')

    def test_a_seeded_window_never_infers_away(self):
        reserve = [[1.0 if d < 5 and 1 <= h < 9 else 0.0 for h in range(24)] for d in range(7)]
        mon = datetime(2026, 11, 16, 5, tzinfo=UTC)
        flat = [{'at': (mon - timedelta(hours=i)).isoformat(), 'used': 10.0} for i in range(30)]
        self.assertEqual(owner.effective('auto', reserve, flat, mon, 0, 3, learned=False)[0], 'auto')

    def test_auto_goes_away_after_quiet_owner_hours_and_vacation_ends(self):
        reserve = [[1.0 if d < 5 and 1 <= h < 9 else 0.0 for h in range(24)] for d in range(7)]
        mon = datetime(2026, 11, 16, 5, tzinfo=UTC)
        flat = [{'at': (mon - timedelta(hours=i)).isoformat(), 'used': 10.0} for i in range(30)]
        self.assertEqual(owner.effective('auto', reserve, flat, mon, 0, 3)[0], 'away')
        busy = [{'at': (mon - timedelta(hours=30 - i)).isoformat(), 'used': i * 0.2 + (2.0 * max(0, i - 25))} for i in range(31)]
        self.assertEqual(owner.effective('auto', reserve, busy, mon, 0, 3)[0], 'auto')
        self.assertEqual(owner.effective('vacation-until-2026-11-20', reserve, [], mon, 0, 3)[0], 'vacation')
        self.assertEqual(owner.effective('vacation-until-2026-11-01', reserve, [], mon, 0, 3)[0], 'auto')
        self.assertEqual(owner.effective('active', reserve, flat, mon, 0, 3)[0], 'active')

    def test_projection_carries_the_last_day_on(self):
        now = datetime(2026, 11, 16, 12, tzinfo=UTC)
        rs = [{'at': (now - timedelta(hours=10)).isoformat(), 'used': 10.0}, {'at': now.isoformat(), 'used': 20.0}]
        self.assertEqual(owner.projection(rs, 20.0, now, now + timedelta(hours=40)), 60.0)
        self.assertIsNone(owner.projection(rs[:1], 20.0, now, now))

    def test_config_errors_name_the_key(self):
        errs = owner.schedule_errors({'schedule': {'days': ['monday'], 'window': '1-14', 'hours': 30}, 'mode': 'gone'})
        self.assertEqual(len(errs), 4)
        bad = owner.schedule_errors({'schedule': {}, 'accounts': 'claude', 'away_after_hours': 'x'})
        self.assertEqual([e.split(' ')[0] for e in bad], ['pacing.accounts', 'pacing.away_after_hours'])

    def test_an_owner_account_under_a_weekly_limit_keeps_the_reserve_out_of_it(self):
        now = datetime(2026, 11, 16, 0, tzinfo=UTC)
        own = {'name': 'agy', 'used': 10.0, 'window_start': (now - timedelta(hours=1)).isoformat(),
               'reset': (now + timedelta(hours=4)).isoformat(), 'target': (now + timedelta(hours=3)).isoformat()}
        lim = {'name': 'agy-weekly', 'share': 1.0, 'used': 20.0, 'readings': [], 'window_start': (now - timedelta(days=2)).isoformat(),
               'target': (now + timedelta(days=4)).isoformat()}
        ctx = owner.context({'accounts': ['agy'], 'schedule': SCHED, 'mode': 'auto', 'away_after_hours': 3}, [], now)
        with_owner = planner.plan_limited(own, lim, {}, {}, [], ctx, now)
        alone = planner.plan_limited(own, lim, {}, {}, [])
        self.assertEqual(with_owner['owner_reserve'], 32.0)   # Mon-Thu 01-09 before Friday's target
        self.assertLess(with_owner['allowance'], alone['allowance'])
        self.assertIn('kept for the owner', planner.explain('agy', {**with_owner, 'why': ''}, now))
        self.assertEqual(owner.schedule_errors({'schedule': {}, 'mode': 'vacation-until-2026-12-01'}), [])


class ModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raw = {'project': {'state': os.path.join(self.tmp.name, 'state')},
               'workers': {'claude': {'kind': 'cli', 'command': ['c', '{prompt}']}},
               'accounts': {'claude': {'worker': 'claude', 'window': '7d', 'resets': '2026-11-14T02:00Z'}}}
        self.cfg = config.normalise(raw)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_dashboard_sets_the_mode_and_the_schedule_and_refuses_bad_ones(self):
        self.assertEqual(dashboard.save_owner(SimpleNamespace(cfg=self.cfg), b'{"mode": "vacation-until-2026-12-01"}')[0], 200)
        self.assertEqual(dashboard.save_owner(SimpleNamespace(cfg=self.cfg), b'{"days": ["sat"], "window": "08-16"}')[0], 200)
        o = owner.owner_of(self.cfg, config.state_dir(self.cfg))
        self.assertEqual((o['mode'], o['schedule']['days'], o['schedule']['window'], o['accounts']),
                         ('vacation-until-2026-12-01', ['sat'], '08-16', ['claude']))
        self.assertEqual(dashboard.save_owner(SimpleNamespace(cfg=self.cfg), b'{"mode": "sleeping"}')[0], 400)
        self.assertEqual(dashboard.save_owner(SimpleNamespace(cfg=self.cfg), b'{"window": "99"}')[0], 400)
        self.assertEqual(dashboard.save_owner(SimpleNamespace(cfg=self.cfg), b'nope')[0], 400)
        self.assertEqual(dashboard.save_owner(SimpleNamespace(cfg=self.cfg), b'{"mode": null}')[0], 200)
        self.assertEqual(owner.owner_of(self.cfg, config.state_dir(self.cfg))['mode'], 'auto')


if __name__ == '__main__':
    unittest.main()
