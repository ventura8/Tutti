import unittest
"""The household's local models as workers (tutti/fleet.py) and the block resolver (tutti/resolver.py): discovery from the
machines' readings, capability routing, the governor, overflow routing within one tick, fallback to the cloud, and the
owner's one action. Throwaway repositories and fake spawns only; no machine or model server is called."""
import os
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

from tests.test_autopilot import FREE, Base, account, attempt, task
from tutti import autopilot, backlog, delegate, fleet, resolver

TAGS = {'models': [
    {'name': 'translategemma:12b', 'details': {'parameter_size': '12.2B'}},
    {'name': 'deepseek-coder-v2:latest', 'details': {'parameter_size': '15.7B'}},
    {'name': 'phi3:latest', 'details': {'parameter_size': '3.8B'}},
    {'name': 'llava:latest', 'details': {'parameter_size': '7B'}},
]}


def blocked_states():
    """The owner's state of 2026-11-14: agy-gemini in panic, agy-claude held by its weekly limit, cursor kept as its reserve,
    claude kept for the owner's working days (no quota_plan room)."""
    held = {'tasks': 0, 'left': 10, 'why': 'kept as its reserve'}
    return {'agy-gemini': {**account('agy-gemini', 'spent', 0, 100.0), 'panic': True},
            'agy-claude': {**account('agy-claude', 'behind', 0), 'quota_plan': {**held, 'why': 'this window used its share'}},
            'cursor': {**account('cursor', 'ahead', 0), 'quota_plan': held},
            'claude': {**account('claude', 'ahead', 0), 'quota_plan': {**held, 'why': 'kept for the owner'}}}


class Discovery(Base):
    def test_models_are_classed_by_what_they_can_do(self):
        got = {m['name']: m for m in (fleet.classify(x) for x in TAGS['models']) if m}
        self.assertNotIn('llava:latest', got)                               # a vision model writes no code
        self.assertEqual(got['translategemma:12b']['kinds'], ['translations'])
        self.assertEqual(got['deepseek-coder-v2:latest']['tier'], 'strong')
        self.assertIn('lint-fix', got['deepseek-coder-v2:latest']['kinds'])
        self.assertNotIn('fix', got['phi3:latest']['kinds'])               # a 3.8B model: docs, summaries, triage

    def test_a_machine_with_models_and_no_worker_gets_one_through_a_tunnel(self):
        cfg = self.cfg()
        cfg['machines']['nuc']['transport'] = 'ssh'
        fl = fleet.entries(cfg, {'nuc': {'tags': TAGS, 'ps': {'models': []}, 'gpu': 'Intel Arc'}}, 1.0)
        found = fleet.workers_for(cfg, fl)
        w = found['nuc-local']
        self.assertEqual((w['machine'], w['model'], w['tunnel']['ssh']), ('nuc', 'deepseek-coder-v2:latest', 'nuc.example'))
        self.assertTrue(w['host'].startswith('127.0.0.1:114'))
        cfg['workers']['pc']['machine'] = 'nuc'                          # a configured worker on that machine: no second one
        self.assertEqual(fleet.workers_for(cfg, fl), {})

    def test_the_workers_follow_the_models_and_a_silent_machine_keeps_its_last(self):
        cfg = self.cfg()
        fleet.save(cfg, {'machines': fleet.entries(cfg, {'nuc': {'tags': TAGS, 'ps': {}}}, 1.0)})
        fleet.apply(cfg)
        self.assertIn('nuc-local', cfg['workers'])
        stale = fleet.entries(cfg, {'nuc': None}, 2.0, fleet.read_state(cfg)['machines'])
        self.assertTrue(stale['nuc']['stale'])
        fleet.save(cfg, {'machines': fleet.entries(cfg, {'nuc': {'tags': {'models': []}}}, 3.0)})
        fleet.apply(cfg)
        self.assertNotIn('nuc-local', cfg['workers'])                    # its models were removed: so is the worker

    def test_tunnels_start_only_for_a_silent_server_on_an_up_machine(self):
        cfg = self.cfg()
        cfg['workers']['pc'].update(machine='nuc', tunnel={'ssh': 'nuc.example', 'remote': '127.0.0.1:11434'})
        calls = []
        def run(argv, **k):
            calls.append(argv)
        self.assertEqual(fleet.ensure_tunnels(cfg, FREE, {'pc': False}, run), ['pc'])
        self.assertEqual(calls[0][1:3], ['start', 'pc'])
        paused = [{**FREE[0], 'status': 'paused', 'reason': 'yield flag is up'}]
        self.assertEqual(fleet.ensure_tunnels(cfg, paused, {'pc': False}, run), [])   # the PC yields: no tunnel either
        self.assertEqual(fleet.ensure_tunnels(cfg, FREE, {'pc': True}, run), [])
        self.assertEqual(fleet.ensure_tunnels(cfg, FREE, {}, run), [])           # not probed yet: no restart every round
        failed = fleet.ensure_tunnels(cfg, FREE, {'pc': False}, lambda argv, **k: SimpleNamespace(returncode=1))
        self.assertEqual(failed, [])                                               # a start that failed is not reported started


