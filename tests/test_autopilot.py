"""`tutti backlog` and `tutti autopilot`: the queue and its sources, the pure plan (pace, room, heat, max_parallel, the
escalation ladder), failure signatures and parking, the retry brief, and whole rounds against a throwaway repository with a
fake worker. Nothing leaves the temp folder; no real machine or account is used."""
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tutti import autopilot, backlog, cli, config, dashboard, delegate, fleet, routing, state

FAKE_WORKER = '''import sys
prompt = sys.argv[1]
with open('a.txt', 'a') as f:
    f.write('fixed\\n' if 'attempt 2' in prompt else 'half\\n')
print('worker done')
'''


def git(cwd, *args):
    return subprocess.run(['git', '-C', cwd, *args], check=True, capture_output=True, text=True).stdout.strip()


def account(name, verdict='behind', room=5, used=10.0, panic=False):
    return {'name': name, 'verdict': verdict, 'room_tasks': room, 'used': used, 'panic': panic, 'panic_at': 95}


def task(tid, status='queued', attempts=(), size='S', repo='app'):
    return {'id': tid, 'repo': repo, 'title': f'task {tid}', 'scope': [], 'check': 'true', 'size': size, 'source': 'test',
            'status': status, 'attempts': list(attempts), 'notes': []}


def attempt(step, sig='s1', ok=False):
    return {'step': step, 'ok': ok, 'signature': sig, 'why': f'failed {sig}', 'run': '1-1', 'stat': '', 'tail': 'E1'}


FREE = [{'name': 'nuc', 'status': 'free', 'free': 2, 'reason': 'fit'}]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, 'state')

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **ap):
        raw = {'project': {'state': self.state, 'worktrees': os.path.join(self.tmp.name, 'wt')},
               'machines': {'nuc': {'host': 'nuc.example'}},
               'workers': {'agy': {'kind': 'cli', 'command': ['agy', '{prompt}']}, 'cursor': {'kind': 'cli', 'command': ['c', '{prompt}']},
                           'paid': {'kind': 'cli', 'command': ['p', '{prompt}'], 'costs_money': True},
                           'pc': {'kind': 'ollama', 'host': 'pc.example:11434', 'model': 'm'}},
               'accounts': {'agy-gemini': {'worker': 'agy', 'args': ['--model', 'g']},
                            'agy-claude': {'worker': 'agy', 'args': ['--model', 'c']},
                            'cursor': {'worker': 'cursor'}},
               'autopilot': {'enabled': True, 'accounts': ['agy-gemini', 'agy-claude'],
                             'ladder': ['agy-gemini', 'agy-claude', 'cursor', 'pc'],
                             'max_parallel': 2, 'repos': {'app': {'path': os.path.join(self.tmp.name, 'repo'), 'check': 'true'}},
                             'fill_room': False, 'auto_review': False, **ap}}
        cfg = config.normalise(raw)
        cfg['_path'] = os.path.join(self.tmp.name, 'tutti.config.yaml')
        # hermetic: the adaptive limit (tutti/parallel.py) reads this computer's free memory and load when `_host` is unset,
        # so a busy laptop (the whole suite running) left no online slot and the local model took the task
        cfg['_host'] = cfg['_host_raw'] = {'mem_gb': 64.0, 'cores': 16, 'load': 0.0}
        cfg['_disk'] = []   # nor this computer's disks (tutti/diskroom.py)
        return cfg


class BacklogTest(Base):
    def test_add_list_and_a_duplicate_title_is_not_added_twice(self):
        cfg = self.cfg()
        with backlog.locked(cfg) as data:
            t, new = backlog.add(data, backlog.new_task('app', 'Fix the README link', ['README.md'], 'true'))
            self.assertTrue(new)
            self.assertEqual(t['id'], 't1')
            _, again = backlog.add(data, backlog.new_task('app', '  fix the readme   link '))
            self.assertFalse(again)
        self.assertIn('Fix the README link', backlog.listing(backlog.load(cfg)))

    def test_bad_size_and_empty_title_are_refused_with_the_fix(self):
        with self.assertRaisesRegex(backlog.BacklogError, 'use one of S, M, L'):
            backlog.new_task('app', 'x', size='XL')
        with self.assertRaisesRegex(backlog.BacklogError, 'title'):
            backlog.new_task('app', '  ')
        with self.assertRaisesRegex(backlog.BacklogError, 'tutti backlog list --all'):
            backlog.find({'tasks': []}, 't9')

    def test_feedback_open_rows_and_dated_notes_are_items_fixed_rows_are_not(self):
        text = ('| # | Date | Command | What happened | Expected | Status |\n| --- | --- | --- | --- | --- | --- |\n'
                '| 3 | 2026-11-14 | `t` | Only the probe ran. | A full run. | Open (roadmap). |\n'
                '| 4 | 2026-11-14 | `t` | Broke. | Works. | Fixed in 0.1.8. |\n\n- 2026-11-14 (x): doctor should detect a broken git.\n'
                '- not a dated note\n- 2026-11-14 (y): a closed one. Fixed in 0.1.9.\n')
        items = backlog.feedback_items(text)
        self.assertEqual(len(items), 2)
        self.assertIn('FEEDBACK #3: Only the probe ran.', items[0])
        self.assertIn('doctor should detect', items[1])

    def test_todo_with_an_owner_tag_and_gap_lines(self):
        todos = backlog.todo_items(['src/a.py:12:    # TODO(owner): split this function', 'src/b.py:3: TODO without owner'])
        self.assertEqual(todos, [('TODO(owner) in src/a.py:12: split this function', 'src/a.py')])
        gaps = backlog.gap_items(['src/c.ts:4: coverage 80 % < 90 %', '', 'a plain gap', 'src/d.py:3:7: E501 too long', 'note: x'])
        self.assertEqual(gaps, [('src/c.ts: coverage 80 % < 90 %', 'src/c.ts'), ('a plain gap', None),
                                ('src/d.py: E501 too long', 'src/d.py'), ('note: x', None)])

    def test_seed_reads_the_repo_sources_once(self):
        repo = os.path.join(self.tmp.name, 'repo')
        os.makedirs(os.path.join(repo, 'docs'))
        git(repo, 'init', '-q')
        with open(os.path.join(repo, 'docs', 'FEEDBACK.md'), 'w', encoding='utf8') as f:
            f.write('- 2026-11-14 (x): the help text is wrong.\n')
        with open(os.path.join(repo, 'a.py'), 'w', encoding='utf8') as f:
            f.write('x = 1  # FIXME(dev): name it\n')
        git(repo, 'add', '-A')
        cfg = self.cfg()
        cfg['autopilot']['repos']['app']['gaps'] = 'echo "a.py: no docstring"'
        with backlog.locked(cfg) as data:
            added = backlog.seed(data, 'app', cfg['autopilot']['repos']['app'])
            self.assertEqual(len(added), 3)
            self.assertEqual({t['source'] for t in added}, {'feedback', 'todo', 'gaps'})
            self.assertEqual(backlog.seed(data, 'app', cfg['autopilot']['repos']['app']), [])


