"""The scout's week (tutti/grokweek.py), the scout as a regular reviewer, the adaptive autopilot limit (tutti/parallel.py),
heavy gates routed away from a short machine, and every account's target by its spend-by time (tutti/target.py)."""
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from tutti import grokweek, pacing, parallel, planner, review, target, trial

START = datetime(2026, 1, 4, tzinfo=UTC)


def acc(used, reset=START + timedelta(days=7), window_start=START, lead=1):
    return {'name': 'grok-bot', 'state': 'ok', 'used': used, 'window_start': window_start.isoformat(),
            'target': (reset - timedelta(hours=lead)).isoformat(), 'reset': reset.isoformat()}


class GrokWeek(unittest.TestCase):
    def test_the_cost_of_a_run_is_measured_from_real_readings_around_its_runs(self):
        t0 = START + timedelta(days=6)
        entries = [{'at': t0.isoformat(), 'account': 'grok-bot', 'used': 1.0},
                   {'at': (t0 + timedelta(hours=1)).isoformat(), 'account': 'grok-bot', 'used': 9.0, 'source': 'estimate'},
                   {'at': (t0 + timedelta(hours=4)).isoformat(), 'account': 'grok-bot', 'used': 3.0}]
        requests = [{'scout': 'grok', 'status': 'answered', 'sent': (t0 + timedelta(hours=2, minutes=i)).timestamp()} for i in range(16)]
        c = grokweek.cost(entries, requests, 'grok-bot', 'grok', fallback=2)
        self.assertEqual((c['pct'], c['runs'], c['measured']), (0.125, 16, True))   # the estimate in between is ignored
        few = grokweek.cost(entries, requests[:2], 'grok-bot', 'grok', fallback=2)
        self.assertEqual((few['pct'], few['measured']), (2.0, False))                  # too few runs: the guess stays

    def test_a_week_of_rounds_spreads_evenly_and_ends_near_95(self):
        used, pct, sent_by_half = 0.0, 0.15, None
        for hour in range(7 * 24):
            now = (START + timedelta(hours=hour)).timestamp()
            b = grokweek.budget(acc(used), now, pct, 95, 6)
            used += min(b['now'], 30) * pct          # a round sends what the line allows (the burst caps at 30 waiting)
            if hour == 84:
                sent_by_half = used
        self.assertGreaterEqual(used, 93)
        self.assertLessEqual(used, 95)
        self.assertTrue(40 <= sent_by_half <= 52, sent_by_half)   # half the week gone, about half the allowance used

    def test_ahead_of_its_line_it_waits_and_at_its_stop_it_sends_nothing(self):
        now = (START + timedelta(days=1)).timestamp()
        self.assertEqual(grokweek.budget(acc(50), now, 0.2, 95, 6)['mode'], 'ahead')
        self.assertEqual(grokweek.budget(acc(95), now, 0.2, 95, 6)['now'], 0)
        self.assertEqual(grokweek.budget({'state': 'no reading'}, now, 0.2, 95, 6)['mode'], 'off')
        burst = grokweek.budget(acc(15), (START + timedelta(days=6, hours=20)).timestamp(), 0.2, 95, 6)
        self.assertEqual((burst['mode'], burst['now']), ('burst', 400))           # the rest, all at once before the reset

    def test_the_card_names_the_runs_left_and_the_mix(self):
        b = grokweek.budget(acc(10), (START + timedelta(days=2)).timestamp(), 0.2, 95, 6)
        mixed = grokweek.mix(b['left'], 5, reviews=6, triage=2, notes=1, checks=1, research=3)
        text = grokweek.text(b, {'pct': 0.2, 'runs': 17, 'measured': True}, mixed, {'review': 2})
        self.assertIn('about 425 runs left this week', text)
        self.assertIn('6 review / 10 triage', text)
        self.assertIn('today: 2 review', text)