class Routing(Base):
    def test_capability_routing_picks_the_model_for_the_kind(self):
        w = {'model': 'deepseek-coder-v2:latest', 'models': [fleet.classify(m) for m in TAGS['models']]}
        tr = fleet.model_for(w, {'title': 'Translate the new strings', 'scope': ['ro.json'], 'size': 'S'})
        self.assertEqual(tr['name'], 'translategemma:12b')
        self.assertEqual(fleet.model_for(w, {'title': 'Fix the ruff E501 findings', 'scope': ['a.py'], 'size': 'M'})['name'],
                         'deepseek-coder-v2:latest')
        small = {'model': 'phi3:latest', 'models': [fleet.classify(TAGS['models'][2])]}
        why, _ = fleet.fits(small, {'title': 'Fix the parser', 'scope': ['a.py'], 'size': 'S'})
        self.assertIn('no model here', why)                               # a 3.8B model is not given code fixes

    def test_a_task_names_no_file_and_tutti_finds_it_from_the_title(self):
        files = ['tutti/energy.py', 'tutti/conduct.py', 'tests/test_energy.py', 'docs/ENERGY.md', 'scripts/release-patch.sh']
        self.assertEqual(fleet.guess_scope('Energy: log calls made by conduct', files),
                         ['tutti/energy.py', 'tutti/conduct.py', 'docs/ENERGY.md'])   # code first, then the docs
        self.assertEqual(fleet.guess_scope('versions at merge time: scripts/release-patch.sh', files), ['scripts/release-patch.sh'])
        self.assertEqual(fleet.guess_scope('Dashboard: push state with events', files), [])

    def test_a_larger_task_is_worked_in_steps(self):
        self.assertEqual(fleet.steps(['a', 'b', 'c'], 2), [['a', 'b'], ['c']])
        self.assertEqual(fleet.steps([], 2), [[]])

    def test_a_failed_local_attempt_escalates_to_the_cloud_first(self):
        cfg = self.cfg()
        t = {**task('t1', attempts=[attempt('pc')]), 'scope': ['a.py']}
        self.assertEqual(autopilot.candidates(cfg, t)[:3], ['agy-gemini', 'agy-claude', 'cursor'])
        p = autopilot.plan(cfg, [t], self.states_ok(), FREE, True)
        self.assertEqual(p['dispatch'], [('t1', 'agy-gemini')])

    def states_ok(self):
        return {n: account(n) for n in ('agy-gemini', 'agy-claude', 'cursor')}

    def test_with_cloud_room_and_nothing_ahead_a_fresh_task_goes_online(self):
        t = {**task('t1'), 'scope': ['a.py']}
        p = autopilot.plan(self.cfg(), [t], self.states_ok(), FREE, True)
        self.assertEqual(p['dispatch'], [('t1', 'agy-gemini')])
        self.assertEqual(p['overflow'], '')


