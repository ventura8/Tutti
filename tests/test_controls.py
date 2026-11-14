"""The owner's switches (controls.json): opt-in on and off, pause and resume of machines, devices, workers and accounts, the
dashboard's POST /api/control, and "stop now" handing each kind of job over before it stops. Fakes for every process."""
import json
import os
import subprocess
import tempfile
import unittest

from tutti import config, controls, dashboard, handover, routing, state
from tutti.routing import Probe


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf8') as f:
        f.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, 'state')
        self.path = os.path.join(self.tmp.name, 'tutti.config.json')
        raw = {'project': {'state': self.state, 'worktrees': self.tmp.name},
               'machines': {'nuc': {'host': 'nuc.example'}, 'pc': {'host': 'pc.example'},
                            'phone': {'transport': 'android', 'host': 'phone.example'}},
               'devices': {'pixel': {'serial': 'R58', 'machine': 'pc'}, 'tab': {'serial': 'T1', 'machine': 'pc'}},
               'workers': {'cursor': {'kind': 'cli', 'command': ['cursor-agent', '-p', '{prompt}']},
                           'agy': {'kind': 'cli', 'command': ['agy', '-p', '{prompt}']},
                           'copilot': {'kind': 'cli', 'command': ['copilot', '-p', '{prompt}'], 'costs_money': True},
                           'fable': {'kind': 'cli', 'command': ['claude', '-p', '{prompt}'], 'opt_in': True}},
               'routing': {'workers': ['cursor', 'agy', 'copilot', 'fable']}}
        write(self.path, json.dumps(raw))



class SwitchTest(Base):
    def test_opt_in_items_are_off_until_switched_on_and_back_off(self):
        cfg = config.load(self.path)
        self.assertNotIn('copilot', cfg['workers'])
        self.assertNotIn('fable', cfg['workers'])
        controls.set_enabled(self.state, 'worker', 'copilot', True)
        controls.set_enabled(self.state, 'worker', 'fable', True)
        cfg = config.load(self.path)
        self.assertEqual(cfg['routing']['workers'], ['cursor', 'agy', 'copilot', 'fable'])
        controls.set_enabled(self.state, 'worker', 'copilot', False)
        self.assertNotIn('copilot', config.load(self.path)['workers'])

    def test_android_lint_runs_are_opt_in(self):
        m = config.load(self.path)['machines']['phone']
        self.assertIn('opt-in', routing.unfit(m, Probe(True, 1, 0, 8)))
        controls.set_enabled(self.state, 'machine', 'phone', True)
        self.assertTrue(config.load(self.path)['machines']['phone']['enabled'])

    def test_a_paused_worker_is_skipped_everywhere_and_comes_back_on_resume(self):
        controls.set_paused(self.state, 'worker', 'agy', True, note='release day')
        cfg = config.load(self.path)
        self.assertNotIn('agy', cfg['workers'])
        self.assertEqual(cfg['routing']['workers'], ['cursor'])
        note = state.off_notes(cfg)[0]
        self.assertIn('paused by', note)
        self.assertIn('release day', note)
        controls.set_paused(self.state, 'worker', 'agy', False)
        self.assertIn('agy', config.load(self.path)['workers'])

    def test_a_paused_machine_and_device_take_nothing_new(self):
        controls.set_paused(self.state, 'machine', 'nuc', True)
        controls.set_paused(self.state, 'device', 'pixel', True)
        cfg = config.load(self.path)
        self.assertIn('paused by', routing.unfit(cfg['machines']['nuc'], Probe(True, 2, 0, 8)))
        self.assertTrue(cfg['devices']['pixel']['paused'])
        listed = state.machine_state(cfg['machines']['nuc'], Probe(True, 2, 0, 8), 1)
        self.assertEqual(listed['status'], 'held')

    def test_unknown_kind_and_stale_entries(self):
        with self.assertRaises(ValueError):
            controls.key('toaster', 'x')
        controls.set_paused(self.state, 'worker', 'gone', True)
        self.assertNotIn('gone', config.load(self.path)['workers'])   # ignored, no crash
        write(controls.path(self.state), 'not json')
        self.assertEqual(controls.read(self.state), {'enabled': {}, 'paused': {}, 'settings': {}, 'handed': {}})

    def test_the_view_lists_opt_in_items_and_who_paused_what(self):
        controls.set_paused(self.state, 'machine', 'pc', True, note='gaming')
        cfg = config.load(self.path)
        v = controls.view(cfg, controls.read(self.state))
        names = {(o['kind'], o['name']): o['on'] for o in v['opt_in']}
        self.assertEqual(names, {('worker', 'copilot'): False, ('worker', 'fable'): False, ('machine', 'phone'): False})
        self.assertEqual([(p['kind'], p['name'], p['note']) for p in v['paused']], [('machine', 'pc', 'gaming')])