class ScoutReviewer(unittest.TestCase):
    def test_grok_is_asked_and_counts_when_copilot_is_spent_and_coderabbit_limited(self):
        now = datetime.now(UTC)
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, 'usage.jsonl')
            pacing.append_log(log, 'copilot', 100, now - timedelta(hours=1), resets=now + timedelta(days=10))
            cfg = {'review': {'reviewers': ['copilot', 'coderabbit', 'grok'], 'timeout_minutes': 1, 'poll_seconds': 1, 'fallback': []},
                   'scouts': {'grok': {}}, 'usage': {'log': log}, 'project': {'state': d}, 'workers': {}, 'accounts': {}}
            later = (now + timedelta(seconds=5)).isoformat()
            comments = [{'user': {'login': 'coderabbitai[bot]'}, 'created_at': later, 'body': 'Review limit reached'},
                        {'user': {'login': 'owner'}, 'created_at': later, 'body': 'grok-scout\n- a finding'}]

            def gh(args):
                if args[:2] == ['pr', 'comment']:
                    return 0, '', ''
                if args[:2] == ['api', 'user']:
                    return 0, 'owner\n', ''                     # the account Grok Bot posts through
                return 0, json.dumps(comments if 'comments' in args[-1] else []), ''
            asked, said = [], []
            orig = trial.ask_review
            trial.ask_review = lambda *a, **k: asked.append((a[1:4], k.get('listed'))) or {'status': 'waiting', 'id': 's1'}
            try:
                rc = review.review(cfg, 7, 'octo/app', gh_fn=gh, say=said.append, clock=lambda: 0, sleep=lambda s: None)
            finally:
                trial.ask_review = orig
        self.assertEqual(rc, 0)
        self.assertEqual(asked, [(('grok', 'octo/app', 7), True)])
        self.assertTrue(any('grok reviewed' in s for s in said), said)
        self.assertTrue(any('copilot will not come' in s for s in said))

    def test_a_scout_without_its_marker_or_from_another_account_has_not_reviewed(self):
        t = datetime.now(UTC)
        self.assertEqual(review.status('grok', [('owner', t, 'just a comment', 'comment')], t - timedelta(1), 'owner'), 'pending')
        fake = [('someone', t, 'grok-scout\n- looks fine', 'comment')]
        self.assertEqual(review.status('grok', fake, t - timedelta(1), 'owner'), 'pending')   # anyone else's marker counts for nothing
        self.assertEqual(review.status('grok', fake, t - timedelta(1), 'someone'), 'reviewed')

    def test_a_pull_request_is_reviewed_again_only_after_a_new_commit(self):
        from tutti import spendrest
        reqs = [{'scout': 'grok', 'kind': 'review', 'repo': 'o/a', 'issue': 3, 'status': 'answered', 'head': 'abc'}]
        self.assertTrue(spendrest.reviewed(reqs, 'grok', 'o/a', 3, 'abc'))       # its own comment, CI, labels: no repeat
        self.assertFalse(spendrest.reviewed(reqs, 'grok', 'o/a', 3, 'def'))      # a new commit: reviewed again


