"""Presence (tutti/presence.py), nothing lost on a power-off (tutti/durable.py, tutti/recovery.py, tutti/sleepwatch.py) and
moving waiting work (tutti/jobmove.py). Fake signals, fake spawns and temp folders; git runs only in temp repositories."""
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from tutti import (
    autopilot,
    backlog,
    config,
    dashboard,
    dispatch,
    durable,
    household,
    jobmove,
    owner,
    presence,
    recovery,
    sharing,
    sleepwatch,
)

NOW = float(int(time.time()))


def NOIO(*a, **k):   # presence.round_io's stand-in: no reconnect, no wake
    return []


def phone(status='ready', reason='ready: takes the next run', name='pixel'):
    return {'name': name, 'serial': 'R58', 'machine': 'pc', 'status': status, 'reason': reason}


def machine(name, reachable=True):
    return {'name': name, 'status': 'free' if reachable else 'offline', 'reason': '', 'reachable': reachable, 'free': 1}


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
               'machines': {'pc': {'host': 'pc.example'}, 'a': {'host': 'a.example'}, 'b': {'host': 'b.example'}},
               'devices': {'pixel': {'serial': 'R58', 'machine': 'pc'}},
               'projects': {'Radio': {'match': 'radio', 'tests': tests or {
                   'e2e': {'kind': 'e2e', 'targets': ['android'], 'run': 'run-e2e --filter {shard}', 'shards': ['a']}}}},
               **extra}
        path = os.path.join(self.tmp.name, 'tutti.config.yaml')
        with open(path, 'w', encoding='utf8') as f:
            json.dump(raw, f)
        return config.load(path)

    def tick(self, cfg, devs, machines=(), now=NOW, alive=lambda pid: True, spawn=None, stop=None):
        return dispatch.tick(cfg, devs, list(machines), now=now, is_alive=alive, spawn=spawn or FakeSpawn(), presence_io=NOIO,
                             stop_fn=stop or (lambda pid: None))

    def shard(self, cfg, key='e2e/a', project='Radio'):
        return dispatch.read_state(cfg)['projects'][project]['shards'][key]