class PlanTest(Base):
    def states(self, **over):
        s = {n: account(n) for n in ('agy-gemini', 'agy-claude')}
        s['cursor'] = account('cursor', 'on pace', None)
        return {**s, **over}

    def test_off_and_an_empty_backlog_say_so(self):
        cfg = self.cfg()
        self.assertEqual(autopilot.plan(cfg, [task('t1')], self.states(), FREE, False)['state'], 'off')
        p = autopilot.plan(cfg, [], self.states(), FREE, True)
        self.assertEqual(p['reason'], autopilot.EMPTY)

    def test_behind_accounts_get_tasks_up_to_max_parallel(self):
        p = autopilot.plan(self.cfg(), [task('t1'), task('t2'), task('t3')], self.states(), FREE, True)
        self.assertEqual(p['dispatch'], [('t1', 'agy-gemini'), ('t2', 'agy-gemini')])
        self.assertEqual(p['state'], 'dispatching')

    def test_when_all_online_are_in_panic_rescue_spreads_work_to_the_most_remaining_quota(self):
        cfg = self.cfg(main_accounts=['agy-claude'])
        cfg['_main_worker'] = 'local-main'
        cfg['workers']['local-main'] = {'kind': 'ollama', 'host': '127.0.0.1:1', 'model': 'm:3b'}
        states = {'agy-claude': account('agy-claude', 'ahead', used=99, panic=True),
                  'agy-gemini': account('agy-gemini', 'ahead', used=97, panic=True),
                  'cursor': account('cursor', 'ahead', used=95, panic=True)}
        states = {n: {**s, 'reading_at': '2026-01-01T00:00:00Z'} for n, s in states.items()}
        p = autopilot.plan(cfg, [task('t1'), task('t2')], states, FREE, True)
        self.assertEqual(p['dispatch'], [('t1', 'cursor'), ('t2', 'agy-gemini')])

    def test_panic_rescue_reserves_shared_window_for_each_dispatch(self):
        cfg = self.cfg(main_accounts=['agy-claude'])
        cfg['_main_worker'] = 'local-main'
        cfg['workers']['local-main'] = {'kind': 'ollama', 'host': '127.0.0.1:1', 'model': 'm:3b'}
        weekly = {'name': 'weekly', 'used': 97, 'panic': True, 'reading_at': '2026-01-01T00:00:00Z'}
        states = {n: {**account(n, 'ahead', used=96, panic=True), 'reading_at': '2026-01-01T00:00:00Z',
                      'limits': [weekly]} for n in ('agy-claude', 'agy-gemini', 'cursor')}
        p = autopilot.plan(cfg, [task('t1'), task('t2')], states, FREE, True)
        self.assertEqual(len(p['dispatch']), 1)   # 3 points left, two tasks would cost 4
        self.assertIn('shared quota', p['waiting']['t2'])

    def test_panic_rescue_fails_closed_when_a_configured_linked_window_is_missing(self):
        cfg = self.cfg(main_accounts=['cursor'])
        cfg['_main_worker'] = 'local-main'
        cfg['workers']['local-main'] = {'kind': 'ollama', 'host': '127.0.0.1:1', 'model': 'm:3b'}
        cfg['_account_limits']['cursor'] = ['weekly']
        cfg['_account_limits']['weekly'] = ['monthly']
        states = {n: {**account(n, 'ahead', used=96, panic=True), 'reading_at': '2026-01-01T00:00:00Z'}
                  for n in ('agy-claude', 'agy-gemini', 'cursor')}
        self.assertFalse(autopilot.panic_rescue(cfg, 'cursor', states['cursor'], states))
        weekly = {'name': 'weekly', 'used': 96, 'panic': True, 'reading_at': '2026-01-01T00:00:00Z'}
        states['cursor']['limits'] = [weekly]
        self.assertFalse(autopilot.panic_rescue(cfg, 'cursor', states['cursor'], states))
        weekly['limits'] = [{'name': 'monthly', 'used': 96, 'panic': True, 'reading_at': '2026-01-01T00:00:00Z'}]
        self.assertTrue(autopilot.panic_rescue(cfg, 'cursor', states['cursor'], states))

    def test_missing_linked_window_blocks_normal_and_direct_auto_dispatch(self):
        cfg = self.cfg(ladder=['agy-gemini'], accounts=['agy-gemini'])
        cfg['accounts'] = {'agy-gemini': cfg['accounts']['agy-gemini']}
        cfg['_account_limits']['agy-gemini'] = ['weekly']
        states = {'agy-gemini': account('agy-gemini')}
        self.assertEqual(autopilot.plan(cfg, [task('t1')], states, FREE, True)['dispatch'], [])
        picked = state.next_worker(cfg, list(states.values()))
        self.assertIsNone(picked['worker'])
        self.assertIn('weekly', picked['reason'])
        states['agy-gemini']['limits'] = [account('weekly')]
        self.assertEqual(autopilot.plan(cfg, [task('t1')], states, FREE, True)['dispatch'], [('t1', 'agy-gemini')])
        self.assertEqual(state.next_worker(cfg, list(states.values()))['worker'], 'agy')

    def test_direct_takeover_never_overrides_linked_window_panic(self):
        s = {**account('cursor', used=None), 'limits': [account('weekly', used=99, panic=True)]}
        picked, why = routing.pick_account([s], takeover=True)
        self.assertIsNone(picked)
        self.assertIn('weekly', why)

    def test_attempt_remains_debited_until_linked_window_is_refreshed(self):
        states = {'cursor': {'reading_at': '2026-01-03T00:00:00Z',
                             'limits': [{'name': 'weekly', 'reading_at': '2026-01-01T00:00:00Z'}]}}
        t = task('t1', 'done', [{'step': 'cursor', 'started': 1767312000}])  # 2026-01-02 UTC
        self.assertEqual(autopilot.spent_units([t], states), {'cursor': 1})
        states['cursor']['limits'][0]['reading_at'] = '2026-01-03T00:00:00Z'
        self.assertEqual(autopilot.spent_units([t], states), {})

    def test_paused_accounts_unreported_attempt_still_debits_a_shared_window(self):
        cfg = self.cfg(main_accounts=['agy-claude'])
        cfg['_main_worker'] = 'local-main'
        cfg['workers']['local-main'] = {'kind': 'ollama', 'host': '127.0.0.1:1', 'model': 'm:3b'}
        cfg['_account_limits']['paused'] = ['weekly']
        cfg['_account_costs']['paused'] = 2
        weekly = {'name': 'weekly', 'used': 97, 'panic': True, 'reading_at': '2026-01-01T00:00:00Z'}
        states = {n: {**account(n, 'ahead', used=96, panic=True), 'reading_at': '2026-01-01T00:00:00Z',
                      'limits': [weekly]} for n in ('agy-claude', 'agy-gemini', 'cursor')}
        done = task('old', 'done', [{'step': 'paused', 'started': 1767312000}])
        self.assertEqual(autopilot.plan(cfg, [done, task('t1')], states, FREE, True)['dispatch'], [])
        weekly['reading_at'] = '2026-01-03T00:00:00Z'
        self.assertEqual(len(autopilot.plan(cfg, [done, task('t1')], states, FREE, True)['dispatch']), 1)

    def test_an_account_at_its_line_or_in_panic_takes_nothing(self):
        states = self.states(**{'agy-gemini': account('agy-gemini', 'on pace'), 'agy-claude': account('agy-claude', panic=True, used=96)})
        cfg = self.cfg(ladder=['agy-gemini', 'agy-claude'])
        p = autopilot.plan(cfg, [task('t1')], states, FREE, True)
        self.assertEqual(p['dispatch'], [])
        self.assertEqual(p['state'], 'stopped')
        self.assertIn('even-pace line', p['reason'])
        self.assertIn('panic', p['reason'])

    def test_no_free_cool_slot_waits_and_names_the_heat(self):
        hot = [{'name': 'nuc', 'status': 'loaded', 'free': 1, 'reason': 'hot: 99 C'}]
        p = autopilot.plan(self.cfg(), [task('t1')], self.states(), hot, True)
        self.assertEqual(p['state'], 'waiting')
        self.assertIn('hot: 99 C', p['reason'])

    def test_running_tasks_count_against_max_parallel_and_room(self):
        running = [task('t1', 'running'), task('t2', 'running')]
        p = autopilot.plan(self.cfg(), [*running, task('t3')], self.states(), FREE, True)
        self.assertEqual(p['state'], 'running')
        self.assertEqual(p['dispatch'], [])

    def test_a_retry_climbs_the_ladder_and_skips_a_step_ahead_of_its_pace(self):
        states = self.states(cursor=account('cursor', 'ahead', None))
        tries = [attempt('agy-gemini'), attempt('agy-claude')]
        scoped = {**task('t1', attempts=tries), 'scope': ['src/a.py']}
        p = autopilot.plan(self.cfg(), [scoped], states, FREE, True)
        self.assertEqual(p['dispatch'], [('t1', 'pc')])   # cursor is ahead: the local model, the strongest step, takes it
        unscoped = autopilot.plan(self.cfg(), [task('t3', attempts=tries)], states, FREE, True)
        self.assertEqual(unscoped['dispatch'], [('t3', 'agy-claude')])   # pc needs files, cursor is ahead: back down to agy
        one = autopilot.plan(self.cfg(), [task('t2', attempts=tries[:1])], states, FREE, True)
        self.assertEqual(one['dispatch'], [('t2', 'agy-claude')])

    def test_a_local_model_on_a_yielding_machine_takes_nothing(self):
        cfg = self.cfg()
        cfg['workers']['pc']['machine'] = 'nuc'
        paused = [{'name': 'nuc', 'status': 'paused', 'free': 2, 'reason': 'yield flag is up'}]
        room, why = autopilot.step_room(cfg, 'pc', {}, {}, paused)
        self.assertEqual(room, 0)
        self.assertIn('yield flag is up', why)
        self.assertEqual(autopilot.step_room(cfg, 'pc', {}, {}, FREE)[0], fleet.UNITS_ANY)   # no quota: fits() decides the size

    def test_a_result_passes_only_with_the_repos_gates_too(self):
        repo = {'check': 'ruff check . && mypy .'}
        both = '( pytest tests/test_new.py ) && ( ruff check . && mypy . )'
        self.assertEqual(autopilot.acceptance({'check': 'pytest tests/test_new.py'}, repo), both)
        self.assertEqual(autopilot.acceptance({'check': ''}, repo), 'ruff check . && mypy .')
        self.assertEqual(autopilot.acceptance({'check': 'make t'}, {}), 'make t')
        self.assertEqual(autopilot.acceptance({'check': 'ruff check . && mypy .'}, repo), 'ruff check . && mypy .')   # never twice
        self.assertEqual(autopilot.acceptance({}, {}), '')
        self.assertIn('( ruff check . && mypy . )', autopilot.brief({**task('t1'), 'check': 'pytest x'}, repo))

    def test_a_window_under_a_weekly_limit_spends_its_planned_share(self):
        cfg = self.cfg()
        plan = {'limit': 'agy-claude-weekly', 'tasks': 3, 'left': 6, 'allowance': 8, 'this_window': 2,
                'why': 'agy-claude: spend by 16:00 UTC'}
        s = {**account('agy-claude', 'on pace', None), 'quota_plan': plan}
        room = autopilot.step_room(cfg, 'agy-claude', {'agy-claude': s}, {'agy-claude': 1}, FREE)
        self.assertEqual(room, (2, ''))
        none = {**s, 'quota_plan': {**plan, 'tasks': 0}}
        room, why = autopilot.step_room(cfg, 'agy-claude', {'agy-claude': none}, {}, FREE)
        self.assertEqual(room, 0)
        self.assertIn('spend by 16:00', why)                # the planner's reason, said as such

    def test_a_fresh_task_goes_only_to_a_trigger_account_behind_its_line(self):
        states = self.states(**{'agy-gemini': account('agy-gemini', 'on pace'), 'agy-claude': account('agy-claude', 'on pace')})
        p = autopilot.plan(self.cfg(), [task('t1')], states, FREE, True)
        self.assertEqual(p['dispatch'], [])               # cursor (on pace) and pc are only for retries
        self.assertEqual(p['state'], 'stopped')

    def test_escalation_starts_above_the_last_failed_step_and_a_reviewer_retry_stays(self):
        states = self.states(cursor=account('cursor', 'on pace', None))
        p = autopilot.plan(self.cfg(), [task('t1', attempts=[attempt('agy-claude')])], states, FREE, True)
        self.assertEqual(p['dispatch'], [('t1', 'cursor')])
        again = autopilot.plan(self.cfg(), [task('t2', attempts=[attempt('agy-claude', ok=True)])], states, FREE, True)
        self.assertEqual(again['dispatch'], [('t2', 'agy-claude')])

    def test_a_task_without_a_check_is_never_sent(self):
        cfg = self.cfg()
        cfg['autopilot']['repos']['app']['check'] = ''
        p = autopilot.plan(cfg, [{**task('t1'), 'check': ''}], self.states(), FREE, True)
        self.assertEqual(p['dispatch'], [])
        self.assertIn('no acceptance check', p['waiting']['t1'])

    def test_the_cost_of_finished_attempts_stays_counted_until_a_new_reading(self):
        states = self.states(**{'agy-gemini': {**account('agy-gemini', room=1), 'reading_at': '2026-11-14T10:00:00+00:00'}})
        cfg = self.cfg(ladder=['agy-gemini'], accounts=['agy-gemini'])
        done = {**task('t0', 'review'), 'attempts': [{**attempt('agy-gemini', ok=True), 'started': 1794660000.0}]}
        self.assertEqual(autopilot.plan(cfg, [done, task('t1')], states, FREE, True)['dispatch'], [])
        older = {**done, 'attempts': [{**done['attempts'][0], 'started': 1794600000.0}]}
        self.assertEqual(autopilot.plan(cfg, [older, task('t1')], states, FREE, True)['dispatch'], [('t1', 'agy-gemini')])

    def test_a_step_that_costs_money_is_never_used(self):
        cfg = self.cfg(ladder=['paid'], accounts=['paid'])
        self.assertIsNone(autopilot.step_of(cfg, 'paid'))
        self.assertEqual(autopilot.plan(cfg, [task('t1')], self.states(), FREE, True)['dispatch'], [])

    def test_a_large_task_needs_its_room(self):
        states = self.states(**{'agy-gemini': account('agy-gemini', room=2), 'agy-claude': account('agy-claude', room=1)})
        cfg = self.cfg(ladder=['agy-gemini', 'agy-claude'])
        p = autopilot.plan(cfg, [task('t1', size='L')], states, FREE, True)
        self.assertEqual(p['dispatch'], [])
        self.assertIn('t1', p['waiting'])


