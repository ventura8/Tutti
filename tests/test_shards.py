"""Suites in depth (tutti/shards.py with the dispatcher): a shard's own devices, extra arguments and held devices, the build
once per commit before any shard, judging a run's output (verdict, started, requeue_on), the cooldown after a requeue,
other tools' lock files around a device run, and the project's report. Fake spawns and local bash only."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from tutti import config, devices, dispatch, shards

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = float(int(time.time()))


def device(name, status='ready', reason='ready: takes the next run'):
    return {'name': name, 'serial': f'S-{name}', 'machine': 'pc', 'status': status, 'reason': reason}


class FakeSpawn:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return SimpleNamespace(pid=4242)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, 'state')

    def cfg(self, suite=None, project=None):
        e2e = {'kind': 'e2e', 'targets': ['android'], 'run': 'run --filter {shard} {extra}',
               'shards': ['Search', {'name': 'Cast', 'devices': ['phone'], 'extra': '-CastTablet S-tablet', 'holds': ['tablet']}],
               **(suite or {})}
        raw = {'project': {'state': self.state}, 'machines': {'pc': {'host': 'pc.example'}},
               'devices': {'phone': {'serial': 'S-phone', 'machine': 'pc'}, 'tablet': {'serial': 'S-tablet', 'machine': 'pc'}},
               'projects': {'Radio': {'match': 'radio', 'tests': {'e2e': e2e}, **(project or {})}}}
        path = os.path.join(self.tmp.name, 'tutti.config.yaml')
        with open(path, 'w', encoding='utf8') as f:
            json.dump(raw, f)
        return config.load(path)

    def tick(self, cfg, rows, now=NOW, spawn=None, alive=lambda pid: True):
        return dispatch.tick(cfg, rows, [], now=now, is_alive=alive, spawn=spawn or FakeSpawn())


class ShardOptionsTest(Base):
    def test_a_shard_holding_the_tablet_goes_to_the_phone_only_with_the_tablet_free(self):
        cfg = self.cfg({'shards': [{'name': 'Cast', 'devices': ['phone'], 'extra': '-CastTablet S-tablet', 'holds': ['tablet']}]})
        p = self.tick(cfg, [device('phone'), device('tablet', 'busy', 'held by phone-e2e subagent')])
        self.assertEqual(p['assign'], [])
        self.assertIn('also need tablet', p['blocked']['device:phone'])
        spawn = FakeSpawn()
        p = self.tick(cfg, [device('phone'), device('tablet')], now=NOW + 1, spawn=spawn)
        self.assertEqual([(t['id'], w['key']) for t, w in p['assign']], [('device:phone', 'e2e/Cast')])
        lease = next(iter(dispatch.read_state(cfg)['leases'].values()))
        self.assertEqual((lease['holds'], lease['extra']), (['tablet'], '-CastTablet S-tablet'))
        argv = dispatch.test_argv(cfg, lease, dispatch.suites(cfg)[0], 'Cast')
        self.assertEqual(argv[-4:], ['--hold-device', 'tablet', 'dispatch-radio-e2e', 'run --filter Cast -CastTablet S-tablet'])
        # the held tablet counts as busy: nothing else goes to it while the cast shard runs
        st = dispatch.read_state(cfg)
        tgts = dispatch.targets([device('phone'), device('tablet')], [], st)
        self.assertTrue(next(t for t in tgts if t['id'] == 'device:tablet')['busy'])

    def test_the_tablet_never_takes_a_phone_only_shard_and_both_share_the_rest(self):
        cfg = self.cfg()
        p = self.tick(cfg, [device('phone'), device('tablet')])
        got = {t['id']: w['shard'] for t, w in p['assign']}
        # Cast needs the phone and holds the tablet, so the round gives the phone Cast or Search, never Cast to the tablet
        self.assertNotEqual(got.get('device:tablet'), 'Cast')
        self.assertTrue(got)

    def test_bad_suite_settings_are_refused(self):
        with self.assertRaises(config.ConfigError) as e:
            self.cfg({'verdict': {'pass': '('}, 'started': {'pattern': 'x'}, 'prepare': {'run': ''}})
        text = str(e.exception)
        for part in ('verdict.pass', 'started is', 'prepare is'):
            self.assertIn(part, text)

    def test_a_shard_this_version_cannot_read_is_skipped_with_a_warning_not_a_config_error(self):
        cfg = self.cfg({'shards': ['Search', {'name': 'Future', 'devices': ['phone'], 'needs_wifi_6': True},
                                   {'name': 'Gone', 'holds': ['nope']}, ['odd'], {'name': 'X', 'extra': 3}]})
        self.assertEqual(dispatch.suites(cfg)[0]['shards'], ['Search'])
        warns = ' '.join(cfg['_warnings'])
        for part in ('shards[1]: shard Future has needs_wifi_6', 'shards[2]: shard Gone names device nope', 'shards[3]',
                     'shards[4]: shard X has extra 3', 'this shard is skipped'):
            self.assertIn(part, warns)


class BuildTest(Base):
    def test_the_build_runs_once_per_commit_before_any_shard(self):
        cfg = self.cfg({'prepare': {'run': 'dotnet build', 'where': 'pc'}})
        spawn = FakeSpawn()
        p = self.tick(cfg, [device('phone'), device('tablet')], spawn=spawn)
        self.assertEqual(p['assign'], [])
        self.assertEqual(spawn.calls[0][-5:-1], ['dispatch', 'build', 'Radio', 'e2e'])
        self.assertIn('the build for this commit is running', p['blocked']['device:tablet'])
        b = dispatch.read_state(cfg)['projects']['Radio']['prepare']['e2e']
        with open(b['rc'], 'w', encoding='utf8') as f:
            f.write('0\n')
        spawn = FakeSpawn()
        p = self.tick(cfg, [device('phone'), device('tablet')], now=NOW + 30, spawn=spawn)
        self.assertEqual([w['key'] for _, w in p['assign']], ['e2e/Cast'])   # Cast on the phone holds the tablet too
        self.assertNotIn('build', ' '.join(spawn.calls[0]))   # built once: no second build on the same commit

    def test_a_failed_build_holds_the_shards_with_its_reason(self):
        cfg = self.cfg({'prepare': {'run': 'dotnet build', 'where': 'pc'}})
        self.tick(cfg, [device('tablet')])
        b = dispatch.read_state(cfg)['projects']['Radio']['prepare']['e2e']
        with open(b['rc'], 'w', encoding='utf8') as f:
            f.write('1\n')
        p = self.tick(cfg, [device('tablet')], now=NOW + 30)
        self.assertIn('the build failed (exit 1', p['blocked']['device:tablet'])
        self.assertTrue(shards.prepare_due(dispatch.read_state(cfg)['projects']['Radio'], dispatch.suites(cfg)[0],
                                           NOW + 30 + shards.PREPARE_RETRY))

    def test_run_prepare_writes_the_exit_code(self):
        cfg = self.cfg({'prepare': {'run': 'exit 3'}})
        rc = os.path.join(self.tmp.name, 'b.rc')
        self.assertEqual(shards.run_prepare(cfg, 'Radio', 'e2e', rc), 3)
        with open(rc, encoding='utf8') as f:
            self.assertEqual(f.read(), '3\n')


class JudgeTest(Base):
    def test_output_decides_requeue_pass_and_fail(self):
        s = {'requeue_on': [r'Another app in front'], 'verdict': {'pass': r'\[E2E\] summary', 'fail': r'\[E2E\] FAIL|FATAL'}}
        self.assertTrue(shards.judge(s, 'Another app in front at 3 min', 0).startswith(shards.REQUEUE))
        self.assertEqual(shards.judge(s, '[E2E] PASS a\n[E2E] summary 1/1', 0), 0)
        self.assertEqual(shards.judge(s, '[E2E] FAIL b\n[E2E] summary 0/1', 0), 1)
        self.assertEqual(shards.judge(s, 'Waiting for the phone to be unlocked.', 0), 1)   # no summary: no test ran
        self.assertEqual(shards.judge({}, 'x', 5), 5)

    def test_a_run_whose_tests_never_start_is_stopped_and_queued_again_then_the_device_rests(self):
        s = {'started': {'pattern': 'Unlocked; running', 'within_seconds': 120}}
        self.assertEqual(shards.not_started(s, 'tutti: device ready\nWaiting to be unlocked.', 60), '')
        why = shards.not_started(s, 'tutti: device ready\nWaiting to be unlocked.', 121)
        self.assertTrue(why.startswith(shards.REQUEUE))
        self.assertEqual(shards.not_started(s, 'Unlocked; running Search', 500), '')
        cfg = self.cfg({'shards': ['Search']})
        self.tick(cfg, [device('tablet')])
        lid = next(iter(dispatch.read_state(cfg)['leases']))
        with open(dispatch.paths(cfg, lid)['rc'], 'w', encoding='utf8') as f:
            f.write(why + '\n')
        p = self.tick(cfg, [device('tablet')], now=NOW + 200)
        sh = dispatch.read_state(cfg)['projects']['Radio']['shards']['e2e/Search']
        self.assertEqual((sh['status'], sh['attempts']), ('queued', 0))
        self.assertIn('left alone', p['blocked']['device:tablet'])
        self.assertEqual(len(self.tick(cfg, [device('tablet')], now=NOW + 200 + 11 * 60)['assign']), 1)

    def test_supervise_stops_a_run_that_never_starts(self):
        cfg = self.cfg({'started': {'pattern': 'Unlocked', 'within_seconds': 0.2}})
        log = os.path.join(self.tmp.name, 'run.log')
        beat = os.path.join(self.tmp.name, 'beat')
        with open(log, 'w', encoding='utf8') as f:
            f.write('tutti: device ready\n')
        open(beat, 'w').close()
        cfg['dispatch']['call_poll_seconds'] = 0.05
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        lease = {'kind': 'android', 'name': 'tablet'}
        rc = dispatch.supervise(cfg, lease, dispatch.suites(cfg)[0], child, beat, look=lambda c, d: 0, log=log)
        self.assertTrue(rc.startswith(shards.REQUEUE))
        self.assertIsNotNone(child.poll())


class ExternalLockTest(Base):
    def run_wrapped(self, lock, command, timeout=10):
        d = {'serial': 'S1', 'external_lock': lock.replace('S1', '{serial}')}
        line = devices.external_wrap([d], command)
        out = os.path.join(self.tmp.name, 'out.txt')
        with open(out, 'w', encoding='utf8') as f:   # a file, not a pipe: the waiting `sleep` must not hold the test open
            child = subprocess.Popen(['bash', '-c', line], stdout=f, stderr=f, start_new_session=True)
        try:
            rc = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, 9)
            child.wait()
            raise
        with open(out, encoding='utf8') as f:
            return SimpleNamespace(returncode=rc, stdout=f.read())

    def test_the_lock_file_is_written_while_the_command_runs_and_removed_after(self):
        lock = os.path.join(self.tmp.name, 'adb-S1.lock')
        r = self.run_wrapped(lock, f'cat {lock}; exit 4')
        self.assertEqual(r.returncode, 4)
        self.assertIn('tutti: device ready', r.stdout)
        self.assertRegex(r.stdout, r'tutti \d{4}-\d\d-\d\d')
        self.assertFalse(os.path.exists(lock))

    def test_a_fresh_lock_of_another_tool_makes_it_wait_and_a_stale_one_does_not(self):
        lock = os.path.join(self.tmp.name, 'adb-S1.lock')
        with open(lock, 'w', encoding='utf8') as f:
            f.write('phone-e2e subagent 2026-11-14T21:44\n')
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_wrapped(lock, 'echo ran', timeout=2)
        old = time.time() - 31 * 60
        os.utime(lock, (old, old))
        self.assertIn('ran', self.run_wrapped(lock, 'echo ran').stdout)
        self.assertEqual(devices.external_wrap([{'serial': 'S1'}], 'echo x'), 'echo x')

    def test_the_readings_say_who_holds_it(self):
        from tests.test_devices import fake_adb
        lock = os.path.join(self.tmp.name, 'adb-S1.lock')
        with open(lock, 'w', encoding='utf8') as f:
            f.write('phone-e2e subagent (cast) 2026-11-14\r\n')
        r = subprocess.run(['bash', os.path.join(ROOT, 'scripts', 'device-readings.sh')], capture_output=True, text=True, check=True,
                           env={**os.environ, 'DEVICE_SERIAL': 'R58', 'ADB': fake_adb(self.tmp.name), 'EXT_LOCK': lock,
                                'EXT_STALE': '1800'})
        got = devices.parse_readings(r.stdout)
        self.assertEqual(got['external'], 1)
        st, why = devices.status(devices.E2E_POLICY, {**got, 'battery': 80, 'battery_c': 30})
        self.assertEqual(st, 'busy')
        self.assertIn('held by phone-e2e subagent (cast)', why)


MDNS_ADB = """#!/usr/bin/env bash
# a fake adb whose TV is listed only after `adb connect` to the ip:port that `adb mdns services` gives today
case "$1" in
  devices) printf 'List of devices attached\\r\\n'; [ -e "$MARK" ] && printf 'adb-TV1-ab._adb-tls-connect._tcp\\tdevice\\r\\n'; exit 0;;
  mdns) printf 'List of discovered mdns services\\nadb-TV1-ab\\t_adb-tls-connect._tcp\\t192.0.2.7:42483\\n'; exit 0;;
  connect) [ "$2" = 192.0.2.7:42483 ] && touch "$MARK"; echo "connected to $2"; exit 0;;