class ComingAndGoing(Base):
    def test_a_device_leaving_mid_shard_is_held_until_stop_is_confirmed(self):
        cfg, stopped = self.cfg(), []
        self.tick(cfg, [phone()])
        self.assertEqual(self.shard(cfg)['status'], 'running')
        gone = phone('offline', 'not connected')
        self.tick(cfg, [gone], now=NOW + 30, stop=stopped.append)   # within the grace: one lost probe is not a departure
        self.assertEqual((self.shard(cfg)['status'], stopped), ('running', []))
        self.tick(cfg, [gone], now=NOW + 200, stop=stopped.append)
        sh = self.shard(cfg)
        self.assertEqual((sh['status'], sh['attempts'], stopped), ('running', 0, []))
        state = dispatch.read_state(cfg)
        lid = next(iter(state['leases']))
        self.assertIn('left mid-run', state['leases'][lid]['uncertain'])
        self.tick(cfg, [gone], now=NOW + 900)   # 12 minutes away with work waiting: not idle
        self.assertEqual(dispatch.read_state(cfg)['warnings'], [])
        p = self.tick(cfg, [phone()], now=NOW + 5 * 3600)
        self.assertEqual(p['assign'], [])
        dispatch.resolve(cfg, lid, True, now=NOW + 5 * 3600)
        p = self.tick(cfg, [phone()], now=NOW + 5 * 3600 + 1)
        self.assertEqual([(t['id'], w['key']) for t, w in p['assign']], [('device:pixel', 'e2e/a')])
        self.assertTrue(any('is back' in e['text'] for e in dispatch.read_state(cfg)['events']))

    def test_a_power_off_without_a_result_keeps_the_lease_held(self):
        cfg = self.cfg()
        self.tick(cfg, [phone()])
        st = dispatch.read_state(cfg)
        lease = next(iter(st['leases'].values()))
        self.assertEqual(dispatch.interrupted({**lease, 'boot': 'old'}, None, False, {}, 'new')[:14], 'interrupted by')
        self.assertEqual(dispatch.interrupted({**lease, 'boot': 'same'}, None, False, {}, 'same'), '')
        with mock.patch.object(durable, 'boot_id', return_value='after-reboot'):
            self.tick(cfg, [phone()], now=NOW + 60, alive=lambda pid: False)
        sh = self.shard(cfg)
        self.assertEqual((sh['attempts'], sh['status']), (0, 'running'))
        self.assertIn('interrupted by', next(iter(dispatch.read_state(cfg)['leases'].values()))['uncertain'])
        with mock.patch.object(durable, 'boot_id', return_value='after-reboot'):
            self.tick(cfg, [phone()], now=NOW + 120, alive=lambda pid: False)
        sh = self.shard(cfg)
        self.assertEqual((sh['attempts'], sh['status']), (0, 'running'))

    def test_left_with_its_owner_and_asleep_at_night_are_told_apart(self):
        s = presence.settings({})
        dev = {**presence.new_record('device', 'pixel'), 'state': presence.PRESENT, 'last_seen': NOW - 600}
        self.assertEqual(presence.classify(dev, NOW, 0, {**s, 'night': '0-0'}, companion=presence.AWAY)[1], 'left with its owner')
        pc = {**presence.new_record('machine', 'pc'), 'last_seen': NOW - 600}
        hour = presence.local_dt(NOW, 0).hour
        night = {**s, 'night': f'{hour}-{(hour + 1) % 24}'}
        self.assertEqual(presence.classify(pc, NOW, 0, night)[0], presence.ASLEEP)
        lap = {**pc, 'laptop': True}
        self.assertEqual(presence.classify(lap, NOW, 0, {**s, 'night': f'{(hour + 2) % 24}-{(hour + 3) % 24}'})[0], presence.AWAY)
        rec = {**pc, 'state': presence.PRESENT}
        presence.observe(rec, False, NOW, 0, night)
        self.assertRegex(presence.label(rec, 0), r'^Asleep since \d\d:\d\d \(asleep at its usual night hours\)$')

    def test_a_machine_s_night_and_profile_hours_are_in_its_own_zone(self):
        cfg = self.cfg()
        pst = presence.read(cfg)
        presence.update(cfg, pst, [], [{**machine('a'), 'zone': 'Asia/Tokyo'}], NOW)
        rec = pst['targets']['machine:a']
        self.assertEqual(rec['zone'], 'Asia/Tokyo')
        hour = presence.local_dt(NOW, 'Asia/Tokyo').hour
        night = {**presence.settings({}), 'night': f'{hour}-{(hour + 1) % 24}'}
        self.assertEqual(presence.classify(rec, NOW, 'UTC', night)[0], presence.ASLEEP)   # Tokyo's night, whatever ours is
        presence.update(cfg, pst, [], [{**machine('a'), 'zone': 'Asia/Tokyo'}], NOW + 1800)
        tokyo = presence.local_dt(NOW, 'Asia/Tokyo')
        self.assertEqual(rec['seen'][tokyo.weekday()][tokyo.hour], 0.5)

    def test_the_profile_keeps_long_shards_off_a_machine_likely_to_leave_and_prefers_the_always_on_one(self):
        cfg = self.cfg({'e2e': {'kind': 'e2e', 'targets': ['machine'], 'run': 'go {shard}', 'shards': ['x', 'y'], 'minutes': 90}})
        pst = presence.read(cfg)
        for name, here in (('a', 0.0), ('b', 1.0)):
            rec = presence.new_record('machine', name)
            rec['seen'] = [[1.0] * 24 for _ in range(7)]
            rec['here'] = [[here] * 24 for _ in range(7)]
            pst['targets'][f'machine:{name}'] = rec
        presence.write(cfg, pst)
        p = self.tick(cfg, [], [machine('a'), machine('b')])
        self.assertEqual([t['id'] for t, _ in p['assign']], ['machine:b'])
        self.assertIn('usually leaves or sleeps soon', p['blocked']['machine:a'])

    def test_a_members_laptop_away_then_back_syncs(self):
        state = household.new_state('home', NOW)
        household.join(state, 'u1', 'h1', 'Ana', 'claude', 'tok', NOW)
        members = presence.members_of(state, NOW + 3600, 300)   # no heartbeat for an hour
        cfg, pst = self.cfg(presence={'grace_seconds': 60}), None
        pst = presence.read(cfg)
        presence.update(cfg, pst, [], [], NOW, {'u1': {'name': 'Ana', 'fresh': True}})
        presence.update(cfg, pst, [], [], NOW + 3600, members)
        self.assertIn(pst['targets']['member:u1']['state'], presence.ABSENT)
        household.heartbeat(state, 'u1', {}, NOW + 7200)
        changes = presence.update(cfg, pst, [], [], NOW + 7200, presence.members_of(state, NOW + 7200, 300))
        self.assertEqual(changes[0][:2], ('member:u1', 'returned'))
        answer = household.sync_project(state, 'gh/x', {'publish': [{'title': 'Fix login'}]}, 'u1', NOW + 7200, 30)
        self.assertEqual([t['key'] for t in answer['tasks']], ['fix login'])

    def test_the_owner_counts_as_away_while_their_laptop_is(self):
        cfg = self.cfg(presence={'owner_laptop': 'a'})
        pst = presence.read(cfg)
        pst['targets']['machine:a'] = {**presence.new_record('machine', 'a'), 'state': presence.AWAY}
        presence.write(cfg, pst)
        self.assertEqual(owner.owner_of(cfg, self.state)['laptop_away'], 'a is away')

    def test_wake_on_lan_is_opt_in_and_lan_only(self):
        self.assertEqual(presence.magic_packet('aa:bb:cc:dd:ee:ff')[:6], b'\xff' * 6)
        self.assertEqual(len(presence.magic_packet('aa:bb:cc:dd:ee:ff')), 102)
        self.assertTrue(presence.lan_only('192.168.1.255'))
        with self.assertRaises(ValueError):
            presence.send_wake('aa:bb:cc:dd:ee:ff', '8.8.8.8')
        cfg = self.cfg()
        cfg['machines']['a']['wol_mac'] = 'aa:bb:cc:dd:ee:ff'
        self.assertIsNone(presence.broadcast_of(cfg['machines']['a']))   # a host name says nothing about the LAN: no packet
        cfg['machines']['a']['host'] = '192.168.1.20'
        st = {'targets': {'machine:a': {**presence.new_record('machine', 'a'), 'state': presence.ASLEEP}}, 'woken': {}}
        self.assertEqual(presence.wake_due(cfg, st, {'machine:a': NOW - 3600}, NOW), [])
        cfg['presence'] = {'wake': True}
        self.assertEqual(presence.wake_due(cfg, st, {'machine:a': NOW - 3600}, NOW), [('a', 'aa:bb:cc:dd:ee:ff', '192.168.1.255')])
        self.assertEqual(presence.wake_due(cfg, st, {'machine:a': NOW - 60}, NOW), [])   # not waiting long enough

    def test_a_returning_phone_s_wireless_port_comes_from_mdns(self):
        text = 'List of discovered mdns services\nadb-R58-x\t_adb-tls-connect._tcp\t192.168.1.7:41235\n'
        self.assertEqual(presence.mdns_port(text, '192.168.1.7'), '192.168.1.7:41235')
        self.assertIsNone(presence.mdns_port(text, '192.168.1.8'))
        self.assertIn('adb connect', presence.reconnect_command({'serial': '192.168.1.7:5555'}).replace('xargs -r adb', 'adb'))