class FailureTest(Base):
    def test_the_same_failure_has_the_same_signature_numbers_aside(self):
        r = {'worker_rc': 0, 'denied': [], 'changed': ['a'], 'checks': [{'run': 'pytest', 'rc': 1, 'log': 'L'}]}
        a = autopilot.signature(r, {'L': 'tests/x.py:12: assert 3 == 4\n1 failed in 0.52s'})
        b = autopilot.signature(r, {'L': 'tests/x.py:13: assert 5 == 4\n1 failed in 0.61s'})
        c = autopilot.signature(r, {'L': 'ImportError: no module'})
        self.assertEqual(a[0], b[0])
        self.assertNotEqual(a[0], c[0])
        self.assertIn('the worker changed nothing', autopilot.signature({**r, 'changed': []}, {})[1])
        self.assertIn('without a report', autopilot.signature(None, {})[1])

    def test_check_logs_are_read_only_from_the_run_folder(self):
        cfg = self.cfg()
        folder = os.path.join(delegate.runs_dir(cfg), '1-1')
        os.makedirs(folder)
        with open(os.path.join(folder, 'ship-1.log'), 'w', encoding='utf8') as f:
            f.write('E: the real failure\n')
        outside = os.path.join(self.tmp.name, 'secret.txt')
        with open(outside, 'w', encoding='utf8') as f:
            f.write('not for a brief\n')
        tails = autopilot.check_tails(cfg, '1-1', [{'log': os.path.join(folder, 'ship-1.log')}, {'log': outside}, {'log': None}])
        self.assertIn('the real failure', tails[os.path.join(folder, 'ship-1.log')])
        self.assertEqual(tails[outside], '')
        self.assertIsNone(autopilot.run_log(cfg, '../x'))

    def test_park_after_the_same_failure_on_the_strongest_step_only(self):
        cfg = self.cfg()
        same = [attempt('pc', 's1')] * 3
        self.assertIn('no progress', autopilot.should_park(cfg, task('t1', attempts=same)))
        mixed = [attempt('pc', 's1'), attempt('pc', 's2'), attempt('pc', 's1')]
        self.assertEqual(autopilot.should_park(cfg, task('t1', attempts=mixed)), '')
        self.assertEqual(autopilot.should_park(cfg, task('t1', attempts=[attempt('cursor', 's1')] * 3)), '')
        self.assertIn('max_attempts', autopilot.should_park(self.cfg(max_attempts=2), task('t1', attempts=[attempt('cursor')] * 2)))

    def test_the_same_failure_twice_on_different_workers_parks_it(self):
        cfg = self.cfg()
        twice = [attempt('agy-gemini', 's9'), attempt('agy-claude', 's9')]
        self.assertIn('the task or its check is the problem', autopilot.should_park(cfg, task('t1', attempts=twice)))
        self.assertEqual(autopilot.should_park(cfg, task('t1', attempts=[attempt('agy-gemini', 's1'), attempt('agy-claude', 's2')])),
                         '')

    def test_the_same_failure_twice_on_one_step_drops_that_step_for_the_task(self):
        cfg = self.cfg()
        t = task('t1', attempts=[attempt('agy-gemini', 's9')] * 2)
        self.assertNotIn('agy-gemini', autopilot.candidates(cfg, t))
        self.assertEqual(autopilot.should_park(cfg, t), '')            # other steps are left: tried there, not parked
        quota = {**attempt('agy-gemini', 'q'), 'why': 'the worker hit its usage limit'}
        t = task('t1', attempts=[quota, {**quota, 'step': 'agy-claude'}])
        self.assertEqual(autopilot.should_park(cfg, t), '')            # the worker's own problem: never parks the task

    def test_env_side_failures_do_not_count_or_park_on_the_strongest_step(self):
        # t260: usage limits, CLI refusals and no-report must not fill max_attempts or park as no_progress
        cfg = self.cfg(max_attempts=2, no_progress=2)
        quota = {**attempt('pc', 'q'), 'why': 'the worker hit its usage limit'}
        self.assertEqual(autopilot.should_park(cfg, task('t1', attempts=[quota, quota])), '')
        refused = {**attempt('pc', 'r'), 'why': "the worker's CLI refused a command: tool read_file auto-denied"}
        self.assertEqual(autopilot.should_park(cfg, task('t1', attempts=[refused, refused])), '')
        norep = {**attempt('pc', 'n'), 'why': 'the run ended without a report'}
        self.assertEqual(autopilot.should_park(cfg, task('t1', attempts=[norep, norep])), '')
        # a real failure still parks once the cap of counted attempts is reached
        real = [attempt('cursor', 's1'), attempt('cursor', 's2')]
        self.assertIn('max_attempts', autopilot.should_park(cfg, task('t1', attempts=real)))

    def test_a_worker_exit_says_whether_it_was_its_limit_or_its_time(self):
        r = {'worker_rc': 3}
        self.assertEqual(autopilot.signature(r, {}, 'Individual quota reached. Resets in 147h44m29s.')[1], 'the worker hit its usage limit')
        self.assertEqual(autopilot.signature(r, {}, 'error: print timeout after 15m0s with the turn still in progress')[1],
                         'the worker ran out of time')
        self.assertEqual(autopilot.signature(r, {}, 'boom')[1], 'the worker exited 3')
        # a task about rate limits, quoted in Tutti's summary, is not the worker's limit
        summary = 'boom\nrun 1-2: agy exit 3; 0 file(s) changed\ntask      Add a rate limit to the API'
        self.assertEqual(autopilot.signature(r, {}, summary)[1], 'the worker exited 3')
        # no report after a usage limit in the log is still the account's limit (t260)
        self.assertEqual(autopilot.signature(None, {}, "You've hit your usage limit. Resets in 3h.")[1],
                         'the worker hit its usage limit')
        self.assertEqual(autopilot.signature(None, {}, 'worker crashed')[1], 'the run ended without a report')

    def test_an_http_429_worker_exit_is_excluded_from_attempt_count(self):
        # CodeRabbit on #616: attempt_of must normalise HTTP 429 to the quota why so park_reason does not count it
        cfg = self.cfg(max_attempts=2)
        runs = delegate.runs_dir(cfg)
        os.makedirs(runs, exist_ok=True)
        with open(os.path.join(runs, '1-1.log'), 'w', encoding='utf8') as f:
            f.write('HTTP 429 Too Many Requests\n')
        t = {**task('t1'), 'run': '1-1', 'step': 'pc', 'worker': 'pc', 'model': '', 'started': 0, 'gates': None}
        a = autopilot.attempt_of(cfg, t, {'rc': 1, 'worker_rc': 1, 'checks': [], 'changed': ['a'], 'stat': '', 'diff': None}, 0)
        self.assertEqual(a['why'], 'the worker hit its usage limit')
        self.assertTrue(autopilot.env_failure(a['why']))
        self.assertEqual(autopilot.should_park(cfg, task('t1', attempts=[a, dict(a)])), '')
        # a bare 429 in a check assertion is still a real attempt
        bare = autopilot.signature({'worker_rc': 1}, {}, 'AssertionError: expected status 429, got 500')[1]
        self.assertEqual(bare, 'the worker exited 1')

    def test_an_exhausted_ladder_parks_even_when_the_last_two_differ(self):
        cfg = self.cfg(ladder=['agy-gemini', 'agy-claude'])
        a, b = attempt('agy-gemini', 'x'), attempt('agy-claude', 'y')
        self.assertIn('every step it may go to', autopilot.should_park(cfg, task('t1', attempts=[a, b, a, b])))

    def test_a_refused_tool_with_an_exit_code_is_the_workers_problem(self):
        words = autopilot.signature({'worker_rc': 1, 'denied': ['tool read_file auto-denied']}, {})[1]
        self.assertTrue(words.startswith(autopilot.WORKER_SIDE))
        self.assertIn('refused a command', words)

    def test_the_same_bare_exit_on_two_workers_is_the_workers_problem(self):
        crash = {**attempt('agy-gemini', 'x3'), 'why': 'the worker exited 3'}
        self.assertEqual(autopilot.should_park(self.cfg(), task('t1', attempts=[crash, {**crash, 'step': 'agy-claude'}])), '')

    def test_the_retry_brief_carries_the_diff_the_failure_the_history_and_the_notes(self):
        t = task('t1', attempts=[attempt('agy-gemini', 's0'), {**attempt('agy-claude', 's1'), 'stat': ' a.txt | 1 +', 'tail': 'E: boom'}])
        t['notes'] = ['keep the API']
        t['scope'] = ['a.txt']
        text = autopilot.brief(t, {'check': 'true'})
        parts = ('attempt 3', 'continue from', 'a.txt | 1 +', 'E: boom', 'attempt 1 (agy-gemini)', 'keep the API',
                 'Change only these paths')
        for part in parts:
            self.assertIn(part, text)