esac
exit 0
"""


class TvTest(Base):
    def test_a_tv_takes_only_suites_that_target_tv_and_needs_no_battery(self):
        cfg = self.cfg({'shards': ['Search']})
        st, why = devices.status(devices.policy({'kind': 'tv'}), {'adb': 'ok', 'state': 'device'})
        self.assertEqual(st, 'ready')
        tv = {**device('tablet'), 'kind': 'tv'}
        self.assertEqual(self.tick(cfg, [tv])['assign'], [])   # an android suite never runs on the TV
        self.assertEqual(dispatch.targets([tv], [], dispatch.read_state(cfg))[0]['kind'], 'tv')
        self.assertTrue(dispatch.compatible({'kind': 'tv', 'name': 'tablet'}, {'targets': ['tv'], 'devices': None}))

    def test_a_wireless_serial_whose_port_changed_is_found_again_through_mdns(self):
        folder = self.tmp.name
        adb = os.path.join(folder, 'adb')
        with open(adb, 'w', encoding='utf8') as f:
            f.write(MDNS_ADB)
        os.chmod(adb, 0o755)
        r = subprocess.run(['bash', os.path.join(ROOT, 'scripts', 'device-readings.sh')], capture_output=True, text=True, check=True,
                           env={**os.environ, 'DEVICE_SERIAL': 'adb-TV1-ab._adb-tls-connect._tcp', 'ADB': adb,
                                'MARK': os.path.join(folder, 'connected')})
        got = devices.parse_readings(r.stdout)
        self.assertEqual((got['state'], got['resolved']), ('device', '192.0.2.7:42483'))

    def test_bad_device_options_are_refused(self):
        errs = devices.errors({'machines': {'pc': {}}, 'devices': {'tv': {'serial': 'x', 'machine': 'pc', 'kind': 'fridge',
                                                                          'calls': 'no', 'external_lock': ''}}})
        text = ' '.join(errs)
        for part in ('kind is \'fridge\'', 'calls is \'no\'', 'external_lock is \'\''):
            self.assertIn(part, text)


class ReportTest(Base):
    def test_the_report_lists_failed_shards_with_the_end_of_their_log(self):
        log = os.path.join(self.tmp.name, 's.log')
        with open(log, 'w', encoding='utf8') as f:
            f.write('[E2E] FAIL SearchUITest.Finds\n[E2E] summary 3/4\n')
        p = {'commit': 'abc123def4567', 'shards': {'e2e/Search': {'status': 'failed', 'attempts': 1, 'why': 'failed (exit 1)',
                                                                  'log': log, 'target': 'device:phone'},
                                                   'e2e/Cast': {'status': 'passed', 'attempts': 0}},
             'prepare': {'e2e': {'status': 'passed', 'why': 'built'}}}
        text = shards.report('Radio', p, NOW)
        for part in ('Commit `abc123def456`', '1 failed, 1 passed', '### e2e/Search (1 attempt(s), on device:phone)',
                     '[E2E] FAIL SearchUITest.Finds', 'build of e2e: passed'):
            self.assertIn(part, text)
        out = os.path.join(self.tmp.name, 'shared', 'radio-e2e.md')
        cfg = self.cfg(project={'report': {'path': out}})
        self.assertTrue(shards.write_report(cfg, 'Radio', p, NOW))
        with open(out, encoding='utf8') as f:
            self.assertIn('## Failed', f.read())


if __name__ == '__main__':
    unittest.main()
