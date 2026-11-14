"""Android devices: readings and the safe-use policy, the config (e2e targets, Termux machines opt-in and last), the adb readings
script against a fake adb, routing of a Termux machine, and `tutti test --device` holding the device lock over the `local`
transport. No real phone: every adb answer comes from a fake on PATH."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from tutti import config, devices, remote, routing
from tutti.routing import Probe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, 'scripts', 'device-readings.sh')
GOOD = {'adb': 'ok', 'state': 'device', 'battery': 80, 'charging': 1, 'battery_c': 31.5, 'screen': 1, 'wifi': 1}

FAKE_ADB = textwrap.dedent('''\
    #!/usr/bin/env bash
    # a fake adb: FAKE_STATE (device), FAKE_LEVEL, FAKE_USB (true|false), FAKE_TEMP (tenths), FAKE_WAKE, FAKE_WIFI
    if [ "$1" = devices ]; then
      printf 'List of devices attached\\r\\n'
      [ "${FAKE_STATE:-device}" = missing ] || printf 'R58\\t%s\\r\\n' "${FAKE_STATE:-device}"
      exit 0
    fi
    shift 2; [ "$1" = shell ] && shift
    case "$*" in
      "dumpsys battery") printf '  AC powered: false\\n  USB powered: %s\\n  level: %s\\n  temperature: %s\\n' \\
          "${FAKE_USB:-true}" "${FAKE_LEVEL:-80}" "${FAKE_TEMP:-315}";;
      "dumpsys power") echo "  mWakefulness=${FAKE_WAKE:-Awake}";;
      "cmd wifi status") echo "${FAKE_WIFI:-Wifi is connected to \\"lab\\"}";;
      "input keyevent KEYCODE_WAKEUP") echo wake >> "${FAKE_LOG:-/dev/null}";;
    esac
''')


def fake_adb(folder):
    path = os.path.join(folder, 'adb')
    with open(path, 'w', encoding='utf8') as f:
        f.write(FAKE_ADB)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


class PolicyTest(unittest.TestCase):
    def test_parse_readings_takes_numbers_and_words_and_skips_noise(self):
        r = devices.parse_readings('adb=ok\nstate=device\nbattery=80\nbattery_c=31.5\nnoise\n=x\n')
        self.assertEqual(r, {'adb': 'ok', 'state': 'device', 'battery': 80, 'battery_c': 31.5})
        self.assertEqual(devices.parse_readings('C box 2m\nR battery=55\nR wifi=1\n2 0.1 8 0', prefix='R '), {'battery': 55, 'wifi': 1})

    def test_a_charged_cool_device_is_ready(self):
        self.assertEqual(devices.unfit(devices.E2E_POLICY, GOOD), '')
        self.assertEqual(devices.unfit(devices.TERMUX_POLICY, {**GOOD, 'screen': 0}), '')
        self.assertEqual(devices.status(devices.E2E_POLICY, GOOD, 'R58'), ('ready', 'ready: takes the next run'))

    def test_each_refusal_names_the_cause_and_the_fix(self):
        cases = [({'adb': 'missing'}, 'adb is not installed'),
                 ({'adb': 'broken'}, 'needs WSL interop'), ({**GOOD, 'state': 'unauthorized'}, 'tap Allow'),
                 ({**GOOD, 'state': 'offline'}, 'adb connect R58'), ({'adb': 'ok', 'state': 'missing'}, 'not connected'),
                 ({**GOOD, 'battery': None}, 'battery not readable'), ({**GOOD, 'battery': 20}, 'battery 20 % is under 50 %'),
                 ({**GOOD, 'charging': 0}, 'not charging'), ({**GOOD, 'battery_c': 41}, 'battery 41 C'),
                 ({**GOOD, 'wifi': 0}, 'not on Wi-Fi'), ({**GOOD, 'screen': 1, 'wifi': 1}, 'screen is on')]
        for readings, words in cases:
            with self.subTest(words=words):
                self.assertIn(words, devices.unfit(devices.TERMUX_POLICY, readings, 'R58'))

    def test_an_unreadable_battery_temperature_refuses_and_an_unanswering_machine_is_named(self):
        self.assertIn('temperature not readable', devices.unfit(devices.E2E_POLICY, {**GOOD, 'battery_c': None}))
        self.assertIn('did not answer over ssh', devices.unfit(devices.E2E_POLICY, {'state': 'unreachable'}))

    def test_an_unquoted_off_reads_as_off(self):
        self.assertEqual(devices.policy({'policy': {'screen': False}})['screen'], 'off')
        self.assertEqual(devices.policy_errors('d', {'screen': False}), [])

    def test_a_termux_machine_without_termux_api_says_to_install_it(self):
        self.assertIn('Termux:API', devices.unfit(devices.TERMUX_POLICY, {}))

    def test_statuses_and_the_dark_screen_note(self):
        self.assertEqual(devices.status(devices.E2E_POLICY, {'adb': 'ok', 'state': 'missing'})[0], 'offline')
        self.assertEqual(devices.status(devices.E2E_POLICY, {**GOOD, 'battery': 5})[0], 'waiting')
        self.assertEqual(devices.status(devices.E2E_POLICY, {**GOOD, 'locked': 1})[0], 'busy')
        self.assertIn('Stay awake', devices.status(devices.E2E_POLICY, {**GOOD, 'screen': 0})[1])

    def test_policy_errors(self):
        errs = '\n'.join(devices.policy_errors('devices.p', {'min_battery': 150, 'require_charging': 'yes', 'screen': 'dim'}))
        for key in ('policy.min_battery', 'policy.require_charging', 'policy.screen'):
            self.assertIn(key, errs)
        self.assertIn('write it as', devices.policy_errors('devices.p', 'strict')[0])
        self.assertEqual(devices.policy_errors('devices.p', {'min_battery': 40, 'wifi_only': True, 'screen': 'off'}), [])


class ConfigTest(unittest.TestCase):
    def test_a_device_gets_its_machine_and_lock(self):
        cfg = config.normalise({'machines': {'nuc': {'host': 'nuc.example'}, 'here': {'host': 'local'}},
                                'devices': {'pixel': {'serial': 'R58'}}})
        self.assertEqual(cfg['devices']['pixel']['machine'], 'here')
        self.assertEqual(cfg['devices']['pixel']['lock'], '~/.tutti/locks/device-pixel.lock')
        self.assertEqual(config.validate(cfg), [])

    def test_device_errors_name_the_key(self):
        cfg = config.normalise({'machines': {'here': {'host': 'local'}},
                                'devices': {'p': {'machine': 'ghost', 'policy': {'min_battery': -1}}}})
        errs = '\n'.join(config.validate(cfg))
        for key in ('devices.p.serial', 'devices.p.machine', 'devices.p.policy.min_battery'):
            self.assertIn(key, errs)

    def test_a_termux_machine_runs_natively_routes_last_and_is_off_until_opted_in(self):
        cfg = config.normalise({'runner': {'kind': 'docker', 'image': 'img'},
                                'machines': {'phone': {'transport': 'android', 'host': 'phone.example'}, 'nuc': {'host': 'nuc.example'}},
                                'routing': {'order': ['phone', 'nuc']}})
        m = cfg['machines']['phone']
        self.assertEqual((m['runner']['kind'], m['port'], m['slots'], m['min_free_gb']), ('native', 8022, 1, 2))
        self.assertTrue(m['lock_prefix'].startswith('/data/data/com.termux/'))
        self.assertEqual(cfg['routing']['order'], ['nuc', 'phone'])
        self.assertEqual(config.validate(cfg), [])
        self.assertIn('opt-in', routing.unfit(m, Probe(True, 1, 0, 8, readings=GOOD)))
        self.assertEqual(routing.unfit({**m, 'enabled': True}, Probe(True, 1, 0, 8, readings={**GOOD, 'screen': 0})), '')

    def test_a_termux_machine_refuses_docker_and_a_bad_port(self):
        phone = {'transport': 'android', 'host': 'p', 'port': 'x', 'runner': {'kind': 'docker', 'image': 'i'}}
        cfg = config.normalise({'machines': {'p': phone}})
        errs = '\n'.join(config.validate(cfg))
        self.assertIn('Android has no Docker', errs)
        self.assertIn('machines.p.port', errs)


class RoutingTest(unittest.TestCase):
    PHONE = {'transport': 'android', 'enabled': True, 'slots': 1}

    def test_a_phone_on_battery_is_skipped_and_never_the_queue(self):
        machines = {'nuc': {}, 'phone': self.PHONE}
        probes = {'nuc': Probe(True, 0, 1, 8), 'phone': Probe(True, 1, 0, 8, readings={**GOOD, 'charging': 0})}
        c = routing.choose(['nuc', 'phone'], machines, probes.get)
        self.assertEqual((c.machine, c.queued), ('nuc', True))
        self.assertIn('phone: not charging', c.reason)

    def test_a_charging_phone_takes_a_run_when_the_others_are_full(self):
        machines = {'nuc': {}, 'phone': self.PHONE}
        probes = {'nuc': Probe(True, 0, 1, 8), 'phone': Probe(True, 1, 0, 8, readings={**GOOD, 'screen': 0})}
        self.assertEqual(routing.choose(['nuc', 'phone'], machines, probes.get).machine, 'phone')

    def test_nothing_is_queued_when_every_machine_is_held(self):
        machines = {'nuc': {'paused': {'by': 'me', 'at': 0}}, 'phone': {**self.PHONE}}
        probes = {'nuc': Probe(True, 2, 0, 8), 'phone': Probe(True, 1, 0, 8, readings={**GOOD, 'charging': 0})}
        with self.assertRaises(routing.NoMachine) as e:
            routing.choose(['nuc', 'phone'], machines, probes.get)
        self.assertIn('resume one', str(e.exception))

    def test_a_paused_machine_takes_nothing_and_is_not_the_queue(self):
        machines = {'nuc': {'paused': {'by': 'me (dashboard)', 'at': 0}}, 'pc': {}}
        probes = {'nuc': Probe(True, 2, 0, 8), 'pc': Probe(True, 0, 0, 8)}
        c = routing.choose(['nuc', 'pc'], machines, probes.get)
        self.assertEqual((c.machine, c.queued), ('pc', True))
        self.assertIn('paused by me (dashboard)', c.reason)


class SelectionTest(unittest.TestCase):
    def cfg(self, **devs):
        return {'machines': {'pc': {}, 'nuc': {'paused': {'by': 'me', 'at': 0}}},
                'devices': {n: {'name': n, 'serial': n, 'machine': d.get('machine', 'pc'), **d} for n, d in devs.items()}}

    def test_any_prefers_a_free_device_over_a_busy_one(self):
        cfg = self.cfg(a={}, b={})
        readings = {'a': {**GOOD, 'locked': 1}, 'b': GOOD}
        d, _ = devices.first_ready(cfg, devices.candidates(cfg, 'any'), lambda m, d, wake: readings[d['name']])
        self.assertEqual(d['name'], 'b')
        readings['b'] = {**GOOD, 'battery': 1}
        d, why = devices.first_ready(cfg, devices.candidates(cfg, 'any'), lambda m, d, wake: readings[d['name']])
        self.assertEqual(d['name'], 'a')   # busy but fit: the run waits in its lock's queue
        self.assertIn('b: battery 1 %', why)

    def test_a_device_whose_machine_is_paused_is_held(self):
        cfg = self.cfg(a={'machine': 'nuc'})
        d, why = devices.first_ready(cfg, devices.candidates(cfg, 'a'), lambda *x: self.fail('not asked'))
        self.assertIsNone(d)
        self.assertIn('its machine nuc is paused by me', why)
        self.assertEqual(devices.devices_state(cfg, lambda *x: self.fail('not asked'))[0]['status'], 'held')

    def test_the_wake_key_is_pressed_only_for_screen_wake(self):
        woke = []
        cfg = self.cfg(a={'policy': {'screen': 'off'}})
        devices.first_ready(cfg, devices.candidates(cfg, 'a'), lambda m, d, wake: woke.append(wake) or {**GOOD, 'screen': 0})
        cfg = self.cfg(a={})
        devices.first_ready(cfg, devices.candidates(cfg, 'a'), lambda m, d, wake: woke.append(wake) or GOOD)
        self.assertEqual(woke, [False, True])

    def test_waiting_ends_with_the_reason(self):
        cfg = self.cfg(a={})
        said = []
        with self.assertRaises(config.ConfigError) as e:
            devices.wait_ready(cfg, 'a', lambda m, d, wake: {**GOOD, 'battery': 3}, said.append, sleep=lambda s: None, wait_minutes=2)
        self.assertIn('no device got ready in 2 min', str(e.exception))
        self.assertEqual(len(said), 1)   # said once, not at every poll
        with self.assertRaises(config.ConfigError):
            devices.candidates({'devices': {'a': {'paused': {'at': 0}}}}, 'any')


class TransportTest(unittest.TestCase):
    M = {'name': 'phone', 'transport': 'android', 'host': '10.0.0.5', 'user': 'u0_a1', 'port': 8022}

    def test_argv_rsh_and_destination(self):
        self.assertEqual(remote.argv(self.M, 'true')[-3:], ['-p', '8022', 'u0_a1@10.0.0.5'][-2:] + ['true'])
        self.assertIn('-p', remote.argv(self.M, 'true'))
        self.assertEqual(remote.rsh({}, self.M), 'ssh -p 8022')
        self.assertEqual(remote.dest({'host': 'h'}), 'h')

    def test_the_probe_reads_termux_power_only_on_android(self):
        self.assertIn('termux-battery-status', remote.probe_script({**self.M, 'yield_file': '~/.y', 'lock_prefix': '/t/', 'slots': 1}))
        self.assertNotIn('termux', remote.probe_script({'transport': 'ssh', 'yield_file': '~/.y', 'lock_prefix': '/t/', 'slots': 1}))

    def test_native_runner_is_niced_bash(self):
        m = {'name': 'phone', 'runner': {'kind': 'native', 'prelude': ''}}
        self.assertTrue(remote.runner_command(m, 't', 'pytest').startswith('nice -n 10 bash -c'))

    def test_device_wrap_sets_the_serial_and_holds_the_lock(self):
        line = remote.device_wrap({'serial': 'R58', 'lock': '/tmp/d.lock'}, 'X=1 bash slot.sh pytest')
        self.assertTrue(line.startswith('mkdir -p "$(dirname /tmp/d.lock)" && env ANDROID_SERIAL=R58 SLOT_LOCKS=/tmp/d.lock MIN_FREE_GB=0 '
                                        'bash {slot} env X=1'))
        self.assertIn('SLOT_LOCKS="$HOME"/.tutti/locks/d.lock', remote.device_wrap({'serial': 'R', 'lock': '~/.tutti/locks/d.lock'}, 'c'))
        self.assertEqual(remote.device_wrap(None, 'cmd'), 'cmd')


@unittest.skipUnless(shutil.which('flock'), 'needs flock(1), as on the Linux machines the scripts run on')
class ReadingsScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adb = fake_adb(self.tmp.name)
        self.lock = os.path.join(self.tmp.name, 'd.lock')

    def read(self, **env):
        r = subprocess.run(['bash', READINGS], env={**os.environ, 'DEVICE_SERIAL': 'R58', 'DEVICE_LOCK': self.lock, 'ADB': self.adb, **env},
                           capture_output=True, text=True, timeout=30, check=True)
        return devices.parse_readings(r.stdout)

    def test_a_good_device(self):
        log = os.path.join(self.tmp.name, 'wake')
        r = self.read(WAKE='1', FAKE_LOG=log)
        self.assertEqual(r, {'locked': 0, 'adb': 'ok', 'state': 'device', 'battery': 80, 'battery_c': 31.5, 'charging': 1,
                             'screen': 1, 'wifi': 1})
        with open(log, encoding='utf8') as f:
            self.assertEqual(f.read(), 'wake\n')

    def test_bad_states_and_readings(self):
        self.assertEqual(self.read(FAKE_STATE='unauthorized')['state'], 'unauthorized')
        self.assertEqual(self.read(FAKE_STATE='missing')['state'], 'missing')
        r = self.read(FAKE_USB='false', FAKE_LEVEL='12', FAKE_WAKE='Asleep', FAKE_WIFI='Wifi is disconnected')
        self.assertEqual((r['charging'], r['battery'], r['screen'], r['wifi']), (0, 12, 0, 0))
        self.assertEqual(self.read(ADB=os.path.join(self.tmp.name, 'no-adb'))['adb'], 'missing')

    def test_a_held_lock_reads_busy(self):
        with open(self.lock, 'w') as f:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            self.assertEqual(self.read()['locked'], 1)


@unittest.skipUnless(shutil.which('flock') and sys.platform.startswith('linux'), 'needs flock(1), as on Linux')
class DeviceRunTest(unittest.TestCase):
    """`tutti test --device` end to end over the local transport, against the fake adb."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        bindir = os.path.join(t, 'bin')
        os.makedirs(bindir)
        fake_adb(bindir)
        self.tree = os.path.join(t, 'app')
        os.makedirs(self.tree)
        self.lock = os.path.join(t, 'pixel.lock')
        self.cfg = os.path.join(t, 'tutti.config.json')
        import json
        with open(self.cfg, 'w') as f:
            json.dump({'project': {'state': os.path.join(t, 'state'), 'worktrees': t},
                       'machines': {'here': {'host': 'local', 'slots': 1, 'min_free_gb': 0, 'lock_prefix': os.path.join(t, 'slot-')}},
                       'devices': {'pixel': {'serial': 'R58', 'lock': self.lock}}}, f)
        self.env = {**os.environ, 'PATH': f'{bindir}:{os.environ["PATH"]}', 'SLOT_POLL': '0.1'}

    def tutti(self, *args, **kw):
        return subprocess.Popen([sys.executable, os.path.join(ROOT, 'bin', 'tutti'), '--config', self.cfg, *args], cwd=self.tree,
                                env={**self.env, **kw}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_the_command_gets_the_serial_and_a_job_record(self):
        p = self.tutti('test', '--device', 'pixel', '--', 'echo serial=$ANDROID_SERIAL')
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn('serial=R58', out)
        folder = os.path.join(self.tmp.name, 'state', 'devices')
        logs = [f for f in os.listdir(folder) if f.endswith('.log')]
        self.assertEqual(len(logs), 1)
        with open(os.path.join(folder, logs[0] + '.target')) as f:
            self.assertEqual(f.read().split(), ['machine:here', 'device:pixel'])
        with open(os.path.join(folder, logs[0])) as f:
            self.assertIn('serial=R58', f.read())

    def test_a_second_run_waits_for_the_device_lock(self):
        import fcntl
        f = open(self.lock, 'w')
        fcntl.flock(f, fcntl.LOCK_EX)
        p = self.tutti('test', '--device', 'pixel', '--', 'touch', 'ran')
        time.sleep(1.5)
        self.assertFalse(os.path.exists(os.path.join(self.tree, 'ran')))
        f.close()
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn('waiting for a free slot', out)   # slot.sh's line, in the job's output
        self.assertTrue(os.path.exists(os.path.join(self.tree, 'ran')))

    def test_a_flat_battery_refuses_with_the_reason(self):
        p = self.tutti('test', '--device', 'pixel', '--wait-minutes', '0', '--', 'touch', 'ran', FAKE_LEVEL='5')
        _out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 3)
        self.assertIn('battery 5 % is under 30 %', err)
        self.assertFalse(os.path.exists(os.path.join(self.tree, 'ran')))


if __name__ == '__main__':
    unittest.main()
