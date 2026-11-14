"""The owner's device rules: release only when every user is done, then restore the device's own settings; the keep-awake
loop lives exactly as long as the run; one install per device at a time; an empty test filter and a non-debuggable APK are
refused. A fake adb on this computer keeps the device's settings in files; HOME is a temp folder."""
import os
import struct
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
import zipfile

from tutti import apk, devices, prep, shards

FAKE_ADB = textwrap.dedent('''\
    #!/usr/bin/env bash
    # a fake adb: settings live in $FAKE/settings/<key>; keys pressed and installs go to $FAKE/log
    shift 2; [ "$1" = shell ] && shift
    set -- $*
    case "$1 $2" in
      "settings get") cat "$FAKE/settings/$4" 2>/dev/null || echo null;;
      "settings put") echo "$5" > "$FAKE/settings/$4";;
      "settings delete") rm -f "$FAKE/settings/$4";;
      "cmd media_session") if [ "$6" = --get ]; then echo "volume is $(cat "$FAKE/settings/vol") in range [0..15]";
                           else echo "$7" > "$FAKE/settings/vol"; fi;;
      "input keyevent") echo "key $3 $(date +%s.%N)" >> "$FAKE/log";;
      "install -r") echo "start $$" >> "$FAKE/log"; sleep 0.5; echo "end $$" >> "$FAKE/log";;
    esac
    exit 0
''')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ['rm', '-rf', self.tmp])
        os.makedirs(os.path.join(self.tmp, 'settings'))
        self.adb = os.path.join(self.tmp, 'adb')
        with open(self.adb, 'w', encoding='utf8') as f:
            f.write(FAKE_ADB)
        os.chmod(self.adb, 0o755)
        self.set('screen_brightness', '200')
        self.set('screen_brightness_mode', '1')
        self.set('vol', '9')
        self.env = {**os.environ, 'HOME': self.tmp, 'FAKE': self.tmp}
        self.d = {'name': 'pixel', 'serial': 'S1', 'kind': 'phone', 'adb': self.adb}

    def set(self, key, value):
        with open(os.path.join(self.tmp, 'settings', key), 'w', encoding='utf8') as f:
            f.write(value + '\n')

    def get(self, key):
        with open(os.path.join(self.tmp, 'settings', key), encoding='utf8') as f:
            return f.read().strip()

    def bash(self, line, timeout=20):
        return subprocess.run(['bash', '-c', line], env=self.env, capture_output=True, text=True, timeout=timeout, check=False)

    def log(self):
        try:
            with open(os.path.join(self.tmp, 'log'), encoding='utf8') as f:
                return f.read().splitlines()
        except OSError:
            return []


class ReleaseTest(Base):
    def test_a_run_prepares_the_device_and_the_last_user_restores_it(self):
        r = self.bash(devices.external_wrap([self.d], 'cat "$FAKE/settings/screen_brightness" "$FAKE/settings/vol"'))
        self.assertIn('77\n2\n', r.stdout)   # about 30 % and about 10 % while in use
        self.assertIn('tutti: device released pixel', r.stdout)
        self.assertEqual((self.get('screen_brightness'), self.get('screen_brightness_mode'), self.get('vol')), ('200', '1', '9'))

    def test_release_waits_for_the_last_user(self):
        users = os.path.join(self.tmp, '.tutti', 'devices', 'pixel', 'users')
        os.makedirs(users)
        open(os.path.join(users, 'claim-roradio'), 'w').close()   # another session still uses it
        r = self.bash(devices.external_wrap([self.d], 'true'))
        self.assertIn('stays in use by claim-roradio', r.stdout)
        self.assertNotIn('device released', r.stdout)
        self.assertEqual(self.get('screen_brightness'), '77')   # still prepared for the other user
        os.remove(os.path.join(users, 'claim-roradio'))
        r = self.bash(prep.release_script(self.d))
        self.assertIn('tutti: device released pixel', r.stdout)
        self.assertEqual((self.get('screen_brightness'), self.get('vol')), ('200', '9'))   # exactly as read first

    def test_a_dead_runs_registration_does_not_hold_the_device(self):
        users = os.path.join(self.tmp, '.tutti', 'devices', 'pixel', 'users')
        os.makedirs(users)
        open(os.path.join(users, 'run-999999'), 'w').close()   # killed with -9: no trap ran
        self.bash(prep.prep_script(self.d))
        r = self.bash(prep.release_script(self.d))
        self.assertIn('tutti: device released pixel', r.stdout)
        self.assertEqual(os.listdir(users), [])

    def test_a_second_run_never_saves_prepared_values_as_the_originals(self):
        users = os.path.join(self.tmp, '.tutti', 'devices', 'pixel', 'users')
        os.makedirs(users)
        open(os.path.join(users, 'claim-x'), 'w').close()
        self.bash(devices.external_wrap([self.d], 'true'))
        self.bash(devices.external_wrap([self.d], 'true'))   # reads 77 now: must keep 200 saved
        os.remove(os.path.join(users, 'claim-x'))
        self.bash(prep.release_script(self.d))
        self.assertEqual(self.get('screen_brightness'), '200')

    def test_a_fresh_lock_of_another_tool_keeps_the_device_prepared(self):
        ext = os.path.join(self.tmp, 'other.lock')
        with open(ext, 'w', encoding='utf8') as f:
            f.write('phone-e2e subagent\n')
        self.bash(prep.prep_script(self.d))
        r = self.bash(prep.release_script(self.d, ext, 1800))
        self.assertIn('stays in use by  phone-e2e subagent', r.stdout)
        self.assertEqual(self.get('screen_brightness'), '77')

    def test_a_tv_gets_its_screensaver_and_sleep_back(self):
        tv = {**self.d, 'kind': 'tv'}
        self.set('screensaver_enabled', '1')
        self.set('screen_off_timeout', '600000')
        r = self.bash(devices.external_wrap([tv], 'cat "$FAKE/settings/screensaver_enabled"'))
        self.assertIn('0\n', r.stdout)
        self.assertEqual((self.get('screensaver_enabled'), self.get('screen_off_timeout')), ('1', '600000'))


