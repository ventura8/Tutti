"""0.1.35: the owner's reserve counts only the working days before the reset; the Do line follows the plan; each window keeps
its own badge; a window may spend over its even share while its weekly projection fits; model and effort follow the pace;
a scout's account offers no backlog tasks and spends the rest of its allowance on scout work by itself; Cursor's meters are
read from its account call with no owner click. Pure, except temp state folders and a fake opener."""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

from test_scouts import KEY, FakeGh, cfg_of

from tutti import autopilot, backlog, config, cursorusage, owner, pacing, planner, readers, scouts, spendrest, state, tiers

UTC = timezone.utc
T = pacing.parse_time
SCHED = {'days': ['mon', 'tue', 'wed', 'thu', 'fri'], 'hours': 8, 'window': '01-14', 'rate': 1.0, 'utc_offset': 0}
OWNER = {'accounts': ['claude'], 'schedule': SCHED, 'mode': 'auto', 'away_after_hours': 3}
SAT = T('2026-11-14T19:15Z')   # a Saturday


def single(now, reset, used):
    target = reset - timedelta(hours=1)
    return {'name': 'claude', 'state': 'ok', 'target': target.isoformat(), 'reset': reset.isoformat(), 'share': 1.0, 'used': used,
            'room_tasks': None, 'verdict': 'ahead', 'action': 'spending too fast: shift work to other workers and local models'}


class OwnerReserve(unittest.TestCase):
    def ctx(self, now):
        return owner.context(OWNER, [], now)

    def test_a_saturday_keeps_nothing_when_the_next_working_day_is_after_the_reset(self):
        reset = T('2026-11-16T00:30Z')   # Monday 00:30: the owner's hours (01-14) start after it
        p = planner.plan_single(single(SAT, reset, 30), {}, SAT, owner=self.ctx(SAT))
        self.assertEqual((p['owner_reserve'], p['owner_days']), (0.0, ''))

    def test_a_saturday_keeps_only_the_working_days_before_the_reset(self):
        p = planner.plan_single(single(SAT, T('2026-11-21T01:59Z'), 30), {}, SAT, owner=self.ctx(SAT))
        self.assertEqual(p['owner_reserve'], 40.0)                                   # Mon-Fri, 8 h x 1 point each
        self.assertEqual(p['owner_days'], 'Mon 16, Tue 17, Wed 18, Thu 19, Fri 20')
        mid = planner.plan_single(single(SAT, T('2026-11-18T00:30Z'), 30), {}, SAT, owner=self.ctx(SAT))
        self.assertEqual((mid['owner_reserve'], mid['owner_days']), (16.0, 'Mon 16, Tue 17'))   # Wed's hours come after the target
        row = next(r for r in planner.rows(p) if r['label'] == 'Kept for the owner')
        self.assertIn('for Mon 16, Tue 17, Wed 18, Thu 19, Fri 20', row['value'])
        self.assertIn('from pacing.schedule until learned', row['value'])

    def test_the_do_line_follows_the_plan_never_slow_down_when_it_says_spend(self):
        s = single(SAT, T('2026-11-16T00:30Z'), 30)          # ahead of the even line, but nothing kept: spend the rest
        out = planner.plan_alone(s, {'worker': 'claude'}, SAT, owner=self.ctx(SAT))
        self.assertGreater(out['quota_plan']['tasks'], 0)
        self.assertTrue(out['action'].startswith('spend: about'))
        self.assertNotIn('too fast', out['action'])
        kept = planner.plan_alone(single(SAT, T('2026-11-21T01:59Z'), 33), {'worker': 'claude'}, SAT, owner=self.ctx(SAT))
        self.assertEqual(kept['quota_plan']['tasks'], 0)
        self.assertIn('kept for the owner\'s working days', kept['action'])


NOW = T('2026-01-06T02:00Z')
WEEK = {'window': '7d', 'resets': '2026-01-08T02:00Z', 'lead_hours': 1}
FIVE = {'window': '5h', 'resets': '2026-01-06T05:00Z', 'lead_hours': 1, 'worker': 'agy', 'limits': ['agy-weekly'], 'window_points': 10}
ACCOUNTS = {'agy': FIVE, 'agy-weekly': WEEK}