class Collector:
    def __init__(self, cfg):
        self.cfg = cfg

    def reload(self):
        self.cfg = config.load(self.cfg['_path'])
        return self.cfg


class ApiTest(Base):
    def post(self, body, collector=None, stop_fn=None):
        c = collector or Collector(config.load(self.path))
        return dashboard.save_control(c, json.dumps(body).encode(), stop_fn=stop_fn or (lambda *a: []))

    def test_every_action(self):
        c = Collector(config.load(self.path))
        self.assertEqual(self.post({'action': 'optin', 'kind': 'worker', 'name': 'copilot', 'on': True}, c)[0], 200)
        self.assertIn('copilot', c.cfg['workers'])
        self.assertEqual(self.post({'action': 'pause', 'kind': 'device', 'name': 'tab', 'note': 'charging'}, c)[0], 200)
        self.assertTrue(c.cfg['devices']['tab']['paused'])
        self.assertEqual(self.post({'action': 'resume', 'kind': 'device', 'name': 'tab'}, c)[0], 200)
        self.assertFalse(c.cfg['devices']['tab'].get('paused'))

    def test_stop_now_hands_over_and_reports_it(self):
        seen = []
        code, answer = self.post({'action': 'pause', 'kind': 'machine', 'name': 'nuc', 'stop': True},
                                 stop_fn=lambda cfg, k, n: seen.append((k, n)) or [('x.log', 'handed over to pc')])
        self.assertEqual((code, seen, answer['handed']), (200, [('machine', 'nuc')], [{'log': 'x.log', 'to': 'handed over to pc'}]))

    def test_bad_bodies_and_names(self):
        self.assertEqual(self.post({'action': 'explode', 'kind': 'worker', 'name': 'agy'})[0], 400)
        self.assertEqual(self.post({'action': 'pause', 'kind': 'worker', 'name': 'ghost'})[0], 404)
        self.assertEqual(self.post({'action': 'optin', 'kind': 'worker', 'name': 'copilot'})[0], 400)
        self.assertEqual(dashboard.save_control(Collector(config.load(self.path)), b'not json')[0], 400)