class KeepAwakeTest(Base):
    def test_the_keep_awake_loop_starts_and_stops_with_the_run(self):
        old = prep.KEEP_AWAKE_SECONDS
        prep.KEEP_AWAKE_SECONDS = 0.2
        self.addCleanup(setattr, prep, 'KEEP_AWAKE_SECONDS', old)
        self.bash(devices.external_wrap([{**self.d, 'prep_defaults': False}], 'sleep 1.2'))
        during = [x for x in self.log() if x.startswith('key KEYCODE_UNKNOWN')]
        self.assertGreaterEqual(len(during), 3)
        time.sleep(0.8)
        after = [x for x in self.log() if x.startswith('key KEYCODE_UNKNOWN')]
        self.assertEqual(len(after), len(during))   # stopped with the run
        self.assertIn('key KEYCODE_WAKEUP', ' '.join(self.log()))   # woken and unlocked first


class InstallTest(Base):
    def test_one_install_per_device_at_a_time(self):
        line = apk.install_script(self.d, '/tmp/app.apk')
        threads = [threading.Thread(target=self.bash, args=(line,)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        steps = [x.split()[0] for x in self.log()]
        self.assertEqual(steps, ['start', 'end', 'start', 'end'])

    def manifest(self, debuggable):
        """A minimal binary AndroidManifest.xml: a resource map with android:debuggable and one element using it."""
        res = struct.pack('<HHI', 0x0180, 8, 12) + struct.pack('<I', apk.DEBUGGABLE)
        attr = struct.pack('<IIIHBBI', 0xffffffff, 0, 0xffffffff, 8, 0, 0x12, 0xffffffff if debuggable else 0)
        elem = struct.pack('<HHI', 0x0102, 16, 36 + len(attr)) + struct.pack('<II', 1, 0xffffffff)
        elem += struct.pack('<IIHHHHHH', 0xffffffff, 1, 20, 20, 1, 0, 0, 0) + attr
        body = res + elem
        return struct.pack('<HHI', 0x0003, 8, 8 + len(body)) + body

    def apk_file(self, debuggable):
        path = os.path.join(self.tmp, f'app-{debuggable}.apk')
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('AndroidManifest.xml', self.manifest(debuggable))
        return path

    def test_a_non_debuggable_apk_is_refused_in_plain_words(self):
        apk.check(self.apk_file(True))
        with self.assertRaisesRegex(apk.ApkError, 'not a debuggable build.*assembleDebug'):
            apk.check(self.apk_file(False))
        with open(os.path.join(self.tmp, 'junk.apk'), 'w', encoding='utf8') as f:
            f.write('x')
        with self.assertRaisesRegex(apk.ApkError, 'not an app package'):
            apk.check(os.path.join(self.tmp, 'junk.apk'))


class FilterTest(unittest.TestCase):
    def test_an_empty_filter_is_refused(self):
        gradle = './gradlew cAT -Pandroid.testInstrumentationRunnerArguments.class='
        for bad in ("npx playwright test --grep ''", 'npx playwright test --grep', gradle + ' x', 'pytest --tests ""'):
            self.assertIn('the test filter is empty', shards.empty_filter(bad), bad)
        for good in ('npx playwright test --grep @smoke', gradle + 'a.B', 'npm test', 'npm run package -- --ci', 'git -g x'):
            self.assertEqual(shards.empty_filter(good), '', good)

    def test_a_device_run_must_show_a_test_running_within_two_minutes(self):
        quiet = 'tutti: device ready\n'
        self.assertIn('no test started within 120 s', shards.not_started({}, quiet, 121, device=True))
        self.assertEqual(shards.not_started({}, quiet, 60, device=True), '')
        self.assertEqual(shards.not_started({}, quiet + 'Running 4 tests\n', 300, device=True), '')
        self.assertEqual(shards.not_started({}, quiet, 300), '')   # a machine run has no such check


if __name__ == '__main__':
    unittest.main()