class OwnerBlock(Base):
    """The block the owner saw on 2026-11-14, reproduced: every online account out, pc with room 1, M tasks, claude 'unknown'."""

    def owner_cfg(self):
        cfg = self.cfg(accounts=['agy-claude', 'agy-gemini', 'claude'])
        cfg['workers']['claude'] = {'kind': 'cli', 'command': ['claude', '-p', '{prompt}']}
        cfg['accounts']['claude'] = {}                                   # no worker: key, as in the owner's config
        cfg['workers']['laptop'] = {'kind': 'ollama', 'host': '127.0.0.1:11434', 'model': 'qwen3.5:9b'}
        cfg['machines']['laptop'] = {'name': 'laptop', 'transport': 'local', 'host': 'local', 'slots': 1}
        repo = cfg['autopilot']['repos']['app']['path']
        os.makedirs(os.path.join(repo, 'tutti'))
        subprocess.run(['git', 'init', '-q', repo], check=True)
        for f in ('tutti/energy.py', 'tutti/planner.py'):
            with open(os.path.join(repo, f), 'w', encoding='utf8') as fh:
                fh.write('x = 1\n')
        subprocess.run(['git', '-C', repo, 'add', '-A'], check=True)
        return cfg

    def machines(self):
        return [{**FREE[0]}, {'name': 'laptop', 'status': 'free', 'free': 1, 'reason': 'fit'}]

    def test_claude_is_a_usable_step_and_a_missing_worker_names_the_real_reason(self):
        cfg = self.owner_cfg()
        self.assertEqual(autopilot.step_of(cfg, 'claude')['worker'], 'claude')
        cfg['accounts']['ghost'] = {}
        self.assertIn('no worker runs this account', autopilot.unusable_why(cfg, 'ghost'))
        self.assertNotIn('unknown, turned off', autopilot.step_room(cfg, 'ghost', {}, {})[1])

    def test_within_one_tick_a_local_worker_takes_an_s_task(self):
        cfg = self.owner_cfg()
        with backlog.locked(cfg) as data:
            backlog.add(data, backlog.new_task('app', 'Energy: log the planner calls', [], 'true', 'S'))
            backlog.add(data, backlog.new_task('app', 'Dashboard: push state with events', [], 'true', 'S'))
        started = []
        p, events = autopilot.tick(cfg, blocked_states(), self.machines(), is_alive=lambda pid: True,
                                   spawn=lambda argv, **k: started.append(argv) or SimpleNamespace(pid=1))
        sent = dict(p['dispatch'])
        self.assertEqual(sent.get('t1'), 'laptop')                        # an M task, no quota points: the idle laptop takes it
        self.assertNotIn('t2', sent)                                       # it names no file a local model could be given
        self.assertIn('ahead of pace', p['overflow'])
        self.assertTrue(any('picked tutti/energy.py' in e for e in events))
        self.assertIn('tutti/energy.py', started[0])                       # the local model is given the guessed files
        self.assertEqual(len([n for n in sent.values() if n == sent['t1']]), 1)   # one task per model server at a time

    def test_when_nothing_can_take_it_the_owner_gets_one_action(self):
        cfg = self.owner_cfg()
        cfg['autopilot']['accounts'] = ['agy-claude', 'agy-gemini']
        with backlog.locked(cfg) as data:
            backlog.add(data, backlog.new_task('app', 'Dashboard: push state with events', [], 'true', 'M'))
        states = {**blocked_states(), 'cursor': account('cursor', 'on pace', 2)}
        autopilot.tick(cfg, states, self.machines(), is_alive=lambda pid: True, spawn=lambda *a, **k: SimpleNamespace(pid=1))
        rv = autopilot.view(cfg)['resolver']
        self.assertEqual([a['fix'] for a in rv['attempts']], ['split or resize', 'any local worker', 'lower the model tier',
                                                              'borrow household spare', 'wake an idle machine'])
        self.assertEqual((rv['action']['kind'], rv['action']['account']), ('allow', 'cursor'))
        resolver.allow(cfg, 'cursor')
        self.assertIn('cursor', autopilot.trigger_accounts(cfg))
        self.assertIn('your one action', autopilot.describe(autopilot.view(cfg)))


