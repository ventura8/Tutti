"""Keep busy (tutti/dispatch.py): the pending-work model, the dispatcher pulling shards to idle targets, the idle watchdog,
leases with a heartbeat and their recovery after a crash. A simulated phone and fake spawns; nothing leaves the temp folder."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

from tutti import calls, config, dashboard, devices, dispatch, jobmove

NOW = float(int(time.time()))   # real time: a lease's heartbeat is a file's mtime


def phone(status='ready', reason='ready: takes the next run', name='pixel'):
    return {'name': name, 'serial': 'R58', 'machine': 'pc', 'status': status, 'reason': reason}


class FakeSpawn:
    def __init__(self):
        self.calls, self.pid = [], 4242

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return SimpleNamespace(pid=self.pid)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, 'state')

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, tests=None, **extra):
        raw = {'project': {'state': self.state},
               'machines': {'pc': {'host': 'pc.example'}},
               'devices': {'pixel': {'serial': 'R58', 'machine': 'pc'}},
               'projects': {'Radio': {'match': 'radio', 'tests': tests or {
                   'e2e': {'kind': 'e2e', 'targets': ['android'], 'run': 'run-e2e --filter {shard}', 'shards': ['a', 'b', 'c']}}}},
               **extra}
        path = os.path.join(self.tmp.name, 'tutti.config.yaml')
        with open(path, 'w', encoding='utf8') as f:
            json.dump(raw, f)
        return config.load(path)

    def tick(self, cfg, devices_rows, now=NOW, alive=lambda pid: True, spawn=None):
        return dispatch.tick(cfg, devices_rows, [], now=now, is_alive=alive, spawn=spawn or FakeSpawn())


class PendingTest(Base):
    def test_every_shard_of_a_new_commit_is_pending_and_a_newer_commit_queues_them_again(self):
        cfg = self.cfg()
        st = dispatch.read_state(cfg)
        dispatch.sync(st, 'Radio', 'c1', {'e2e': ['a', 'b']}, NOW)
        st['projects']['Radio']['shards']['e2e/a']['status'] = 'passed'
        self.assertEqual([w['key'] for w in dispatch.pending(cfg, st)], ['e2e/b'])
        dispatch.sync(st, 'Radio', 'c2', {'e2e': ['a', 'b']}, NOW)
        self.assertEqual([w['key'] for w in dispatch.pending(cfg, st)], ['e2e/a', 'e2e/b'])

    def test_a_failed_shard_is_retried_until_its_retries_are_spent(self):
        cfg = self.cfg()
        st = dispatch.read_state(cfg)
        dispatch.sync(st, 'Radio', 'c1', {'e2e': ['a']}, NOW)
        st['projects']['Radio']['shards']['e2e/a'].update(status='failed', attempts=2)
        self.assertEqual(len(dispatch.pending(cfg, st)), 1)
        st['projects']['Radio']['shards']['e2e/a']['attempts'] = 3
        self.assertEqual(dispatch.pending(cfg, st), [])

    def test_bad_suites_are_refused_with_the_key_and_the_fix(self):
        with self.assertRaises(config.ConfigError) as e:
            self.cfg({'e2e': {'targets': ['toaster'], 'kind': 'soak', 'devices': ['nope'], 'retries': -1}})
        text = str(e.exception)
        for part in ('tests.e2e.run is missing', 'targets names "toaster"', 'kind is \'soak\'', 'devices names "nope"', 'retries is -1'):
            self.assertIn(part, text)


class DispatchTest(Base):
    def test_an_idle_phone_with_pending_shards_is_dispatched_within_one_tick(self):
        cfg, spawn = self.cfg(), FakeSpawn()
        p = self.tick(cfg, [phone()], spawn=spawn)
        self.assertEqual([(t['id'], w['key']) for t, w in p['assign']], [('device:pixel', 'e2e/a')])
        self.assertEqual(spawn.calls[0][-3:], ['dispatch', 'run', spawn.calls[0][-1]])
        st = dispatch.read_state(cfg)
        lease = next(iter(st['leases'].values()))
        self.assertEqual((lease['target'], lease['key']), ('device:pixel', 'e2e/a'))
        self.assertEqual(st['projects']['Radio']['shards']['e2e/a']['status'], 'running')
        # the next tick: the phone holds a lease, so nothing more goes to it
        self.assertEqual(self.tick(cfg, [phone()], now=NOW + 30)['assign'], [])

    def test_a_paused_phone_stays_idle_and_the_watchdog_says_why_with_a_resume_button(self):
        cfg, spawn = self.cfg(), FakeSpawn()
        held = phone('held', 'paused by owner (dashboard) at 10:00')
        p = self.tick(cfg, [held], spawn=spawn)
        self.assertEqual(p['assign'], [])
        self.assertIn('pixel is paused', p['blocked']['device:pixel'])
        self.assertEqual(dispatch.read_state(cfg)['warnings'], [])   # not yet idle_minutes
        self.tick(cfg, [held], now=NOW + 6 * 60, spawn=spawn)
        w = dispatch.view(cfg)['warnings'][0]
        self.assertEqual(spawn.calls, [])
        self.assertIn('Idle for 6 min while Radio has 3 e2e shards pending: blocked by pixel is paused', w['text'])
        self.assertEqual(w['fix'], {'action': 'resume', 'kind': 'device', 'name': 'pixel', 'label': 'Resume pixel'})
        self.assertIn(w['text'], [e['text'] for e in dispatch.view(cfg)['events']])

    def test_dispatch_off_blocks_every_target_and_offers_the_switch(self):
        cfg = self.cfg()
        dispatch.set_enabled(cfg, False)
        self.tick(cfg, [phone()])
        self.tick(cfg, [phone()], now=NOW + 600)
        w = dispatch.view(cfg)['warnings'][0]
        self.assertEqual((w['state'], w['fix']['action']), ('blocked', 'dispatch_on'))
        self.assertFalse(dispatch.view(cfg)['enabled'])

    def test_lint_on_a_phone_is_opt_in(self):
        cfg = self.cfg({'lint': {'kind': 'lint', 'targets': ['android'], 'run': 'lint'}})
        p = self.tick(cfg, [phone()])
        self.assertIn('are opt-in', p['blocked']['device:pixel'])
        self.assertEqual(dispatch.fix_for({'kind': 'android', 'name': 'pixel'}, p['blocked']['device:pixel'])['action'], 'optin')
        cfg['devices']['pixel']['enabled'] = True
        self.assertEqual(len(self.tick(cfg, [phone()], now=NOW + 1)['assign']), 1)

    def test_a_crashed_run_holds_its_lease_until_admin_confirms_stop(self):
        cfg = self.cfg()
        self.tick(cfg, [phone()])
        p = self.tick(cfg, [phone()], now=NOW + 60, alive=lambda pid: False)
        st = dispatch.read_state(cfg)
        self.assertTrue(any('the run died without a result' in e['text'] for e in st['events']))
        self.assertEqual(st['projects']['Radio']['shards']['e2e/a']['attempts'], 0)
        self.assertEqual(p['assign'], [])
        self.assertEqual(len(st['leases']), 1)
        lid = next(iter(st['leases']))
        self.assertIn('confirmed-stopped', st['leases'][lid]['uncertain'])
        with self.assertRaises(ValueError):
            dispatch.resolve(cfg, lid, False)
        with dispatch.locked(cfg) as locked:
            dispatch.recover(cfg, locked, NOW + 61, lambda pid: False, rc_fn=lambda *_: 0)
        self.assertIn(lid, dispatch.read_state(cfg)['leases'])  # late result does not clear a hold
        dispatch.resolve(cfg, lid, True, now=NOW + 62)
        st = dispatch.read_state(cfg)
        self.assertNotIn(lid, st['leases'])
        self.assertEqual(st['projects']['Radio']['shards']['e2e/a']['status'], 'queued')

    def test_a_stale_heartbeat_ends_the_lease_even_with_a_live_pid(self):
        cfg = self.cfg()
        self.tick(cfg, [phone()])
        lid = next(iter(dispatch.read_state(cfg)['leases']))
        os.utime(dispatch.paths(cfg, lid)['beat'], (NOW - 3600, NOW - 3600))
        with dispatch.locked(cfg) as st:
            ended = dispatch.recover(cfg, st, NOW + 60, lambda pid: True)
        self.assertIn('no heartbeat', ended[0])
        self.assertIn('uncertain', dispatch.read_state(cfg)['leases'][lid])

    def test_unconfirmed_stop_result_stays_held_even_when_a_late_result_arrives(self):
        cfg = self.cfg()
        self.tick(cfg, [phone()])
        lid = next(iter(dispatch.read_state(cfg)['leases']))
        with dispatch.locked(cfg) as st:
            dispatch.recover(cfg, st, NOW + 10, lambda _: True, rc_fn=lambda *_: dispatch.STOP_UNCERTAIN)
        with dispatch.locked(cfg) as st:
            dispatch.recover(cfg, st, NOW + 11, lambda _: False, rc_fn=lambda *_: 0)
        self.assertIn('local stop was not confirmed', dispatch.read_state(cfg)['leases'][lid]['uncertain'])
        with self.assertRaises(jobmove.MoveError):
            jobmove.move_shard(cfg, 'Radio', 'e2e/a', '', False, NOW + 12, stop=mock.Mock())
        self.assertIn(lid, dispatch.read_state(cfg)['leases'])

    def test_concurrent_recovery_records_one_durable_hold(self):
        cfg = self.cfg()
        self.tick(cfg, [phone()])
        lid = next(iter(dispatch.read_state(cfg)['leases']))
        def recover_once():
            with dispatch.locked(cfg) as st:
                dispatch.recover(cfg, st, NOW + 10, lambda _: False, rc_fn=lambda *_: None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: recover_once(), range(2)))
        st = dispatch.read_state(cfg)
        self.assertIn('uncertain', st['leases'][lid])
        self.assertEqual(sum('held:' in e['text'] for e in st['events']), 1)
        def late_result():
            with dispatch.locked(cfg) as state:
                dispatch.recover(cfg, state, NOW + 11, lambda _: False, rc_fn=lambda *_: 0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: late_result(), range(2)))
        self.assertIn(lid, dispatch.read_state(cfg)['leases'])

    def test_new_commit_does_not_queue_a_shard_with_a_held_old_lease(self):
        cfg = self.cfg()
        self.tick(cfg, [phone()])
        with dispatch.locked(cfg) as st:
            lid = next(iter(st['leases']))
            dispatch.recover(cfg, st, NOW + 10, lambda _: False, rc_fn=lambda *_: None)
            dispatch.sync(st, 'Radio', 'new-commit', {'e2e': ['a']}, NOW + 11)
            self.assertNotIn('e2e/a', [w['key'] for w in dispatch.pending(cfg, st)])
            self.assertIn('uncertain', st['leases'][lid])

    def test_held_or_already_admitted_lease_never_starts_another_runner(self):
        cfg = self.cfg()
        self.tick(cfg, [phone()])
        lid = next(iter(dispatch.read_state(cfg)['leases']))
        spawn = mock.Mock(return_value=object())
        dispatch.admit_run(cfg, lid, ('Radio', 'e2e/a'), lambda _: ['fake'], spawn)
        with self.assertRaises(dispatch.AdmissionRefused):
            dispatch.admit_run(cfg, lid, ('Radio', 'e2e/a'), lambda _: ['fake'], spawn)
        with dispatch.locked(cfg) as st:
            dispatch.recover(cfg, st, NOW + 10, lambda _: False, rc_fn=lambda *_: None)
        with self.assertRaises(dispatch.AdmissionRefused):
            dispatch.admit_run(cfg, lid, ('Radio', 'e2e/a'), lambda _: ['fake'], spawn)
        self.assertEqual(spawn.call_count, 1)

    def test_runner_admission_is_refused_if_failover_fences_the_host(self):
        cfg = self.cfg()
        self.tick(cfg, [phone()])
        lid = next(iter(dispatch.read_state(cfg)['leases']))
        spawn = mock.Mock()
        with mock.patch('tutti.failover.may_write', side_effect=[True, False]):
            with self.assertRaises(dispatch.AdmissionRefused):
                dispatch.admit_run(cfg, lid, ('Radio', 'e2e/a'), lambda _: ['fake'], spawn)
        spawn.assert_not_called()
        self.assertNotIn('run_admitted', dispatch.read_state(cfg)['leases'][lid])

    def test_concurrent_runner_admission_and_recovery_never_start_after_hold(self):
        cfg = self.cfg()
        self.tick(cfg, [phone()])
        lid = next(iter(dispatch.read_state(cfg)['leases']))
        with dispatch.locked(cfg) as st:
            dispatch.recover(cfg, st, NOW + 10, lambda _: False, rc_fn=lambda *_: None)
        spawn = mock.Mock()
        def attempt():
            try:
                dispatch.admit_run(cfg, lid, ('Radio', 'e2e/a'), lambda _: ['fake'], spawn)
            except dispatch.AdmissionRefused:
                return False
            return True
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(lambda _: attempt(), range(2))), [False, False])
        spawn.assert_not_called()

    def test_recovery_waits_for_atomic_launch_and_then_holds_the_lease(self):
        cfg = self.cfg()
        self.tick(cfg, [phone()])
        lid = next(iter(dispatch.read_state(cfg)['leases']))
        entered, release = threading.Event(), threading.Event()
        def spawn(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return object()
        def recover():
            with dispatch.locked(cfg) as st:
                dispatch.recover(cfg, st, NOW + 10, lambda _: False, rc_fn=lambda *_: None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            start = pool.submit(dispatch.admit_run, cfg, lid, ('Radio', 'e2e/a'), lambda _: ['fake'], spawn)
            self.assertTrue(entered.wait(2))
            recovery = pool.submit(recover)
            release.set()
            start.result(timeout=2)
            recovery.result(timeout=2)
        lease = dispatch.read_state(cfg)['leases'][lid]
        self.assertTrue(lease['run_admitted'])
        self.assertIn('uncertain', lease)

    def test_stop_waits_after_kill_and_holds_if_the_child_still_does_not_exit(self):
        child = mock.Mock(pid=42)
        child.wait.side_effect = [subprocess.TimeoutExpired('test', 1), subprocess.TimeoutExpired('test', 1)]
        with mock.patch('os.killpg') as kill:
            with self.assertRaises(dispatch.StopUncertain):
                dispatch.stop(child, grace=1)
        self.assertEqual(kill.call_count, 2)
        self.assertEqual(child.wait.call_count, 2)

    def test_missing_suite_is_no_launch_but_failed_launch_is_uncertain(self):
        cfg = self.cfg()
        self.tick(cfg, [phone()])
        lid = next(iter(dispatch.read_state(cfg)['leases']))
        popen = mock.Mock(side_effect=OSError('launch failed'))
        with mock.patch.object(dispatch, 'suites', return_value=[]):
            self.assertEqual(dispatch.run(cfg, lid, popen=popen, after=lambda _: None), 2)
        popen.assert_not_called()
        self.assertEqual(dispatch.read_rc(cfg, lid), 2)
        with dispatch.locked(cfg) as st:
            st['leases'][lid].pop('run_admitted')
        with self.assertRaises(OSError):
            dispatch.run(cfg, lid, popen=popen, after=lambda _: None)
        self.assertEqual(dispatch.read_rc(cfg, lid), dispatch.STOP_UNCERTAIN)

    def test_held_or_admitted_run_cannot_overwrite_a_result_on_no_suite_or_bad_config(self):
        cfg = self.cfg()
        self.tick(cfg, [phone()])
        lid = next(iter(dispatch.read_state(cfg)['leases']))
        with open(dispatch.paths(cfg, lid)['rc'], 'w', encoding='utf8') as f:
            f.write('0\n')
        for field in ('uncertain', 'run_admitted'):
            with dispatch.locked(cfg) as st:
                st['leases'][lid].pop('uncertain', None)
                st['leases'][lid].pop('run_admitted', None)
                st['leases'][lid][field] = True
            with mock.patch.object(dispatch, 'suites', return_value=[]):
                self.assertEqual(dispatch.run(cfg, lid, after=lambda _: None), 2)
            with mock.patch.object(dispatch, 'test_argv', side_effect=config.ConfigError('bad config')) as build:
                self.assertEqual(dispatch.run(cfg, lid, after=lambda _: None), 2)
                build.assert_not_called()
            self.assertEqual(dispatch.read_rc(cfg, lid), 0)

    def test_a_finished_run_records_passed_and_its_heartbeat_ran(self):
        cfg = self.cfg({'unit': {'kind': 'unit', 'targets': ['machine'], 'run': f'{sys.executable} -c "pass"'}})
        with dispatch.locked(cfg) as st:
            dispatch.sync(st, 'Radio', None, {'unit': []}, NOW)
            w = dispatch.pending(cfg, st)[0]
            lid = dispatch.start(cfg, st, {'id': 'machine:pc', 'kind': 'machine', 'name': 'pc'}, w, NOW, FakeSpawn())
        seen = {}

        def popen(argv, **kw):
            seen['argv'] = argv
            return subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
        rc = dispatch.run(cfg, lid, popen=popen, after=lambda c: seen.setdefault('after', True))
        self.assertEqual(rc, 0)
        self.assertEqual(seen['argv'][-4:-1], ['--on', 'pc', 'dispatch-radio-unit'])
        self.assertTrue(seen['after'])
        with dispatch.locked(cfg) as st:
            dispatch.recover(cfg, st, NOW + 5, lambda pid: False)
        self.assertEqual(dispatch.read_state(cfg)['projects']['Radio']['shards']['unit/all']['status'], 'passed')


# fake `dumpsys telephony.registry` output: two SIM slots, each with its own mCallState
def registry(*states):
    return ''.join(f'  Phone Id={i}\n    mCallState={c}\n    mCallIncomingNumber=\n' for i, c in enumerate(states))


class CallTest(Base):
    def patch(self, name, fn):
        p = mock.patch.object(calls, name, fn)
        p.start()
        self.addCleanup(p.stop)

    def cfg_call(self, **suite):
        return self.cfg({'e2e': {'kind': 'e2e', 'targets': ['android'], 'run': 'sleep 60', 'shards': ['a'], **suite}},
                        dispatch={'call_poll_seconds': 0.05, 'resume_after_call_seconds': 30})

    def lease(self, cfg):
        with dispatch.locked(cfg) as st:
            dispatch.sync(st, 'Radio', 'c1', {'e2e': ['a']}, NOW)
            w = dispatch.pending(cfg, st)[0]
            return dispatch.start(cfg, st, {'id': 'device:pixel', 'kind': 'android', 'name': 'pixel'}, w, NOW, FakeSpawn())

    def test_the_call_state_reads_every_sim_slot(self):
        self.assertEqual(devices.call_state(registry(0, 0)), 0)
        self.assertEqual(devices.call_state(registry(0, 1)), 1)      # ringing on the second SIM
        self.assertEqual(devices.call_state(registry(2, 1)), 2)      # offhook wins over ringing
        self.assertIsNone(devices.call_state('no telephony here'))
        self.assertEqual(devices.status(devices.E2E_POLICY, {'adb': 'ok', 'state': 'device', 'call': 1, 'battery': 80,
                                                             'battery_c': 30})[0], 'call')

    def test_ringing_mid_shard_stops_it_and_queues_it_again_with_no_failure_counted(self):
        cfg = self.cfg_call()
        lid = self.lease(cfg)
        looks = iter([devices.call_state(registry(0, 1))] * 50)
        keys = []
        self.patch('release_focus', lambda c, d: keys.append('HOME'))   # the edge: no adb here

        def popen(argv, **kw):
            return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1)'], start_new_session=True)
        rc = dispatch.run(cfg, lid, popen=popen, after=lambda c: None, look=lambda c, d: next(looks))
        self.assertEqual((rc, keys), (0, ['HOME']))
        self.assertEqual(dispatch.read_rc(cfg, lid), dispatch.STOPPED_FOR_CALL)
        with dispatch.locked(cfg) as st:
            dispatch.recover(cfg, st, NOW + 5, lambda pid: False)
        sh = dispatch.read_state(cfg)['projects']['Radio']['shards']['e2e/a']
        self.assertEqual((sh['status'], sh['attempts']), ('queued', 0))
        self.assertIn('not counted as a failure', sh['why'])

    def test_a_ringing_phone_takes_nothing_and_takes_work_again_after_the_delay(self):
        cfg = self.cfg_call()
        ringing = {**phone('call', 'Paused: phone call (ringing)'), 'call': 1}
        p = self.tick(cfg, [ringing])
        self.assertEqual(p['assign'], [])
        self.assertIn('Paused: phone call (ringing)', p['blocked']['device:pixel'])
        idle = {**phone(), 'call': 0}
        p = self.tick(cfg, [idle], now=NOW + 10)   # the call just ended: 30 s to go
        self.assertIn('work starts again in 30 s', p['blocked']['device:pixel'])
        self.assertIn('in 10 s', self.tick(cfg, [idle], now=NOW + 30)['blocked']['device:pixel'])
        self.assertEqual(len(self.tick(cfg, [idle], now=NOW + 41)['assign']), 1)

    def test_a_suite_with_pause_and_resume_hooks_waits_for_the_call_and_goes_on(self):
        cfg = self.cfg_call(pause='pause-it', resume='resume-it')
        cfg['dispatch']['resume_after_call_seconds'] = 0.1
        lid = self.lease(cfg)
        ran = []
        self.patch('release_focus', lambda c, d: ran.append('HOME'))
        self.patch('hook', lambda c, d, cmd: ran.append(cmd) or True)
        looks = iter([1, 2, 2, 0] + [0] * 500)

        def popen(argv, **kw):
            return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.6)'], start_new_session=True)
        rc = dispatch.run(cfg, lid, popen=popen, after=lambda c: None, look=lambda c, d: next(looks), sleep=lambda x: None)
        self.assertEqual((rc, ran[:3]), (0, ['HOME', 'pause-it', 'resume-it']))

    def test_the_owner_picking_the_phone_up_holds_it_only_when_opted_in(self):
        cfg = self.cfg_call()
        lit = {**phone(), 'screen': 1}
        self.assertEqual(len(self.tick(cfg, [lit])['assign']), 1)
        cfg2 = self.cfg_call()
        cfg2['dispatch']['pause_on_owner_use'] = True
        st = {'leases': {}, 'calls': {}}
        self.assertEqual(calls.holds(cfg2, st, [lit], NOW), {'pixel': calls.OWNER})
        st['leases'] = {'x': {'kind': 'android', 'name': 'pixel'}}   # a run drives it: its lit screen is the test's
        self.assertEqual(calls.holds(cfg2, st, [lit], NOW), {})


class PriorityTest(Base):
    def test_free_targets_go_by_priority_and_the_fair_share_starves_no_project(self):
        cfg = self.cfg()
        cfg['projects'] = {'Map': {'priority': 2}, 'Tutti': {'priority': 0}, 'Radio': {'priority': 1}, 'Other': {}}
        self.assertEqual([config.project_priority(cfg, n) for n in ('Tutti', 'Radio', 'Map')], [0, 1, 2])
        self.assertEqual(config.project_priority(cfg, 'Other'), config.UNRANKED_PRIORITY)
        # six free targets, three projects with plenty of work: shares 3, 2, 1 (weights 1, 1/2, 1/3), nobody left out
        work = [{'project': p, 'key': f'e2e/{i}', 'kind': 'e2e', 'targets': ['machine'], 'machines': None, 'devices': None}
                for p in ('Map', 'Radio', 'Tutti') for i in range(6)]
        tgts = [{'id': f'machine:m{i}', 'kind': 'machine', 'name': f'm{i}', 'busy': False, 'blocked': ''} for i in range(6)]
        got = [w['project'] for _, w in dispatch.plan(cfg, {'leases': {}}, tgts, work, True)['assign']]
        self.assertEqual({p: got.count(p) for p in got}, {'Tutti': 3, 'Radio': 2, 'Map': 1})
        self.assertEqual(got[0], 'Tutti')
        # a project already running work gives way: Tutti holds 2 runs, so Radio goes first
        lease = {'project': 'Tutti', 'target': 'machine:x'}
        first = dispatch.plan(cfg, {'leases': {'a': lease, 'b': lease}}, tgts[:1], work, True)['assign'][0][1]['project']
        self.assertEqual(first, 'Radio')

    def test_a_bad_priority_is_refused(self):
        errs = config.validate(config.normalise({'projects': {'Radio': {'priority': -1}}}))
        self.assertTrue(any('projects.Radio.priority is -1' in e for e in errs))


class DashboardTest(Base):
    def test_the_keep_busy_switch_and_a_round_now(self):
        cfg = self.cfg()
        c = SimpleNamespace(cfg=cfg, dispatch_tick=lambda: {'assign': [], 'blocked': {'device:pixel': 'x'}})
        self.assertEqual(dashboard.save_dispatch(c, b'{"enabled": false}'), (200, {'ok': True, 'enabled': False}))
        self.assertFalse(dispatch.settings(cfg)['enabled'])
        self.assertEqual(dashboard.save_dispatch(c, b'{"action": "now"}')[1]['blocked'], {'device:pixel': 'x'})
        self.assertEqual(dashboard.save_dispatch(c, b'{"enabled": "yes"}')[0], 400)


if __name__ == '__main__':
    unittest.main()