class HandoverTest(Base):
    def job(self, folder, name, target, spec, pid=4242):
        log = os.path.join(self.state, folder, f'{name}.log')
        write(log, 'line 1\nline 2\n')
        write(log + '.target', target + '\n')
        write(log + '.pid', f'{pid}\n')
        write(log + '.job', json.dumps(spec))
        return log

    def stop(self, kind, name, **hands):
        ended = []
        cfg = config.load(self.path)
        alive = controls.is_tutti
        controls.is_tutti = lambda pid: True
        self.addCleanup(setattr, controls, 'is_tutti', alive)
        out = handover.stop_now(cfg, kind, name, end=ended.append, gone=lambda pid: True, **hands)
        return out, ended

    def test_a_test_run_is_stopped_and_started_again_where_routing_says(self):
        log = self.job('tests', '1-1', 'machine:nuc', {'kind': 'test', 'track': 't', 'device': False, 'args': ['t', 'pytest']})
        started = []
        out, ended = self.stop('machine', 'nuc', probe_fn=lambda m: Probe(True, 1, 0, 8),
                               start=lambda cfg, args, name: started.append(args) or '/x.log')
        self.assertEqual(ended, [4242])
        self.assertEqual(started, [['test', '--on', 'nuc', 't', 'pytest']])
        self.assertIn('handed over to nuc', out[0][1])
        with open(log + '.handoff') as f:
            self.assertIn('handed over to', f.read())

    def test_a_device_run_moves_to_another_ready_device(self):
        self.job('devices', '1-2', 'machine:pc device:pixel', {'kind': 'test', 'track': 'app', 'device': True, 'args': ['app', 'gradle']})
        good = {'adb': 'ok', 'state': 'device', 'battery': 90, 'charging': 1, 'battery_c': 30}
        started = []
        out, _ = self.stop('device', 'pixel', readings_fn=lambda m, d: good,
                           start=lambda cfg, args, name: started.append(args) or '/x.log')
        self.assertEqual(started, [['test', '--device', 'tab', 'app', 'gradle']])
        self.assertIn('device tab', out[0][1])

    def test_a_device_on_a_paused_machine_is_not_picked(self):
        controls.set_paused(self.state, 'machine', 'pc', True)
        cfg = config.load(self.path)
        self.assertEqual(handover.free_device(cfg, lambda m, d: self.fail('a paused machine is not asked')), '')

    def test_a_device_run_waits_for_any_device_when_none_is_ready(self):
        self.job('devices', '1-3', 'device:pixel', {'kind': 'test', 'track': 'app', 'device': True, 'args': ['app', 'gradle']})
        started = []
        out, _ = self.stop('device', 'pixel', readings_fn=lambda m, d: {'adb': 'ok', 'state': 'missing'},
                           start=lambda cfg, args, name: started.append(args) or '/x.log')
        self.assertEqual(started[0][:3], ['test', '--device', 'any'])
        self.assertIn('first device that gets ready', out[0][1])

    def test_a_file_transfer_keeps_its_partial_file(self):
        self.job('files', '1-4', 'machine:nuc', {'kind': 'files'})
        out, ended = self.stop('machine', 'nuc')
        self.assertEqual(ended, [4242])
        self.assertIn('partial file is kept', out[0][1])

    def test_an_agent_run_commits_its_work_writes_a_handoff_and_goes_to_the_next_worker(self):
        wt = os.path.join(self.tmp.name, 'tutti-run-parser')   # <worktrees>/<delegate.prefix><track>
        os.makedirs(wt)
        git = ['git', '-C', wt, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid']
        subprocess.run([*git, 'init', '-q'], check=True)
        write(os.path.join(wt, 'a.txt'), 'one\n')
        subprocess.run([*git, 'add', '.'], check=True)
        subprocess.run([*git, 'commit', '-q', '-m', 'init'], check=True)
        write(os.path.join(wt, 'a.txt'), 'two\n')
        subprocess.run(['git', '-C', wt, 'config', 'user.name', 't'], check=True)
        subprocess.run(['git', '-C', wt, 'config', 'user.email', 't@example.invalid'], check=True)
        log = self.job('runs', '1-5', 'worker:agy', {'kind': 'run', 'worker': 'agy', 'task': 'fix the parser', 'repo': wt,
                                                     'track': 'parser', 'run_id': '1-5', 'files': ['a.txt'],
                                                     'checks': [{'run': 'pytest', 'where': 'route'}]})
        started = []
        out, ended = self.stop('worker', 'agy', pick=lambda cfg: {'worker': 'cursor', 'args': [], 'reason': 'room'},
                               start=lambda cfg, args, name: started.append(args) or '/x.log')
        self.assertEqual(ended, [4242])
        self.assertIn('handed over to cursor', out[0][1])
        branch = subprocess.run(['git', '-C', wt, 'log', '-1', '--format=%s', 'tutti/handoff/parser'], capture_output=True, text=True)
        self.assertIn('partial work of run 1-5', branch.stdout)
        head = subprocess.run(['git', '-C', wt, 'log', '-1', '--format=%s', 'HEAD'], capture_output=True, text=True)
        self.assertEqual(head.stdout.strip(), 'init')   # HEAD stays: the next worker's diff still holds the earlier work
        with open(os.path.join(wt, 'a.txt')) as f:
            self.assertEqual(f.read(), 'two\n')
        args = started[0]
        self.assertEqual(args[:2], ['run', 'cursor'])
        self.assertIn('Continue from this handoff', args[2])
        self.assertEqual(args[3:8], ['a.txt', '--repo', wt, '--track', 'parser'])
        self.assertEqual(args[-3:], ['--check=pytest', '--check-where', 'route'])
        self.assertTrue(os.path.exists(log[:-4] + '.handoff.md'))

    def test_an_agent_run_with_no_worker_left_keeps_the_handoff(self):
        self.job('runs', '1-6', 'worker:agy', {'kind': 'run', 'worker': 'agy', 'task': 't', 'repo': '/nope', 'track': 'x'})
        out, _ = self.stop('worker', 'agy', pick=lambda cfg: {'worker': None, 'reason': 'every account is spent'},
                           start=lambda *a: self.fail('nothing to start'))
        self.assertIn('not handed over: every account is spent', out[0][1])

    def test_a_track_or_repository_from_a_file_is_never_trusted_as_a_path(self):
        self.job('runs', '1-9', 'worker:agy', {'kind': 'run', 'worker': 'agy', 'task': 't', 'repo': 'relative', 'track': '../../etc'})
        out, _ = self.stop('worker', 'agy', pick=lambda cfg: {'worker': 'cursor', 'args': []}, start=lambda *a: self.fail('started'))
        self.assertIn('not handed over', out[0][1])
        self.assertIsNone(handover.worktree_of(config.load(self.path), '-rf'))

    def test_a_stopped_job_is_marked_ended_so_its_pid_is_never_taken_again(self):
        log = self.job('files', '1-10', 'machine:nuc', {'kind': 'files'})
        self.stop('machine', 'nuc')
        with open(log + '.rc') as f:
            self.assertEqual(f.read(), '143\n')
        self.assertEqual(self.stop('machine', 'nuc'), ([], []))

    def test_an_account_pause_finds_the_runs_that_spend_it(self):
        from tutti import delegate
        cfg = {'accounts': {'agy-claude': {'worker': 'agy', 'args': ['--model', 'claude']},
                            'agy-gemini': {'worker': 'agy', 'args': ['--model', 'gemini']}, 'cursor': {}}}
        self.assertEqual(delegate.accounts_of_run(cfg, 'agy', ['--model', 'gemini']), ['agy-gemini'])
        self.assertEqual(delegate.accounts_of_run(cfg, 'agy', []), ['agy-claude', 'agy-gemini'])
        self.assertEqual(delegate.accounts_of_run(cfg, 'cursor'), ['cursor'])

    def test_finished_or_other_jobs_are_left_alone(self):
        log = self.job('tests', '1-7', 'machine:pc', {'kind': 'test'})
        self.job('tests', '1-8', 'machine:nuc', {'kind': 'test'})
        write(os.path.join(self.state, 'tests', '1-8.log.rc'), '0\n')
        out, ended = self.stop('machine', 'nuc')
        self.assertEqual((out, ended), ([], []))
        self.assertFalse(os.path.exists(log + '.handoff'))

    def test_the_process_tree_is_ended_children_first(self):
        proc = os.path.join(self.tmp.name, 'proc')
        for pid, ppid in ((10, 1), (11, 10), (12, 11), (13, 1)):
            write(os.path.join(proc, str(pid), 'stat'), f'{pid} (x y) S {ppid} 0 0')
        self.assertEqual(handover.descendants(10, proc), [11, 12])
        signalled = []
        handover.end_tree(10, kill=lambda p, s: signalled.append(p), find=lambda p: [11, 12])
        self.assertEqual(signalled, [11, 12, 10])


if __name__ == '__main__':
    unittest.main()


class BeforeTheFirstProbe(unittest.TestCase):
    def test_the_config_names_are_listed_while_the_first_probe_runs(self):
        from tutti import state
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tutti.config.example.yaml')
        cfg = config.load(path)
        with tempfile.TemporaryDirectory() as d:
            cfg['project']['state'] = d
            out = state.assemble(cfg, {})
        self.assertEqual(out['machines']['items'], [])   # nothing probed yet
        self.assertEqual([m['name'] for m in out['machines']['configured']], list(cfg['machines']))
        self.assertEqual([w['name'] for w in out['workers']['configured']], list(cfg['workers']))
        self.assertEqual(len(out['devices']['configured']), len(cfg.get('devices') or {}))

    def test_an_empty_config_lists_nothing(self):
        from tutti import state
        self.assertEqual(state.configured(config.normalise({})), {'machines': [], 'devices': [], 'workers': []})