class Sleep(Base):
    def test_sleep_does_not_hold_dispatch_leases_after_failover_fences_this_machine(self):
        cfg = self.cfg()
        self.tick(cfg, [phone()])
        before = dispatch.read_state(cfg)['leases']
        with mock.patch('tutti.failover.may_write', side_effect=[True, False]):
            sleepwatch.going_down(cfg, 'sleep', 4.0, running=lambda *_: [])
        self.assertEqual(dispatch.read_state(cfg)['leases'], before)

    def test_the_sleep_signal_hands_running_work_over_and_wake_starts_it_again(self):
        cfg = self.cfg()
        self.tick(cfg, [phone()])
        log = os.path.join(self.tmp.name, 'run.log')
        with open(log + '.job', 'w', encoding='utf8') as f:
            json.dump({'kind': 'test', 'args': ['--on', 'pc', 't', 'make e2e'], 'track': 't'}, f)
        ended, stopped = [], []

        def running(folder, kind, name):
            return [(log, 99)] if (kind, name) == ('machine', 'pc') else []
        with mock.patch.object(dispatch, 'stop_pid', stopped.append):
            noted = sleepwatch.going_down(cfg, 'sleep', 4.0, running=running, end=ended.append)
        self.assertEqual((ended, stopped), ([99], []))
        self.assertEqual(noted[0]['kind'], 'test')
        sh = self.shard(cfg)
        self.assertEqual((sh['status'], sh['attempts']), ('running', 0))
        self.assertIn('execution stop is unconfirmed', next(iter(dispatch.read_state(cfg)['leases'].values()))['uncertain'])
        self.assertEqual(presence.read(cfg)['targets'][sleepwatch.HOST]['state'], presence.ASLEEP)
        started = []
        sleepwatch.woke(cfg, start=lambda c, args, name: started.append(args) or 'log')
        self.assertEqual(started, [['test', '--on', 'pc', 't', 'make e2e']])
        self.assertEqual(presence.read(cfg)['resume'], [])   # never started twice

    def test_the_watcher_holds_a_delay_lock_and_lets_go_once_handed_over(self):
        lines = ['/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (true,)\n',
                 '/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (false,)\n']
        procs = []

        def popen(argv, **kw):
            p = SimpleNamespace(argv=argv, stdout=iter(lines), terminate=mock.Mock())
            procs.append(p)
            return p
        calls = []
        done = threading.Event()
        done.set()   # the holding thread does not loop here: the lock follows the signals and is_busy only
        sleepwatch.watch({}, popen=popen, delay=lambda: 4.0, down=lambda c, w, b: calls.append(('down', w, b)),
                         up=lambda c: calls.append(('up',)), is_busy=lambda c: True, stop=done)
        self.assertEqual(calls, [('down', 'sleep', 4.0), ('up',)])
        self.assertTrue(procs[0].terminate.called)              # the first lock let go after the handover
        self.assertEqual(procs[2].argv[:2], ['systemd-inhibit', '--what=sleep:shutdown'])   # taken again on wake
        self.assertIn(f'--pid={os.getpid()}', procs[2].argv)   # it ends with this process: never `sleep infinity`
        self.assertNotIn('infinity', procs[2].argv)
        self.assertTrue(procs[2].terminate.called)              # and let go when the watcher ends
        self.assertEqual(sleepwatch.max_delay(lambda *a, **k: SimpleNamespace(stdout='t 5000000')), 4.0)
        self.assertIsNone(sleepwatch.parse('something else'))

    def test_the_lock_is_held_only_while_work_runs_and_one_per_process(self):
        procs = []
        lock = sleepwatch.Lock(lambda argv, **kw: procs.append(SimpleNamespace(argv=argv, env=kw.get('env'), poll=lambda: None,
                                                                              terminate=mock.Mock(), wait=mock.Mock())) or procs[-1])
        lock.follow(False)
        self.assertEqual(procs, [])                              # idle: no lock at all
        lock.follow(True)
        lock.follow(True)
        self.assertEqual(len(procs), 1)                          # busy twice: still one lock
        self.assertEqual(procs[0].env['TUTTI_PROC'], 'inhibit')
        lock.follow(False)
        self.assertTrue(procs[0].terminate.called)              # the work ended: let go
        with mock.patch.dict(os.environ, {'TUTTI_NO_DAEMONS': '1'}):
            self.assertIsNone(sleepwatch.start_in_background({}))   # tests never start a real inhibitor
        cfg = {'project': {'state': tempfile.gettempdir()}, 'machines': {'pc': {}}}
        self.assertTrue(sleepwatch.busy(cfg, running=lambda state, kind, name: [('log', 1)]))
        self.assertFalse(sleepwatch.busy(cfg, running=lambda state, kind, name: []))


