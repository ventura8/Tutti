"""The security audit's low findings (docs/SECURITY-AUDIT-2026-11.md, L1 to L12) and H4 on macOS and Windows: one test each
that plays the attack or checks the pin. Temp folders only; nothing leaves the machine."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

from tutti import adbhost, authdevice, browser, discover, household, httpguard, install, scrub

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf8') as f:
        return f.read()


class InstallersTest(unittest.TestCase):
    def test_the_source_installers_take_a_release_tag_and_hash_checked_requirements(self):   # L1, L2
        for name in ('install.sh', 'install.ps1'):
            text = read('install', name)
            self.assertNotIn('| bash', text.split('\n', 3)[1] + text.split('\n', 3)[2])
            self.assertNotIn('| iex', text)
            self.assertNotIn('--upgrade pip', text)
            self.assertIn('--require-hashes', text)
            self.assertIn("tag -l 'v*'", text)
        for name in ('bootstrap.sh', 'bootstrap.ps1'):
            self.assertIn('--require-hashes', read('install', name))
        for req in ('requirements.txt', 'requirements-dev.txt'):
            lines = [ln for ln in read(req).splitlines() if re.match(r'^[a-z0-9-]+==', ln, re.I)]
            self.assertTrue(lines)
            self.assertIn('--hash=sha256:', read(req))
        self.assertIn('@v', install.CHANNELS['uv']['install'])

    def test_platform_tools_are_one_version_checked_by_sha256(self):   # L3
        self.assertNotIn('latest', adbhost.PLATFORM_TOOLS_URL)
        self.assertRegex(adbhost.PLATFORM_TOOLS_SHA256, r'^[0-9a-f]{64}$')
        calls = []
        host = adbhost.Host.__new__(adbhost.Host)
        host.bash = lambda body, timeout=None: calls.append(body) or (0, '')
        host.install_tools()
        self.assertIn(f'echo "{adbhost.PLATFORM_TOOLS_SHA256}  $t/pt.zip" | sha256sum -c', calls[0])
        self.assertLess(calls[0].index('sha256sum'), calls[0].index('extractall'))


class DeviceStartTest(unittest.TestCase):
    def test_a_lan_page_cannot_ask_google_for_codes_without_end(self):   # L8
        posted = []
        cfg = {}
        flows = authdevice.Flows(cfg, post=lambda url, f: posted.append(f) or (200, {'device_code': 'd', 'user_code': 'U'}),
                                 clock=lambda: 1000.0)
        real = authdevice.client
        authdevice.client = lambda c: ('cid', '')
        try:
            for _ in range(authdevice.STARTS_PER_MINUTE):
                flows.start()
            with self.assertRaisesRegex(authdevice.DeviceError, 'last minute'):
                flows.start()
        finally:
            authdevice.client = real
        self.assertEqual(len(posted), authdevice.STARTS_PER_MINUTE)


class HouseholdCapsTest(unittest.TestCase):
    def state(self):
        s = household.new_state('home', 0)
        household.join(s, 'a' * 16, 'ha', 'Ana', 'claude', 't', 0)
        household.join(s, 'b' * 16, 'hb', 'Bo', 'claude', 't', 0)
        return s

    def test_a_member_cannot_raise_its_own_share_and_the_admin_can(self):   # L9
        s = self.state()
        household.heartbeat(s, 'b' * 16, {'weight': 10}, 1)
        self.assertEqual(s['members']['b' * 16]['weight'], household.WEIGHT_CAP)
        household.set_weight(s, 'a' * 16, 'hb', 3, 2)
        household.heartbeat(s, 'b' * 16, {'weight': 10}, 3)
        self.assertEqual(s['members']['b' * 16]['weight'], 3)
        with self.assertRaisesRegex(household.HouseholdError, 'admin'):
            household.set_weight(s, 'b' * 16, 'hb', 10, 4)

    def test_made_up_lease_keys_and_projects_never_fill_the_state(self):   # L9
        s = self.state()
        household.sync_project(s, 'git.example/a', {'held': [f'k{i}' for i in range(5000)]}, 'b' * 16, 1, 30)
        self.assertEqual(s['leases'], {})
        s['projects'] = {f'p{i}': {'tasks': {}} for i in range(household.MAX_PROJECTS)}
        with self.assertRaisesRegex(household.HouseholdError, 'shares'):
            household.sync_project(s, 'git.example/new', {}, 'b' * 16, 1, 30)


class ViewersTest(unittest.TestCase):
    def test_a_token_on_a_command_line_never_reaches_the_dashboard(self):   # L10
        job = discover.script_job('/nonexistent', 1, 'ship.sh', ['--token', 'ghp_' + 'a' * 36, 'GH_TOKEN=abc123secret'], 0, 600)
        self.assertNotIn('ghp_', job['title'])
        self.assertNotIn('abc123secret', job['title'])
        self.assertEqual(scrub.secrets_only('pytest tests/test_x.py -k fast'), 'pytest tests/test_x.py -k fast')   # nothing else

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_a_link_from_data_is_never_javascript(self):   # L11
        src = read('tutti', 'web', 'app.js')
        fn = src[src.index('function safeHref'):src.index('function el(')]
        cases = ['https://github.com/o/r/pull/1', 'mailto:a@b.c', '#settings', 'app.css', 'javascript:alert(1)',
                 ' JavaScript:alert(1)', 'java\tscript:alert(1)', 'data:text/html,x', 'vbscript:x']
        out = subprocess.run(['node', '-e', f'{fn}\nconsole.log(JSON.stringify({cases!r}.map(safeHref)))'],
                             capture_output=True, text=True, check=True).stdout
        import json
        self.assertEqual(json.loads(out), cases[:4] + ['#'] * 5)


class InviteFoundingTest(unittest.TestCase):
    def test_an_unproved_invite_join_never_founds_a_household(self):   # L12
        from tutti import config, coordinator
        with tempfile.TemporaryDirectory() as d:
            cfg = config.normalise({'project': {'state': os.path.join(d, 'state')}})
            svc = coordinator.Service(cfg)
            code, _answer = svc.handle(coordinator.JOIN, {}, b'{"invite": "made-up", "nonce": "' + b'0' * 32 + b'"}', '10.0.0.9')
            self.assertEqual(code, 503)
            self.assertFalse(os.path.exists(coordinator.state_path(cfg)))


class OtherPlatformsTest(unittest.TestCase):
    """H4 on macOS (lsof) and Windows (the TCP table and the process token)."""

    def test_macos_asks_lsof_who_owns_the_client_end(self):
        out = ('p501\nu501\nn127.0.0.1:50000->127.0.0.1:4777\np900\nu900\nn127.0.0.1:4777->127.0.0.1:50000\n')
        def run(argv, **kw):   # a fake lsof
            return SimpleNamespace(stdout=out)
        self.assertEqual(httpguard.lsof_uid(('127.0.0.1', 50000), ('127.0.0.1', 4777), run), 501)
        ok = httpguard.loopback_owner_ok(('127.0.0.1', 50000), ('127.0.0.1', 4777), uid=900, proc='/nonexistent',
                                         platform='darwin', run=run, sleep=lambda s: None)
        self.assertFalse(ok)   # another login's process
        self.assertTrue(httpguard.loopback_owner_ok(('127.0.0.1', 50000), ('127.0.0.1', 4777), uid=501, proc='/nonexistent',
                                                    platform='darwin', run=run))
        def missing(argv, **kw):
            raise FileNotFoundError('lsof')
        self.assertTrue(httpguard.loopback_owner_ok(('127.0.0.1', 1), ('127.0.0.1', 2), uid=1, proc='/nonexistent',
                                                    platform='darwin', run=missing))

    def test_windows_compares_the_process_token_with_ours(self):
        class Api:
            def __init__(self, pid, sids):
                self.pid, self.sids = pid, sids

            def pid_of(self, c, s):
                return self.pid

            def sid_of(self, pid):
                return self.sids.get(pid)
        me = os.getpid()
        self.assertTrue(httpguard.loopback_owner_ok(('127.0.0.1', 1), ('127.0.0.1', 2), platform='win32',
                                                    winapi=Api(7, {7: 'S-1-5-21-1', me: 'S-1-5-21-1'})))
        self.assertFalse(httpguard.loopback_owner_ok(('127.0.0.1', 1), ('127.0.0.1', 2), platform='win32',
                                                     winapi=Api(7, {7: 'S-1-5-21-2', me: 'S-1-5-21-1'})))
        self.assertFalse(httpguard.loopback_owner_ok(('127.0.0.1', 1), ('127.0.0.1', 2), platform='win32',
                                                     winapi=Api(7, {me: 'S-1-5-21-1'})))   # its token cannot be read
        self.assertFalse(httpguard.loopback_owner_ok(('127.0.0.1', 1), ('127.0.0.1', 2), platform='win32',
                                                     winapi=Api(None, {})))               # not in the table


class BrowserTabsTest(unittest.TestCase):
    def test_the_tab_limit_holds_across_processes(self):   # CodeRabbit on #97
        with tempfile.TemporaryDirectory() as d:
            cfg = {'project': {'state': d}, 'browser': {'max_tabs': 1}}
            b1, b2 = browser.Browser(cfg, watch=False), browser.Browser(cfg, watch=False)
            slept = []
            b2.sleep = lambda s: (slept.append(s), (_ for _ in ()).throw(TimeoutError('waits')))
            fd = b1.host_slot()
            self.assertIsNotNone(fd)
            with self.assertRaises(TimeoutError):
                b2.host_slot()   # the other process's task waits for the slot
            os.close(fd)
            fd2 = b2.host_slot()
            self.assertIsNotNone(fd2)
            os.close(fd2)


class ReleasesStuckTest(unittest.TestCase):
    def test_merged_versions_no_release_publishes_are_named_with_the_fix(self):
        from tutti import stuck
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, '.git'))
            with open(os.path.join(d, 'CHANGELOG.md'), 'w', encoding='utf8') as f:
                f.write('# Changelog\n\n## 0.10.0 (x)\n\n## 0.9.5 (x)\n\n## 0.9.4 (x)\n\n## 0.9.3 (x)\n')
            sums = lambda url, dest=None: f'{"0" * 64}  tutti-0.9.4.tar.gz\n'
            w = stuck.check(d, sums)
            self.assertIn('releases stuck: 2 merged version(s) unpublished (0.9.5, 0.10.0)', w)
            self.assertIn('newest release is 0.9.4', w)
            self.assertIn('Fix:', w)
            self.assertEqual(stuck.check(d, lambda url, dest=None: f'{"0" * 64}  tutti-0.10.0.tar.gz\n'), '')

            def offline(url, dest=None):
                raise OSError('no network')
            self.assertEqual(stuck.check(d, offline), '')            # cannot tell: never a false alarm
            self.assertEqual(stuck.check(os.path.join(d, 'nowhere'), sums), '')   # a release install: not a checkout


if __name__ == '__main__':
    unittest.main()