def r(at, used, account='agy-weekly'):
    return {'at': at, 'account': account, 'used': used, 'note': ''}


def bound(weekly, five_used=2, accounts=ACCOUNTS):
    entries = [r(at, u) for at, u in weekly] + [r(NOW.isoformat(), five_used, 'agy')]
    return pacing.account_state('agy', accounts['agy'], entries, NOW, accounts)


class Windows(unittest.TestCase):
    def test_each_window_keeps_its_own_badge_and_the_do_line_names_the_binding_limit(self):
        s = bound([('2026-01-05T03:00Z', 70), ('2026-01-05T23:00Z', 80), ('2026-01-06T02:00Z', 84)])   # projected past 100
        self.assertEqual(s['verdict'], 'behind')                     # 2 % of the 5 h window against ~40 % by now
        self.assertEqual(s['limits'][0]['verdict'], 'ahead')
        self.assertEqual(s['quota_plan']['tasks'], 0)
        self.assertEqual(s['binding']['name'], 'agy-weekly')
        self.assertIn('is binding', s['action'])
        self.assertNotIn('spending too slowly', s['action'])
        self.assertNotIn('bounded task', s['action'])

    def test_a_window_may_spend_over_its_even_share_while_the_weekly_projection_fits(self):
        even = bound([('2026-01-05T03:00Z', 70), ('2026-01-05T23:00Z', 80), ('2026-01-06T02:00Z', 81)])['quota_plan']
        joint = bound([('2026-01-05T23:00Z', 80), ('2026-01-06T02:00Z', 81)])['quota_plan']   # 1 point in 3 h: ends near 97
        self.assertEqual(even['allowance'], 2.0)                                 # 20 points over 10 windows
        self.assertGreater(joint['allowance'], 2.0)
        self.assertLessEqual(joint['projected'] + (joint['allowance'] - joint['this_window']), 100.5)
        self.assertGreater(joint['tasks'], even['tasks'])

    def test_the_do_line_and_the_decision_never_disagree(self):
        for weekly in ([('2026-01-05T23:00Z', 80), ('2026-01-06T02:00Z', 81)],
                       [('2026-01-05T03:00Z', 70), ('2026-01-05T23:00Z', 80), ('2026-01-06T02:00Z', 84)]):
            s = bound(weekly)
            decision = next(x['value'] for x in s['quota_plan']['rows'] if x['label'] == 'Decision')
            if s['quota_plan']['tasks']:
                self.assertTrue(s['action'].startswith('spend: about'))
                self.assertIn('task(s) now', decision)
            else:
                self.assertEqual(decision, 'nothing more this window')
                self.assertFalse(s['action'].startswith(('spend', 'spending too slowly')))


TIERED = {'accounts': {'agy-claude': {'worker': 'agy', 'args': ['--model', 'claude-sonnet-5-5-medium', '--settings', '{}'], 'tiers': {
    'strong': {'args': ['--model', 'claude-opus-5-5-high'], 'sizes': ['L', 'M']},
    'cheap': {'args': ['--model', 'claude-sonnet-5-5-low'], 'sizes': ['S']}}}}}


def plan_state(verdict, projected=None):
    return {'name': 'agy-claude', 'verdict': 'behind', 'quota_plan': {'limit': 'agy-claude-weekly', 'projected': projected,
                                                                       'limit_verdict': verdict}}


