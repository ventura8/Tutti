"""The quota planner (tutti/planner.py): an account bound by a longer limit (agy's 5 h windows under their weekly quota),
each window's allowance across the week, quiet hours, the reserve, soonest-expiry routing, and a simulated week that wastes
less than spreading the weekly room evenly over every window (the min() rule)."""
import os
import tempfile
import unittest
from datetime import timedelta

from tutti import config, pacing, planner, readers, routing

T = pacing.parse_time
NOW = T('2026-01-06T02:00Z')
# weekly: 2026-01-01T02:00 -> 2026-01-08T02:00, target 01:00; the 5 h window holding NOW: 00:00 -> 05:00 (10 windows to the target)
WEEK = {'window': '7d', 'resets': '2026-01-08T02:00Z', 'lead_hours': 1}
FIVE = {'window': '5h', 'resets': '2026-01-06T05:00Z', 'lead_hours': 1, 'worker': 'agy', 'limits': ['agy-weekly'], 'window_points': 10}
ACCOUNTS = {'agy': FIVE, 'agy-weekly': WEEK}


def r(at, used, account='agy-weekly'):
    return {'at': at, 'account': account, 'used': used, 'note': ''}


def state(weekly, five_used=30, now=NOW, accounts=ACCOUNTS, hours=None):
    entries = [r(at, u) for at, u in weekly] + [r(now.isoformat(), five_used, 'agy')]
    return pacing.account_state('agy', accounts['agy'], entries, now, accounts, hours)