WRITER = '''
import os, sys, random
sys.path.insert(0, sys.argv[1])
from tutti import durable
p, log = sys.argv[2], sys.argv[3]
i = 0
while True:
    i += 1
    durable.dump_json(p, {"i": i, "pad": "x" * random.randint(1, 20000)})
    durable.append_line(log, '{"i": %d, "pad": "%s"}' % (i, "y" * random.randint(1, 5000)))
'''


class NothingLost(Base):
    def test_start_keeps_a_healthy_usage_log_and_does_not_resume_when_nothing_waits(self):
        cfg = self.cfg()
        os.makedirs(self.state, exist_ok=True)
        log = os.path.join(self.state, 'usage.jsonl')
        original = '{"at": "2026-11-14T00:00:00+00:00", "account": "x", "used": 1}\n'
        with open(log, 'w', encoding='utf8') as f:
            f.write(original)
        resume = mock.Mock()
        with mock.patch.object(presence, 'read', return_value={'resume': []}), mock.patch('tutti.telemetry.record'):
            lines = recovery.on_start(cfg, resume=resume)
        self.assertEqual(lines, [])
        resume.assert_not_called()
        with open(log, encoding='utf8') as f:
            self.assertEqual(f.read(), original)

    def test_start_resumes_waiting_jobs_once_and_reports_the_count(self):
        cfg = self.cfg()
        resume = mock.Mock(return_value=['run-1', 'run-2'])
        with mock.patch.object(presence, 'read', return_value={'resume': ['run-1', 'run-2']}), \
                mock.patch('tutti.telemetry.record'):
            lines = recovery.on_start(cfg, resume=resume)
        resume.assert_called_once_with(cfg)
        self.assertEqual(lines, ['2 job(s) stopped by a shutdown started again'])

    def test_a_kill_at_random_points_during_writes_never_corrupts_state(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path, log = os.path.join(self.tmp.name, 's.json'), os.path.join(self.tmp.name, 'u.jsonl')
        rng = random.Random(7)
        for _ in range(6):
            p = subprocess.Popen([sys.executable, '-c', WRITER, root, path, log])
            time.sleep(0.05 + rng.random() * 0.2)
            os.kill(p.pid, signal.SIGKILL)
            p.wait()
            with open(path, encoding='utf8') as f:
                self.assertIn('i', json.load(f))   # whole: the old file or the new one
            durable.repair_jsonl(log)
            with open(log, encoding='utf8') as f:
                self.assertTrue(all(json.loads(line) for line in f))
        self.assertTrue(durable.sweep(self.tmp.name, alive=lambda pid: False) or True)
        self.assertEqual([x for x in os.listdir(self.tmp.name) if x.endswith('.tmp')], [])

    def test_start_repairs_a_torn_log_and_removes_dead_writers_temp_files(self):
        cfg = self.cfg()
        os.makedirs(self.state, exist_ok=True)
        log = os.path.join(self.state, 'usage.jsonl')
        with open(log, 'w', encoding='utf8') as f:
            f.write('{"at": "2026-11-14T00:00:00+00:00", "account": "x", "used": 1}\n{"at": "2026-11-1')
        with open(os.path.join(self.state, 'backlog.json.999999.tmp'), 'w', encoding='utf8') as f:
            f.write('{')
        with mock.patch.object(durable, 'pid_alive', return_value=False):
            lines = recovery.on_start(cfg, resume=lambda c: [])
        self.assertEqual(len(durable.read_jsonl(log)), 1)
        self.assertTrue(any('torn last line' in x for x in lines))
        self.assertFalse(os.path.exists(os.path.join(self.state, 'backlog.json.999999.tmp')))

    def test_an_unreported_attempt_after_power_off_stays_held_and_not_counted(self):
        cfg = {'autopilot': {'repos': {'r': {'path': self.tmp.name, 'wip_push': False}}, 'no_progress': 3, 'max_attempts': 12},
               'project': {'state': self.state, 'worktrees': self.tmp.name}, 'delegate': {'prefix': 'tutti-'}}
        t = {'id': 't1', 'title': 'Fix it', 'status': 'running', 'run': 'x', 'boot': 'old', 'job_bridge': '', 'attempts': [], 'repo': 'r',
             'scope': [], 'source': 'manual'}
        data = {'tasks': [t]}
        with mock.patch.object(autopilot, 'finished', return_value=(True, None)), \
                mock.patch.object(durable, 'boot_id', return_value='new'):
            events = autopilot.collect(cfg, data, NOW, lambda pid: False)
        self.assertEqual((t['status'], t['attempts'], events), ('running', [], []))
        self.assertIn('waiting for its report', t['recovery_note'])
        t.update(status='running', boot='new')
        with mock.patch.object(autopilot, 'finished', return_value=(False, None)):
            autopilot.collect(cfg, data, NOW, lambda pid: True)
        self.assertEqual(t['status'], 'running')   # alive: never started a second time


def git(cwd, *args):
    return subprocess.run(['git', '-C', cwd, *args], capture_output=True, text=True, check=True).stdout.strip()


GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@x.example', 'GIT_COMMITTER_NAME': 't',
           'GIT_COMMITTER_EMAIL': 't@x.example', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}


