"""Device prep and restore (tutti/prep.py): the current values are read and saved before anything changes, exactly those are
put back when the run ends, a crash leaves a file the next round restores from, and a command the device lacks (`media`)
is skipped cleanly. A fake adb keeps the device's settings in a folder; nothing leaves the temp folder."""
import os
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from tutti import devices, dispatch, prep

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# adb -s <serial> shell '<command>': settings get|put|delete, cmd media_session volume, and no `media` binary
FAKE_ADB = r'''#!/usr/bin/env bash
if [ "$1" = devices ]; then printf 'List of devices attached\r\nTV1\tdevice\r\n'; exit 0; fi
shift 2; [ "$1" = shell ] && shift
set -- $*
d="$FAKE_DEVICE"
case "$1 $2" in
  "settings get") if [ -e "$d/$3.$4" ]; then cat "$d/$3.$4"; else echo null; fi;;
  "settings put") printf '%s' "$5" > "$d/$3.$4";;
  "settings delete") rm -f "$d/$3.$4";;
  "cmd media_session")
    if [ "$6" = --get ]; then echo "volume is $(cat "$d/volume") in range [0..25]"; else printf '%s' "$7" > "$d/volume"; fi;;
  "media volume") echo "/system/bin/sh: media: inaccessible or not found";;
esac
exit 0
'''

TV = {'name': 'tv', 'serial': 'TV1', 'machine': 'pc', '_night': {'on': False}, 'prep': [
    {'setting': 'global stay_on_while_plugged_in', 'value': 7},
    {'setting': 'secure screensaver_enabled', 'value': 0},
    {'setting': 'system screen_off_timeout', 'value': 2147483647},
    {'setting': 'secure sleep_timeout', 'value': -1},
    {'name': 'media volume', 'read': 'media volume --stream 3 --get', 'set': 'media volume --stream 3 --set {value}', 'value': 2},
    {'name': 'music volume', 'read': 'cmd media_session volume --stream 3 --get', 'parse': r'volume is (\d+)',
     'set': 'cmd media_session volume --stream 3 --set {value}', 'value': 2, 'cap': True}]}


class PrepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dev = os.path.join(self.tmp.name, 'device')
        os.makedirs(self.dev)
        self.adb = os.path.join(self.tmp.name, 'adb')
        with open(self.adb, 'w', encoding='utf8') as f:
            f.write(FAKE_ADB)
        os.chmod(self.adb, os.stat(self.adb).st_mode | stat.S_IEXEC)
        self.home = os.path.join(self.tmp.name, 'home')
        os.makedirs(self.home)
        for k, v in (('global.stay_on_while_plugged_in', '0'), ('secure.screensaver_enabled', '1'),
                     ('system.screen_off_timeout', '600000'), ('volume', '7')):
            self.put(k, v)
        self.device = {**TV, 'adb': self.adb}

    def put(self, key, value):
        with open(os.path.join(self.dev, key), 'w', encoding='utf8') as f:
            f.write(value)

    def get(self, key):
        p = os.path.join(self.dev, key)
        return open(p, encoding='utf8').read() if os.path.exists(p) else None

    def bash(self, line):
        env = {**os.environ, 'HOME': self.home, 'FAKE_DEVICE': self.dev}
        return subprocess.run(['bash', '-c', line], env=env, capture_output=True, text=True, timeout=60)

    def snapshot(self):
        return {k: self.get(k) for k in ('global.stay_on_while_plugged_in', 'secure.screensaver_enabled', 'system.screen_off_timeout',
                                         'secure.sleep_timeout', 'volume')}

    def test_the_values_read_first_are_exactly_what_is_put_back(self):
        before = self.snapshot()
        seen = os.path.join(self.tmp.name, 'seen')
        r = self.bash(devices.external_wrap([self.device], f'cat {self.dev}/secure.sleep_timeout {self.dev}/volume > {seen}; exit 3'))
        self.assertEqual(r.returncode, 3, r.stderr)
        with open(seen, encoding='utf8') as f:
            self.assertEqual(f.read(), '-12')   # during the run: sleep_timeout -1, volume capped to 2
        self.assertIn('tutti: device ready', r.stdout)
        self.assertEqual(self.snapshot(), before)   # sleep_timeout was unset: deleted again, not written as "null"
        self.assertFalse(os.path.exists(os.path.join(self.home, '.tutti', 'prep', 'tv.saved')))

    def test_a_missing_binary_is_skipped_cleanly(self):
        r = self.bash(devices.external_wrap([self.device], 'true'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('prep media volume skipped', r.stdout)
        self.assertNotIn('restored media volume', r.stdout)

    def test_a_volume_already_low_is_left_alone(self):
        self.put('volume', '1')
        r = self.bash(devices.external_wrap([self.device], f'cat {self.dev}/volume'))
        self.assertIn('tutti: device ready\n1', r.stdout)

    def test_a_crashed_run_is_restored_by_the_next_round(self):
        before = self.snapshot()
        setup, _restore = prep.scripts([self.device])
        self.bash(f'{setup}; kill -9 $$')   # no EXIT trap runs: the saved file stays behind
        self.assertEqual(self.get('global.stay_on_while_plugged_in'), '7')
        r = subprocess.run(['bash', os.path.join(ROOT, 'scripts', 'device-readings.sh')], capture_output=True, text=True, check=True,
                           env={**os.environ, 'HOME': self.home, 'FAKE_DEVICE': self.dev, 'DEVICE_SERIAL': 'TV1', 'ADB': self.adb,
                                'PREP_SAVED': os.path.join(self.home, '.tutti', 'prep', 'tv.saved')})
        self.assertEqual(devices.parse_readings(r.stdout)['prep_left'], 1)

        def sh(machine, line, **kw):
            return self.bash(line)
        cfg = {'devices': {'tv': self.device}, 'machines': {'pc': {}}}
        st = {'leases': {}, 'events': []}
        done = dispatch.restore_left(cfg, st, [{'name': 'tv', 'status': 'ready', 'prep_left': 1}], time.time(), sh=sh)
        self.assertEqual(done, ['tv'])
        self.assertEqual(self.snapshot(), before)
        # a device a run holds is never restored under it
        self.assertEqual(dispatch.restore_left(cfg, {'leases': {'x': {'name': 'tv'}}, 'events': []},
                                               [{'name': 'tv', 'status': 'ready', 'prep_left': 1}], time.time(), sh=sh), [])

    def test_bad_prep_entries_are_refused(self):
        errs = prep.errors('devices.tv', [{'setting': 'nowhere x', 'value': 1}, {'read': 'a', 'set': 'b', 'value': 1}, {'value': 1},
                                          {'read': 'a', 'set': 'b {value}', 'value': 1, 'parse': '('}])
        self.assertEqual(len(errs), 4)
        self.assertIn('"<global|secure|system> <key>"', errs[0])
        self.assertEqual(prep.errors('devices.tv', None), [])

    def test_brightness_is_configurable_and_does_not_mutate_defaults(self):
        device = {'name': 'test-phone', 'kind': 'phone', 'brightness_pct': 50}
        out = prep.wanted(device, night=False)
        brightness_item = next(item for item in out if item.get('setting') == 'system screen_brightness')
        self.assertEqual(brightness_item['value'], 128)
        default_out = prep.wanted({'name': 'default-phone', 'kind': 'phone'}, night=False)
        default_item = next(item for item in default_out if item.get('setting') == 'system screen_brightness')
        self.assertEqual(default_item['value'], 77)
        self.assertEqual(prep.wanted({'name': 'no-defaults', 'kind': 'phone', 'prep_defaults': False}, night=False), [])

    def test_bad_brightness_pct_is_refused(self):
        self.assertEqual(prep.brightness_error('devices.ph', None), [])
        self.assertEqual(prep.brightness_error('devices.ph', 30), [])
        for bad in (-1, 101, True, '30'):
            errs = prep.brightness_error('devices.ph', bad)
            self.assertEqual(len(errs), 1)
            self.assertIn('brightness_pct', errs[0])

    def test_person_watch_restores_settings_on_interruption(self):
        device = {'name': 'ph', 'serial': 's1', 'kind': 'phone', 'adb': 'adb'}
        script = prep.person_watch(device)
        self.assertIn('getevent -qc 1', script)
        self.assertIn('restored screen_brightness', script)
        self.assertIn('restored media volume', script)
        with mock.patch('tutti.procs.daemons_allowed', return_value=True):
            lease = prep.lease_script(device, '/tmp/x/claim-1', 10, 'test')
        self.assertIn('getevent -qc 1', lease)
        self.assertIn('restored screen_brightness', lease)


class TvGetsNoKeepAwakeKey(unittest.TestCase):
    DEVICE = {'name': 'googletv', 'serial': 'x:5555', 'adb': 'adb'}

    def check(self, script):
        self.assertIn('pm has-feature android.software.leanback', script)
        self.assertIn('= false ] && k=1 || k=0', script)
        self.assertLess(script.index('|| k=0'), script.index('[ $k = 0 ]'))
        self.assertLess(script.index('[ $k = 0 ]'), script.index('KEYCODE_UNKNOWN'))

    def test_a_runs_keep_awake_loop_skips_the_key_on_a_tv(self):
        from tutti import prep
        self.check(prep.keep_awake(self.DEVICE))

    def test_a_leases_keep_awake_loop_skips_the_key_on_a_tv(self):
        from tutti import prep
        with mock.patch('tutti.procs.daemons_allowed', return_value=True):
            self.check(prep.lease_script(self.DEVICE, '/tmp/x/claim-1', 10, 'test'))

if __name__ == '__main__':
    unittest.main()