class Allowance(unittest.TestCase):
    def test_room_is_spread_over_the_windows_that_can_run(self):
        self.assertEqual(planner.allowance(30, [None, None, None]), 10)
        self.assertEqual(planner.allowance(30, [None, 0, None]), 15)       # a quiet window's share moves to the others
        self.assertEqual(planner.allowance(30, [0, None, None]), 0)        # this window cannot run

    def test_a_window_takes_what_the_later_windows_cannot_hold(self):
        self.assertEqual(planner.allowance(30, [25, 5, 5]), 20)            # 10 each would lose 10 at the end
        self.assertEqual(planner.allowance(30, [8, 5, 5]), 8)              # never more than the window has left

    def test_over_its_share_the_window_stops(self):
        # 80 % at the window's start, 84 % now: room 20 over 10 windows of 10 points = 2 points for this one
        s = state([('2026-01-05T23:00Z', 80), ('2026-01-06T02:00Z', 84)])
        p = s['quota_plan']
        self.assertEqual((p['windows'], p['allowance'], p['this_window'], p['left'], p['tasks']), (10, 2.0, 4.0, -2.0, 0))
        self.assertEqual((s['bound_by'], s['room_tasks']), ('agy-weekly', None))
        self.assertIn('4 of 2 points', routing.account_unfit(s))

    def test_within_its_share_it_takes_tasks_and_says_why(self):
        s = state([('2026-01-05T03:00Z', 70), ('2026-01-05T23:00Z', 80), ('2026-01-06T02:00Z', 81)])   # projected past 100: even share
        p = s['quota_plan']
        self.assertEqual((p['left'], p['tasks']), (1.0, 5))               # 1 weekly point = 10 points of the 5 h window = 5 tasks
        self.assertEqual(routing.account_unfit(s), '')
        self.assertIn('agy: spend by 04:00 UTC', p['why'])
        self.assertIn('resets 05:00 UTC', p['why'])
        self.assertIn('this window: 1 of 2 weekly points', p['why'])
        self.assertIn('would be lost at its end → 5 task(s) now', p['why'])
        spent = state([('2026-01-05T23:00Z', 80), ('2026-01-06T02:00Z', 84)])['quota_plan']['why']
        self.assertIn('→ nothing more this window', spent)

    def test_the_plan_comes_as_label_value_rows_without_the_account_name(self):
        # projected past 100: the even share
        p = state([('2026-01-05T03:00Z', 70), ('2026-01-05T23:00Z', 80), ('2026-01-06T02:00Z', 81)])['quota_plan']
        got = {r['label']: r.get('value') or r.get('at') for r in p['rows']}
        self.assertEqual(got['This window'], '1 of 2 weekly points')
        self.assertEqual(got['Spend by'], p['expires'])                    # a time stays ISO: the page shows it in local time
        self.assertEqual((got['Decision'], got['Why']), ('5 task(s) now', '10 % would be lost at its end'))
        self.assertFalse(any('agy' in str(r.get('value', '')) for r in p['rows']))
        single = planner.rows({'limit': None, 'left': 86, 'expires': '2026-12-09T21:00:00+00:00', 'resets': '2026-12-09T22:00:00+00:00',
                               'tasks': 0})
        self.assertEqual([(r['label'], r.get('value') or r.get('at')) for r in single],
                         [('Points left', '86'), ('Spend by', '2026-12-09T21:00:00+00:00'), ('Resets', '2026-12-09T22:00:00+00:00'),
                          ('Decision', 'no extra tasks'), ('Why', 'not behind its pace')])

    def test_the_allowance_grows_as_the_week_runs_out_of_windows(self):
        early = state([('2026-01-01T02:00Z', 0)], now=T('2026-01-01T06:00Z'), five_used=0)
        late = state([('2026-01-07T19:00Z', 90)], now=T('2026-01-07T21:00Z'), five_used=0)
        self.assertEqual(early['quota_plan']['windows'], 33)                    # 05:00 to 01:00 seven days later: 32.8 windows
        self.assertEqual(early['quota_plan']['allowance'], round(100 / 33, 1))
        # the last window (21:00 to the target at 01:00: 4 of its 5 hours, 8 points) gets all it can hold
        self.assertEqual((late['quota_plan']['windows'], late['quota_plan']['allowance']), (1, 8.0))

    def test_the_reserve_is_kept_for_the_owner(self):
        accounts = {'agy': FIVE, 'agy-weekly': {**WEEK, 'reserve': 15}}
        s = state([('2026-01-07T19:00Z', 80)], now=T('2026-01-07T21:00Z'), five_used=0, accounts=accounts)
        self.assertEqual(s['quota_plan']['allowance'], 5.0)                     # 100 - 15 reserve - 80 used

    def test_quiet_hours_give_the_window_nothing(self):
        s = state([('2026-01-05T23:00Z', 40)], hours=['00:00-06:00'])
        self.assertEqual((s['quota_plan']['allowance'], s['quota_plan']['tasks']), (0.0, 0))
        self.assertIn('quiet hours', routing.account_unfit(s))
        self.assertTrue(planner.quiet(T('2026-01-06T23:30Z'), ['22:00-06:00']))   # a span past midnight wraps
        self.assertFalse(planner.quiet(T('2026-01-06T12:00Z'), ['22:00-06:00']))

    def test_a_weekly_reset_since_the_window_started_counts_from_zero(self):
        lim = {'name': 'w', 'readings': [{'at': '2026-01-08T03:00:00+00:00', 'used': 2.0}], 'window_start': '2026-01-08T02:00:00+00:00'}
        self.assertEqual(planner.used_at(lim, T('2026-01-08T00:00Z')), (0.0, True))
        self.assertEqual(planner.used_at({**lim, 'window_start': '2026-01-07T00:00:00+00:00'}, T('2026-01-08T00:00Z')), (2.0, False))

    def test_a_spent_weekly_closes_the_window(self):
        s = state([('2026-01-06T01:00Z', 100)])
        self.assertEqual((s['verdict'], s['bound_by']), ('spent', 'agy-weekly'))
        self.assertIsNone(routing.pick_account([s])[0])

    def test_without_a_weekly_reading_the_window_paces_alone(self):
        s = state([])
        self.assertIsNone(s['quota_plan'])
        self.assertEqual(routing.account_unfit(s), '')


