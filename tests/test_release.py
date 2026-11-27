"""The owner's device rules: release only when every user is done, then restore the device's own settings; the keep-awake
loop lives exactly as long as the run; one install per device at a time; an empty test filter and a non-debuggable APK are
refused. A fake adb on this computer keeps the device's settings in files; HOME is a temp folder."""
import os
import struct
import subprocess
import textwrap
import threading
import time
import unittest
import zipfile
from unittest import mock

import tmpdirs

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
      "pm has-feature") echo false;;
      "input keyevent") echo "key $3 $(date +%s.%N)" >> "$FAKE/log";;
      "install -r") echo "start $$" >> "$FAKE/log"; sleep 0.5; echo "end $$" >> "$FAKE/log";;
    esac
    exit 0
''')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdirs.mkdtemp()
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
        # the day's prep (night mode off): the tests run at any hour
        self.d = {'name': 'pixel', 'serial': 'S1', 'kind': 'phone', 'adb': self.adb, '_night': {'on': False}}

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


class LeaseTest(Base):
    """A lease for a requester outside Tutti's runs (`tutti devices claim|lease`): exclusive, prepped, kept awake, timed."""

    def claim(self, who='roradio'):
        return os.path.join(self.tmp, '.tutti', 'devices', 'pixel', 'users', f'claim-{who}')

    def test_a_lease_preps_the_device_and_its_release_restores_it(self):
        r = self.bash(prep.lease_script(self.d, self.claim(), 60, 'roradio'))
        self.assertIn('leased to roradio for 60 min', r.stdout)
        self.assertIn('SERIAL=S1', r.stdout)
        self.assertEqual(self.get('screen_brightness'), '77')
        with open(self.claim(), encoding='utf8') as f:
            self.assertGreater(int(f.read()), time.time() + 3000)   # its end time
        os.remove(self.claim())
        r = self.bash(prep.release_script(self.d))
        self.assertIn('tutti: device released pixel', r.stdout)
        self.assertEqual(self.get('screen_brightness'), '200')

    def test_a_second_requester_waits_and_a_lapsed_lease_frees_the_device(self):
        first, second = self.claim('map-team'), self.claim('map.team')
        self.assertEqual(self.bash(prep.lease_script(self.d, first, 60, 'map-team')).returncode, 0)
        r = self.bash(prep.lease_script(self.d, second, 60, 'map.team'))
        self.assertEqual(r.returncode, 3)
        self.assertIn('in use by claim-map-team', r.stdout)
        with open(first, 'w', encoding='utf8') as f:
            f.write(str(int(time.time()) - 1))   # its requester crashed: the lease ran out
        r = self.bash(prep.lease_script(self.d, second, 60, 'map.team'))
        self.assertIn('leased to map.team', r.stdout)
        os.remove(second)
        self.bash(prep.release_script(self.d))

    def fast(self):
        old = prep.KEEP_AWAKE_SECONDS
        prep.KEEP_AWAKE_SECONDS = 0.2
        self.addCleanup(setattr, prep, 'KEEP_AWAKE_SECONDS', old)

    def wait_for(self, check, seconds=5):
        end = time.time() + seconds
        while time.time() < end and not check():
            time.sleep(0.1)
        return check()

    def test_a_requesters_name_is_never_run_as_a_command(self):
        self.fast()
        who = 'x"; touch "$FAKE/pwned"; echo "$(touch $FAKE/pwned2)'
        r = self.bash(prep.lease_script(self.d, self.claim('x'), 60, who))
        self.assertIn(f'leased to {who} for 60 min', r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'pwned')) or os.path.exists(os.path.join(self.tmp, 'pwned2')))
        os.remove(self.claim('x'))
        self.bash(prep.release_script(self.d))

    def test_one_loop_per_device_and_it_restores_the_device_when_the_lease_runs_out(self):
        self.fast()
        pidfile = os.path.join(self.tmp, '.tutti', 'devices', 'pixel', 'awake.pid')
        with mock.patch.dict(os.environ, {'TUTTI_NO_DAEMONS': ''}):   # this test is about the real loop: it ends below
            script = prep.lease_script(self.d, self.claim(), 60, 'roradio')
        self.bash(script)
        with open(pidfile, encoding='utf8') as f:
            first = f.read().strip()
        self.addCleanup(lambda: self.wait_for(lambda: not os.path.exists(f'/proc/{first}'), 10))
        self.bash(script)   # the same requester again: no second loop
        with open(pidfile, encoding='utf8') as f:
            self.assertEqual(f.read().strip(), first)
        self.assertEqual(self.get('screen_brightness'), '77')
        with open(self.claim(), 'w', encoding='utf8') as f:
            f.write(str(int(time.time()) - 1))   # nobody calls release: the loop sees the end and restores by itself
        self.assertTrue(self.wait_for(lambda: self.get('screen_brightness') == '200'))
        self.assertTrue(self.wait_for(lambda: not os.path.exists(pidfile)))
        self.assertFalse(os.path.exists(self.claim()))

    def test_two_requesters_at_once_get_one_lease(self):
        self.fast()
        out = {}
        threads = [threading.Thread(target=lambda w=w: out.__setitem__(w, self.bash(prep.lease_script(self.d, self.claim(w), 60, w))))
                   for w in ('a', 'b')]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(r.returncode for r in out.values()), [0, 3])
        for w in ('a', 'b'):
            if os.path.exists(self.claim(w)):
                os.remove(self.claim(w))
        self.bash(prep.release_script(self.d))


class PersonTest(Base):
    """A person picks the device up during a run: Tutti stops, requeues, sends nothing, and undoes nothing of theirs."""

    def person(self, ago=0):
        d = os.path.join(self.tmp, '.tutti', 'devices', 'pixel')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'person'), 'w', encoding='utf8') as f:
            f.write(str(int(time.time()) - ago))

    def test_a_persons_own_change_is_kept_at_release(self):
        self.bash(prep.prep_script(self.d))
        self.set('screen_brightness', '150')   # the owner turned it up while setting an alarm
        r = self.bash(prep.release_script(self.d))
        self.assertIn('kept screen_brightness at 150: changed by a person', r.stdout)
        self.assertEqual((self.get('screen_brightness'), self.get('vol')), ('150', '9'))   # Tutti's own volume went back

    def test_a_person_stops_the_run_which_is_queued_again(self):
        line = devices.external_wrap([{**self.d, 'prep_defaults': False}], f'touch {self.tmp}/started; sleep 30; touch {self.tmp}/ended')
        log = os.path.join(self.tmp, 'run.log')
        with open(log, 'w', encoding='utf8') as f:   # a file, not a pipe: a background loop must not hold the test open
            child = subprocess.Popen(['bash', '-c', line], env=self.env, stdout=f, stderr=f, start_new_session=True)
        time.sleep(1)
        self.person()
        try:
            child.wait(timeout=20)
        finally:
            subprocess.run(['pkill', '-g', str(child.pid)], check=False)   # the session's leftovers, by its group id
        with open(log, encoding='utf8') as f:
            out = f.read()
        self.assertIn(devices.PERSON_LINE, out)
        self.assertTrue(os.path.exists(f'{self.tmp}/started'))
        self.assertFalse(os.path.exists(f'{self.tmp}/ended'))
        self.assertTrue(shards.judge({}, out, 143).startswith(shards.REQUEUE))

    def test_no_keep_awake_key_while_a_person_uses_it_and_the_card_says_so(self):
        self.person()
        old = prep.KEEP_AWAKE_SECONDS
        prep.KEEP_AWAKE_SECONDS = 0.2
        self.addCleanup(setattr, prep, 'KEEP_AWAKE_SECONDS', old)
        self.bash(f'{{ {prep.keep_awake(self.d)} & }}; sleep 1; kill $!')
        self.assertEqual([x for x in self.log() if 'KEYCODE_UNKNOWN' in x], [])
        st, why = devices.status(devices.E2E_POLICY, {'adb': 'ok', 'state': 'device', 'battery': 80, 'charging': 1,
                                                     'battery_c': 30, 'person': 1})
        self.assertEqual(st, 'person')
        self.assertIn('in use by a person — waiting', why)


class NightTest(Base):
    """Night mode: 1 % brightness and every volume at 0 while Tutti works a device at night; the alarm stays the person's."""

    def night(self, **kw):
        return {**self.d, '_night': {'on': True, 'hours': '00-24', **kw}}   # a whole-day window: night whatever the hour

    def test_at_night_brightness_is_1_and_every_volume_0_and_the_alarm_is_left(self):
        names = {it['name']: it['value'] for it in prep.items(self.night())}
        self.assertEqual((names['screen_brightness'], names['screen_brightness_mode']), (1, 0))
        self.assertEqual({names[n] for n in ('media volume', 'ring volume', 'notification volume', 'system volume')}, {0})
        self.assertFalse(any('--stream 4' in it['set'] for it in prep.items(self.night())))   # 4: the alarm
        self.assertEqual({it['name']: it['value'] for it in prep.items(self.d)}['screen_brightness'], 77)   # by day

    def test_off_or_not_listed_means_the_day_settings(self):
        self.assertFalse(prep.night_now(self.night(on=False)))
        from tutti import devices as dv
        cfg = {'machines': {'pc': {'transport': 'local'}}, 'devices': {'a': {'serial': 'x'}, 'b': {'serial': 'y'}},
               'night': {'enabled': True, 'hours': '22-08', 'devices': ['a']}}
        dv.normalise(cfg)
        self.assertEqual((cfg['devices']['a']['_night']['on'], cfg['devices']['b']['_night']['on']), (True, False))

    def test_a_night_prep_is_put_back_by_a_day_release(self):
        self.bash(prep.prep_script(self.night()))
        self.assertEqual((self.get('screen_brightness'), self.get('vol')), ('1', '0'))
        r = self.bash(prep.release_script(self.d))   # the release happens after 08:00: the day's list
        self.assertIn('tutti: device released pixel', r.stdout)
        self.assertEqual((self.get('screen_brightness'), self.get('screen_brightness_mode'), self.get('vol')), ('200', '1', '9'))


class DndTest(unittest.TestCase):
    def test_the_devices_own_dnd_schedule_is_learned_across_midnight(self):
        rec = {str(h): [5, 5] for h in (22, 23, 0, 1, 2, 3, 4, 5, 6)}
        rec.update({str(h): [0, 5] for h in range(7, 22)})
        self.assertEqual(devices.dnd_hours(rec), '22-07')
        self.assertEqual(devices.dnd_hours({'3': [1, 1]}), '')   # too few looks: not known yet
        seen = devices.learn_dnd({}, 'pixel', 1, 1_800_000_000)
        self.assertEqual(sum(v[0] for v in seen['pixel'].values()), 1)


class TvPrepTest(Base):
    """A TV's screensaver and sleep settings must be read and must take, or the run does not start."""

    def tv(self):
        return {**self.d, 'kind': 'tv', 'prep_defaults': True}

    def test_a_tv_is_prepared_and_its_settings_read_back(self):
        self.set('screensaver_enabled', '1')
        self.set('screensaver_activate_on_sleep', '1')
        self.set('screen_off_timeout', '600000')
        r = self.bash(prep.prep_script(self.tv()))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.get('screensaver_enabled'), self.get('screensaver_activate_on_sleep')), ('0', '0'))

    def test_a_tv_that_does_not_answer_fails_loudly_instead_of_running(self):
        tv = {**self.tv(), 'adb': '/bin/false'}   # adb answers nothing, even after a reconnect
        r = self.bash(prep.prep_script(tv) + '; echo ran')
        self.assertEqual(r.returncode, 4)
        self.assertIn('prep failed', r.stdout)
        self.assertNotIn('ran', r.stdout)


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