class RoundTest(Base):
    """Whole rounds: dispatch (the run started for real, in a worktree, by a fake worker), collect, retry, publish."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp.name, 'repo')
        os.makedirs(self.repo)
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'config', 'user.email', 'tester@example.invalid')
        git(self.repo, 'config', 'user.name', 'Tester')
        with open(os.path.join(self.repo, 'a.txt'), 'w', encoding='utf8') as f:
            f.write('one\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'chore: start')
        script = os.path.join(self.tmp.name, 'worker.py')
        with open(script, 'w', encoding='utf8') as f:
            f.write(FAKE_WORKER)
        cfg = self.cfg(ladder=['fake-a', 'fake-b'], accounts=['fake-a'], max_parallel=1)
        cfg['machines'] = {}
        cfg['workers']['fake'] = {'kind': 'cli', 'command': [sys.executable, script, '{prompt}']}
        cfg['accounts'] = {'fake-a': {'worker': 'fake'}, 'fake-b': {'worker': 'fake'}}
        cfg['autopilot']['repos']['app'].update(check='grep -q fixed a.txt')
        self.c = cfg
        self.states = {'fake-a': account('fake-a'), 'fake-b': account('fake-b', 'on pace', None)}

    def run_now(self, argv, **kw):
        """The spawned run, done at once in this process (the real `tutti run`), so the next tick finds its report."""
        with mock.patch('sys.stdout'), mock.patch('sys.stderr'):
            cli.main(argv[2:])
        return SimpleNamespace(pid=os.getpid())

    def tick(self):
        with mock.patch.object(config, 'load', return_value=self.c):
            return autopilot.tick(self.c, self.states, [], is_alive=lambda pid: False, spawn=self.run_now)

    def test_a_failing_task_retries_from_its_worktree_one_step_up_then_is_ready_for_review(self):
        with backlog.locked(self.c) as data:
            backlog.add(data, backlog.new_task('app', 'Mark a.txt fixed', ['a.txt'], ''))
        p, events = self.tick()
        self.assertEqual(p['dispatch'], [('t1', 'fake-a')])
        p, events = self.tick()                            # collects the failure, sends attempt 2 to the next step
        self.assertIn('failed on fake-a', events[0])
        self.assertEqual(p['dispatch'], [('t1', 'fake-b')])
        _, events = self.tick()
        self.assertIn('passed on fake-b', events[0])
        t = backlog.load(self.c)['tasks'][0]
        self.assertEqual(t['status'], 'review')
        self.assertEqual([a['ok'] for a in t['attempts']], [False, True])
        self.assertEqual(t['attempts'][1]['why'], 'passed')
        self.assertTrue(t['branch'].startswith('autopilot/t1-'))
        self.assertEqual(git(t['worktree'], 'log', '--format=%s', '-1'), 'chore(autopilot): Mark a.txt fixed')
        with open(os.path.join(t['worktree'], 'a.txt'), encoding='utf8') as f:
            self.assertEqual(f.read(), 'one\nhalf\nfixed\n')   # attempt 2 continued from attempt 1's changes
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')
        self.assertNotIn('autopilot/', git(self.repo, 'branch', '--show-current'))
        v = autopilot.view(self.c)
        self.assertEqual(v['review'][0]['attempt_count'], 2)
        self.assertIn('passed', autopilot.describe(v) + 'passed')

    def test_a_run_that_ends_without_a_report_counts_as_a_failed_attempt(self):
        with backlog.locked(self.c) as data:
            backlog.add(data, backlog.new_task('app', 'x', [], 'true'))
        with mock.patch.object(config, 'load', return_value=self.c):
            autopilot.tick(self.c, self.states, [], is_alive=lambda pid: True, spawn=lambda *a, **k: SimpleNamespace(pid=1))
            _, events = autopilot.tick(self.c, self.states, [], is_alive=lambda pid: True, spawn=lambda *a, **k: SimpleNamespace(pid=1))
        self.assertEqual(events, [])                       # still running
        self.states['fake-a'] = account('fake-a', 'on pace')
        self.states['fake-b'] = account('fake-b', 'ahead')
        _, events = autopilot.tick(self.c, self.states, [], is_alive=lambda pid: False)
        self.assertIn('without a report', events[0])
        self.assertEqual(autopilot.read_state(self.c)['last']['state'], 'stopped')


class SurfaceTest(Base):
    def test_config_errors_name_the_key(self):
        cfg = self.cfg(ladder=['paid', 'nobody'], max_parallel=0, repos={'x': {'check': 'true'}, 'y': {'path': '.', 'pr': 'yes'}})
        errs = '\n'.join(config.validate(cfg))
        for part in ('costs money', '"nobody"', 'autopilot.max_parallel', 'autopilot.repos.x needs path', 'autopilot.repos.y.pr'):
            self.assertIn(part, errs)
        self.assertEqual(config.validate(self.cfg()), [])

    def test_a_worker_that_accounts_use_must_be_named_by_its_account(self):
        errs = '\n'.join(config.validate(self.cfg(ladder=['agy'])))
        self.assertIn('name the account instead (agy-gemini, agy-claude)', errs)
        self.assertIsNone(autopilot.step_of(self.cfg(), 'agy'))

    def test_publish_stops_when_git_fails_before_the_commit(self):
        calls = []

        def run(cwd, *args):
            calls.append(args[0])
            return SimpleNamespace(returncode=1 if args[0] == 'checkout' else 0, stderr='bad ref', stdout='')
        t = task('t1')
        autopilot.publish(self.cfg(), t, {'worktree': '/wt', 'diff': '/d.patch', 'id': '1-1'}, run=run)
        self.assertEqual(calls, ['checkout'])
        self.assertIn('git checkout failed', t['publish_note'])

    def test_a_start_that_fails_leaves_the_task_queued(self):
        cfg = self.cfg(ladder=['agy-gemini'], accounts=['agy-gemini'])
        cfg['machines'] = {}
        cfg['_path'] = os.path.join(self.tmp.name, 'x.yaml')
        with backlog.locked(cfg) as data:
            backlog.add(data, backlog.new_task('app', 'x', [], 'true'))

        def broken(*a, **k):
            raise OSError('no such program')
        _, events = autopilot.tick(cfg, {'agy-gemini': account('agy-gemini')}, [], spawn=broken)
        self.assertIn('could not be started', events[0])
        self.assertEqual(backlog.load(cfg)['tasks'][0]['status'], 'queued')

    def test_the_dashboard_toggle_and_its_bad_body(self):
        cfg = self.cfg(enabled=False)
        self.assertFalse(autopilot.enabled(cfg))
        self.assertEqual(dashboard.save_autopilot(SimpleNamespace(cfg=cfg), b'{"enabled": true}')[0], 200)
        self.assertTrue(autopilot.enabled(cfg))
        self.assertEqual(dashboard.save_autopilot(SimpleNamespace(cfg=cfg), b'{"enabled": "yes"}')[0], 400)
        self.assertEqual(dashboard.save_autopilot(SimpleNamespace(cfg=cfg), b'not json')[0], 400)

    def test_the_collector_ticks_only_while_on(self):
        cfg = self.cfg(enabled=False)
        c = dashboard.Collector(cfg, probe_fn=None, available_fn=None, reader=mock.Mock(), activity_fn=None, prs_fn=None)
        self.assertIsNone(c.autopilot_tick())
        autopilot.set_enabled(cfg, True)
        p, _ = c.autopilot_tick()
        self.assertEqual(p['reason'], autopilot.EMPTY)
        self.assertEqual(c.snapshot()['autopilot']['last']['reason'], autopilot.EMPTY)

    def test_backlog_cli_add_retry_rm_and_unknown_repo(self):
        cfg = self.cfg()
        with mock.patch.object(config, 'load', return_value=cfg), mock.patch('sys.stdout'):
            self.assertEqual(cli.main(['backlog', 'add', 'app', 'Fix a typo', '--scope', 'README.md']), 0)
            self.assertEqual(cli.main(['backlog', 'retry', 't1', '--note', 'use the British spelling']), 0)
            self.assertEqual(cli.main(['backlog', 'add', 'nope', 'x']), 2)
            with backlog.locked(cfg) as data:
                data['tasks'][0].update(status='running', run='1-1')
            self.assertEqual(cli.main(['backlog', 'retry', 't1']), 2)   # a running task is never changed under its run
            with backlog.locked(cfg) as data:
                data['tasks'][0]['status'] = 'queued'
            self.assertEqual(cli.main(['backlog', 'rm', 't1']), 0)
            self.assertEqual(cli.main(['autopilot', 'off']), 0)
        t = backlog.load(cfg)['tasks'][0]
        self.assertEqual((t['status'], t['notes'], t['check']), ('dropped', ['use the British spelling'], 'true'))
        self.assertFalse(autopilot.enabled(cfg))


class LocalModelTest(Base):
    def test_a_local_model_may_create_a_scoped_file_and_has_no_limit_scan(self):
        from tutti import local
        self.assertIn('new file', local.current(os.path.join(self.tmp.name, 'nope.py')))
        written = local.apply_blocks('=== FILE: sub/new.py ===\n```\nx = 1\n```\n', ['sub/new.py'], self.tmp.name)
        self.assertEqual(written, ['sub/new.py'])
        cli.note_limits(self.cfg(), 'pc', ['usage limit reached'])   # no command to match: nothing happens, no KeyError


class DelegateCheckTest(Base):
    def test_run_check_replaces_the_configured_checks(self):
        cfg = self.cfg()
        cfg['delegate']['checks'] = [{'run': 'false'}]
        given = [{'run': 'true', 'where': 'local'}]
        self.assertEqual(delegate.chosen_checks(cfg, self.tmp.name, given), (given, '--check'))
        self.assertEqual(delegate.chosen_checks(cfg, self.tmp.name, None)[0], [{'run': 'false'}])
        a = cli.parser().parse_args(['run', 'agy', 't', '--repo', '.', '--check', 'bash scripts/check.sh'])
        self.assertEqual((a.check, a.check_where), (['bash scripts/check.sh'], 'local'))



class TakeoverWhileTheMainAgentPanics(unittest.TestCase):
    CFG = {'accounts': {'claude': {'worker': 'claude'}, 'cursor': {'worker': 'cursor'}},
           'workers': {'claude': {'kind': 'cli'}, 'cursor': {'kind': 'cli'}}, 'autopilot': {}}

    def room(self, states, name='cursor', running=None, local_main=False):
        from tutti import autopilot
        cfg = self.CFG
        if local_main:
            cfg = {**cfg, '_main_worker': 'local', 'workers': {**cfg['workers'], 'local': {'kind': 'ollama'}}}
            states = {n: {'reading_at': '2026-01-01T00:00:00Z', **s} for n, s in states.items()}
        with mock.patch.object(autopilot, 'costs_money', return_value=False), \
                mock.patch.object(autopilot, 'trigger_accounts', return_value=[]):
            return autopilot.step_room(cfg, name, states, running or {})

    def test_an_ahead_account_takes_tasks_only_while_the_main_agent_panics(self):
        ahead = {'verdict': 'ahead', 'used': 40}
        self.assertGreaterEqual(self.room({'claude': {'panic': True, 'used': 96}, 'cursor': ahead})[0], 1)
        self.assertEqual(self.room({'claude': {'verdict': 'ahead', 'used': 50}, 'cursor': ahead})[0], 0)
        near = {'claude': {'panic': True, 'used': 96}, 'cursor': {'verdict': 'ahead', 'used': 94}}
        self.assertEqual(self.room(near, running={'cursor': 1})[0], 0)

    def test_it_fills_up_to_its_own_panic_line(self):
        from tutti import autopilot
        acc = {'accounts': {'cursor': {'task_pct': 2}}}
        self.assertEqual(autopilot.takeover_room(acc, 'cursor', {'used': 35, 'panic_at': 95}), 30)
        self.assertEqual(autopilot.takeover_room(acc, 'cursor', {'used': 94, 'panic_at': 95}), 1)
        self.assertEqual(autopilot.takeover_room(acc, 'cursor', {'used': 95, 'panic_at': 95}), 0)

    def test_panic_rescue_is_bounded_and_never_uses_spent_or_unknown_quota(self):
        panic = {'claude': {'panic': True, 'used': 96}}
        self.assertEqual(self.room({**panic, 'cursor': {'panic': True, 'used': 96}}, local_main=True)[0], 1)
        self.assertEqual(self.room({**panic, 'cursor': {'panic': True, 'used': 96}}, running={'cursor': 1}, local_main=True)[0], 0)
        self.assertEqual(self.room({**panic, 'cursor': {'panic': True, 'used': 99}}, local_main=True)[0], 0)
        self.assertEqual(self.room({**panic, 'cursor': {'panic': True, 'used': None}}, local_main=True)[0], 0)
        self.assertEqual(self.room({**panic, 'cursor': {'verdict': 'spent', 'used': 100}}, local_main=True)[0], 0)
        self.assertEqual(self.room(panic, name='claude')[0], 0)

    def test_a_spent_linked_window_blocks_rescue(self):
        states = {'claude': {'panic': True, 'used': 96},
                  'cursor': {'used': 10, 'limits': [{'name': 'weekly', 'used': 100, 'verdict': 'spent'}]}}
        self.assertEqual(self.room(states, local_main=True)[0], 0)

    def test_paused_account_does_not_turn_its_worker_into_an_unmetered_step(self):
        from tutti import autopilot
        cfg = config.normalise({'accounts': {'cursor-other': {'worker': 'cursor', 'paused': {'by': 'owner'}}},
                                'workers': {'cursor': {'kind': 'cli', 'command': ['cursor-agent', '{prompt}']}}})
        self.assertIsNone(autopilot.step_of(cfg, 'cursor'))

    def test_current_main_worker_account_triggers_handover(self):
        from tutti import autopilot
        cfg = {'accounts': {}, 'workers': {}, 'autopilot': {}, '_main_worker': 'cursor',
               '_account_workers': {'cursor-other': 'cursor'}}
        self.assertTrue(autopilot.main_in_panic(cfg, {'cursor-other': {'used': 97, 'panic': True}}))

    def test_main_worker_keeps_room_if_one_of_its_accounts_is_healthy(self):
        from tutti import autopilot
        cfg = {'accounts': {}, 'workers': {'cursor': {'kind': 'cli'}}, 'autopilot': {}, '_main_worker': 'cursor',
               '_account_workers': {'cursor': 'cursor', 'cursor-other': 'cursor'}}
        states = {'cursor': {'used': 100, 'verdict': 'spent'}, 'cursor-other': {'used': 20}}
        self.assertFalse(autopilot.main_in_panic(cfg, states))
        states['cursor-other'] = {'used': 97, 'panic': True}
        self.assertTrue(autopilot.main_in_panic(cfg, states))

    def test_panic_rescue_waits_for_a_local_main_and_obeys_quiet_windows(self):
        from tutti import autopilot
        cfg = {**self.CFG, '_main_worker': 'claude'}
        states = {'claude': {'panic': True, 'used': 96, 'reading_at': '2026-01-01T00:00:00Z'},
                  'cursor': {'panic': True, 'used': 96, 'reading_at': '2026-01-01T00:00:00Z'}}
        self.assertFalse(autopilot.panic_rescue(cfg, 'cursor', states['cursor'], states))
        cfg = {**cfg, '_main_worker': 'local', 'workers': {**cfg['workers'], 'local': {'kind': 'ollama'}}}
        self.assertTrue(autopilot.panic_rescue(cfg, 'cursor', states['cursor'], states))
        quiet = {**states['cursor'], 'quota_plan': {'limit': 'weekly', 'allowance': 0}}
        self.assertFalse(autopilot.panic_rescue(cfg, 'cursor', quiet, {**states, 'cursor': quiet}))

    def test_panic_rescue_does_not_start_while_an_online_account_is_not_in_panic(self):
        from tutti import autopilot
        cfg = {**self.CFG, '_main_worker': 'local',
               'workers': {**self.CFG['workers'], 'local': {'kind': 'ollama'}, 'other': {'kind': 'cli'}},
               'accounts': {**self.CFG['accounts'], 'other': {'worker': 'other'}}}
        states = {'claude': {'panic': True, 'used': 96}, 'cursor': {'panic': True, 'used': 96},
                  'other': {'used': 40, 'quota_plan': {'left': 0}}}
        self.assertFalse(autopilot.panic_rescue(cfg, 'cursor', states['cursor'], states))

    def test_main_accounts_may_be_a_single_name(self):
        from tutti import autopilot
        self.assertEqual(autopilot.main_accounts({'autopilot': {'main_accounts': 'codex'}}), ['codex'])



class LocalShareLoad(unittest.TestCase):
    def share(self, here, use_pct):
        from tutti import autopilot, fleet
        with mock.patch.object(fleet, 'machine_of', return_value='laptop'):
            return autopilot.local_share({}, here, [{'name': 'laptop', 'use_pct': use_pct}])

    def test_the_owners_load_on_their_own_cores_does_not_hold_tuttis_share(self):
        out = self.share({'cores': 16, 'load': 5.5}, 6)
        self.assertEqual(out['cores'], 1)
        self.assertEqual(out['load'], 0.0)

    def test_an_owner_using_more_than_their_cores_still_leaves_no_room(self):
        out = self.share({'cores': 16, 'load': 17.0}, 6)
        self.assertEqual(out['load'], 2.0)

    def test_unknown_load_or_share_is_left_as_is(self):
        self.assertIsNone(self.share({'cores': 16, 'load': None}, 6)['load'])
        self.assertEqual(self.share({'cores': 16, 'load': 3.0}, None), {'cores': 16, 'load': 3.0})

if __name__ == '__main__':
    unittest.main()
