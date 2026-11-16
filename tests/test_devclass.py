"""The RoRadio harness pieces made generic (0.22.0): per device class command deny lists (tutti/devclass.py), prep items
that are only read and put back (rotation, timeout, screensaver), a run whose filter matched no test as a FAIL, requeue
patterns read only after the runner started, and shards_from joining the listed shards. Pure: no adb, no ssh."""
import re
import subprocess
import unittest

from tutti import devclass, devices, dispatch, prep, shards

TV = {'name': 'tv', 'kind': 'tv', 'serial': 'S-tv', 'adb': 'adb'}
PHONE = {'name': 'ph', 'serial': 'S-ph', 'adb': 'adb'}


class DenyList(unittest.TestCase):
    def test_a_tv_denies_size_density_rotation_and_device_state(self):
        rules = devclass.deny({}, TV)
        for cmd in ('adb shell wm size 1080x1920', 'wm density 320', 'settings put system user_rotation 1',
                    'cmd device_state state 2', 'settings put system accelerometer_rotation 0'):
            self.assertTrue(devclass.denied(rules, cmd), cmd)
        self.assertEqual(devclass.denied(rules, 'am start -n x/.Main'), '')

    def test_a_phone_denies_nothing_and_the_config_replaces_a_class(self):
        self.assertEqual(devclass.deny({}, PHONE), [])
        cfg = {'device_classes': {'tv': {'deny': []}, 'phone': {'deny': [r'\breboot\b']}}}
        self.assertEqual(devclass.deny(cfg, TV), [])
        self.assertTrue(devclass.denied(devclass.deny(cfg, PHONE), 'adb reboot'))

    def test_bad_config_is_named(self):
        self.assertEqual(devclass.errors({}), [])
        self.assertTrue(devclass.errors({'device_classes': 3}))
        self.assertIn('device_classes.tv', devclass.errors({'device_classes': {'tv': {'deny': 'x'}}})[0])
        self.assertIn('deny[0]', devclass.errors({'device_classes': {'tv': {'deny': ['(']}}})[0])

    def test_a_run_naming_a_denied_command_is_refused_on_a_tv(self):
        wrap = devices.external_wrap([TV], 'adb shell wm size 800x600 && run-tests')
        self.assertIn('tutti: refused', wrap)
        self.assertEqual(subprocess.run(['bash', '-c', wrap], capture_output=True, check=False).returncode, 5)
        self.assertEqual(devclass.refusal([PHONE], 'adb shell wm size 800x600'), '')

    def test_the_script_is_told_its_class_and_deny_pattern(self):
        out = subprocess.run(['bash', '-c', devclass.export([TV]) + 'echo "$TUTTI_DEVICE_CLASS|$TUTTI_DEVICE_DENY"'],
                             capture_output=True, text=True, check=True).stdout
        cls, rx = out.strip().split('|', 1)
        self.assertEqual(cls, 'tv')
        self.assertTrue(re.search(rx, 'wm density 300'))


class KeepItems(unittest.TestCase):
    def test_a_phone_saves_rotation_and_timeout_and_never_sets_them(self):
        names = [it['name'] for it in prep.items(PHONE, False)]
        for k in ('accelerometer_rotation', 'user_rotation', 'screen_off_timeout', 'screensaver_enabled'):
            self.assertIn(k, names)
        script = prep.prep_script(PHONE)
        self.assertIn('settings get system user_rotation', script)
        self.assertNotIn('settings put system user_rotation', script)
        self.assertIn('restored user_rotation', prep.restore_script(PHONE))

    def test_a_tv_never_touches_rotation(self):
        script = prep.prep_script(TV) + prep.restore_script(TV)
        self.assertNotIn('rotation', script)

    def test_a_kept_item_goes_back_whatever_the_run_left(self):
        self.assertIn('{ true; }', prep.restore_script(PHONE))