class Tiers(unittest.TestCase):
    def test_behind_gets_the_strong_models_and_ahead_the_cheap_ones(self):
        strong = tiers.choose(TIERED, 'agy-claude', plan_state('ahead', projected=60))
        self.assertEqual(strong['tier'], 'strong')
        self.assertEqual(strong['args'], ['--settings', '{}', '--model', 'claude-opus-5-5-high'])
        self.assertEqual((strong['model'], strong['effort']), ('claude-opus-5-5-high', 'high'))
        self.assertIn('agy-claude behind: agy-claude-weekly projected to end at 60 %', strong['label'])
        cheap = tiers.choose(TIERED, 'agy-claude', plan_state('behind', projected=130))
        self.assertEqual((cheap['tier'], cheap['model'], cheap['sizes']), ('cheap', 'claude-sonnet-5-5-low', ['S']))
        same = tiers.choose(TIERED, 'agy-claude', plan_state('on pace', projected=95))
        self.assertEqual((same['tier'], same['args'][1]), ('default', 'claude-sonnet-5-5-medium'))

    def test_effort_flags_and_unread_states(self):
        cfg = {'accounts': {'claude': {'worker': 'claude', 'tiers': {'strong': {'args': ['--model', 'opus', '--effort', 'high']}}}}}
        c = tiers.choose(cfg, 'claude', {'name': 'claude', 'verdict': 'late'})
        self.assertEqual((c['model'], c['effort']), ('opus', 'high'))
        self.assertEqual(tiers.choose(cfg, 'claude', None)['tier'], 'default')
        self.assertIn('no accounts.claude.tiers.cheap', tiers.choose(cfg, 'claude', {'name': 'claude', 'verdict': 'ahead'})['reason'])

    def test_bad_tiers_name_their_keys(self):
        text = '\n'.join(tiers.errors({'accounts': {'a': {'tiers': {'fast': {}}}, 'b': {'tiers': {'cheap': {'sizes': ['XL']}}}}}))
        self.assertIn('accounts.a.tiers', text)
        self.assertIn('accounts.b.tiers.cheap.sizes', text)

    def test_a_strong_account_takes_the_large_task_first(self):
        out = {'dispatch': [], 'waiting': {}}
        cfg = {'autopilot': {'accounts': ['weak', 'strong'], 'ladder': ['weak', 'strong']}}
        tasks = [{'id': 't1', 'size': 'S', 'attempts': []}, {'id': 't2', 'size': 'L', 'attempts': []}]
        orig = autopilot.candidates
        autopilot.candidates = lambda c, t, *a: ['strong', 'weak']
        try:
            autopilot.assign(cfg, tasks, {'strong': (3, ''), 'weak': (3, '')}, 5, out, {'strong': ['L', 'M']})
        finally:
            autopilot.candidates = orig
        self.assertEqual(out['dispatch'], [('t1', 'weak'), ('t2', 'strong')])


def grok_acc(used=13.0, now=None, verdict='behind'):
    now = now or datetime(2026, 1, 10, 20, tzinfo=UTC)
    return {'name': 'grok-bot', 'state': 'ok', 'verdict': verdict, 'used': used, 'panic': False, 'room_tasks': 42,
            'target': '2026-01-10T23:00:00+00:00', 'reset': '2026-01-11T00:00:00+00:00', 'window_start': '2026-01-04T00:00:00+00:00'}