class Governor(Base):
    def test_heat_quiet_hours_memory_and_a_busy_cpu_machine_say_no(self):
        cfg = self.cfg()
        cfg['workers']['pc']['machine'] = 'nuc'
        hot = [{'name': 'nuc', 'status': 'loaded', 'reason': 'too hot: CPU 99 C'}]
        self.assertIn('too hot', fleet.governor(cfg, 'pc', hot))
        busy = [{'name': 'nuc', 'status': 'busy', 'reason': 'no free slot'}]
        self.assertIn('CPU', fleet.governor(cfg, 'pc', busy))
        self.assertEqual(fleet.governor(cfg, 'pc', busy, hardware={'gpu': 'RTX'}), '')   # a GPU model runs beside the tests
        cfg['machines']['nuc']['quiet_hours'] = ['00:00-23:59']
        self.assertIn('quiet hours', fleet.governor(cfg, 'pc', FREE, datetime(2026, 11, 14, 12, tzinfo=timezone.utc)))
        cfg['machines']['nuc']['quiet_hours'] = []
        self.assertIn('memory', fleet.governor(cfg, 'pc', FREE, mem_gb=1.0))
        self.assertEqual(fleet.governor(cfg, 'pc', FREE, mem_gb=16.0), '')

    def test_a_governed_machine_takes_nothing_even_while_the_cloud_is_ahead(self):
        cfg = self.cfg()
        cfg['workers']['pc']['machine'] = 'nuc'
        t = {**task('t1'), 'scope': ['a.py']}
        hot = [{'name': 'nuc', 'status': 'loaded', 'free': 2, 'reason': 'too hot: CPU 99 C'}]
        p = autopilot.plan(cfg, [t], blocked_states(), hot, True)
        self.assertEqual(p['dispatch'], [])
        self.assertIn('too hot', autopilot.step_room(cfg, 'pc', blocked_states(), {}, hot)[1])

    def test_a_failed_step_ends_the_run_and_the_chosen_model_is_used(self):
        cfg = self.cfg()
        cfg['workers']['pc']['models'] = [fleet.classify(m) for m in TAGS['models']]
        seen = []
        from tutti import local
        orig = local.agent
        local.agent = lambda c, w, t, chk, files, base='.': seen.append(w['model']) and False
        try:
            rc, _ = delegate.run_worker(cfg, 'pc', 'Fix the parser', ['a.py', 'b.py', 'c.py', 'd.py'], self.tmp.name, None,
                                        ('--model', 'deepseek-coder-v2:latest'), None, None)
        finally:
            local.agent = orig
        self.assertEqual(rc, 1)
        self.assertEqual(seen, ['deepseek-coder-v2:latest'])               # step 1 of 2 failed: step 2 never ran

    def test_the_step_loop_uses_the_model_for_the_kind(self):
        cfg = self.cfg()
        cfg['workers']['pc']['models'] = [fleet.classify(m) for m in TAGS['models']]
        seen = []
        from tutti import local
        orig = local.agent
        local.agent = lambda c, w, t, chk, files, base='.': seen.append((w['model'], list(files))) or True
        try:
            rc, _ = delegate.run_worker(cfg, 'pc', 'Translate the strings', ['a.json', 'b.json', 'c.json'], self.tmp.name, None, (),
                                        None, None)
        finally:
            local.agent = orig
        self.assertEqual(rc, 0)
        self.assertEqual(seen[0][0], 'translategemma:12b')
        self.assertEqual(len(seen), 2)                                     # 3 files, 2 per step for a 12B model


class LocalSizes(unittest.TestCase):
    """Owner, 2026-11-17: local models failed most M and L tasks; a strong one takes S and M, a normal one S only."""

    def test_no_local_tier_takes_an_l_task_and_a_normal_one_takes_s_only(self):
        self.assertNotIn('L', fleet.TIERS['strong']['sizes'])
        self.assertEqual(fleet.TIERS['normal']['sizes'], ('S',))
        self.assertEqual(fleet.TIERS['small']['sizes'], ('S',))