@mock.patch.dict(os.environ, GIT_ENV)
class Checkpoints(Base):
    def test_manual_checkpoint_only_snapshots_running_tasks(self):
        cfg = self.cfg()
        os.makedirs(self.state, exist_ok=True)
        running = {'id': 't1', 'title': 'task', 'repo': 'app', 'run': '1-1', 'status': 'running'}
        queued = {'id': 't2', 'title': 'other', 'repo': 'app', 'status': 'queued'}
        backlog.save(cfg, {'tasks': [running, queued]})
        snap, push = mock.Mock(return_value='new-sha'), mock.Mock(return_value='')
        with mock.patch('tutti.failover.may_write', return_value=True), \
                mock.patch.object(autopilot, 'tree_of', return_value='/example/worktree'), \
                mock.patch.object(autopilot, 'wip_remote', return_value='origin'), \
                mock.patch.object(autopilot, 'tick', side_effect=AssertionError('must not dispatch')):
            results = autopilot.checkpoint_now(cfg, 100, snap=snap, push=push)
        self.assertEqual([(task_id, result['pushed']) for task_id, result in results], [('t1', True)])
        self.assertEqual(snap.call_count, 1)
        self.assertEqual(push.call_count, 1)
        stored = backlog.load(cfg)['tasks']
        self.assertEqual(stored[0]['wip']['sha'], 'new-sha')
        self.assertEqual(stored[1], queued)

    def test_manual_checkpoint_fences_and_reports_missing_tree(self):
        cfg = self.cfg()
        os.makedirs(self.state, exist_ok=True)
        task = {'id': 't1', 'title': 'task', 'repo': 'app', 'run': '1-1', 'status': 'running'}
        backlog.save(cfg, {'tasks': [task]})
        snap = mock.Mock()
        with mock.patch('tutti.failover.may_write', side_effect=[True, False]):
            self.assertIsNone(autopilot.checkpoint_now(cfg, 100, snap=snap))
        snap.assert_not_called()
        self.assertEqual(backlog.load(cfg)['tasks'], [task])
        with mock.patch('tutti.failover.may_write', return_value=True), \
                mock.patch.object(autopilot, 'tree_of', return_value=None):
            results = autopilot.checkpoint_now(cfg, 100, snap=snap)
        self.assertIn('worktree unavailable', results[0][1]['error'])
        self.assertFalse(results[0][1]['pushed'])
        snap.assert_not_called()

    def test_no_new_snapshot_does_not_claim_a_push_or_reuse_a_failed_sha(self):
        cfg = self.cfg()
        task = {'id': 't1', 'title': 'task', 'repo': 'app', 'run': '1-1',
                'wip': {'sha': 'old-sha', 'pushed': False, 'error': 'push failed'}}
        push = mock.Mock()
        status = autopilot.checkpoint_status(cfg, task, '/example/worktree', 100, lambda *_: '', push)
        self.assertIsNone(status['sha'])
        self.assertFalse(status['pushed'])
        self.assertEqual(status['previous_sha'], 'old-sha')
        self.assertIn('unchanged or capture failed', status['error'])
        push.assert_not_called()
        task['wip'] = status
        again = autopilot.checkpoint_status(cfg, task, '/example/worktree', 101, lambda *_: '', push)
        self.assertEqual(again['previous_sha'], 'old-sha')
        self.assertFalse(again['pushed'])
        task.pop('wip')
        first = autopilot.checkpoint_status(cfg, task, '/example/worktree', 102, lambda *_: '', push)
        self.assertIsNone(first['sha'])
        self.assertFalse(first['pushed'])
        self.assertNotIn('previous_sha', first)

    def test_checkpoint_push_status_requires_a_new_sha_and_a_configured_remote(self):
        cfg = self.cfg()
        task = {'id': 't1', 'title': 'task', 'repo': 'app', 'run': '1-1'}
        def snap(*_args):
            return 'new-sha'
        with mock.patch.object(autopilot, 'wip_remote', return_value=''):
            push = mock.Mock()
            local = autopilot.checkpoint_status(cfg, task, '/example/worktree', 100, snap, push)
            self.assertEqual((local['sha'], local['pushed']), ('new-sha', False))
            self.assertIn('local only', local['error'])
            push.assert_not_called()
        with mock.patch.object(autopilot, 'wip_remote', return_value='origin'):
            good = autopilot.checkpoint_status(cfg, task, '/example/worktree', 100, snap, lambda *_: '')
            failed = autopilot.checkpoint_status(cfg, task, '/example/worktree', 100, snap, lambda *_: 'push failed')
        self.assertEqual((good['sha'], good['pushed'], good['error']), ('new-sha', True, None))
        self.assertEqual((failed['sha'], failed['pushed'], failed['error']), ('new-sha', False, 'push failed'))

    def test_checkpoint_exceptions_clear_stale_success(self):
        cfg = self.cfg()
        task = {'id': 't1', 'title': 'task', 'repo': 'app', 'run': '1-1',
                'wip': {'sha': 'old-sha', 'pushed': True}}
        def broken(*_args):
            raise OSError('unavailable')
        capture = autopilot.checkpoint_status(cfg, task, '/example/worktree', 100, broken, mock.Mock())
        self.assertFalse(capture['pushed'])
        self.assertIsNone(capture['sha'])
        self.assertEqual(capture['previous_sha'], 'old-sha')
        self.assertIn('capture failed', capture['error'])
        with mock.patch.object(autopilot, 'wip_remote', return_value='origin'):
            pushed = autopilot.checkpoint_status(cfg, task, '/example/worktree', 100, lambda *_: 'new-sha', broken)
        self.assertFalse(pushed['pushed'])
        self.assertEqual(pushed['sha'], 'new-sha')
        self.assertIn('push failed', pushed['error'])

    def test_running_checkpoint_round_records_uncertain_capture(self):
        cfg = self.cfg()
        task = {'id': 't1', 'title': 'task', 'repo': 'app', 'run': '1-1', 'status': 'running', 'started': 1,
                'wip': {'sha': 'old-sha', 'pushed': False}}
        with mock.patch.object(autopilot, 'tree_of', return_value='/example/worktree'):
            autopilot.checkpoints(cfg, {'tasks': [task]}, 1000, snap=lambda *_: '', push=mock.Mock())
        self.assertEqual(task['checkpoint'], 1000)
        self.assertIsNone(task['wip']['sha'])
        self.assertFalse(task['wip']['pushed'])
        self.assertEqual(task['wip']['previous_sha'], 'old-sha')

    def repo(self, name):
        wt = os.path.join(self.tmp.name, name)
        os.makedirs(wt)
        git(wt, 'init', '-q', '-b', 'main')
        with open(os.path.join(wt, 'a.txt'), 'w', encoding='utf8') as f:
            f.write('base\n')
        git(wt, 'add', '-A')
        git(wt, 'commit', '-qm', 'base')
        return wt

    def test_move_now_from_an_away_laptop_resumes_from_the_synced_checkpoint(self):
        laptop = self.repo('laptop')
        bare = os.path.join(self.tmp.name, 'origin.git')
        git(self.tmp.name, 'init', '-q', '--bare', '-b', 'main', bare)
        git(laptop, 'push', '-q', bare, 'main')
        with open(os.path.join(laptop, 'a.txt'), 'a', encoding='utf8') as f:
            f.write('half done\n')
        with open(os.path.join(laptop, 'new.txt'), 'w', encoding='utf8') as f:
            f.write('untracked too\n')
        head = git(laptop, 'rev-parse', 'HEAD')
        sha = autopilot.checkpoint(laptop, 'refs/heads/tutti/wip/fix-1', 'r1')
        self.assertTrue(sha)
        self.assertEqual(git(laptop, 'rev-parse', 'HEAD'), head)                      # the worker's tree is not touched
        self.assertIn('new.txt', git(laptop, 'status', '--porcelain'))
        self.assertEqual(autopilot.push_wip(laptop, sha, 'fix-1', bare), '')
        self.assertEqual(git(bare, 'for-each-ref', '--format=%(refname)'), 'refs/heads/main\nrefs/tutti/wip/fix-1')   # never main
        nuc = os.path.join(self.tmp.name, 'nuc')
        git(self.tmp.name, 'clone', '-q', bare, nuc)
        done = autopilot.reconcile(nuc, 'refs/heads/tutti/wip/fix-1', bare)
        self.assertIn('restored', done)
        with open(os.path.join(nuc, 'new.txt'), encoding='utf8') as f:
            self.assertEqual(f.read(), 'untracked too\n')
        self.assertIn('kept as they are', autopilot.reconcile(nuc, 'refs/heads/tutti/wip/fix-1', bare))   # never twice