class SpendTheRest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 1, 10, 20, tzinfo=UTC).timestamp()
        self.watches = {'dependency-notes': {'every': '7d', 'task': 'notes'}, 'live-site': {'every': '1d', 'task': 'look',
                                                                                             'project': 'Map'}}
        self.cfg = cfg_of(self.tmp.name, watches=self.watches, max_open=10, per_tick=10)
        self.cfg['projects'] = {'Map': {'priority': 2}, 'Tutti': {'priority': 0, 'self_improve': {'autopilot_repo': 'tutti'}}}
        self.sc = scouts.settings(self.cfg, 'grok')
        self.requests = [{'scout': 'grok', 'watch': w, 'status': 'answered', 'added': self.now - 5 * 3600, 'sent': self.now - 5 * 3600}
                         for w in self.watches]

    def tearDown(self):
        self.tmp.cleanup()

    def test_it_runs_under_its_weekly_line_all_week_and_under_its_stop(self):
        self.assertTrue(spendrest.active(self.sc, grok_acc(), self.now)[0])                      # the burst before the reset
        self.assertTrue(spendrest.active(self.sc, grok_acc(), self.now - 2 * 86400)[0])           # days before: under its line
        self.assertIn('even line', spendrest.active(self.sc, grok_acc(used=80), self.now - 2 * 86400)[1])   # ahead: waits
        self.assertIn('stops at 95', spendrest.active(self.sc, grok_acc(used=96), self.now)[1])
        off = {**self.sc, 'spend_rest': False}
        self.assertIn('off', spendrest.active(off, grok_acc(), self.now)[1])
        self.assertEqual(spendrest.points_left({**self.sc, '_task_pct': 2}, grok_acc(used=90)), 2)

    def test_extra_watches_and_questions_go_in_project_priority_order(self):
        with backlog.locked(self.cfg) as data:
            backlog.add(data, backlog.new_task('tutti', 'scout: which CLIs changed their usage pages this week?'))
            backlog.add(data, backlog.new_task('tutti', 'fix the reader'))
        p = spendrest.plan(self.cfg, self.sc, self.requests, grok_acc(), self.now)
        self.assertEqual([i['ref'] for i in p['items']], ['t1', 'live-site', 'dependency-notes'])   # Tutti 0, Map 2, unranked
        self.assertTrue(spendrest.line(p).startswith('Scout: burst before the reset: 41 run(s)'))   # (95 - 13) / 2 points
        self.assertEqual([t['id'] for t in backlog.queued(backlog.load(self.cfg))], ['t2'])   # a question is no worker's task
        recent = [{**x, 'sent': self.now - 3600} for x in self.requests]
        self.assertEqual([i['ref'] for i in spendrest.plan(self.cfg, self.sc, recent, grok_acc(), self.now)['items']], ['t1'])   # min gap

    def test_a_round_sends_within_the_caps_and_marks_the_question(self):
        with backlog.locked(self.cfg) as data:
            backlog.add(data, backlog.new_task('tutti', 'scout: what changed?'))
        gh, posts = FakeGh(), []

        def post(url, key, body):
            posts.append(body)
            return 200
        with scouts.locked(self.cfg) as data:
            data['requests'] = list(self.requests)
            sc = {**self.sc, 'spend_rest': {'burst_per_tick': 2}}
            events, n = spendrest.round_(self.cfg, sc, data, grok_acc(), 'http://x', KEY, 0, gh, post, self.now)
        self.assertEqual(n, 2)                                            # the burst's per_tick holds
        self.assertTrue(all('scout week' in e for e in events))
        self.assertEqual(len(posts), 2)
        with scouts.locked(self.cfg) as data:
            events, n = spendrest.round_(self.cfg, self.sc, data, grok_acc(), 'http://x', KEY, 0, gh, post, self.now)
        self.assertEqual(n, 1)                                            # each watch runs once more, the question once
        self.assertEqual(backlog.load(self.cfg)['tasks'][0]['status'], 'review')

    def test_bad_spend_rest_config_names_its_keys(self):
        self.assertEqual(spendrest.errors('k', True), [])
        self.assertIn('k.stop_at', '\n'.join(spendrest.errors('k', {'stop_at': 0, 'x': 1})))
        self.assertIn('true, false', spendrest.errors('k', 'yes')[0])

    def test_the_scout_account_card_offers_no_backlog_tasks(self):
        now = datetime(2026, 1, 3, 20, tzinfo=UTC)
        pacing.append_log(state.usage_log_path(self.cfg), 'grok-bot', 13, now - timedelta(hours=1))
        a = next(x for x in state.usage_state(self.cfg, now)['accounts'] if x['name'] == 'grok-bot')
        self.assertIsNone(a['room_tasks'])
        self.assertNotIn('bounded task', a['action'])
        self.assertNotIn('backlog', a['action'].replace('no backlog tasks', ''))
        self.assertTrue(a['action'].startswith('Scout: '))
        decision = {x['label']: x.get('value') for x in a['quota_plan']['rows']}
        self.assertNotIn('task(s) now', decision['Decision'])
        self.assertEqual(a['quota_plan']['tasks'], 0)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


ANSWER = {'billingCycleEnd': '1796903991000', 'planUsage': {'autoPercentUsed': 27.49, 'apiPercentUsed': 13.77, 'totalPercentUsed': 26.2},
          'autoBucketModels': ['x']}


class CursorReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth = os.path.join(self.tmp.name, 'auth.json')
        with open(self.auth, 'w', encoding='utf8') as f:
            json.dump({'accessToken': 'test-token-not-real'}, f)

    def tearDown(self):
        self.tmp.cleanup()

    def test_both_meters_and_the_reset_come_from_the_account_call(self):
        seen = []

        def opener(req, timeout):
            seen.append(req)
            return FakeResponse(json.dumps(ANSWER).encode())
        data, problem = cursorusage.fetch(self.auth, opener)
        self.assertIsNone(problem)
        self.assertEqual(seen[0].full_url, cursorusage.URL)
        self.assertEqual(cursorusage.parse(data, 'autoPercentUsed'), (27.5, datetime.fromtimestamp(1796903991, UTC)))
        self.assertEqual(cursorusage.parse(data, 'apiPercentUsed')[0], 13.8)
        self.assertNotIn('autoBucketModels', data)
        with self.assertRaises(ValueError):
            cursorusage.parse(data, 'nope')

    def test_problems_say_what_to_do_and_never_the_token(self):
        self.assertIn('cursor-agent login', cursorusage.fetch(os.path.join(self.tmp.name, 'none.json'))[1])

        def denied(req, timeout):
            raise urllib.error.HTTPError(cursorusage.URL, 401, 'no', {}, None)
        _, why = cursorusage.fetch(self.auth, denied)
        self.assertIn('cursor-agent login', why)
        self.assertNotIn('test-token-not-real', why)
        _, why = cursorusage.fetch(self.auth, lambda req, timeout: FakeResponse(b'{}'))
        self.assertIn('reading helper', why)

    def test_one_call_serves_both_accounts_and_the_config_checks_the_field(self):
        calls = []
        cfg = {'accounts': {'cursor': {'window': 'monthly', 'reader': {'format': 'cursor-api', 'field': 'autoPercentUsed'}},
                            'cursor-other': {'window': 'monthly', 'reader': {'format': 'cursor-api', 'field': 'apiPercentUsed'}}},
               'dashboard': {'probe_seconds': 60}}
        rd = readers.Readers(cfg, clock=lambda: 0, cursor=lambda: calls.append(1) or (ANSWER, None))
        got = rd.read_all(datetime(2026, 11, 14, tzinfo=UTC))
        self.assertEqual((got['cursor']['used'], got['cursor-other']['used'], len(calls)), (27.5, 13.8, 1))
        bad = config.normalise({'accounts': {'c': {'window': 'monthly', 'resets': '2026-12-10T00:00Z',
                                                   'reader': {'format': 'cursor-api', 'field': 'x'}}}})
        self.assertEqual(config.validate(bad), [])
        self.assertIn('accounts.c.reader.field', '\n'.join(bad['_warnings']))
        self.assertNotIn('reader', bad['accounts']['c'])
        self.assertFalse(pacing.staleness('cursor', cfg['accounts']['cursor'], None, datetime(2026, 11, 14, tzinfo=UTC))[0])


if __name__ == '__main__':
    unittest.main()


class Lenient(unittest.TestCase):
    """A config written for a newer Tutti never stops this one: an unknown reader format or account key is a warning and the
    reader is skipped (the owner's dashboard was down on 2026-11-14 over `format: cursor-api` before the code shipped)."""

    def test_an_unknown_reader_format_and_key_are_warnings_and_the_dashboard_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'tutti.config.yaml')
            with open(path, 'w', encoding='utf8') as f:
                f.write(f'project: {{state: {os.path.join(tmp, "state")}}}\naccounts:\n  cursor:\n    window: monthly\n'
                        '    resets: 2026-12-10T00:00Z\n    reader: {format: future-api, field: x}\n    shiny_new_key: 1\n')
            err = io.StringIO()
            orig, sys.stderr = sys.stderr, err
            try:
                cfg = config.load(path)
            finally:
                sys.stderr = orig
        self.assertNotIn('reader', cfg['accounts']['cursor'])
        self.assertEqual(cfg['accounts']['cursor']['window'], 'monthly')
        text = err.getvalue()
        self.assertIn("accounts.cursor.reader.format is 'future-api'", text)
        self.assertIn('this reader is skipped', text)
        self.assertIn('accounts.cursor.shiny_new_key is not a key', text)
        rd = readers.Readers(cfg, run=lambda c: self.fail('no reader may run'))
        self.assertEqual(rd.read_all(datetime(2026, 11, 14, tzinfo=UTC)), {})