class Background(unittest.TestCase):
    """Cursor spends itself outside Tutti (its automatic reviews): the rise with no Tutti run on it is planned around."""
    RS = [{'at': '2026-01-06T00:00Z', 'used': 10.0}, {'at': '2026-01-06T02:00Z', 'used': 11.0},     # idle: +1 in 2 h
          {'at': '2026-01-06T04:00Z', 'used': 20.0}, {'at': '2026-01-06T06:00Z', 'used': 21.0}]    # a run at 03:00: skipped

    def test_the_rise_without_runs_is_the_background_rate(self):
        busy = [(T('2026-01-06T03:00Z'), T('2026-01-06T03:30Z'))]
        self.assertEqual(planner.background_rate(self.RS, busy), 0.5)                    # 2 points over 4 idle hours

    def test_too_little_idle_time_says_nothing(self):
        busy = [(T('2026-01-06T00:30Z'), T('2026-01-06T05:00Z'))]
        self.assertIsNone(planner.background_rate(self.RS, busy))

    def test_the_plan_keeps_room_for_it_and_the_reserve_defaults_to_zero(self):
        s = {'name': 'cursor', 'state': 'ok', 'share': 1.0, 'used': 40.0, 'target': '2026-01-08T02:00:00+00:00', 'verdict': 'behind',
             'room_tasks': 30}
        alone = planner.plan_alone(s, {}, NOW)['quota_plan']
        bg = planner.plan_alone(s, {}, NOW, rate=0.25)['quota_plan']
        self.assertEqual(alone['left'], 60.0)                                            # no reserve unless configured
        self.assertEqual((bg['background'], bg['left']), (12.0, 48.0))                   # 48 h left x 0.25 points
        self.assertIn('after 12 expected outside Tutti', bg['why'])
        self.assertEqual(bg['tasks'], 24)                                               # 48 points left / 2 per task, under the pace's 30
        on_pace = planner.plan_alone({**s, 'verdict': 'on pace', 'room_tasks': None}, {}, NOW)['quota_plan']
        self.assertEqual(on_pace['tasks'], 0)
        self.assertIn('no extra tasks (not behind its pace)', on_pace['why'])

    def test_runs_of_the_worker_are_its_busy_spans(self):
        with tempfile.TemporaryDirectory() as d:
            for name, text in (('1767664800-1', 'run cursor: fix it'), ('1767668400-2', 'run agy: other')):
                with open(os.path.join(d, f'{name}.log.cmd'), 'w', encoding='utf8') as f:
                    f.write(text)
            with open(os.path.join(d, 'broken.log.cmd'), 'w', encoding='utf8') as f:
                f.write('run cursor: no epoch in the name')
            spans = readers.busy_intervals(d, 'cursor', NOW)
        self.assertEqual(spans, [(T('2026-01-06T02:00Z'), NOW)])                         # still running: until now


class SoonestFirst(unittest.TestCase):
    def test_the_capacity_that_expires_first_is_used_first(self):
        month = {'name': 'cursor', 'verdict': 'on pace', 'quota_plan': {'expires': '2026-01-30T00:00:00+00:00', 'tasks': 9, 'left': 18}}
        window = {'name': 'agy', 'verdict': 'on pace', 'quota_plan': {'expires': '2026-01-06T04:00:00+00:00', 'tasks': 2, 'left': 4}}
        self.assertEqual(routing.pick_account([month, window])[0]['name'], 'agy')

    def test_an_account_ahead_of_its_pace_still_comes_last(self):
        window = {'name': 'agy', 'verdict': 'ahead', 'quota_plan': {'expires': '2026-01-06T04:00:00+00:00', 'tasks': 2, 'left': 4}}
        month = {'name': 'cursor', 'verdict': 'on pace', 'quota_plan': None}
        self.assertEqual(routing.pick_account([window, month])[0]['name'], 'cursor')