class Moves(Base):
    def test_move_now_pins_a_shard_to_the_chosen_target_and_a_bump_goes_first(self):
        cfg = self.cfg({'e2e': {'kind': 'e2e', 'targets': ['machine'], 'run': 'go {shard}', 'shards': ['x', 'y'], 'machines': ['a']}})
        self.tick(cfg, [], [machine('a')])
        self.tick(cfg, [], [machine('a', False)], now=NOW + 300)
        waiting = jobmove.waiting(cfg, NOW + 300)
        self.assertEqual({j['id'] for j in waiting}, {'shard:Radio:e2e/x', 'shard:Radio:e2e/y'})
        jobmove.move(cfg, 'shard:Radio:e2e/y', 'b', bump=True, now=NOW + 301, stop=lambda pid: None)
        p = self.tick(cfg, [], [machine('a', False), machine('b')], now=NOW + 302)
        self.assertEqual([(t['id'], w['key']) for t, w in p['assign']], [('machine:b', 'e2e/y')])
        with self.assertRaises(jobmove.MoveError):
            jobmove.move(cfg, 'shard:Radio:e2e/x', 'toaster')

    def test_evacuate_moves_everything_waiting_on_a_machine(self):
        cfg = self.cfg({'e2e': {'kind': 'e2e', 'targets': ['machine'], 'run': 'go {shard}', 'shards': ['x', 'y'], 'machines': ['a']}})
        self.tick(cfg, [], [machine('a', True)])
        self.tick(cfg, [], [machine('a', False)], now=NOW + 300, stop=lambda pid: None)
        lines = jobmove.evacuate(cfg, 'a', NOW + 301, stop=lambda pid: None)
        self.assertEqual(len(lines), 2)
        self.assertEqual(sum('not moved' in line for line in lines), 1)
        p = self.tick(cfg, [], [machine('a', False), machine('b')], now=NOW + 302)
        self.assertEqual(len(p['assign']), 1)   # one free slot on b; the other waits for it, no longer for a
        self.assertEqual(len(jobmove.waiting(cfg, NOW + 302)), 1)  # the uncertain lease remains held

    def test_the_auto_move_timer_moves_only_urgent_work_after_its_minutes(self):
        tests = {'e2e': {'kind': 'e2e', 'targets': ['machine'], 'run': 'go {shard}', 'shards': ['x'], 'machines': ['a']}}
        cfg = self.cfg(tests, presence={'move_after_minutes': 10})
        self.tick(cfg, [], [machine('a')])
        self.tick(cfg, [], [machine('a', False)], now=NOW + 300, stop=lambda pid: None)
        self.assertEqual(jobmove.auto_moves(cfg, NOW + 400), [])                 # not urgent
        cfg['projects']['Radio']['urgent'] = True
        self.assertEqual(jobmove.auto_moves(cfg, NOW + 400), [])                 # not 10 minutes yet
        self.assertIn('not moved', jobmove.auto_moves(cfg, NOW + 1000)[0])
        self.assertEqual(jobmove.auto_moves({**cfg, 'presence': {}}, NOW + 2000), [])   # off by default

    def test_a_moved_task_is_queued_from_its_checkpoint_with_no_attempt_counted(self):
        cfg = self.cfg()
        cfg['autopilot']['repos'] = {'r': {'path': self.tmp.name, 'wip_push': False}}
        with backlog.locked(cfg) as data:
            data['tasks'].append({'id': 't9', 'title': 'Fix it', 'status': 'running', 'run': 'x', 'attempts': [], 'repo': 'r',
                                  'scope': [], 'worker': 'w'})
        killed = []
        with self.assertRaises(jobmove.MoveError):   # no ladder worker runs on b: refused, never pinned where nothing runs
            jobmove.move(cfg, 'task:t9', 'b', now=NOW)
        out = jobmove.move(cfg, 'task:t9', 'auto', bump=True, now=NOW, is_alive=lambda pid: True, kill=killed.append)
        t = backlog.find(backlog.load(cfg), 't9')
        self.assertEqual((t['status'], t['attempts'], t['pin'], t['urgent']), ('queued', [], None, True))
        self.assertIn('moved this task', t['resume_note'])
        self.assertIn('the best available worker', out)

    def test_the_returning_laptop_never_runs_moved_work_a_second_time(self):
        state = household.new_state('home', NOW)
        for u, n in (('u1', 'Ana'), ('u2', 'Bo')):
            household.join(state, u, u, n, 'claude', 'tok', NOW)
        household.sync_project(state, 'gh/x', {'publish': [{'title': 'Fix login', 'scope': ['src/login.py']}],
                                                'held': ['fix login']}, 'u1', NOW, 30)
        self.assertIn('moved from Ana', household.reassign(state, 'gh/x', 'fix login', 'u2', NOW + 60))
        back = household.sync_project(state, 'gh/x', {'held': ['fix login']}, 'u1', NOW + 120, 30)   # Ana's laptop is back
        self.assertEqual(back['mine'], [])
        self.assertEqual(back['held'], {'fix login': 'Bo'})
        household.reassign(state, 'gh/x', 'fix login', None, NOW + 3700)   # freed: Ana still never takes it back
        self.assertEqual(household.claim(state, household.lease_key('gh/x', 'fix login'), 'u1', NOW + 3800, 30), (False, None))
        cfg = self.cfg()
        data = {'tasks': [{'id': 't1', 'title': 'Fix login', 'status': 'running', 'run': 'x', 'started': NOW, 'attempts': [],
                           'repo': 'r', 'scope': ['src/login.py']}]}
        killed = []
        with mock.patch.object(autopilot.delegate, 'paths', return_value={'pid': os.devnull}):
            moved, _, _ = sharing.moved_of({'keys': {'r': 'gh/x'}, 'answer': {'projects': {'gh/x': back}}}, data)
            events = autopilot.moved_away(cfg, data, moved,
                                          lambda pid: True, killed.append)
        self.assertEqual(data['tasks'][0]['status'], 'moved')
        self.assertIn('not run here again', events[0])

    def test_moved_worktree_is_preserved_when_snapshot_succeeds_or_fails(self):
        cfg = self.cfg()
        wt = os.path.join(self.tmp.name, 'moved-work')
        os.makedirs(wt)
        path = os.path.join(wt, 'a.txt')
        with open(path, 'w', encoding='utf8') as f:
            f.write('unfinished work\n')
        with open(os.path.join(wt, 'new.txt'), 'w', encoding='utf8') as f:
            f.write('untracked work\n')
        for outcome in ('backup-sha', '', OSError('git failed')):
            task = {'id': 't1', 'title': 'Fix login', 'status': 'running', 'run': 'x', 'repo': 'r'}
            snap = mock.Mock(side_effect=outcome if isinstance(outcome, OSError) else None,
                             return_value=outcome if isinstance(outcome, str) else None)
            with mock.patch.object(autopilot, 'stop_run', return_value=wt):
                event = autopilot.give_up_here(cfg, task, 'holder', lambda _: True, lambda _: None, snap=snap)
            self.assertEqual(task['status'], 'moved')
            self.assertIn('not run here again', event)
            self.assertIn('backup at' if outcome == 'backup-sha' else 'backup unconfirmed', task['recovery_note'])
            with open(path, encoding='utf8') as f:
                self.assertEqual(f.read(), 'unfinished work\n')
            self.assertTrue(os.path.exists(os.path.join(wt, 'new.txt')))

    def test_a_viewer_cannot_move_work(self):
        cfg = self.cfg()
        viewer = SimpleNamespace(cfg=cfg, authorize=lambda headers, need: (403, 'viewers cannot move work: ask an admin'))
        headers = {'Host': '127.0.0.1:4777', 'Origin': 'http://127.0.0.1:4777', 'Content-Type': 'application/json',
                   'X-Tutti-Token': 'tok', 'Content-Length': '40'}
        body = b'{"job": "shard:Radio:e2e/a", "to": "auto"}'
        code, answer = dashboard.post(viewer, headers, '/api/work/move', 4777, 'tok', lambda n: body)
        self.assertEqual((code, answer['error']), (403, 'viewers cannot move work: ask an admin'))
        admin = SimpleNamespace(cfg=cfg, authorize=lambda headers, need: None)
        missing = b'{"job": "task:nope", "to": "auto"}'
        self.assertEqual(dashboard.post(admin, headers, '/api/work/move', 4777, 'tok', lambda n: missing)[0], 409)


if __name__ == '__main__':
    unittest.main()