class Parallel(unittest.TestCase):
    CFG = {'autopilot': {}, 'machines': {'nuc': {'transport': 'ssh'}, 'laptop': {'transport': 'local'}}}

    def test_the_limit_rises_with_free_capacity_and_drops_under_memory_pressure(self):
        roomy = parallel.limit(self.CFG, 2, 40, {'mem_gb': 12.0, 'cores': 16, 'load': 2.0}, 5)
        tight = parallel.limit(self.CFG, 2, 40, {'mem_gb': 4.5, 'cores': 16, 'load': 2.0}, 5)
        self.assertEqual(roomy['limit'], 12)                       # the ceiling: memory would hold 13 more
        self.assertGreater(roomy['limit'], tight['limit'])
        self.assertEqual((tight['limit'], tight['by']), (2, 'memory'))   # 0.5 GB above the 4 GB kept: no new run
        self.assertIn('running 2 of 2 possible: limited by memory', parallel.line(tight))

    def test_unknown_free_memory_keeps_it_low_unless_the_owner_caps_it(self):
        blind = parallel.limit(self.CFG, 0, 40, {'mem_gb': None, 'cores': 16, 'load': 0.0}, 9)
        self.assertEqual((blind['limit'], blind['by']), (2, 'memory'))
        capped = parallel.limit({**self.CFG, 'autopilot': {'max_parallel': 5}}, 0, 40, {'mem_gb': None, 'cores': 16, 'load': 0.0}, 9)
        self.assertEqual(capped['limit'], 5)

    def test_the_accounts_room_and_the_owner_cap_bound_it(self):
        small = parallel.limit(self.CFG, 1, 2, {'mem_gb': 30.0, 'cores': 16, 'load': 0.0}, 9)
        self.assertEqual((small['limit'], small['by'], small['others']), (3, 'accounts', 12))
        capped = parallel.limit({**self.CFG, 'autopilot': {'max_parallel': 4}}, 1, 40, {'mem_gb': 30.0, 'cores': 16, 'load': 0.0}, 9)
        self.assertEqual((capped['limit'], capped['by']), (4, 'cap'))

    def test_local_runs_are_shown_separately_when_accounts_are_spent(self):
        p = {'running': 0, 'limit': 0, 'by': 'accounts', 'why': 'room in the accounts for 0 more',
             'local_running': 3, 'local_limit': 3}
        self.assertEqual(parallel.line(p), 'online 0 of 0 (accounts spent); local 3 of 3 (one per model server)')

    def test_heavy_gates_go_to_the_nuc_and_light_ones_stay_while_memory_lasts(self):
        here = {'mem_gb': 10.0}
        self.assertEqual(parallel.check_where(self.CFG, 'local', 'npx eslint . && npx tsc', here), 'route')
        self.assertEqual(parallel.check_where(self.CFG, 'local', 'bash scripts/check.sh', here), 'local')
        self.assertEqual(parallel.check_where(self.CFG, 'local', 'bash scripts/check.sh', {'mem_gb': 2.0}), 'route')
        alone = {'autopilot': {}, 'machines': {'laptop': {'transport': 'local'}}}
        self.assertEqual(parallel.check_where(alone, 'local', 'npx eslint .', here), 'local')   # nowhere to send it
        self.assertIn('autopilot.parallel.run_gb', parallel.errors({'run_gb': 0})[0])

    def test_the_host_reading_names_free_memory(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write('MemTotal: 16000000 kB\nMemAvailable: 4194304 kB\n')
        try:
            self.assertEqual(parallel.host(f.name)['mem_gb'], 4.0)
        finally:
            os.unlink(f.name)


def simulate(window, step, a, start, hours_after=0):
    """Readings every `step` through one window, spending what the pace allows; returns (used at the spend-by time, the
    state after the reset). The account's anchor is its first reset."""
    a = {'window': window, 'resets': start.isoformat(), 'task_pct': 1, **a}
    entries, used, t = [], 0.0, start + step
    reset = pacing.window_at(start, window, start + step)[1]
    by = reset - timedelta(hours=a.get('lead_hours', 1))
    while t <= by:
        entries.append({'at': t.isoformat(), 'account': 'x', 'used': used})
        s = pacing.account_state('x', a, entries, t)
        used = min(100.0, used + (s['room_tasks'] or 0) * a['task_pct'])
        t += step
    after = pacing.account_state('x', a, entries, reset + timedelta(hours=hours_after))
    return used, after


class Targets(unittest.TestCase):
    def test_every_window_reaches_its_target_by_the_spend_by_time(self):
        for window, step in (('5h', timedelta(minutes=10)), ('7d', timedelta(hours=1)), ('monthly', timedelta(hours=6))):
            used, _ = simulate(window, step, {}, START)
            self.assertGreaterEqual(used, 99, window)

    def test_a_lower_target_is_respected(self):
        used, _ = simulate('7d', timedelta(hours=1), {'target_pct': 80}, START)
        self.assertTrue(79 <= used <= 80, used)

    def test_work_goes_on_right_after_the_reset(self):
        _, after = simulate('5h', timedelta(minutes=10), {}, START, hours_after=1)
        self.assertEqual((after['state'], after['used'], after['verdict']), ('ok', 0.0, 'behind'))
        self.assertTrue(after['after_reset'])
        self.assertGreater(after['room_tasks'], 0)

    def test_the_main_agents_target_comes_with_the_owners_reserve(self):
        owner = {'accounts': ['claude'], 'target_pct': 90}
        self.assertEqual(pacing.target_of('claude', {}, owner), 90)
        self.assertEqual(pacing.target_of('claude', {'target_pct': 70}, owner), 70)    # the account's own wins
        self.assertEqual(pacing.target_of('agy', {}, owner), 100)
        now = START + timedelta(days=3)
        s = {'share': 1.0, 'used': 30.0, 'target': (START + timedelta(days=7)).isoformat(), 'reset': None, 'room_tasks': 50}
        p = planner.plan_single(s, {'target_pct': 90, 'reserve': 10}, now)
        self.assertEqual(p['left'], 50.0)                                              # 90 - 10 kept - 30 used

    def test_the_card_says_where_it_ends_and_why_it_falls_short(self):
        now = START + timedelta(days=5)
        s = {'state': 'ok', 'used': 40.0, 'target_pct': 100, 'target': (START + timedelta(days=6, hours=23)).isoformat(),
             'readings': [{'at': (now - timedelta(hours=10)).isoformat(), 'used': 39.0}, {'at': now.isoformat(), 'used': 40.0}]}
        o = target.outlook(s, now, {'queued': 0, 'fill_room': True, 'parallel': 6})
        self.assertIn('Target 100 % · heading for', o['text'])
        self.assertIn('no work queued', o['text'])
        self.assertIn('fill_room adds tasks, up to 6 runs at once', o['text'])
        ok = target.outlook({**s, 'used': 97.0}, now, {})
        self.assertEqual(ok['short'], '')
        self.assertIn('on track to finish at', ok['text'])


if __name__ == '__main__':
    unittest.main()


class SecondOpinions(unittest.TestCase):
    def test_a_task_failing_twice_gets_one_question_and_its_answer_joins_the_notes(self):
        import sys
        sys.path.insert(0, os.path.dirname(__file__))
        from test_scouts import cfg_of

        from tutti import backlog, scouts, spendrest
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d)
            with backlog.locked(cfg) as data:
                t, _ = backlog.add(data, backlog.new_task('app', 'Fix the probe'))
                t['attempts'] = [{'ok': False, 'why': 'check failed', 'tail': 'E1'}, {'ok': False, 'why': 'check failed', 'tail': 'E2'}]
            items = spendrest.seconds(cfg, [], 'grok')
            self.assertEqual([(i['ref'], i['key']) for i in items], [('t1', 't1#2')])
            self.assertIn('E2', items[0]['task'])
            asked = [{'scout': 'grok', 'second_opinion': {'key': 't1#2'}}]
            self.assertEqual(spendrest.seconds(cfg, asked, 'grok'), [])                 # asked once per failure count
            with scouts.locked(cfg) as data:
                data['requests'] = [{'id': 's1', 'scout': 'grok', 'status': 'answered', 'answer': 'look at probe.py', 'url': 'u',
                                     'second_opinion': {'key': 't1#2', 'task': 't1'}}]
                answers = spendrest.second_answers(data)
            self.assertEqual(spendrest.feed_seconds(cfg, answers), ['s1 second opinion on t1: added to its notes'])
            self.assertIn('look at probe.py', backlog.find(backlog.load(cfg), 't1')['notes'][-1])
            self.assertTrue(scouts.load(cfg)['requests'][0]['second_opinion']['fed'])
