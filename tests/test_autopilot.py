"""`tutti backlog` and `tutti autopilot`: the queue and its sources, the pure plan (pace, room, heat, max_parallel, the
escalation ladder), failure signatures and parking, the retry brief, and whole rounds against a throwaway repository with a
fake worker. Nothing leaves the temp folder; no real machine or account is used."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tutti import (
    autopilot, backlog, cli, config, controls, dashboard, delegate, dispatch, fleet, household, planner, routing, sharing,
    state, users,
)

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
TEST_NOW = autopilot.datetime(2026, 1, 1, 1, tzinfo=autopilot.timezone.utc)


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

    def test_explicitly_different_scope_or_check_does_not_discard_work(self):
        cfg = self.cfg()
        with backlog.locked(cfg) as data:
            first, added = backlog.add(data, backlog.new_task('app', 'Fix check', ['a.py', 'b.py'], 'test-a'))
            self.assertTrue(added)
            duplicate, added = backlog.add(data, backlog.new_task('app', 'fix  check', ['b.py', 'a.py'], 'test-a'))
            self.assertFalse(added)
            self.assertEqual(duplicate['id'], first['id'])
            for repo, scope, check in (('app', ['other.py'], 'test-a'), ('app', ['a.py', 'b.py'], 'test-b'),
                                       ('other', ['a.py', 'b.py'], 'test-a')):
                with self.subTest(repo=repo, scope=scope, check=check):
                    _, added = backlog.add(data, backlog.new_task(repo, 'Fix check', scope, check))
                    self.assertTrue(added)
            _, added = backlog.add(data, backlog.new_task('app', 'Fix check'))
            self.assertFalse(added)  # missing metadata retains the existing title-only behavior

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

    def test_local_household_selection_is_scoped_unique_and_requires_fresh_model(self):
        cfg = self.cfg()
        cfg['_sharing_machine_at'] = TEST_NOW.timestamp()
        cfg['workers']['pc'].update(model='qwen2.5-coder:7b', machine='nuc')
        cfg['autopilot']['repos']['app']['household'] = True
        item = {**task('t1'), 'scope': ['a.py'], 'source': 'household', 'household_instance': 'a' * 32}
        model = fleet.classify({'name': 'qwen2.5-coder:7b'})
        inventory = {'machines': {'nuc': {'stale': False, 'updated': TEST_NOW.timestamp(),
                                            'tags_endpoint': 'pc.example:11434', 'models': [model]}}}
        with mock.patch.object(fleet, 'read_state', return_value=inventory):
            choices = autopilot.scoped_local_choices(cfg, {'tasks': [item]}, {'pc': (3, '')}, 1, FREE, TEST_NOW)
            self.assertEqual(list(choices), ['t1'])
            self.assertEqual(choices['t1']['request']['scope'], ['a.py'])
            missing = [{'name': 'other', 'status': 'free', 'free': 2}]
            self.assertEqual(autopilot.scoped_local_choices(cfg, {'tasks': [item]}, {'pc': (3, '')}, 1, missing, TEST_NOW), {})
            busy = [{'name': 'nuc', 'status': 'busy', 'free': 0}]
            self.assertEqual(autopilot.scoped_local_choices(cfg, {'tasks': [item]}, {'pc': (3, '')}, 1, busy, TEST_NOW), {})
            own = {**item, 'source': 'user'}
            self.assertEqual(list(autopilot.scoped_local_choices(cfg, {'tasks': [own]}, {'pc': (3, '')}, 1, FREE, TEST_NOW)), ['t1'])
            cfg['workers']['other-local'] = {**cfg['workers']['pc']}
            cfg['autopilot']['ladder'].append('other-local')
            distinct = {**item, 'id': 't2', 'title': 'different', 'household_instance': 'b' * 32}
            both_rooms = {'pc': (3, ''), 'other-local': (3, '')}
            self.assertEqual(len(autopilot.scoped_local_choices(cfg, {'tasks': [item, distinct]}, both_rooms, 2, FREE, TEST_NOW)), 1)
            duplicate = {**item, 'id': 't2'}
            self.assertEqual(autopilot.scoped_local_choices(cfg, {'tasks': [item, duplicate]}, {'pc': (3, '')}, 1, FREE, TEST_NOW), {})
            inventory['machines']['nuc']['stale'] = True
            self.assertEqual(autopilot.scoped_local_choices(cfg, {'tasks': [item]}, {'pc': (3, '')}, 1, FREE, TEST_NOW), {})

    def test_local_scoped_selection_checks_model_per_task_before_debit(self):
        cfg = self.cfg()
        cfg['_sharing_machine_at'] = TEST_NOW.timestamp()
        cfg['autopilot']['repos']['app']['household'] = True
        cfg['workers']['pc'].update(model='qwen2.5-coder:7b', machine='nuc', host='one.example:11434')
        cfg['workers']['pc2'] = {**cfg['workers']['pc'], 'host': 'two.example:11434'}
        cfg['autopilot']['ladder'] = ['pc', 'pc2']
        first = {**task('t1'), 'scope': ['a.py'], 'household_instance': 'a' * 32}
        second = {**task('t2'), 'title': 'Other', 'scope': ['b.py'], 'household_instance': 'b' * 32}
        model = fleet.classify({'name': 'qwen2.5-coder:7b'})
        inventory = {'machines': {'nuc': {'stale': False, 'updated': TEST_NOW.timestamp(),
                                            'tags_endpoint': 'pc.example:11434', 'models': [model]}}}
        def eligible(_cfg, worker, item, _inventory, _now):
            return (worker, item['id']) in {('pc2', 't1'), ('pc', 't2')}
        with mock.patch.object(fleet, 'read_state', return_value=inventory), \
             mock.patch.object(autopilot, 'fresh_local_model', side_effect=eligible):
            choices = autopilot.scoped_local_choices(cfg, {'tasks': [first, second]},
                                                       {'pc': (3, ''), 'pc2': (3, '')}, 2, FREE, TEST_NOW)
            one_slot = autopilot.scoped_local_choices(cfg, {'tasks': [first, second]},
                                                      {'pc': (3, ''), 'pc2': (3, '')}, 2,
                                                      [{'name': 'nuc', 'status': 'free', 'free': 1}], TEST_NOW)
            cfg['workers']['pc2']['machine'] = 'nuc2'
            two_hosts = autopilot.scoped_local_choices(cfg, {'tasks': [first, second]},
                                                       {'pc': (3, ''), 'pc2': (3, '')}, 2,
                                                       [{'name': 'nuc', 'status': 'free', 'free': 1},
                                                        {'name': 'nuc2', 'status': 'free', 'free': 1}], TEST_NOW)
            cfg['machines']['desktop'] = {**cfg['machines']['nuc'], 'dedicated': True}
            cfg['workers']['pc2']['machine'] = 'desktop'
            aliases = autopilot.scoped_local_choices(cfg, {'tasks': [first, second]},
                                                     {'pc': (3, ''), 'pc2': (3, '')}, 2,
                                                     [{'name': 'nuc', 'status': 'free', 'free': 1},
                                                      {'name': 'desktop', 'status': 'free', 'free': 1}], TEST_NOW)
        self.assertEqual({key: val['worker'] for key, val in choices.items()}, {'t1': 'pc2', 't2': 'pc'})
        self.assertEqual(len(one_slot), 1)
        self.assertEqual(len(two_hosts), 2)
        self.assertEqual(len(aliases), 1)

    def test_scoped_model_requires_matching_observed_endpoint(self):
        cfg = self.cfg()
        cfg['workers']['pc'].update(model='qwen2.5-coder:7b', machine='nuc')
        item = task('t1')
        model = fleet.classify({'name': 'qwen2.5-coder:7b'})
        entry = {'stale': False, 'updated': TEST_NOW.timestamp(), 'models': [model]}
        self.assertFalse(autopilot.fresh_local_model(cfg, 'pc', item, {'nuc': entry}, TEST_NOW))
        entry['tags_endpoint'] = 'other.example:11434'
        self.assertFalse(autopilot.fresh_local_model(cfg, 'pc', item, {'nuc': entry}, TEST_NOW))
        entry['tags_endpoint'] = 'http://pc.example:11434'
        self.assertTrue(autopilot.fresh_local_model(cfg, 'pc', item, {'nuc': entry}, TEST_NOW))

    def test_sharing_capacity_recovers_without_queued_tasks_and_preserves_fill_limit(self):
        cfg = self.cfg()
        cfg['_sharing_machine_at'] = TEST_NOW.timestamp()
        cfg['_parallel_limit'] = 7
        states = self.states(**{'agy-gemini': account('agy-gemini', 'ahead'), 'agy-claude': account('agy-claude', 'ahead')})
        autopilot.refresh_sharing_capacity(cfg, {'tasks': []}, states, FREE, TEST_NOW, True)
        self.assertEqual(cfg['_sharing_online_limit'], 0)
        states['agy-gemini'] = account('agy-gemini', 'behind', room=2)
        autopilot.refresh_sharing_capacity(cfg, {'tasks': []}, states, FREE, TEST_NOW, True)
        self.assertGreater(cfg['_sharing_online_limit'], 0)
        self.assertEqual(cfg['_parallel_limit'], 7)
        with mock.patch.object(autopilot, 'step_room', side_effect=ValueError('private detail')):
            autopilot.refresh_sharing_capacity(cfg, {'tasks': []}, states, FREE, TEST_NOW, True)
        self.assertEqual(cfg['_sharing_online_limit'], 0)
        self.assertNotIn('private detail', cfg['_sharing_capacity_error'])
        for failure in (AttributeError('bad inventory'), OSError('unreadable inventory')):
            with mock.patch.object(autopilot, 'step_room', side_effect=failure):
                autopilot.refresh_sharing_capacity(cfg, {'tasks': []}, states, FREE, TEST_NOW, True)
            self.assertEqual(cfg['_sharing_local_scoped'], {})
            self.assertIn('shared work waits', cfg['_sharing_capacity_error'])

    def test_eligible_shared_retry_uses_legacy_sharing_with_mixed_fresh_queue(self):
        cfg = self.cfg()
        cfg['autopilot']['repos']['app']['household'] = True
        cfg['_parallel_limit'] = 3
        states = self.states(**{'agy-gemini': account('agy-gemini', 'ahead'),
                                'agy-claude': account('agy-claude', 'ahead'), 'cursor': account('cursor', 'on pace', 3)})
        fresh = task('fresh')
        retry = task('retry', attempts=[attempt('agy-claude')])
        data = {'tasks': [fresh, retry]}
        autopilot.refresh_sharing_capacity(cfg, data, states, FREE, TEST_NOW, True)
        self.assertIsNone(cfg['_sharing_local_scoped'])
        self.assertNotIn('_sharing_online_limit', cfg)
        ask = sharing.ask(cfg, data, {'app': 'git.example/app'}, {})['projects']['git.example/app']
        self.assertEqual(ask['want'], ['task fresh', 'task retry'])
        self.assertNotIn('want_scoped', ask)   # the pre-existing protocol also works with old coordinators
        autopilot.refresh_sharing_capacity(cfg, {'tasks': [fresh]}, states, FREE, TEST_NOW, True)
        self.assertEqual(cfg['_sharing_local_scoped'], {})
        rooms = {'cursor': (3, '')}
        self.assertTrue(autopilot.has_online_shared_retry(cfg, [retry], rooms, FREE, TEST_NOW))
        unchecked = {**retry, 'check': ''}
        cfg['autopilot']['repos']['app']['check'] = ''
        self.assertFalse(autopilot.has_online_shared_retry(cfg, [unchecked], rooms, FREE, TEST_NOW))
        cfg['autopilot']['repos']['app']['check'] = 'true'
        self.assertFalse(autopilot.has_online_shared_retry(cfg, [{**retry, 'pin': 'worker:agy'}], rooms, FREE, TEST_NOW))
        cfg['autopilot']['repos']['app']['spare'] = True
        cfg['_spare_accounts'] = []
        self.assertFalse(autopilot.has_online_shared_retry(cfg, [retry], rooms, FREE, TEST_NOW))
        cfg['_spare_accounts'] = ['cursor']
        self.assertTrue(autopilot.has_online_shared_retry(cfg, [retry], rooms, FREE, TEST_NOW))
        cfg['autopilot']['repos']['app']['spare'] = False
        cfg['_panic_budget'] = {'stale': 99}
        with mock.patch.object(autopilot, 'rescue_budgets', return_value=({'cursor': ['current']}, {'current': 0})), \
             mock.patch.object(autopilot, 'has_online_shared_retry', return_value=False) as eligible:
            autopilot.refresh_sharing_capacity(cfg, data, states, FREE, TEST_NOW, True)
        self.assertEqual(eligible.call_args.args[0]['_panic_budget'], {'current': 0})

    def test_scoped_dispatch_revalidates_worker_model_and_fleet_before_start(self):
        cfg = self.cfg()
        cfg['_sharing_machine_at'] = TEST_NOW.timestamp()
        cfg['workers']['pc'].update(model='qwen2.5-coder:7b', machine='nuc')
        cfg['workers']['other-local'] = {**cfg['workers']['pc']}
        item = {**task('t1'), 'scope': ['a.py'], 'source': 'household', 'household_instance': 'a' * 32}
        request = {'key': 'task t1', 'instance': 'a' * 32, 'nonce': 'b' * 32}
        choice = {'task': dict(item), 'worker': 'pc', 'model': 'qwen2.5-coder:7b', 'request': request}
        result = {'scoped': {'t1': choice}, 'keys': {'app': 'project'},
                  'answer': {'projects': {'project': {'scoped_claims': [{**request, 'lease_seconds': 300}]}}}}
        model = fleet.classify({'name': 'qwen2.5-coder:7b'})
        inventory = {'machines': {'nuc': {'stale': False, 'updated': TEST_NOW.timestamp(),
                                            'tags_endpoint': 'pc.example:11434', 'models': [model]}}}
        with mock.patch.object(fleet, 'read_state', return_value=inventory):
            for worker in ('other-local', 'agy-gemini'):
                plan = {'dispatch': [('t1', worker)], 'waiting': {}}
                autopilot.filter_scoped_dispatch(cfg, {'tasks': [item]}, result, plan, FREE, TEST_NOW, 0)
                self.assertEqual(plan['dispatch'], [])
            plan = {'dispatch': [('t1', 'pc')], 'waiting': {}}
            autopilot.filter_scoped_dispatch(cfg, {'tasks': [item]}, result, plan, FREE, TEST_NOW, 0)
            self.assertEqual(plan['dispatch'], [('t1', 'pc')])
            plan = {'dispatch': [('t1', 'pc')], 'waiting': {}}
            autopilot.filter_scoped_dispatch(cfg, {'tasks': [item]}, result, plan, FREE, TEST_NOW, 271)
            self.assertEqual(plan['dispatch'], [])   # conservative lease-duration margin, independent of wall-clock skew
            inventory['machines']['nuc']['stale'] = True
            plan = {'dispatch': [('t1', 'pc')], 'waiting': {}}
            autopilot.filter_scoped_dispatch(cfg, {'tasks': [item]}, result, plan, FREE, TEST_NOW, 0)
            self.assertEqual(plan['dispatch'], [])
            inventory['machines']['nuc']['stale'] = False
            cfg['workers']['pc']['model'] = 'qwen3:8b'
            plan = {'dispatch': [('t1', 'pc')], 'waiting': {}}
            autopilot.filter_scoped_dispatch(cfg, {'tasks': [item]}, result, plan, FREE, TEST_NOW, 0)
            self.assertEqual(plan['dispatch'], [])

    def test_scoped_spawn_rechecks_after_slow_launch_intent_save(self):
        cfg = self.cfg()
        cfg['_now'], cfg['_sharing_machine_at'] = TEST_NOW, TEST_NOW.timestamp()
        cfg['workers']['pc'].update(model='qwen2.5-coder:7b', machine='nuc')
        item = {**task('t1'), 'scope': ['a.py'], 'source': 'household', 'household_instance': 'a' * 32}
        request = {'key': 'task t1', 'instance': 'a' * 32, 'nonce': 'b' * 32}
        choice = {'task': dict(item), 'worker': 'pc', 'model': 'qwen2.5-coder:7b', 'request': request}
        result = {'scoped': {'t1': choice}, 'keys': {'app': 'project'},
                  'answer': {'projects': {'project': {'scoped_claims': [{**request, 'lease_seconds': 300}]}}}}
        model = fleet.classify({'name': 'qwen2.5-coder:7b'})
        inventory = {'machines': {'nuc': {'stale': False, 'updated': TEST_NOW.timestamp(),
                                            'tags_endpoint': 'pc.example:11434', 'models': [model]}}}
        elapsed, rejected, spawn = [0], [False], mock.Mock()
        def save(_cfg, _data):
            elapsed[0] = 61
        guarded = autopilot.scoped_spawn(cfg, item, 'pc', choice, result, FREE, 0, lambda: elapsed[0], spawn, rejected)
        with mock.patch.object(backlog, 'save', side_effect=save), mock.patch.object(fleet, 'read_state', return_value=inventory):
            run_id, event = autopilot.start_reserved(cfg, {'tasks': [item]}, item, 'pc', TEST_NOW.timestamp(), guarded, {})
        self.assertIsNone(run_id)
        self.assertTrue(rejected[0])
        self.assertEqual(item['status'], 'queued')
        self.assertIn('observation changed', event)
        spawn.assert_not_called()

    def test_malformed_inventory_holds_only_scoped_dispatch(self):
        cfg = self.cfg()
        cfg['_now'], cfg['_sharing_machine_at'] = TEST_NOW, TEST_NOW.timestamp()
        cfg['workers']['pc'].update(model='qwen2.5-coder:7b', machine='nuc')
        item = {**task('t1'), 'scope': ['a.py']}
        other = {**task('t2'), 'repo': 'other'}
        choice = {'task': dict(item), 'worker': 'pc', 'model': 'qwen2.5-coder:7b',
                  'request': {'key': 't1', 'instance': 'a' * 32, 'nonce': 'b' * 32}}
        result = {'scoped': {'t1': choice}, 'keys': {'app': 'project'}, 'answer': {}}
        for inventory in ({'machines': []}, {'machines': {'nuc': {'models': [123]}}}, {'machines': 'invalid'}):
            plan = {'dispatch': [('t1', 'pc'), ('t2', 'pc')], 'waiting': {}}
            with mock.patch.object(fleet, 'read_state', return_value=inventory):
                autopilot.filter_scoped_dispatch(cfg, {'tasks': [item, other]}, result, plan, FREE, TEST_NOW, 0)
            self.assertEqual(plan['dispatch'], [('t2', 'pc')])
            self.assertIn('wait for the next scoped reservation', plan['waiting']['t1'])
        cfg['workers']['pc'] = 'invalid'
        plan = {'dispatch': [('t1', 'pc'), ('t2', 'pc')], 'waiting': {}}
        with mock.patch.object(fleet, 'read_state', return_value={'machines': {}}):
            autopilot.filter_scoped_dispatch(cfg, {'tasks': [item, other]}, result, plan, FREE, TEST_NOW, 0)
        self.assertEqual(plan['dispatch'], [('t2', 'pc')])
        self.assertIn('wait for the next scoped reservation', plan['waiting']['t1'])

    def test_off_and_an_empty_backlog_say_so(self):
        cfg = self.cfg()
        self.assertEqual(autopilot.plan(cfg, [task('t1')], self.states(), FREE, False)['state'], 'off')
        p = autopilot.plan(cfg, [], self.states(), FREE, True)
        self.assertEqual(p['reason'], autopilot.EMPTY)

    def test_task_priority_orders_dispatch_and_explicit_priority_overrides_title(self):
        low = {**task('t1'), 'title': 'P1: later', 'priority': 1}
        high = {**task('t2'), 'title': 'P0: first', 'priority': 0}
        plan = autopilot.plan(self.cfg(), [low, high], self.states(), FREE, True)
        self.assertEqual([x[0] for x in plan['dispatch']], ['t2', 't1'])
        high['priority'] = 2
        plan = autopilot.plan(self.cfg(), [low, high], self.states(), FREE, True)
        self.assertEqual([x[0] for x in plan['dispatch']], ['t1', 't2'])

    def test_behind_accounts_get_tasks_up_to_max_parallel(self):
        p = autopilot.plan(self.cfg(), [task('t1'), task('t2'), task('t3')], self.states(), FREE, True)
        self.assertEqual(p['dispatch'], [('t1', 'agy-gemini'), ('t2', 'agy-gemini')])
        self.assertEqual(p['state'], 'dispatching')

    def test_ahead_account_uses_measured_room_slowly_without_crossing_safety_gates(self):
        cfg = self.cfg(accounts=['agy-gemini'], ladder=['agy-gemini'])
        cfg['_slow_shared_accounts'] = []
        cfg['accounts']['agy-gemini']['task_pct'] = 2
        now = TEST_NOW
        s = {**account('agy-gemini', 'ahead', None, used=20), 'share': 1,
             'reading_at': '2026-01-01T00:50:00+00:00', 'reader_problem': '', 'target': '2026-01-03T01:00:00+00:00'}
        s['quota_plan'] = autopilot.planner.plan_single(s, cfg['accounts']['agy-gemini'], now)
        s['quota_plan']['why'] = 'no extra tasks now'
        states = {'agy-gemini': s}
        self.assertEqual(autopilot.plan(cfg, [task('t1'), task('t2')], states, FREE, True, now)['dispatch'],
                         [('t1', 'agy-gemini')])
        self.assertEqual(autopilot.plan(cfg, [task('t1')], states, FREE, True, now)['slow_dispatch'], ['t1'])
        cfg['accounts']['agy-gemini']['share_spare'] = True
        cfg['_slow_shared_accounts'] = users.slow_shares(cfg)
        self.assertEqual(autopilot.plan(cfg, [task('t1')], states, FREE, True, now)['dispatch'], [])
        users.write(self.state, share={'agy-gemini': False})
        cfg['_slow_shared_accounts'] = users.slow_shares(cfg)
        self.assertEqual(autopilot.plan(cfg, [task('t1')], states, FREE, True, now)['dispatch'], [('t1', 'agy-gemini')])
        cfg['_slow_shared_accounts'] = None
        self.assertEqual(autopilot.plan(cfg, [task('t1')], states, FREE, True, now)['dispatch'], [])
        with open(users.path(self.state), 'w', encoding='utf8') as f:
            f.write('{')
        self.assertIsNone(users.slow_shares(cfg))
        cfg['_slow_shared_accounts'] = []
        planned = {**s, 'quota_plan': {**s['quota_plan'], 'tasks': 3}}
        self.assertEqual(len(autopilot.plan(cfg, [task('t1'), task('t2')], {'agy-gemini': planned}, FREE, True, now)['dispatch']), 2)
        recent = task('old', 'done', [{**attempt('agy-gemini'), 'started': now.timestamp() - 3600}])
        self.assertEqual(autopilot.plan(cfg, [recent, task('t1')], states, FREE, True, now)['dispatch'], [])
        recent['attempts'][0]['started'] = now.timestamp() - 4 * 3600
        self.assertEqual(autopilot.plan(cfg, [recent, task('t1')], states, FREE, True, now)['dispatch'], [('t1', 'agy-gemini')])
        running = {**task('active', 'running'), 'step': 'agy-gemini'}
        self.assertEqual(autopilot.plan(cfg, [running, task('t1')], states, FREE, True, now)['dispatch'], [])
        for changed in ({'panic': True}, {'stale': True}, {'limits': [{'name': 'weekly'}]},
                        {'used': 94}, {'reader_problem': 'reader failed'},
                        {'quota_plan': {**s['quota_plan'], 'slow_left': 0}}):
            self.assertEqual(autopilot.plan(cfg, [task('t1')], {'agy-gemini': {**s, **changed}}, FREE, True, now)['dispatch'], [])
        cfg['accounts']['agy-gemini']['task_pct'] = 0
        self.assertEqual(autopilot.plan(cfg, [task('t1')], states, FREE, True, now)['dispatch'], [])
        cfg['accounts']['agy-gemini']['task_pct'] = 2
        cfg['_account_limits']['other'] = ['agy-gemini']
        self.assertEqual(autopilot.plan(cfg, [task('t1')], states, FREE, True, now)['dispatch'], [])
        del cfg['_account_limits']
        cfg['accounts']['other'] = {'limits': ['agy-gemini']}
        self.assertEqual(autopilot.plan(cfg, [task('t1')], states, FREE, True, now)['dispatch'], [])

    def test_slow_start_rechecks_current_clock_and_effective_share(self):
        cfg = self.cfg(accounts=['agy-gemini'], ladder=['agy-gemini'])
        now = TEST_NOW
        s = {**account('agy-gemini', 'ahead', None, used=20), 'share': 1,
             'reading_at': '2026-01-01T00:50:00+00:00', 'reader_problem': '', 'target': '2026-01-03T01:00:00+00:00'}
        s['quota_plan'] = autopilot.planner.plan_single(s, cfg['accounts']['agy-gemini'], now)
        states, tasks = {'agy-gemini': s}, [task('t1')]
        with mock.patch.object(config, 'load', return_value=cfg) as load, \
                mock.patch.object(state, 'usage_state', return_value={'accounts': [s]}) as usage:
            self.assertTrue(autopilot.slow_start_allowed(cfg, 'agy-gemini', states, tasks, now.timestamp()))
            self.assertFalse(autopilot.slow_start_allowed(cfg, 'agy-gemini', states, tasks, now.timestamp() + 3 * 3600))
            with mock.patch.object(autopilot, 'start_reserved', return_value=('run', 'started')) as start:
                data = {'tasks': tasks}
                later = now.timestamp() + 60
                self.assertEqual(autopilot.start_planned(cfg, data, tasks[0], 'agy-gemini', now.timestamp(), None, states,
                                                         lambda: later, True)[0], 'run')
                self.assertEqual(start.call_args.args[4], later)
                start.reset_mock()
                self.assertIsNone(autopilot.start_planned(cfg, data, tasks[0], 'agy-gemini', now.timestamp(), None, states,
                                                          lambda: now.timestamp() + 3 * 3600, True)[0])
                start.assert_not_called()
            usage.return_value = {'accounts': [{**s, 'used': 99, 'panic': True}]}
            self.assertFalse(autopilot.slow_start_allowed(cfg, 'agy-gemini', states, tasks, now.timestamp()))
            usage.return_value = {'accounts': [s]}
            load.return_value = {**cfg, 'disabled_accounts': ['agy-gemini']}
            self.assertFalse(autopilot.slow_start_allowed(cfg, 'agy-gemini', states, tasks, now.timestamp()))
            load.return_value = {**cfg, 'disabled_workers': ['agy']}
            self.assertFalse(autopilot.slow_start_allowed(cfg, 'agy-gemini', states, tasks, now.timestamp()))
            load.return_value = cfg
            users.write(self.state, share={'agy-gemini': True})
            self.assertFalse(autopilot.slow_start_allowed(cfg, 'agy-gemini', states, tasks, now.timestamp()))
            with open(os.path.join(self.state, 'controls.json'), 'w', encoding='utf8') as f:
                f.write('{')
            self.assertFalse(autopilot.slow_start_allowed(cfg, 'agy-gemini', states, tasks, now.timestamp()))

    def test_worker_status_rechecks_slow_room_using_the_current_reading(self):
        cfg = self.cfg(accounts=['agy-gemini'], ladder=['agy-gemini'])
        cfg['_slow_shared_accounts'] = []
        cfg['fleet']['enabled'] = False
        now = TEST_NOW
        s = {**account('agy-gemini', 'ahead', None, used=20), 'share': 1,
             'reading_at': '2026-01-01T00:50:00+00:00', 'reader_problem': '', 'target': '2026-01-03T01:00:00+00:00'}
        s['quota_plan'] = autopilot.planner.plan_single(s, cfg['accounts']['agy-gemini'], now)
        s['quota_plan']['why'] = 'no extra tasks now'
        with mock.patch.object(backlog, 'load', return_value={'tasks': [task('t1')]}):
            self.assertEqual([x['name'] for x in dispatch.worker_rows(cfg, {'agy-gemini': s}, FREE, now)], ['agy-gemini'])
            self.assertEqual(dispatch.worker_rows(cfg, {'agy-gemini': {**s, 'stale': True}}, FREE, now), [])
            cfg['autopilot']['accounts'] = []
            self.assertEqual(dispatch.worker_rows(cfg, {'agy-gemini': s}, FREE, now), [])
        cfg['autopilot']['accounts'] = ['agy-gemini']
        with mock.patch.object(backlog, 'load', return_value={'tasks': [task('large', size='L')]}):
            self.assertEqual(dispatch.worker_rows(cfg, {'agy-gemini': s}, FREE, now), [])

    def test_slow_start_reloads_real_account_and_worker_pause_controls(self):
        path = os.path.join(self.tmp.name, 'config.yaml')
        raw = {'project': {'state': self.state, 'worktrees': os.path.join(self.tmp.name, 'wt')},
               'machines': {'nuc': {'host': 'nuc.example'}},
               'workers': {'agy': {'kind': 'cli', 'command': ['agy', '{prompt}']}},
               'accounts': {'agy-gemini': {'worker': 'agy'}},
               'autopilot': {'accounts': ['agy-gemini'], 'ladder': ['agy-gemini'],
                             'repos': {'app': {'path': self.tmp.name, 'check': 'true'}}}}
        with open(path, 'w', encoding='utf8') as f:
            json.dump(raw, f)
        cfg = config.load(path)
        now = TEST_NOW
        s = {**account('agy-gemini', 'ahead', None, used=20), 'share': 1,
             'reading_at': '2026-01-01T00:50:00+00:00', 'reader_problem': '', 'target': '2026-01-03T01:00:00+00:00'}
        s['quota_plan'] = autopilot.planner.plan_single(s, cfg['accounts']['agy-gemini'], now)
        states, tasks = {'agy-gemini': s}, [task('t1')]
        with mock.patch.object(state, 'usage_state', return_value={'accounts': [s]}):
            self.assertTrue(autopilot.slow_start_allowed(cfg, 'agy-gemini', states, tasks, now.timestamp()))
            controls.set_paused(self.state, 'account', 'agy-gemini', True)
            self.assertFalse(autopilot.slow_start_allowed(cfg, 'agy-gemini', states, tasks, now.timestamp()))
            controls.set_paused(self.state, 'account', 'agy-gemini', False)
            controls.set_paused(self.state, 'worker', 'agy', True)
            self.assertFalse(autopilot.slow_start_allowed(cfg, 'agy-gemini', states, tasks, now.timestamp()))

    def test_slow_start_rejects_newer_usage_recorded_after_planning(self):
        path = os.path.join(self.tmp.name, 'config.yaml')
        raw = {'project': {'state': self.state, 'worktrees': os.path.join(self.tmp.name, 'wt')},
               'workers': {'agy': {'kind': 'cli', 'command': ['agy', '{prompt}']}},
               'accounts': {'agy-gemini': {'worker': 'agy', 'window': '7d', 'resets': '2026-01-08T00:00:00Z'}},
               'autopilot': {'accounts': ['agy-gemini'], 'ladder': ['agy-gemini'],
                             'repos': {'app': {'path': self.tmp.name, 'check': 'true'}}}}
        with open(path, 'w', encoding='utf8') as f:
            json.dump(raw, f)
        cfg, now = config.load(path), TEST_NOW
        os.makedirs(self.state, exist_ok=True)
        autopilot.pacing.append_log(state.usage_log_path(cfg), 'agy-gemini', 20,
                                    autopilot.datetime.fromtimestamp(now.timestamp() - 600, autopilot.timezone.utc))
        observed = state.usage_state(cfg, now)['accounts'][0]
        observed['reader_problem'] = ''
        states, tasks = {'agy-gemini': observed}, [task('t1')]
        self.assertTrue(autopilot.slow_start_allowed(cfg, 'agy-gemini', states, tasks, now.timestamp()))
        autopilot.pacing.append_log(state.usage_log_path(cfg), 'agy-gemini', 99,
                                    autopilot.datetime.fromtimestamp(now.timestamp() - 60, autopilot.timezone.utc))
        self.assertFalse(autopilot.slow_start_allowed(cfg, 'agy-gemini', states, tasks, now.timestamp()))

    def test_when_all_online_are_in_panic_rescue_spreads_work_to_the_most_remaining_quota(self):
        cfg = self.cfg(main_accounts=['agy-claude'])
        cfg['_main_worker'] = 'local-main'
        cfg['workers']['local-main'] = {'kind': 'ollama', 'host': '127.0.0.1:1', 'model': 'm:3b'}
        states = {'agy-claude': account('agy-claude', 'ahead', used=99, panic=True),
                  'agy-gemini': account('agy-gemini', 'ahead', used=97, panic=True),
                  'cursor': account('cursor', 'ahead', used=95, panic=True)}
        states = {n: {**s, 'reading_at': '2026-01-01T00:00:00Z'} for n, s in states.items()}
        p = autopilot.plan(cfg, [task('t1'), task('t2')], states, FREE, True, now=TEST_NOW)
        self.assertEqual(p['dispatch'], [('t1', 'cursor'), ('t2', 'agy-gemini')])

    def test_panic_rescue_reserves_shared_window_for_each_dispatch(self):
        cfg = self.cfg(main_accounts=['agy-claude'])
        cfg['_main_worker'] = 'local-main'
        cfg['workers']['local-main'] = {'kind': 'ollama', 'host': '127.0.0.1:1', 'model': 'm:3b'}
        weekly = {'name': 'weekly', 'used': 97, 'panic': True, 'reading_at': '2026-01-01T00:00:00Z'}
        states = {n: {**account(n, 'ahead', used=96, panic=True), 'reading_at': '2026-01-01T00:00:00Z',
                      'limits': [weekly]} for n in ('agy-claude', 'agy-gemini', 'cursor')}
        p = autopilot.plan(cfg, [task('t1'), task('t2')], states, FREE, True, now=TEST_NOW)
        self.assertEqual(len(p['dispatch']), 1)   # 3 points left, two tasks would cost 4
        self.assertIn('shared quota', p['waiting']['t2'])

    def test_panic_rescue_fails_closed_when_a_configured_linked_window_is_missing(self):
        cfg = self.cfg(main_accounts=['cursor'])
        cfg['_main_worker'] = 'local-main'
        cfg['workers']['local-main'] = {'kind': 'ollama', 'host': '127.0.0.1:1', 'model': 'm:3b'}
        cfg['_account_limits']['cursor'] = ['weekly']
        cfg['_account_limits']['weekly'] = ['monthly']
        cfg['_now'] = TEST_NOW
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

    def test_direct_takeover_rejects_unknown_own_or_linked_usage(self):
        for s in (account('cursor', used=None),
                  {**account('cursor', used=20), 'limits': [account('weekly', used=None)]}):
            picked, why = routing.pick_account([s], takeover=True)
            self.assertIsNone(picked)
            self.assertIn('unknown', why)

    def test_attempt_remains_debited_until_linked_window_is_refreshed(self):
        states = {'cursor': {'reading_at': '2026-01-03T00:00:00Z',
                             'limits': [{'name': 'weekly', 'reading_at': '2026-01-01T00:00:00Z'}]}}
        t = task('t1', 'done', [{'step': 'cursor', 'started': 1767312000}])  # 2026-01-02 UTC
        self.assertEqual(autopilot.spent_units([t], states, self.cfg()), {'cursor': 1})
        states['cursor']['limits'][0]['reading_at'] = '2026-01-03T00:00:00Z'
        self.assertEqual(autopilot.spent_units([t], states, self.cfg()), {})

    def test_local_server_is_reused_after_completion_but_not_while_running(self):
        cfg = self.cfg()
        states = {n: account(n, 'ahead', room=0) for n in ('agy-gemini', 'agy-claude', 'cursor')}
        completed = task('old', 'done', [{**attempt('pc', ok=True), 'started': 1000}])
        waiting = {**task('new'), 'scope': ['src/a.py']}
        self.assertEqual(autopilot.plan(cfg, [completed, waiting], states, FREE, True)['dispatch'], [('new', 'pc')])
        running = {**task('running', 'running'), 'step': 'pc'}
        self.assertEqual(autopilot.plan(cfg, [completed, running, waiting], states, FREE, True)['dispatch'], [])

    def test_local_worker_aliases_share_server_capacity(self):
        cfg = self.cfg()
        cfg['workers']['pc2'] = {**cfg['workers']['pc'], 'host': 'http://pc.example:11434/'}
        states = {n: account(n, 'ahead', room=0) for n in ('agy-gemini', 'agy-claude', 'cursor')}
        first = {**task('first'), 'scope': ['src/a.py']}
        second = {**task('second'), 'scope': ['src/b.py']}
        planned = autopilot.plan(cfg, [first, second], states, FREE, True)['dispatch']
        self.assertEqual(len(planned), 1)
        running = {**task('running', 'running'), 'step': 'pc'}
        self.assertEqual(autopilot.plan(cfg, [running, second], states, FREE, True)['dispatch'], [])
        cfg['workers']['pc2']['host'] = 'other.example:11434'
        self.assertEqual(len(autopilot.plan(cfg, [first, second], states, FREE, True)['dispatch']), 2)

    def test_online_reservation_returns_when_a_reconfigured_account_is_readded(self):
        cfg = self.cfg()
        online_account = cfg['accounts'].pop('cursor')
        online_worker = cfg['workers']['cursor']
        old = task('old', 'done', [{**attempt('cursor'), 'started': 1000}])
        cfg['workers']['cursor'] = {'kind': 'ollama', 'host': 'model.example:11434', 'model': 'm'}
        self.assertEqual(autopilot.spent_units([old], {}, cfg), {})
        cfg['accounts']['cursor'] = online_account
        cfg['workers']['cursor'] = online_worker
        self.assertEqual(autopilot.spent_units([old], {}, cfg), {'cursor': 1})

    def test_paused_accounts_unreported_attempt_still_debits_a_shared_window(self):
        cfg = self.cfg(main_accounts=['agy-claude'])
        cfg['_main_worker'] = 'local-main'
        cfg['workers']['local-main'] = {'kind': 'ollama', 'host': '127.0.0.1:1', 'model': 'm:3b'}
        cfg['_account_limits']['paused'] = ['weekly']
        cfg['_account_costs']['paused'] = 2
        weekly = {'name': 'weekly', 'used': 97, 'panic': True, 'reading_at': '2026-01-03T00:00:00Z'}
        states = {n: {**account(n, 'ahead', used=96, panic=True), 'reading_at': '2026-01-03T00:00:00Z',
                      'limits': [weekly]} for n in ('agy-claude', 'agy-gemini', 'cursor')}
        started = autopilot.datetime(2026, 1, 3, 0, 30, tzinfo=autopilot.timezone.utc).timestamp()
        now = autopilot.datetime(2026, 1, 3, 1, tzinfo=autopilot.timezone.utc)
        done = task('old', 'done', [{'step': 'paused', 'started': started}])
        self.assertEqual(autopilot.plan(cfg, [done, task('t1')], states, FREE, True, now=now)['dispatch'], [])
        weekly['reading_at'] = '2026-01-03T00:45:00Z'
        self.assertEqual(len(autopilot.plan(cfg, [done, task('t1')], states, FREE, True, now=now)['dispatch']), 1)

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

    def test_measured_ladder_account_takes_fresh_work_when_triggers_cannot(self):
        cfg = self.cfg(fresh_fallback_accounts=['cursor'])
        planned = {**account('cursor', 'on pace'), 'reading_at': '2026-01-01T00:00:00Z',
                   'quota_plan': {'tasks': 2, 'left': 4, 'limit': None}}
        states = self.states(**{'agy-gemini': account('agy-gemini', 'ahead'),
                               'agy-claude': account('agy-claude', 'ahead'), 'cursor': planned})
        cfg['autopilot']['fresh_fallback_accounts'] = []
        self.assertEqual(autopilot.plan(cfg, [task('t1')], states, FREE, True, now=TEST_NOW)['dispatch'], [])
        cfg['autopilot']['fresh_fallback_accounts'] = ['cursor']
        self.assertEqual(autopilot.plan(cfg, [task('t1', size='M')], states, FREE, True, now=TEST_NOW)['dispatch'], [('t1', 'cursor')])
        p = autopilot.plan(cfg, [task('t1'), task('t2')], states, FREE, True, now=TEST_NOW)
        self.assertEqual(p['dispatch'], [('t1', 'cursor')])
        running = {**task('t1', 'running'), 'step': 'cursor'}
        self.assertEqual(autopilot.plan(cfg, [running, task('t2')], states, FREE, True, now=TEST_NOW)['dispatch'], [])
        cfg['autopilot']['max_parallel'] = 1
        other_running = {**task('t0', 'running'), 'step': 'agy-gemini'}
        self.assertEqual(autopilot.plan(cfg, [other_running, task('t2')], states, FREE, True, now=TEST_NOW)['dispatch'], [])
        states['agy-gemini'] = account('agy-gemini')
        self.assertEqual(autopilot.plan(cfg, [task('t3')], states, FREE, True, now=TEST_NOW)['dispatch'], [('t3', 'agy-gemini')])

    def test_fresh_fallback_fits_task_size_under_strict_panic_bound(self):
        cfg = self.cfg(fresh_fallback_accounts=['cursor'])
        ready = {**account('cursor', 'on pace', room=9, used=90), 'reading_at': '2026-01-01T00:00:00Z',
                 'quota_plan': {'tasks': 9, 'left': 18, 'limit': None}}
        states = self.states(**{'agy-gemini': account('agy-gemini', 'ahead'),
                               'agy-claude': account('agy-claude', 'ahead'), 'cursor': ready})
        for size, expected in [('S', True), ('M', True), ('L', False)]:
            with self.subTest(size=size):
                plan = autopilot.plan(cfg, [task('t1', size=size)], states, FREE, True, now=TEST_NOW)
                self.assertEqual(bool(plan['dispatch']), expected)
        ready['used'] = 91   # two units would reach panic exactly
        self.assertEqual(autopilot.plan(cfg, [task('t1', size='M')], states, FREE, True, now=TEST_NOW)['dispatch'], [])
        ready['used'] = 90
        self.assertEqual(autopilot.plan(cfg, [task('t1'), task('t2')], states, FREE, True, now=TEST_NOW)['dispatch'],
                         [('t1', 'cursor')])
        running = {**task('t0', 'running'), 'step': 'cursor'}
        self.assertEqual(autopilot.plan(cfg, [running, task('t1')], states, FREE, True, now=TEST_NOW)['dispatch'], [])
        retry = task('retry', attempts=[attempt('agy-claude')])
        self.assertEqual(autopilot.plan(cfg, [retry, task('fresh')], states, FREE, True, now=TEST_NOW)['dispatch'],
                         [('retry', 'cursor')])
        cfg['accounts']['cursor']['task_pct'] = 0.1
        ready['used'] = 94.8
        self.assertEqual(autopilot.plan(cfg, [task('t1', size='M')], states, FREE, True, now=TEST_NOW)['dispatch'], [])
        self.assertEqual(autopilot.plan(cfg, [task('t1')], states, FREE, True, now=TEST_NOW)['dispatch'], [('t1', 'cursor')])
        ready['used'] = 94.8000000001
        self.assertEqual(autopilot.plan(cfg, [task('t1', size='M')], states, FREE, True, now=TEST_NOW)['dispatch'], [])

    def test_fresh_ladder_fallback_refuses_unknown_stale_shared_and_reserved_quota(self):
        cfg = self.cfg(fresh_fallback_accounts=['cursor'])
        ready = {**account('cursor', 'on pace'), 'reading_at': '2026-01-01T00:00:00Z',
                 'quota_plan': {'tasks': 2, 'left': 4, 'limit': None}}
        blocked = ({**ready, 'used': None}, {**ready, 'stale': True}, {**ready, 'after_reset': True},
                   {**ready, 'reader_problem': 'failed'}, {**ready, 'verdict': 'ahead'},
                   {**ready, 'panic': True}, {**ready, 'reading_at': '2026-01-02T00:00:00Z'},
                   {**ready, 'used': 94},
                   {**ready, 'quota_plan': {'tasks': 0, 'left': 0, 'limit': None}})
        for s in blocked:
            with self.subTest(state=s):
                states = self.states(**{'agy-gemini': account('agy-gemini', 'ahead'),
                                       'agy-claude': account('agy-claude', 'ahead'), 'cursor': s})
                self.assertEqual(autopilot.plan(cfg, [task('t1')], states, FREE, True, now=TEST_NOW)['dispatch'], [])
        states['cursor'] = ready
        cfg['_account_limits']['cursor'] = ['weekly']
        self.assertEqual(autopilot.plan(cfg, [task('t1')], states, FREE, True, now=TEST_NOW)['dispatch'], [])
        cfg['_account_limits']['cursor'] = []
        cfg['_account_limits']['other'] = ['cursor']
        self.assertEqual(autopilot.plan(cfg, [task('t1')], states, FREE, True, now=TEST_NOW)['dispatch'], [])
        cfg['_account_limits']['other'] = []
        cfg['disabled_accounts'] = ['cursor']
        self.assertEqual(autopilot.plan(cfg, [task('t1')], states, FREE, True, now=TEST_NOW)['dispatch'], [])

    def test_fallback_waits_until_completed_attempt_is_reflected_in_new_reading(self):
        cfg = self.cfg(fresh_fallback_accounts=['cursor'])
        s = {**account('cursor', 'behind', room=2), 'reading_at': '2026-01-01T00:00:00Z',
             'target': '2026-01-02T00:00:00Z', 'share': 1.0}
        s['quota_plan'] = planner.plan_single(s, cfg['accounts']['cursor'], TEST_NOW)
        states = self.states(**{'agy-gemini': account('agy-gemini', 'ahead'),
                               'agy-claude': account('agy-claude', 'ahead'), 'cursor': s})
        self.assertGreater(s['quota_plan']['tasks'], 0)
        self.assertEqual(autopilot.plan(cfg, [task('t1')], states, FREE, True, now=TEST_NOW)['dispatch'], [('t1', 'cursor')])
        recent = {**attempt('cursor', ok=True), 'started': autopilot._ts('2026-01-01T00:30:00Z')}
        finished = {**task('t0', 'review'), 'attempts': [recent]}
        self.assertEqual(autopilot.plan(cfg, [finished, task('t1')], states, FREE, True, now=TEST_NOW)['dispatch'], [])
        s['reading_at'] = '2026-01-01T00:45:00Z'
        self.assertEqual(autopilot.plan(cfg, [finished, task('t1')], states, FREE, True, now=TEST_NOW)['dispatch'], [('t1', 'cursor')])

    def test_real_planner_room_allows_sized_fallback_after_attempt_is_reflected(self):
        cfg = self.cfg(fresh_fallback_accounts=['cursor'])
        s = {**account('cursor', 'behind', room=9), 'reading_at': '2026-01-01T00:00:00Z',
             'target': '2026-01-02T00:00:00Z', 'share': 1.0}
        s['quota_plan'] = planner.plan_single(s, cfg['accounts']['cursor'], TEST_NOW)
        states = self.states(**{'agy-gemini': account('agy-gemini', 'ahead'),
                               'agy-claude': account('agy-claude', 'ahead'), 'cursor': s})
        self.assertGreaterEqual(s['quota_plan']['tasks'], 3)
        recent = {**attempt('cursor', ok=True), 'started': autopilot._ts('2026-01-01T00:30:00Z')}
        finished = {**task('t0', 'review'), 'attempts': [recent]}
        for size in ('M', 'L'):
            with self.subTest(size=size):
                self.assertEqual(autopilot.plan(cfg, [task('t1', size=size)], states, FREE, True, now=TEST_NOW)['dispatch'],
                                 [('t1', 'cursor')])
                self.assertEqual(autopilot.plan(cfg, [finished, task('t1', size=size)], states, FREE, True,
                                                now=TEST_NOW)['dispatch'], [])
                s['reading_at'] = '2026-01-01T00:45:00Z'
                self.assertEqual(autopilot.plan(cfg, [finished, task('t1', size=size)], states, FREE, True,
                                                now=TEST_NOW)['dispatch'], [('t1', 'cursor')])
                s['reading_at'] = '2026-01-01T00:00:00Z'

    def test_fallback_config_rejects_unknown_account_with_action(self):
        cfg = self.cfg(fresh_fallback_accounts=['not-configured'])
        errors = '\n'.join(config.validate(cfg))
        self.assertIn('autopilot.fresh_fallback_accounts', errors)
        self.assertIn('accounts:', errors)
        self.assertIn('use []', errors)

    def test_paused_opted_in_account_is_filtered_without_config_error(self):
        cfg = self.cfg(fresh_fallback_accounts=['cursor'])
        cfg['accounts']['cursor']['paused'] = True
        config.opt_out(cfg)
        self.assertEqual(cfg['autopilot']['fresh_fallback_accounts'], [])
        self.assertNotIn('fresh_fallback_accounts', '\n'.join(config.validate(cfg)))

    def test_fallback_allowance_does_not_reduce_retry_takeover_room(self):
        cfg = self.cfg(fresh_fallback_accounts=['cursor'], main_accounts=['agy-claude'], ladder=['agy-claude', 'cursor'])
        cfg['_main_worker'] = 'agy'
        cursor = {**account('cursor', 'behind', room=5), 'reading_at': '2026-01-01T00:00:00Z',
                  'quota_plan': {'tasks': 1, 'left': 2, 'limit': None}}
        states = self.states(**{'agy-claude': account('agy-claude', 'ahead', used=98, panic=True),
                               'agy-gemini': account('agy-gemini', 'ahead', used=98, panic=True), 'cursor': cursor})
        retry = task('t1', size='M', attempts=[attempt('agy-claude')])
        self.assertEqual(autopilot.plan(cfg, [retry], states, FREE, True, now=TEST_NOW)['dispatch'], [('t1', 'cursor')])
        second = task('t2', size='M', attempts=[attempt('agy-claude')])
        self.assertEqual(autopilot.plan(cfg, [retry, second], states, FREE, True, now=TEST_NOW)['dispatch'],
                         [('t1', 'cursor'), ('t2', 'cursor')])
        self.assertEqual(autopilot.plan(cfg, [retry, task('fresh')], states, FREE, True, now=TEST_NOW)['dispatch'],
                         [('t1', 'cursor'), ('fresh', 'cursor')])

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

    def test_environment_failures_on_every_step_do_not_exhaust_the_ladder(self):
        cfg = self.cfg(ladder=['agy-gemini', 'agy-claude'], max_attempts=2, no_progress=2)
        for why in ('the run ended without a report', "the worker's CLI refused a command: permission denied",
                    'the worker hit its usage limit'):
            tries = [{**attempt(step, 'env'), 'why': why} for step in
                     ('agy-gemini', 'agy-gemini', 'agy-claude', 'agy-claude')]
            with self.subTest(why=why):
                self.assertEqual(autopilot.worn_out(tries), set())
                t = task('t1', attempts=tries)
                self.assertEqual(autopilot.park_reason(cfg, t), ('', ''))
                self.assertEqual(set(autopilot.candidates(cfg, t)), {'agy-gemini', 'agy-claude'})
                self.assertEqual(autopilot.after_failure(cfg, t, now=1000), '')
                self.assertEqual(t['status'], 'queued')
                self.assertEqual(t['not_before'], 1240)  # 4 failures: 4-minute backoff
                self.assertEqual(autopilot.backoff(task('t1', attempts=tries * 10)), autopilot.BACKOFF_MAX)
        real = [attempt(step, 'real') for step in ('agy-gemini', 'agy-gemini', 'agy-claude', 'agy-claude')]
        self.assertTrue(autopilot.should_park(cfg, task('t1', attempts=real)))

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
        with mock.patch('sys.stdout'), mock.patch('sys.stderr'), \
                mock.patch.dict(os.environ, {'TUTTI_RUN_IDENTITY': kw['env'].get('TUTTI_RUN_IDENTITY', '')}):
            cli.main(argv[2:])
        return SimpleNamespace(pid=os.getpid())

    def tick(self):
        with mock.patch.object(config, 'load', return_value=self.c):
            return autopilot.tick(self.c, self.states, [], is_alive=lambda pid: False, spawn=self.run_now)

    def test_two_round_scoped_household_reservation_uses_one_global_local_slot(self):
        cfg = self.c
        cfg['_sharing_machine_at'] = TEST_NOW.timestamp()
        cfg['machines'] = {'nuc': {'host': 'nuc.example'}, 'nuc2': {'host': 'nuc2.example'}}
        cfg['workers']['pc'] = {'kind': 'ollama', 'host': 'pc.example:11434', 'model': 'qwen2.5-coder:7b', 'machine': 'nuc'}
        cfg['workers']['pc2'] = {**cfg['workers']['pc'], 'host': 'pc2.example:11434', 'machine': 'nuc2'}
        cfg['autopilot']['ladder'] = ['fake-a', 'pc', 'pc2']
        cfg['autopilot']['repos']['app']['household'] = True
        cfg['autopilot']['repos']['other'] = {**cfg['autopilot']['repos']['app']}
        states = {'fake-a': account('fake-a', 'ahead')}
        now = TEST_NOW.timestamp()
        remote = household.new_state('home', now)
        household.sync_project(remote, 'github.com/example/other', {'publish': [
            {'title': 'Fix other', 'scope': ['b.py'], 'size': 'S'}]}, 'peer', now, 30)
        with backlog.locked(cfg) as data:
            backlog.add(data, backlog.new_task('app', 'Fix mine', ['a.py'], 'true', 'S', 'user'))
        requests = []
        elapsed = {'seconds': 0, 'delay': False}
        class Coordinator:
            def call(self, path, payload):
                requests.append(payload)
                if elapsed['delay']:
                    elapsed['seconds'] += 61
                return {'projects': {key: household.sync_project(remote, key, ask, 'self', now + len(requests), 30)
                                     for key, ask in payload['projects'].items()}}
        model = fleet.classify({'name': 'qwen2.5-coder:7b'})
        inventory = {'machines': {'nuc': {'stale': True, 'updated': now, 'tags_endpoint': 'pc.example:11434', 'models': [model]},
                                  'nuc2': {'stale': False, 'updated': now, 'tags_endpoint': 'pc2.example:11434', 'models': [model]}}}
        machines = [*FREE, {'name': 'nuc2', 'status': 'free', 'free': 1}]
        with mock.patch.object(sharing.coordinator, 'client_of', return_value=Coordinator()), \
             mock.patch.object(sharing, 'key_of_repo', side_effect=lambda _cfg, name, _run=None: f'github.com/example/{name}'), \
             mock.patch.object(fleet, 'read_state', return_value=inventory), \
             mock.patch.object(sharing, 'renew', return_value=True):
            def clock():
                return elapsed['seconds']
            first, _ = autopilot.tick(cfg, states, machines, now=now, beat={}, spawn=lambda *a, **k: SimpleNamespace(pid=1234),
                                      monotonic=clock)
            mirrored = backlog.load(cfg)['tasks']
            self.assertEqual({t['title'] for t in mirrored}, {'Fix mine', 'Fix other'})
            self.assertTrue(all(re.fullmatch(r'[0-9a-f]{32}', t.get('household_instance') or '') for t in mirrored))
            elapsed['delay'] = True
            delayed, _ = autopilot.tick(cfg, states, machines, now=now + 1, beat={}, spawn=lambda *a, **k: SimpleNamespace(pid=1234),
                                        monotonic=clock)
            elapsed['delay'] = False
            second, _ = autopilot.tick(cfg, states, machines, now=now + 2, beat={}, spawn=lambda *a, **k: SimpleNamespace(pid=1234),
                                       monotonic=clock)
        self.assertEqual(first['dispatch'], [])
        self.assertEqual(sum(len(p['want_scoped']) for p in requests[0]['projects'].values()), 0)
        self.assertEqual(sum(len(p['want_scoped']) for p in requests[1]['projects'].values()), 1)
        self.assertEqual(delayed['dispatch'], [])   # a slow coordinator made the machine/model observations too old
        self.assertEqual(len(second['dispatch']), 1)
        self.assertEqual(second['dispatch'][0][1], 'pc2')

    def test_old_coordinator_cannot_authorize_local_dispatch_even_with_an_old_lease(self):
        cfg = self.c
        cfg['_sharing_machine_at'] = TEST_NOW.timestamp()
        cfg['machines'] = {'nuc': {'host': 'nuc.example'}}
        cfg['workers']['pc'] = {'kind': 'ollama', 'host': 'pc.example:11434', 'model': 'qwen2.5-coder:7b', 'machine': 'nuc'}
        cfg['autopilot']['ladder'] = ['fake-a', 'pc']
        cfg['autopilot']['repos']['app']['household'] = True
        states, now = {'fake-a': account('fake-a', 'ahead')}, TEST_NOW.timestamp()
        remote = household.new_state('home', now)
        key = 'github.com/example/app'
        household.sync_project(remote, key, {'publish': [{'title': 'Fix peer', 'scope': ['a.py'], 'size': 'S'}]}, 'peer', now, 30)
        calls = []
        class Coordinator:
            def call(self, path, payload):
                calls.append(payload)
                project_ask = dict(payload['projects'][key])
                project_ask.pop('want_scoped', None)   # an old coordinator ignores the extension
                answer = household.sync_project(remote, key, project_ask, 'self', now + len(calls), 30)
                answer.pop('scoped_claims', None)
                return {'projects': {key: answer}}
        model = fleet.classify({'name': 'qwen2.5-coder:7b'})
        inventory = {'machines': {'nuc': {'stale': False, 'updated': now, 'tags_endpoint': 'pc.example:11434', 'models': [model]}}}
        with mock.patch.object(sharing.coordinator, 'client_of', return_value=Coordinator()), \
             mock.patch.object(sharing, 'key_of_repo', return_value=key), \
             mock.patch.object(fleet, 'read_state', return_value=inventory):
            autopilot.tick(cfg, states, FREE, now=now, beat={})
            household.claim(remote, household.lease_key(key, 'fix peer'), 'self', now + 1, 5)
            p, _ = autopilot.tick(cfg, states, FREE, now=now + 2, beat={}, spawn=lambda *a, **k: SimpleNamespace(pid=1234))
        self.assertEqual(len(calls[1]['projects'][key]['want_scoped']), 1)
        self.assertEqual(p['dispatch'], [])

    def test_each_scoped_start_rechecks_after_a_slow_earlier_start(self):
        cfg, now = self.c, TEST_NOW.timestamp()
        cfg['_sharing_machine_at'] = now
        cfg['machines'] = {'nuc': {'host': 'nuc.example'}}
        cfg['workers']['pc'] = {'kind': 'ollama', 'host': 'one.example:11434', 'model': 'qwen2.5-coder:7b', 'machine': 'nuc'}
        cfg['workers']['pc2'] = {**cfg['workers']['pc'], 'host': 'two.example:11434'}
        cfg['autopilot']['ladder'] = ['pc', 'pc2']
        with backlog.locked(cfg) as data:
            a = backlog.add(data, backlog.new_task('app', 'First', ['a.py'], 'true', 'S', 'household'))[0]
            b = backlog.add(data, backlog.new_task('app', 'Second', ['b.py'], 'true', 'S', 'household'))[0]
        choices, claims = {}, []
        for item, worker, nonce in ((a, 'pc', 'a' * 32), (b, 'pc2', 'b' * 32)):
            request = {'key': household.fold(item['title']), 'instance': 'c' * 32, 'nonce': nonce}
            choices[item['id']] = {'task': dict(item), 'worker': worker, 'model': 'qwen2.5-coder:7b', 'request': request}
            claims.append({**request, 'lease_seconds': 300})
        shared = {'keys': {'app': 'project'}, 'scoped': choices, 'error': None,
                  'answer': {'projects': {'project': {'scoped_claims': claims}}}}
        selected = [(a['id'], 'pc'), (b['id'], 'pc2')]
        elapsed = {'seconds': 0}
        def clock():
            return elapsed['seconds']
        def start(_cfg, _data, item, _step, _now, _spawn, _states):
            elapsed['seconds'] += 61
            return '1-1', f'{item["id"]} started'
        model = fleet.classify({'name': 'qwen2.5-coder:7b'})
        inventory = {'machines': {'nuc': {'stale': False, 'updated': now, 'tags_endpoint': 'one.example:11434', 'models': [model]}}}
        plan = {'state': 'dispatching', 'reason': 'two tasks', 'dispatch': selected, 'waiting': {}}
        with mock.patch.object(autopilot, 'refresh_sharing_capacity'), mock.patch.object(sharing, 'sync', return_value=shared), \
             mock.patch.object(sharing, 'under_lock', return_value=({}, [])), \
             mock.patch.object(sharing, 'moved_of', return_value=({}, {}, set())), \
             mock.patch.object(sharing, 'renew', return_value=True), mock.patch.object(autopilot, 'plan', return_value=plan), \
             mock.patch.object(autopilot, 'start_reserved', side_effect=start) as start_mock, \
             mock.patch.object(autopilot, 'fresh_local_model', return_value=True), \
             mock.patch.object(fleet, 'read_state', return_value=inventory):
            result, events = autopilot.tick(cfg, {}, FREE, now=now, beat={}, monotonic=clock)
        self.assertEqual(start_mock.call_count, 1)
        self.assertEqual(result['dispatch'], [selected[0]])
        self.assertIn(b['id'], result['waiting'])
        self.assertTrue(any(b['id'] in event and 'changed' in event for event in events))

    def test_ahead_account_is_rechecked_and_dispatches_on_a_later_round(self):
        self.c['autopilot'].update(ladder=['fake-b'], accounts=['fake-b'])
        self.c['workers'].pop('pc')
        self.states['fake-b'] = account('fake-b', 'ahead', used=30)
        with backlog.locked(self.c) as data:
            backlog.add(data, backlog.new_task('app', 'Mark a.txt fixed', ['a.txt'], ''))
        first, _ = self.tick()
        self.assertEqual(first['dispatch'], [])
        self.assertIn('next successful active dashboard round', first['reason'])
        self.assertEqual(backlog.load(self.c)['tasks'][0]['status'], 'queued')
        self.states['fake-b'] = account('fake-b', 'behind', used=30)
        second, _ = self.tick()
        self.assertEqual(second['dispatch'], [('t1', 'fake-b')], second)

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

    def test_a_run_without_a_report_is_held_instead_of_retried(self):
        with backlog.locked(self.c) as data:
            backlog.add(data, backlog.new_task('app', 'x', [], 'true'))
        with mock.patch.object(config, 'load', return_value=self.c):
            autopilot.tick(self.c, self.states, [], is_alive=lambda pid: True, spawn=lambda *a, **k: SimpleNamespace(pid=1))
            _, events = autopilot.tick(self.c, self.states, [], is_alive=lambda pid: True, spawn=lambda *a, **k: SimpleNamespace(pid=1))
        self.assertEqual(events, [])                       # still running
        self.states['fake-a'] = account('fake-a', 'on pace')
        self.states['fake-b'] = account('fake-b', 'ahead')
        _, events = autopilot.tick(self.c, self.states, [], is_alive=lambda pid: False)
        self.assertEqual(events, [])
        held = backlog.load(self.c)['tasks'][0]
        self.assertEqual((held['status'], held['attempts']), ('running', []))
        self.assertIn('without a report', held['recovery_note'])


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

    def test_launch_intent_is_durable_before_child_start_and_survives_crash(self):
        cfg = self.cfg(ladder=['agy-gemini'], accounts=['agy-gemini'])
        cfg['machines'] = {}
        with backlog.locked(cfg) as data:
            backlog.add(data, backlog.new_task('app', 'x', [], 'true'))
        def crash(c, task, step, now, spawn, states, run_id=None):
            saved = backlog.load(c)['tasks'][0]
            self.assertEqual((saved['status'], saved['launch_pending'], saved['run']), ('running', True, run_id))
            self.assertRegex(saved['run_identity'], r'^[0-9a-f]{32}$')
            raise RuntimeError('simulated process loss after durable intent')
        with mock.patch.object(autopilot, 'dispatch', side_effect=crash), self.assertRaisesRegex(RuntimeError, 'simulated'):
            autopilot.tick(cfg, {'agy-gemini': account('agy-gemini')}, [])
        saved = backlog.load(cfg)['tasks'][0]
        self.assertTrue(saved['launch_pending'])
        self.assertEqual(saved['status'], 'running')
        _, events = autopilot.tick(cfg, {'agy-gemini': account('agy-gemini')}, [], is_alive=lambda p: False)
        saved = backlog.load(cfg)['tasks'][0]
        self.assertEqual((saved['status'], saved['attempts']), ('running', []))
        self.assertIn('unverified', saved['recovery_note'])
        self.assertFalse(any('sent to' in event for event in events))

    def test_persisted_run_identity_reaches_spawned_worker(self):
        cfg = self.cfg(ladder=['agy-gemini'], accounts=['agy-gemini'])
        cfg['machines'] = {}
        with backlog.locked(cfg) as data:
            backlog.add(data, backlog.new_task('app', 'x', [], 'true'))
        seen = []
        def fake_spawn(*args, **kwargs):
            seen.append(kwargs['env']['TUTTI_RUN_IDENTITY'])
            return SimpleNamespace(pid=123)
        autopilot.tick(cfg, {'agy-gemini': account('agy-gemini')}, [], spawn=fake_spawn)
        saved = backlog.load(cfg)['tasks'][0]
        self.assertEqual(seen, [saved['run_identity']])
        self.assertRegex(saved['run_identity'], r'^[0-9a-f]{32}$')

    def test_unknown_post_launch_failure_is_held_without_retry(self):
        cfg = self.cfg(ladder=['agy-gemini'], accounts=['agy-gemini'])
        cfg['machines'] = {}
        with backlog.locked(cfg) as data:
            backlog.add(data, backlog.new_task('app', 'x', [], 'true'))
        with mock.patch.object(autopilot, 'dispatch', side_effect=OSError('pid file unavailable')):
            _, events = autopilot.tick(cfg, {'agy-gemini': account('agy-gemini')}, [])
        saved = backlog.load(cfg)['tasks'][0]
        self.assertEqual((saved['status'], saved['launch_pending'], saved['attempts']), ('running', True, []))
        self.assertIn('unverified', saved['recovery_note'])
        self.assertIn('held', events[0])
        self.assertEqual(autopilot.task_view(cfg, saved, saved['started'])['recovery_note'], saved['recovery_note'])

    def test_a_late_report_recovers_the_saved_launch_and_keeps_its_gates(self):
        cfg = self.cfg(ladder=['agy-gemini'], accounts=['agy-gemini'])
        cfg['machines'] = {}
        cfg['autopilot']['repos']['app']['check'] = 'example-check'
        t = task('t1')
        autopilot.reserve_launch(cfg, t, 'agy-gemini', 100)
        self.assertEqual((t['gates'], t['job_bridge']), ('example-check', ''))
        t['recovery_note'] = 'launch outcome is unverified'
        report = {'id': t['run'], 'run_identity': t['run_identity'], 'rc': 0, 'worker_rc': 0, 'checks': [], 'changed': True}
        observed = []
        def attempt(c, saved, r, now):
            observed.append(saved['gates'])
            return {'ok': True, 'step': 'agy-gemini'}
        with mock.patch.object(autopilot, 'finished', return_value=(True, report)), \
                mock.patch.object(autopilot, 'attempt_of', side_effect=attempt):
            autopilot.collect(cfg, {'tasks': [t]}, 120, lambda p: False, publish_fn=lambda c, task, r: None)
        self.assertEqual((t['status'], observed), ('review', ['example-check']))
        self.assertNotIn('launch_pending', t)
        self.assertNotIn('recovery_note', t)

    def test_collect_preserves_other_recovery_notes(self):
        cfg = self.cfg()
        t = task('t1', status='running')
        t.update(run='1-1', started=100, recovery_note='separate recovery state')
        with mock.patch.object(autopilot, 'finished', return_value=(False, None)), \
                mock.patch.object(autopilot, 'stuck', return_value=''):
            autopilot.collect(cfg, {'tasks': [t]}, 120, lambda p: True)
        self.assertEqual(t['recovery_note'], 'separate recovery state')

    def test_report_identity_mismatch_is_held_and_matching_result_can_complete(self):
        cfg = self.cfg()
        identity = 'a' * 32
        t = task('t1', status='running')
        t.update(run='1700000000-1', run_identity=identity, report_identity_bound=True,
                 step='agy-gemini', started=100, launch_pending=True)
        result = {'id': t['run'], 'run_identity': 'b' * 32, 'rc': 0}
        published = []
        with mock.patch.object(autopilot, 'finished', return_value=(True, result)):
            self.assertEqual(autopilot.collect(cfg, {'tasks': [t]}, 120, lambda p: True,
                                               publish_fn=lambda c, t, r: published.append(r)), [])
        self.assertEqual((t['status'], t['attempts'], published), ('running', [], []))
        self.assertIn('identity does not match', t['recovery_note'])
        result['run_identity'] = identity
        with mock.patch.object(autopilot, 'finished', return_value=(True, result)), \
                mock.patch.object(autopilot, 'attempt_of', return_value={'ok': True, 'step': 'agy-gemini'}):
            autopilot.collect(cfg, {'tasks': [t]}, 130, lambda p: True, publish_fn=lambda c, t, r: published.append(r))
        self.assertEqual((t['status'], published), ('review', [result]))
        self.assertNotIn('recovery_note', t)

    def test_report_from_a_different_run_is_never_accepted(self):
        t = task('t1', status='running')
        t.update(run='1700000000-1', run_identity='a' * 32, report_identity_bound=True)
        self.assertFalse(autopilot.report_identity_matches(t, {'id': '1700000000-2', 'run_identity': 'a' * 32}))
        for invalid in ('', None, 'bad'):
            t['run_identity'] = invalid
            self.assertFalse(autopilot.report_identity_matches(t, {'id': t['run'], 'run_identity': invalid}))
        legacy = task('t2', status='running')
        legacy['run_identity'] = 'a' * 32  # pre-upgrade attempts had an identity but no bound-report protocol
        self.assertTrue(autopilot.report_identity_matches(legacy, {'id': 'legacy'}))
        t.update(run_identity='a' * 32, report_identity_bound=False)
        self.assertFalse(autopilot.report_identity_matches(t, {'id': t['run'], 'run_identity': 'a' * 32}))

    def test_unready_bridge_holds_new_work_without_overwriting_other_holds(self):
        pending = [task('t1'), task('t2'), task('t3', status='running')]
        hold = {'t1': 'other reason'}
        autopilot.hold_unready_bridge(hold, pending, 'update client')
        self.assertEqual(hold, {'t1': 'other reason', 't2': 'update client'})
        self.assertEqual(autopilot.hold_unready_bridge({}, pending, ''), None)

    def test_old_bridge_client_defers_queued_work_before_spawn(self):
        cfg = self.cfg(ladder=['agy-gemini'], accounts=['agy-gemini'])
        cfg['machines'] = {}
        cfg['host'] = {'me': 'host', 'jobs': 'client'}
        with backlog.locked(cfg) as data:
            backlog.add(data, backlog.new_task('app', 'x', [], 'true'))
        with mock.patch('tutti.bridge.call', return_value=(0, {'version': 'old'})), \
                mock.patch('tutti.autopilot.dispatch') as spawn:
            plan, _ = autopilot.tick(cfg, {'agy-gemini': account('agy-gemini')}, [])
        spawn.assert_not_called()
        self.assertEqual(backlog.load(cfg)['tasks'][0]['status'], 'queued')
        self.assertIn('update and restart', plan['waiting']['t1'])

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

    def test_off_collector_checkpoints_and_holds_reports_without_starting_or_stopping_work(self):
        cfg = self.cfg(enabled=False)
        one, two, three = (task('t1', 'running'), task('t2', 'running'), task('t3', 'running'))
        one['run'], two['run'], three['run'] = '1-1', '2-2', '3-3'
        three['recovery_note'] = 'existing recovery state'
        with backlog.locked(cfg) as data:
            data['tasks'] = [one, two, three]
        collector = dashboard.Collector(cfg, probe_fn=None, available_fn=None, reader=mock.Mock(), activity_fn=None, prs_fn=None)
        with mock.patch('tutti.failover.may_write', return_value=True), \
                mock.patch.object(autopilot, 'checkpoints') as checkpoint, \
                mock.patch.object(delegate, 'load_report', side_effect=lambda _cfg, run: {'id': run} if run != '2-2' else None), \
                mock.patch.object(autopilot, 'collect', side_effect=AssertionError('must not collect')), \
                mock.patch.object(autopilot, 'stuck', side_effect=AssertionError('must not stop a run')), \
                mock.patch.object(autopilot, 'tick', side_effect=AssertionError('must not dispatch')), \
                mock.patch('tutti.workq.tick', side_effect=AssertionError('must not start workq')):
            self.assertIsNone(collector.autopilot_tick())
        checkpoint.assert_called_once()
        tasks = backlog.load(cfg)['tasks']
        self.assertEqual([t['status'] for t in tasks], ['running', 'running', 'running'])
        self.assertEqual(tasks[0]['recovery_note'], autopilot.PAUSED_REPORT_HOLD)
        self.assertNotIn('recovery_note', tasks[1])
        self.assertEqual(tasks[2]['recovery_note'], 'existing recovery state')

    def test_off_collector_does_not_checkpoint_when_not_the_failover_writer(self):
        cfg = self.cfg(enabled=False)
        collector = dashboard.Collector(cfg, probe_fn=None, available_fn=None, reader=mock.Mock(), activity_fn=None, prs_fn=None)
        for checks in ([False], [True, False]):
            with self.subTest(checks=checks), mock.patch('tutti.failover.may_write', side_effect=checks), \
                    mock.patch.object(autopilot, 'checkpoints') as checkpoint:
                self.assertIsNone(collector.autopilot_tick())
            checkpoint.assert_not_called()

    def test_off_collector_holds_missing_and_invalid_run_ids(self):
        cfg = self.cfg(enabled=False)
        one, two = task('t1', 'running'), task('t2', 'running')
        two['run'] = '../escape'
        with backlog.locked(cfg) as data:
            data['tasks'] = [one, two]
        collector = dashboard.Collector(cfg, probe_fn=None, available_fn=None, reader=mock.Mock(), activity_fn=None, prs_fn=None)
        with mock.patch('tutti.failover.may_write', return_value=True), mock.patch.object(autopilot, 'checkpoints'), \
                mock.patch.object(autopilot, 'stuck', side_effect=AssertionError('must not stop a run')):
            self.assertIsNone(collector.autopilot_tick())
        saved = backlog.load(cfg)['tasks']
        self.assertEqual([t['status'] for t in saved], ['running', 'running'])
        self.assertTrue(all('recovery_note' not in t for t in saved))

    def test_resuming_collection_clears_only_its_own_off_mode_note(self):
        cfg = self.cfg()
        running = task('t1', 'running')
        running.update(run='1-1', recovery_note=autopilot.PAUSED_REPORT_HOLD)
        with mock.patch.object(autopilot, 'finished', return_value=(False, None)), \
                mock.patch.object(autopilot, 'stuck', return_value=''):
            autopilot.collect(cfg, {'tasks': [running]}, 100, lambda _pid: True)
        self.assertEqual(running['status'], 'running')
        self.assertNotIn('recovery_note', running)

    def test_collector_remeasures_host_and_disk_on_each_autopilot_round(self):
        cfg = self.cfg()
        collector = dashboard.Collector(cfg, probe_fn=None, available_fn=None, reader=mock.Mock(), activity_fn=None, prs_fn=None)
        samples = [{'mem_gb': 32, 'cores': 4, 'load': 8}, {'mem_gb': 32, 'cores': 4, 'load': 0},
                   {'mem_gb': 32, 'cores': 4, 'load': 7}]
        rounds, tick = [], autopilot.tick
        def capture(round_cfg, *args, **kwargs):
            result = tick(round_cfg, *args, **kwargs)
            rounds.append(round_cfg)
            return result
        with mock.patch('tutti.parallel.host', side_effect=samples), \
             mock.patch('tutti.diskroom.problems', side_effect=[['disk low'], ['disk low'], [], [], ['disk low'], ['disk low']]), \
             mock.patch.object(autopilot, 'tick', side_effect=capture), mock.patch('tutti.workq.tick'):
            for sample, disk in zip(samples, (['disk low'], [], ['disk low']), strict=True):
                collector.autopilot_tick()
                current = rounds[-1]
                self.assertEqual(current['_host_raw'], sample)
                self.assertEqual(current['_disk'], disk)
                capacity, blocked = autopilot.capacity_now(current, [], FREE, TEST_NOW, room=3)
                if disk:
                    self.assertEqual(capacity, (0, 0))
                    self.assertIn('disk low', blocked['reason'])
                else:
                    self.assertGreater(capacity[0], 0)
                    self.assertIsNone(blocked)
        self.assertEqual(cfg['_disk'], [])  # the shared config and its other readers were not mutated

    def test_collector_refreshes_cpu_bound_even_when_disk_stays_healthy(self):
        cfg = self.cfg()
        collector = dashboard.Collector(cfg, probe_fn=None, available_fn=None, reader=mock.Mock(), activity_fn=None, prs_fn=None)
        loads, bounds, tick = (8, 0, 8), [], autopilot.tick
        def capture(round_cfg, *args, **kwargs):
            result = tick(round_cfg, *args, **kwargs)
            autopilot.capacity_now(round_cfg, [], FREE, TEST_NOW, room=3)
            bounds.append(round_cfg['_parallel']['bounds']['cpu'])
            return result
        with mock.patch('tutti.parallel.host', side_effect=[{'mem_gb': 32, 'cores': 4, 'load': n} for n in loads]), \
             mock.patch('tutti.diskroom.problems', return_value=[]), \
             mock.patch.object(autopilot, 'tick', side_effect=capture), mock.patch('tutti.workq.tick'):
            for _ in loads:
                collector.autopilot_tick()
        self.assertEqual(bounds, [0, 16, 0])

    def test_collector_retains_previous_parallel_limit_without_mutating_shared_config(self):
        cfg = self.cfg()
        collector = dashboard.Collector(cfg, probe_fn=None, available_fn=None, reader=mock.Mock(), activity_fn=None, prs_fn=None)
        previous = []
        def tick(round_cfg, *_args, **_kwargs):
            previous.append(round_cfg.get('_parallel_limit'))
            round_cfg['_parallel_limit'] = 7
            return None
        with mock.patch.object(autopilot, 'tick', side_effect=tick), mock.patch('tutti.workq.tick'):
            collector.autopilot_tick()
            collector.autopilot_tick()
        self.assertEqual(previous, [None, 7])
        self.assertNotIn('_parallel_limit', cfg)

    def test_config_reload_clears_previous_autopilot_parallel_limit(self):
        cfg = self.cfg()
        collector = dashboard.Collector(cfg, probe_fn=None, available_fn=None, reader=mock.Mock(), activity_fn=None, prs_fn=None)
        collector.autopilot_parallel_limit = 7
        fresh = self.cfg()
        with mock.patch.object(config, 'load', return_value=fresh):
            collector.reload()
        self.assertIs(collector.cfg, fresh)
        self.assertIsNone(collector.autopilot_parallel_limit)

    def test_collector_does_not_reuse_cached_measurement_when_host_probe_fails(self):
        cfg = self.cfg()
        collector = dashboard.Collector(cfg, probe_fn=None, available_fn=None, reader=mock.Mock(), activity_fn=None, prs_fn=None)
        with mock.patch('tutti.parallel.host', side_effect=OSError('probe failed')), \
             mock.patch('tutti.diskroom.problems', return_value=[]), mock.patch('tutti.workq.tick') as workq:
            with self.assertRaisesRegex(OSError, 'probe failed'):
                collector.autopilot_tick()
        workq.assert_not_called()

    def test_backlog_cli_omitted_check_uses_default_without_making_a_duplicate(self):
        cfg = self.cfg()
        with mock.patch.object(config, 'load', return_value=cfg), mock.patch('sys.stdout'):
            self.assertEqual(cli.main(['backlog', 'add', 'app', 'Fix check', '--check', 'test-a']), 0)
            self.assertEqual(cli.main(['backlog', 'add', 'app', 'Fix check']), 0)
            self.assertEqual(cli.main(['backlog', 'add', 'app', 'Fix check', '--check', 'test-b']), 0)
            self.assertEqual(cli.main(['backlog', 'add', 'app', 'Default check']), 0)
            self.assertEqual(cli.main(['backlog', 'add', 'app', 'Default check', '--check', 'test-c']), 0)
        tasks = backlog.load(cfg)['tasks']
        self.assertEqual([t['check'] for t in tasks], ['test-a', 'test-b', 'true'])
        self.assertFalse(tasks[-1]['check_explicit'])

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
            cfg = {**cfg, '_now': TEST_NOW, '_main_worker': 'local', 'workers': {**cfg['workers'], 'local': {'kind': 'ollama'}}}
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

    def test_other_account_linked_to_cursor_gets_room_when_main_panics(self):
        from tutti import autopilot
        cfg = {'accounts': {'claude': {'worker': 'claude'}, 'cursor-other': {'worker': 'cursor'}},
               'workers': {'claude': {'kind': 'cli'}, 'cursor': {'kind': 'cli'}}, 'autopilot': {}}
        states = {'claude': {'panic': True, 'used': 96}, 'cursor-other': {'used': 16.7, 'verdict': 'on pace'}}
        with mock.patch.object(autopilot, 'costs_money', return_value=False), \
                mock.patch.object(autopilot, 'trigger_accounts', return_value=[]):
            room, _ = autopilot.step_room(cfg, 'cursor-other', states, {})
            self.assertGreater(room, 0)
            states['claude'] = {'used': 50, 'verdict': 'ahead'}
            states['cursor-other']['verdict'] = 'ahead'
            self.assertEqual(autopilot.step_room(cfg, 'cursor-other', states, {})[0], 0)

    def test_it_fills_up_to_its_own_panic_line(self):
        from tutti import autopilot
        acc = {'accounts': {'cursor': {'task_pct': 2}}}
        self.assertEqual(autopilot.takeover_room(acc, 'cursor', {'used': 35, 'panic_at': 95}), 30)
        self.assertEqual(autopilot.takeover_room(acc, 'cursor', {'used': 94, 'panic_at': 95}), 1)
        self.assertEqual(autopilot.takeover_room(acc, 'cursor', {'used': 95, 'panic_at': 95}), 0)
        self.assertEqual(autopilot.takeover_room(acc, 'cursor', {'used': None, 'panic_at': 95}), 0)

    def test_takeover_does_not_assume_unknown_account_or_linked_usage_is_zero(self):
        main = {'panic': True, 'used': 96}
        for current in ({'verdict': 'on pace', 'used': None},
                        {'verdict': 'on pace', 'used': 20, 'limits': [{'name': 'weekly', 'used': None}]}):
            self.assertEqual(self.room({'claude': main, 'cursor': current})[0], 0)

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
        cfg = {**self.CFG, '_now': TEST_NOW, '_main_worker': 'claude'}
        states = {'claude': {'panic': True, 'used': 96, 'reading_at': '2026-01-01T00:00:00Z'},
                  'cursor': {'panic': True, 'used': 96, 'reading_at': '2026-01-01T00:00:00Z'}}
        self.assertFalse(autopilot.panic_rescue(cfg, 'cursor', states['cursor'], states))
        cfg = {**cfg, '_main_worker': 'local', 'workers': {**cfg['workers'], 'local': {'kind': 'ollama'}}}
        self.assertTrue(autopilot.panic_rescue(cfg, 'cursor', states['cursor'], states))
        quiet = {**states['cursor'], 'quota_plan': {'limit': 'weekly', 'allowance': 0}}
        self.assertFalse(autopilot.panic_rescue(cfg, 'cursor', quiet, {**states, 'cursor': quiet}))

    def test_panic_rescue_requires_fresh_real_valid_readings_even_with_a_reader(self):
        cfg = {**self.CFG, '_now': TEST_NOW, '_main_worker': 'local',
               'workers': {**self.CFG['workers'], 'local': {'kind': 'ollama'}},
               'accounts': {**self.CFG['accounts'], 'cursor': {'worker': 'cursor', 'reader': {'command': ['agent']}}}}
        states = {'claude': {'panic': True, 'used': 96, 'reading_at': '2026-01-01T00:30:00Z'},
                  'cursor': {'panic': True, 'used': 96, 'reading_at': '2026-01-01T00:30:00Z', 'stale': False, 'reader_problem': None}}
        self.assertTrue(autopilot.panic_rescue(cfg, 'cursor', states['cursor'], states))
        unknown = {k: v for k, v in states['cursor'].items() if k != 'reader_problem'}
        self.assertFalse(autopilot.panic_rescue(cfg, 'cursor', unknown, {**states, 'cursor': unknown}))
        for change in ({'used': -20}, {'used': 101}, {'used': 'unknown'}, {'used': float('nan')}, {'used': float('inf')},
                       {'reading_at': 'invalid'}, {'reading_at': '2026-01-01T02:00:00Z'},
                       {'reading_at': '2025-12-31T20:00:00Z'}, {'selected_source': 'estimate'}):
            bad = {**states['cursor'], **change}
            self.assertFalse(autopilot.panic_rescue(cfg, 'cursor', bad, {**states, 'cursor': bad}), change)
        bad_main = {**states['claude'], 'used': 'unknown'}
        self.assertFalse(autopilot.panic_rescue(cfg, 'cursor', states['cursor'], {**states, 'claude': bad_main}))
        bad_link = {**states['cursor'], 'limits': [{'name': 'weekly', 'used': 'unknown'}]}
        self.assertFalse(autopilot.takes_over(cfg, 'cursor', bad_link, states))

    def test_selected_estimate_is_not_mistaken_for_the_older_real_reading(self):
        entries = [{'account': 'cursor', 'at': '2026-01-01T00:20:00Z', 'used': 20, 'source': 'reader'},
                   {'account': 'cursor', 'at': '2026-01-01T00:30:00Z', 'used': 96, 'source': 'estimate'}]
        account_cfg = {'window': '7d', 'resets': '2026-01-07T00:00:00Z'}
        observed = autopilot.pacing.account_state('cursor', account_cfg, entries, TEST_NOW)
        self.assertEqual(observed['reading_source'], 'reader')
        self.assertEqual(observed['selected_source'], 'estimate')
        self.assertFalse(autopilot.observed_quota({'accounts': {'cursor': account_cfg}}, 'cursor', observed, TEST_NOW))

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

    def test_cpu_reason_distinguishes_host_from_tuttis_share(self):
        from tutti import parallel
        here = self.share({'cores': 16, 'load': 13.1}, 50)
        bound, reason = parallel.bounds({}, 0, None, here, None)['cpu']
        unchanged = {k: v for k, v in here.items() if k != 'cpu_share'}
        self.assertEqual(bound, parallel.bounds({}, 0, None, unchanged, None)['cpu'][0])
        self.assertIn('load 5.1 of 8 cores for Tutti', reason)
        self.assertIn('50% share of 16 host cores; host load 13.1', reason)
        full = self.share({'cores': 16, 'load': 13.1}, 100)
        self.assertNotIn('share of', parallel.bounds({}, 0, None, full, None)['cpu'][1])

    def test_unknown_load_or_share_is_left_as_is(self):
        unknown = self.share({'cores': 16, 'load': None}, 6)
        self.assertIsNone(unknown['load'])
        self.assertNotIn('cpu_share', unknown)
        from tutti import parallel
        self.assertNotIn('cpu', parallel.bounds({}, 0, None, unknown, None))
        malformed = {'cores': 8, 'load': 5.0, 'cpu_share': {'host_load': None}}
        self.assertEqual(parallel.bounds({}, 0, None, malformed, None)['cpu'][1], 'load 5.0 of 8 cores')
        self.assertEqual(self.share({'cores': 16, 'load': 3.0}, None), {'cores': 16, 'load': 3.0})

if __name__ == '__main__':
    unittest.main()