def simulate(rule, quiet_hours, budget=100.0, per_window=10.0):
    """A week of 5 h windows to the target (167 h): each runnable window spends what the rule allows; returns the waste."""
    start = T('2026-01-01T02:00Z')
    target = start + timedelta(hours=167)
    windows = []
    t = start
    while t < target:
        end = min(t + timedelta(hours=5), target)
        cap = per_window * (end - t) / timedelta(hours=5)
        windows.append(cap if planner.runnable(t, end, quiet_hours) else 0.0)
        t += timedelta(hours=5)
    room = budget
    for i, cap in enumerate(windows):
        if rule == 'plan':
            spend = planner.allowance(room, windows, i)
        else:   # min(): the room spread evenly over every window left, runnable or not
            spend = min(room / (len(windows) - i), cap)
        room -= min(spend, cap)
    return room


class SimulatedWeek(unittest.TestCase):
    def test_the_planner_wastes_less_than_the_min_rule(self):
        for hours in (['23:00-07:00'], ['20:00-08:00'], []):
            plan, even = simulate('plan', hours), simulate('min', hours)
            self.assertLessEqual(plan, even, hours)
            self.assertLess(plan, 0.01, hours)                            # nothing expires unused
        self.assertGreater(simulate('min', ['20:00-08:00']), 5)

    def test_spending_in_the_last_window_is_counted_once(self):
        # 16:00-21:00 then 21:00 to the target (8 points): room 40 gives this window all its 10; 6 spent, 4 still usable
        s = state([('2026-01-07T15:00Z', 60), ('2026-01-07T17:00Z', 66)], now=T('2026-01-07T17:00Z'), five_used=60)
        p = s['quota_plan']
        self.assertEqual((p['windows'], p['allowance'], p['this_window'], p['left']), (2, 10.0, 6.0, 4.0))
        self.assertGreater(p['tasks'], 0)          # half the windows quiet: min() loses 7 points

    def test_when_capacity_is_short_both_spend_all_they_can(self):
        self.assertAlmostEqual(simulate('plan', ['00:00-23:59'], budget=50), 50)   # nothing can run: everything is lost