class Verdicts(unittest.TestCase):
    def test_zero_tests_is_a_fail_even_with_exit_0(self):
        s = {'verdict': {'pass': 'summary'}}
        self.assertEqual(shards.judge(s, '[E2E] 0 tests match "Foo"\n[E2E] summary', 0), 1)
        self.assertEqual(shards.judge({}, 'No tests found for filter', 0), 1)
        self.assertEqual(shards.judge({}, '12 tests ran', 0), 0)
        early = 'module-a: No tests found\n' + 'ok\n' * 40 + 'BUILD SUCCESSFUL'
        self.assertEqual(shards.judge({}, early, 0), 0)   # one empty module early in the log is not the summary
        self.assertEqual(shards.judge({'verdict': {'none': ''}}, '0 tests matched', 0), 0)

    def test_another_app_counts_only_after_the_runner_started(self):
        s = {'started': {'pattern': 'Unlocked; running', 'within_seconds': 180}, 'requeue_on': ['Another app in front']}
        self.assertEqual(shards.judge(s, 'Another app in front\nUnlocked; running\nok', 0), 0)
        self.assertTrue(str(shards.judge(s, 'Unlocked; running\nAnother app in front', 0)).startswith(shards.REQUEUE))
        self.assertEqual(shards.judge({'requeue_on': ['x']}, 'y', 0), 0)

    def test_verdict_none_is_a_known_key(self):
        self.assertEqual(shards.suite_errors({}, 'k', {'verdict': {'none': 'x'}}, lambda *a: []), [])


class ShardsFrom(unittest.TestCase):
    def cfg(self):
        return {'projects': {'P': {'tests': {'e2e': {'run': 'x {shard}', 'shards': [{'name': 'TvRemoteE2ETest', 'devices': ['tv']}],
                                                    'shards_from': {'run': 'ls', 'where': 'local'}}}}}}

    def test_discovered_classes_join_the_listed_ones(self):
        st = {'projects': {}}
        dispatch.refresh(self.cfg(), st, 10_000, ask_fn=lambda c, spec: 'AUITest\nTvRemoteE2ETest\nBE2ETest\n')
        got = sorted(st['projects']['P']['shards'])
        self.assertEqual(got, ['e2e/AUITest', 'e2e/BE2ETest', 'e2e/TvRemoteE2ETest'])

    def test_no_answer_keeps_the_listed_shards(self):
        st = {'projects': {}}
        dispatch.refresh(self.cfg(), st, 10_000, ask_fn=lambda c, spec: None)
        self.assertEqual(list(st['projects']['P']['shards']), ['e2e/TvRemoteE2ETest'])


class Dedicated(unittest.TestCase):
    def test_a_dedicated_machine_takes_only_suites_that_name_it(self):
        from tutti import workq
        cfg = {'machines': {'win': {'dedicated': True}, 'nuc': {}}}
        t = {'kind': 'machine', 'name': 'win'}
        self.assertTrue(dispatch.dedicated_refuses(cfg, t, {'machines': None}))
        self.assertFalse(dispatch.dedicated_refuses(cfg, t, {'machines': ['win']}))
        self.assertFalse(dispatch.dedicated_refuses(cfg, {'kind': 'machine', 'name': 'nuc'}, {'machines': None}))
        rows = [{'name': 'win', 'status': 'free', 'free': 1}]
        cfg.update(routing={'order': []})
        self.assertEqual(workq.assign(cfg, [{'name': 'j'}], rows, set(), None), [])


class DesktopSession(unittest.TestCase):
    def test_a_windows_ui_run_goes_through_an_interactive_scheduled_task(self):
        from tutti import winsession
        line = winsession.wrap('pwsh -c ".\\build.ps1 pipeline"', 'ventu', 'C:\\R', before='test -e x && exit 3')
        self.assertTrue(line.startswith('test -e x && exit 3; '))
        for part in ('/ru ventu /it', 'schtasks.exe /run', 'schtasks.exe /delete', 'rc.txt', 'cd /d C:\\R', 'trap'):
            self.assertIn(part, line)
        self.assertNotIn('/rp', line)
        subprocess.run(['bash', '-n', '-c', line], check=True)

    def test_the_dispatcher_wraps_a_desktop_suite_and_checks_its_config(self):
        from tutti import winsession
        self.assertEqual(winsession.errors('k', None), [])
        self.assertTrue(winsession.errors('k', {'dir': 'x'}))
        cfg = {'projects': {'R': {'tests': {'p': {'run': 'pwsh {shard}', 'desktop': {'user': 'ventu'}, 'shards': ['a'],
                                                  'targets': ['machine']}}}}, 'machines': {}, '_path': '/x'}
        s = dispatch.suites(cfg)[0]
        argv = dispatch.test_argv(cfg, {'kind': 'machine', 'name': 'win'}, {**s, 'dir': '/tmp'}, 'a')
        self.assertIn('/ru ventu /it', argv[-1])


if __name__ == '__main__':
    unittest.main()