class ReviewCases(unittest.TestCase):
    def test_a_first_weekly_reading_after_the_window_started_does_not_close_it(self):
        accounts = {'agy': {k: v for k, v in FIVE.items() if k != 'window_points'}, 'agy-weekly': WEEK}
        s = state([('2026-01-06T02:00Z', 50)], accounts=accounts)        # the rise in this window is not known
        self.assertIsNone(s['quota_plan']['window_points'])
        self.assertGreater(s['quota_plan']['tasks'], 0)

    def test_task_pct_zero_plans_no_tasks_instead_of_failing(self):
        accounts = {'agy': {**FIVE, 'task_pct': 0}, 'agy-weekly': WEEK}
        self.assertEqual(state([('2026-01-05T23:00Z', 40)], accounts=accounts)['quota_plan']['tasks'], 0)
        alone = planner.plan_alone({'name': 'c', 'state': 'ok', 'share': 1.0, 'used': 10.0, 'target': '2026-01-08T00:00Z',
                                    'room_tasks': 3}, {'task_pct': 0}, NOW)
        self.assertEqual(alone['quota_plan']['tasks'], 0)

    def test_a_weekly_in_panic_with_an_unread_window_closes_it_without_failing(self):
        lim = {'name': 'w', 'state': 'ok', 'panic': True, 'verdict': 'ahead', 'window': '7d', 'action': 'stop', 'used': 97}
        own = {'name': 'agy', 'state': 'no reading', 'panic': False, 'verdict': 'no reading', 'used': None, 'room_tasks': None}
        s = planner.bind(own, [lim], {}, {}, None, NOW)
        self.assertFalse(s['panic'])
        self.assertIn('its limit w: panic', routing.account_unfit(s))

    def test_the_limit_with_the_fewest_tasks_binds(self):
        # two limits: one roomy, one with its window share nearly used; the second binds even with more points left
        roomy = {**WEEK}
        tight = {**WEEK, 'resets': '2026-01-06T06:00Z', 'window': '1d', 'panic_at': 100}
        accounts = {'agy': {**FIVE, 'limits': ['w1', 'w2']}, 'w1': roomy, 'w2': tight}
        entries = [r('2026-01-05T23:00Z', 10, 'w1'), r('2026-01-05T23:00Z', 99, 'w2'), r(NOW.isoformat(), 30, 'agy')]
        s = pacing.account_state('agy', accounts['agy'], entries, NOW, accounts)
        own = pacing.account_state('agy', {k: v for k, v in FIVE.items() if k != 'limits'}, entries, NOW)
        plans = [planner.plan_limited(own, pacing.account_state(n, accounts[n], entries, NOW), accounts['agy'], accounts[n], None)
                 for n in ('w1', 'w2')]
        self.assertNotEqual(plans[0]['tasks'], plans[1]['tasks'])
        self.assertEqual(s['quota_plan']['tasks'], min(p['tasks'] for p in plans))

    def test_a_used_reserve_stops_routing_to_a_single_window_account(self):
        s = {'name': 'cursor', 'state': 'ok', 'share': 1.0, 'used': 80.0, 'target': '2026-01-08T00:00Z', 'verdict': 'on pace',
             'room_tasks': None, 'panic': False, 'panic_at': 95}
        planned = planner.plan_alone(s, {'reserve': 20}, NOW)
        self.assertIn('reserve', routing.account_unfit(planned))

    def test_nothing_new_starts_in_quiet_hours(self):
        from tutti import autopilot
        cfg = config.normalise({'autopilot': {'enabled': True, 'quiet_hours': ['00:00-06:00'], 'repos': {}}})
        p = autopilot.plan(cfg, [{'id': 't1', 'repo': 'x', 'status': 'queued'}], {}, [], True, NOW)
        self.assertNotIn('quiet hours', p['reason'])                     # nothing queued for a known repo: idle
        cfg['autopilot']['repos'] = {'x': {'path': '/tmp', 'check': 'true'}}
        p = autopilot.plan(cfg, [{'id': 't1', 'repo': 'x', 'status': 'queued', 'check': 'true'}], {}, [], True, NOW)
        self.assertEqual(p['state'], 'waiting')
        self.assertIn('quiet hours', p['reason'])


class LimitsConfig(unittest.TestCase):
    def errors(self, limits):
        cfg = config.normalise({'workers': {'agy': {'kind': 'cli', 'command': ['agy', '-p', '{prompt}']}},
                                'accounts': {'agy': {**FIVE, 'limits': limits}, 'agy-weekly': WEEK}})
        return [e for e in config.validate(cfg) if '.limits' in e]

    def test_a_known_other_account_is_accepted(self):
        self.assertEqual(self.errors(['agy-weekly']), [])

    def test_unknown_self_and_non_list_are_refused_with_the_fix(self):
        self.assertIn('not under accounts', self.errors(['nope'])[0])
        self.assertIn('the account itself', self.errors(['agy'])[0])
        self.assertIn('a list of other accounts', self.errors('agy-weekly')[0])

    def test_quiet_hours_and_the_reserve_are_checked(self):
        self.assertEqual(config.quiet_hours_errors(['22:00-06:00']), [])
        self.assertIn('HH:MM-HH:MM', config.quiet_hours_errors(['10pm-6am'])[0])
        cfg = config.normalise({'accounts': {'cursor': {'window': 'monthly', 'reserve': -5}}})
        self.assertTrue(any('reserve' in e for e in config.validate(cfg)))


if __name__ == '__main__':
    unittest.main()
