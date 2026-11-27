"""The work browser (tutti/browser.py, browserbroker.py, browsersandbox.py, browsercmd.py): the owner's profile only read for
its DevToolsActivePort, a dedicated profile of its own, DevTools on 127.0.0.1 or a Unix socket only, one tab per task closed
after, a queue, idle shutdown, the broker's scopes and log, and the isolated unit's sandbox. No real browser starts."""
import json
import os
import socket
import sys
import threading
import time
import types
import unittest
import unittest.mock
import urllib.error
import urllib.request

import tmpdirs

from tutti import browser, browserbroker, browsercmd, browsersandbox


def tmp():
    """A temp folder removed when its test ends (tests/tmpdirs.py; no leak into /tmp)."""
    return tmpdirs.mkdtemp()


class Proc:
    def __init__(self):
        self.alive, self.terminated = True, False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive, self.terminated = False, True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.alive = False


class FakeCdp:
    def __init__(self, endpoint, pages=None):
        self.endpoint, self.calls, self.open, self.closed, self.pages = endpoint, [], set(), False, pages or {}

    def call(self, method, params=None, session=None):
        self.calls.append((method, params, session))
        if method == 'Target.createTarget':
            t = f'T{len(self.calls)}'
            self.open.add(t)
            self.last_url = params['url']
            return {'targetId': t}
        if method == 'Target.attachToTarget':
            return {'sessionId': 'S' + params['targetId']}
        if method == 'Target.closeTarget':
            self.open.discard(params['targetId'])
            return {}
        if method == 'Runtime.evaluate':
            expr = params['expression']
            if expr == 'location.href':
                return {'result': {'value': self.pages.get('href', self.last_url)}}
            return {'result': {'value': json.dumps(self.pages.get('value', {'text': 'hi'}))}}
        return {}

    def close(self):
        self.closed = True


class Fake:
    def __init__(self, files=None, pages=None):
        self.launches, self.procs, self.cdps, self.reads, self.now = [], [], [], [], 0.0
        self.files, self.pages = files if files is not None else {}, pages

    def popen(self, argv, **kw):
        self.launches.append(argv)
        self.procs.append(Proc())
        profile = [a for a in argv if a.startswith('--user-data-dir=')][0].split('=', 1)[1]
        self.files[os.path.join(profile, browser.ACTIVE_PORT)] = '9333\n/devtools/browser/abc\n'
        return self.procs[-1]

    def read(self, path):
        self.reads.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def connect(self, endpoint):
        self.cdps.append(FakeCdp(endpoint, self.pages))
        return self.cdps[-1]

    def browser(self, cfg, **kw):
        return browser.Browser(cfg, popen=self.popen, connect=self.connect, clock=lambda: self.now, sleep=lambda s: None,
                               finder=lambda c: ('/usr/bin/chromium', None), read_file=self.read, watch=False, env={}, **kw)


class Paths(unittest.TestCase):
    def setUp(self):
        self.home = tmp()

    def test_dedicated_profile_is_its_own_per_platform(self):
        self.assertEqual(browser.profile_dir(None, 'Linux', {}, self.home), os.path.join(self.home, '.local/share/tutti/browser'))
        self.assertIn(os.path.join('Application Support', 'tutti', 'browser'), browser.profile_dir(None, 'Darwin', {}, self.home))
        self.assertTrue(browser.profile_dir(None, 'Windows', {'LOCALAPPDATA': 'C:/L'}, self.home).startswith('C:/L'))

    def test_dedicated_mode_refuses_the_owners_profile(self):
        own = os.path.join(self.home, '.config', 'google-chrome')
        for bad in (own, os.path.join(own, 'Default')):
            with self.subTest(bad=bad), self.assertRaises(browser.BrowserError):
                browser.check_profile(bad, 'Linux', {}, self.home)
        good = browser.profile_dir(None, 'Linux', {}, self.home)
        self.assertEqual(browser.check_profile(good, 'Linux', {}, self.home), good)

    def test_argv_separate_profile_and_localhost_port(self):
        argv = browser.launch_argv('/c', '/p/tutti', 9333, True)
        for flag in ('--user-data-dir=/p/tutti', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=9333', '--headless=new'):
            self.assertIn(flag, argv)

    def test_find_chrome_then_playwright_then_an_install_hint(self):
        self.assertEqual(browser.find(None, 'Linux', {}, self.home, which=lambda n: '/bin/chromium' if n == 'chromium' else None)[0],
                         '/bin/chromium')
        cache = os.path.join(self.home, '.cache', 'ms-playwright', 'chromium-1200', 'chrome-linux')
        os.makedirs(cache)
        open(os.path.join(cache, 'chrome'), 'w').close()
        path, _ = browser.find(None, 'Linux', {}, self.home, which=lambda n: None,
                               exists=lambda p: p.startswith(self.home) and os.path.exists(p))
        self.assertEqual(path, os.path.join(cache, 'chrome'))
        path, why = browser.find(None, 'Darwin', {}, tmp(), which=lambda n: None, exists=lambda p: False)
        self.assertIsNone(path)
        self.assertIn('brew install', why)

    def test_only_127_0_0_1_is_driven(self):
        for bad in ('ws://192.0.2.1:9222/devtools/browser/x', 'ws://0.0.0.0:9222/x', 'wss://127.0.0.1:1/x'):
            with self.subTest(bad=bad), self.assertRaises(browser.BrowserError):
                browser.local_ws(bad)
        self.assertEqual(browser.local_ws('ws://127.0.0.1:9222/devtools/browser/x').port, 9222)


class Existing(unittest.TestCase):
    """The owner's Chrome: never started, restarted or closed; only its DevToolsActivePort file is read."""

    def setUp(self):
        self.profile = tmp()
        self.cfg = {'browser': {'mode': 'existing', 'user_profile': self.profile, 'idle_minutes': 1}}

    def test_attaches_to_the_running_chrome_and_reads_only_its_port_file(self):
        f = Fake({os.path.join(self.profile, browser.ACTIVE_PORT): '9222\n/devtools/browser/u1\n'})
        b = f.browser(self.cfg)
        with b.task('https://a.example/x', 'ana@example'):
            self.assertEqual(b.state()['tabs'][0]['who'], 'ana@example')
        self.assertEqual(f.cdps[0].endpoint, 'ws://127.0.0.1:9222/devtools/browser/u1')
        self.assertEqual(f.launches, [])
        self.assertEqual(f.reads, [os.path.join(self.profile, browser.ACTIVE_PORT)])
        self.assertEqual(f.cdps[0].open, set())   # the tab was closed

    def test_without_remote_debugging_it_says_how_and_starts_nothing(self):
        f = Fake()
        b = f.browser(self.cfg)
        with self.assertRaises(browser.BrowserError) as e:
            b.endpoint()
        self.assertIn('chrome://inspect/#remote-debugging', str(e.exception))
        self.assertEqual(f.launches, [])
        with self.assertRaises(browser.BrowserError):
            b.start(True)

    def test_idle_closes_the_connection_never_the_owners_chrome(self):
        f = Fake({os.path.join(self.profile, browser.ACTIVE_PORT): '9222\n/devtools/browser/u1\n'})
        b = f.browser(self.cfg)
        with b.task('https://a.example'):
            f.now = 1000
            self.assertFalse(b.reap())
        f.now += 61
        self.assertTrue(b.reap())
        self.assertTrue(f.cdps[0].closed)
        self.assertEqual(f.launches, [])


class Dedicated(unittest.TestCase):
    def setUp(self):
        self.f = Fake()
        self.cfg = {'browser': {'mode': 'dedicated', 'profile': os.path.join(tmp(), 'tb'), 'idle_minutes': 1,
                                'max_tabs': 1}}

    def test_started_on_demand_reused_and_each_tab_closed(self):
        b = self.f.browser(self.cfg)
        self.assertEqual(self.f.launches, [])
        with b.task('https://a.example/x'):
            pass
        with self.assertRaises(RuntimeError), b.task('https://a.example/y'):
            raise RuntimeError('boom')
        self.assertEqual(len(self.f.launches), 1)
        self.assertIn(f'--user-data-dir={self.cfg["browser"]["profile"]}', self.f.launches[0])
        self.assertEqual(self.f.cdps[0].open, set())
        self.assertEqual(sum(1 for c in self.f.cdps[0].calls if c[0] == 'Target.closeTarget'), 2)

    def test_idle_shutdown_terminates_its_own_chrome(self):
        b = self.f.browser(self.cfg)
        with b.task('https://a.example'):
            pass
        self.f.now += 30
        self.assertFalse(b.reap())
        self.f.now += 31
        self.assertTrue(b.reap())
        self.assertTrue(self.f.procs[0].terminated)

    def test_the_queue_holds_tasks_beyond_max_tabs(self):
        b = self.f.browser(self.cfg)
        b.start(True)
        entered, release = threading.Event(), threading.Event()

        def first():
            with b.task('https://a.example/1', 'u1'):
                entered.set()
                release.wait(5)
        t = threading.Thread(target=first)
        t.start()
        entered.wait(5)
        t2 = threading.Thread(target=lambda: b.task('https://a.example/2', 'u2').__enter__())
        t2.start()
        for _ in range(50):
            if b.state()['queue']:
                break
            time.sleep(0.02)
        self.assertEqual([q['who'] for q in b.state()['queue']], ['u2'])
        self.assertEqual(len(b.state()['tabs']), 1)
        release.set()
        t.join(5)
        t2.join(5)
        self.assertEqual(b.state()['queue'], [])


class Fixes(unittest.TestCase):
    def test_signed_out_by_site_and_whole_login_segments(self):
        url = 'https://cursor.example/dashboard/spending'
        self.assertFalse(browser.signed_out(url, 'https://www.cursor.example/dashboard/spending'))
        self.assertFalse(browser.signed_out(url, 'https://cursor.example/author/oauth-apps'))
        self.assertTrue(browser.signed_out(url, 'https://cursor.example/login?next=x'))
        self.assertTrue(browser.signed_out(url, 'https://authenticator.other.example/'))
        self.assertTrue(browser.signed_out(url, 'https://authenticator.cursor.example/?client_id=x'))
        self.assertFalse(browser.signed_out(url, 'https://cursor.example/dashboard/spending?next=2'))
        self.assertFalse(browser.signed_out(url, 'https://cursor.example/auth/settings'))

    def test_a_failing_task_still_closes_its_tab_and_keeps_the_connection(self):
        profile = tmp()
        f = Fake({os.path.join(profile, browser.ACTIVE_PORT): '9222\n/devtools/browser/u\n'})
        tunnel = Proc()
        b = f.browser({'browser': {'mode': 'existing', 'user_profile': profile}})
        b.tunnel = lambda cfg: ('ws://127.0.0.1:20001/devtools/browser/u', tunnel)
        with self.assertRaises(OSError), b.task('https://a.example'):
            raise OSError('the task failed')
        self.assertEqual(f.cdps[0].open, set())
        self.assertIsNotNone(b.cdp)
        self.assertFalse(tunnel.terminated)

    def test_a_connection_that_cannot_close_a_tab_is_dropped_with_its_tunnel(self):
        profile = tmp()
        f = Fake({os.path.join(profile, browser.ACTIVE_PORT): '9222\n/devtools/browser/u\n'})
        tunnel = Proc()
        b = f.browser({'browser': {'mode': 'existing', 'user_profile': profile}})
        b.tunnel = lambda cfg: ('ws://127.0.0.1:20001/devtools/browser/u', tunnel)
        with b.task('https://a.example'):
            def broken(*a, **k):
                raise OSError('reset')
            f.cdps[0].call = broken
        self.assertIsNone(b.cdp)
        self.assertTrue(tunnel.terminated)

    def test_dedicated_mode_attaches_to_a_chrome_another_process_started(self):
        profile = os.path.join(tmp(), 'tb')
        f = Fake({os.path.join(profile, browser.ACTIVE_PORT): '9333\n/devtools/browser/d\n'})
        b = f.browser({'browser': {'mode': 'dedicated', 'profile': profile}})
        b.probe = lambda port: None
        self.assertEqual(b.endpoint(), 'ws://127.0.0.1:9333/devtools/browser/d')
        self.assertEqual(f.launches, [])

    def test_existing_mode_never_changes_the_owners_downloads(self):
        profile = tmp()
        f = Fake({os.path.join(profile, browser.ACTIVE_PORT): '9222\n/devtools/browser/u\n'})
        b = f.browser({'browser': {'mode': 'existing', 'user_profile': profile}}, downloads='/tmp/x')
        b.connection()
        self.assertNotIn('Browser.setDownloadBehavior', [c[0] for c in f.cdps[0].calls])
        d = Fake().browser({'browser': {'mode': 'dedicated', 'profile': os.path.join(tmp(), 'p')}}, downloads='/tmp/x')
        d.connection()
        self.assertIn(('Browser.setDownloadBehavior', {'behavior': 'allow', 'downloadPath': '/tmp/x'}, None),
                      d.connect.__self__.cdps[0].calls)

    def test_the_card_shows_sites_and_names_never_addresses_or_queries(self):
        profile = tmp()
        f = Fake({os.path.join(profile, browser.ACTIVE_PORT): '9222\n/devtools/browser/u\n'})
        cfg = {'browser': {'mode': 'existing', 'user_profile': profile}, 'accounts': {}, 'project': {'state': tmp()}}
        b = f.browser(cfg)
        browsercmd._SHARED.update(b=b, key=json.dumps(browser.settings(cfg), sort_keys=True))
        try:
            with b.task('https://a.example/x?code=SECRET', 'ana@example.org'):
                v = browsercmd.view(cfg)
        finally:
            browsercmd._SHARED.clear()
        self.assertEqual(v['tabs'][0]['who'], 'ana')
        self.assertNotIn('SECRET', json.dumps(v))


class Round3(unittest.TestCase):
    def test_fragmented_messages_and_pings(self):
        cdp = browser.WsCdp.__new__(browser.WsCdp)
        browser.Cdp.__init__(cdp)
        msg = json.dumps({'id': 1, 'result': {'ok': 1}}).encode()
        frames = bytes([0x01, 5]) + msg[:5] + bytes([0x89, 0]) + bytes([0x80, len(msg) - 5]) + msg[5:]
        sent = []
        cdp.sock = types.SimpleNamespace(sendall=sent.append, recv=lambda n: b'')
        cdp.rest = frames
        self.assertEqual(cdp.receive(), {'id': 1, 'result': {'ok': 1}})
        self.assertEqual(sent[0][0], 0x8A)   # the ping got its pong

    def test_a_sign_in_goes_first(self):
        profile = os.path.join(tmp(), 'tb')
        f = Fake()
        b = f.browser({'browser': {'mode': 'dedicated', 'profile': profile}})
        b.signing = True
        got = []
        t = threading.Thread(target=lambda: got.append(b.task('https://a.example', 'u').__enter__()))
        t.start()
        t.join(0.3)
        self.assertEqual(got, [])
        with b.cond:
            b.signing = False
            b.cond.notify_all()
        t.join(5)
        self.assertEqual(len(got), 1)

    def test_a_snap_wrapper_is_not_a_sandbox_chrome(self):
        d = tmp()
        wrapper = os.path.join(d, 'chromium')
        with open(wrapper, 'w', encoding='utf8') as f:
            f.write('#!/bin/sh\nexec /snap/bin/chromium "$@"\n')
        self.assertTrue(browsercmd.snap_wrapper(wrapper))
        real = os.path.join(d, 'chrome')
        with open(real, 'wb') as f:
            f.write(b'\x7fELF')
        self.assertFalse(browsercmd.snap_wrapper(real))

    def test_remote_profile_paths_expand_home_there_and_stay_quoted(self):
        f = browsercmd.remote_port_file
        self.assertEqual(f({'browser': {'mode': 'dedicated', 'profile': '~/work chrome'}}), '"$HOME"\'/work chrome/DevToolsActivePort\'')
        self.assertEqual(f({'browser': {'mode': 'existing', 'user_profile': '/p/x'}}), '/p/x/DevToolsActivePort')
        self.assertIn('"$HOME/.config/google-chrome"', f({'browser': {'mode': 'existing'}}))

    def test_a_sign_in_check_reports_success_and_failure(self):
        cfg = {'accounts': {}, 'browser': {'mode': 'existing', 'sites': ['https://a.example/', 'https://b.example/']},
               'project': {'state': tmp()}}
        b = types.SimpleNamespace(sites={})

        class Brk:
            def __init__(self):
                self.b = b

            def page(self, kind, url, extract, who, tries, wait):
                if 'b.example' in url:
                    raise browser.BrowserError('the work browser does not answer')
                return {'signed_out': False}
        code, answer = browsercmd.act(cfg, None, {'action': 'check'}, Brk())
        self.assertEqual(code, 200)
        self.assertFalse(answer['ok'])
        self.assertIn('does not answer', answer['problems']['https://b.example/'])
        self.assertTrue(b.sites['https://a.example/']['signed_in'])
        b.sites.clear()
        Brk.page = lambda self, *a, **k: {'signed_out': False}
        self.assertTrue(browsercmd.act(cfg, None, {'action': 'check'}, Brk())[1]['ok'])

    def test_file_tasks_use_a_local_folder_only_for_a_local_browser_machine(self):
        state = tmp()
        local = {'browser': {'mode': 'dedicated'}, 'accounts': {}, 'project': {'state': state}}
        remote_cfg = {'browser': {'mode': 'dedicated', 'machine': 'nuc'}, 'accounts': {}, 'project': {'state': state},
                      'machines': {'nuc': {'transport': 'ssh', 'host': 'nuc.example'}}}
        fake = types.SimpleNamespace(mode='dedicated')
        self.assertEqual(browsercmd.broker(local, fake).downloads, os.path.join(state, 'browser-downloads'))
        brk = browsercmd.broker(remote_cfg, fake)
        self.assertIsNone(brk.downloads)
        brk.file_tasks = {'export'}
        with self.assertRaises(browserbroker.OutOfScope):
            brk.take('export', 'a.csv', 'w')

    def test_reading_an_unknown_page_is_not_a_success(self):
        cfg = {'accounts': {}, 'browser': {'mode': 'existing'}, 'project': {'state': tmp()}}
        brk = browserbroker.Broker(None, {}, os.path.join(tmp(), 'l'))
        self.assertEqual(browsercmd.act(cfg, None, {'action': 'read', 'url': 'https://x.example/'}, brk)[0], 404)
        code, answer = browsercmd.act(cfg, None, {'action': 'read', 'url': ['https://x.example/']}, brk)
        self.assertEqual(code, 400)
        self.assertIn('give the page\'s address as text', answer['error'])


class WebSocket(unittest.TestCase):
    def test_frame_round_trip(self):
        buf = [browser.ws_frame('x' * 300, mask=b'\x01\x02\x03\x04')]

        def recv(n):
            out, buf[0] = buf[0][:n], buf[0][n:]
            return out
        self.assertEqual(browser.ws_read(recv), (1, b'x' * 300))

    def test_one_call_against_a_local_server(self):
        srv = socket.socket()
        srv.bind(('127.0.0.1', 0))
        srv.listen(1)

        def serve():
            c, _ = srv.accept()
            data = b''
            while b'\r\n\r\n' not in data:
                data += c.recv(4096)
            msg = json.dumps({'id': 1, 'result': {'ok': 1}}).encode()
            c.sendall(b'HTTP/1.1 101 Switching Protocols\r\n\r\n' + bytes([0x81, len(msg)]) + msg)
            c.recv(4096)
            c.close()
        threading.Thread(target=serve, daemon=True).start()
        cdp = browser.WsCdp(f'ws://127.0.0.1:{srv.getsockname()[1]}/devtools/browser/x')
        self.assertEqual(cdp.call('Browser.getVersion'), {'ok': 1})
        cdp.close()
        srv.close()

    def test_unix_socket_messages_end_with_nul(self):
        path = os.path.join(tmp(), 'd.sock')
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(path)
        srv.listen(1)

        def serve():
            c, _ = srv.accept()
            got = c.recv(4096)
            self.assertTrue(got.endswith(b'\0'))
            c.sendall(json.dumps({'method': 'event'}).encode() + b'\0' + json.dumps({'id': 1, 'result': {'v': 2}}).encode() + b'\0')
            c.close()
        threading.Thread(target=serve, daemon=True).start()
        cdp = browser.open_cdp('unix:' + path)
        self.assertEqual(cdp.call('Browser.getVersion'), {'v': 2})
        cdp.close()
        with self.assertRaises(browser.BrowserError):
            browser.UnixCdp(path + '.missing')
        srv.close()


class Broker(unittest.TestCase):
    def setUp(self):
        self.profile = tmp()
        self.log = os.path.join(tmp(), browserbroker.LOG)
        self.files = {os.path.join(self.profile, browser.ACTIVE_PORT): '9222\n/devtools/browser/u1\n'}

    def broker(self, pages=None):
        self.f = Fake(dict(self.files), pages)
        b = self.f.browser({'browser': {'mode': 'existing', 'user_profile': self.profile}})
        return browserbroker.Broker(b, {'usage-reading': ['cursor.example'], 'page': []}, self.log, tmp())

    def lines(self):
        with open(self.log, encoding='utf8') as f:
            return [json.loads(x) for x in f]

    def test_out_of_scope_is_refused_before_a_tab_opens_and_logged(self):
        brk = self.broker()
        for kind, url in (('usage-reading', 'https://evil.example/'), ('page', 'https://cursor.example/'),
                          ('usage-reading', 'http://cursor.example/'), ('nope', 'https://cursor.example/')):
            with self.subTest(kind=kind, url=url), self.assertRaises(browserbroker.OutOfScope):
                brk.page(kind, url, who='w')
        self.assertEqual(self.f.cdps, [])
        self.assertEqual([x['action'] for x in self.lines()], ['refused'] * 4)

    def test_in_scope_task_logged_without_the_query_and_tab_closed(self):
        brk = self.broker({'value': {'readings': [{'account': 'c', 'used': 3}]}})
        r = brk.page('usage-reading', 'https://www.cursor.example/spending?code=SECRET', 'text', 'worker@nuc', tries=1, wait=0)
        self.assertEqual(r['value']['readings'][0]['used'], 3)
        self.assertEqual(self.f.cdps[0].open, set())
        with open(self.log, encoding='utf8') as f:
            text = f.read()
        self.assertNotIn('SECRET', text)
        self.assertEqual([(x['action'], x['who']) for x in self.lines()], [('open', 'worker@nuc'), ('close', 'worker@nuc')])

    def test_a_page_that_leaves_the_scope_returns_no_content(self):
        brk = self.broker({'value': {'text': 'private'}, 'href': 'https://other.example/x'})
        r = brk.page('usage-reading', 'https://cursor.example/spending', 'text', 'w', tries=1, wait=0)
        self.assertTrue(r['withheld'])
        self.assertEqual(r['value'], {})

    def test_no_task_brings_its_own_javascript(self):
        brk = self.broker()
        with self.assertRaises(browser.BrowserError):
            brk.page('usage-reading', 'https://cursor.example/spending', 'document.cookie', 'w')
        self.assertEqual(self.f.cdps, [])

    def test_a_worker_takes_only_a_named_file_for_a_file_task(self):
        brk = self.broker()
        brk.file_tasks = {'export'}
        brk.scope['export'] = ['cursor.example']
        with open(os.path.join(brk.downloads, 'a.csv'), 'w', encoding='utf8') as f:
            f.write('1')
        self.assertEqual(brk.take('export', 'a.csv', 'w'), b'1')
        for bad in ('../a.csv', 'b.csv', '.'):
            with self.subTest(bad=bad), self.assertRaises(browser.BrowserError):
                brk.take('export', bad, 'w')
        with self.assertRaises(browserbroker.OutOfScope):
            brk.take('usage-reading', 'a.csv', 'w')

    def test_a_file_over_the_cap_is_refused_with_the_setting_to_raise(self):
        brk = self.broker()
        brk.file_tasks = {'export'}
        brk.max_bytes = 4
        with open(os.path.join(brk.downloads, 'big.csv'), 'w', encoding='utf8') as f:
            f.write('12345')
        with open(os.path.join(brk.downloads, 'ok.csv'), 'w', encoding='utf8') as f:
            f.write('1234')
        self.assertEqual(brk.take('export', 'ok.csv', 'w'), b'1234')
        with self.assertRaisesRegex(browser.BrowserError, 'browser.max_file_mb'):
            brk.take('export', 'big.csv', 'w')
        self.assertEqual(browserbroker.Broker(None, {}, 'l').max_bytes, browserbroker.MAX_FILE_MB * 1024 * 1024)

    def test_the_workers_http_broker_needs_its_token_and_keeps_the_scope(self):
        brk = self.broker()
        httpd = browserbroker.serve(brk, 'usage-reading', 'worker@nuc')
        self.assertEqual(httpd.server_address[0], '127.0.0.1')
        url = f'http://127.0.0.1:{httpd.server_address[1]}/page'

        def post(body, token):
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST', headers={'X-Tutti-Task': token})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                e.close()
                return e.code
        with browserbroker.running(httpd):
            self.assertEqual(post({'url': 'https://cursor.example/'}, 'wrong'), 403)
            self.assertEqual(post({'url': 'https://evil.example/'}, httpd.token), 403)
            self.assertEqual(post({'url': 'https://cursor.example/x', 'extract': 'text'}, httpd.token), 200)
        self.assertEqual(self.f.cdps[0].open, set())


class Sandbox(unittest.TestCase):
    def test_the_unit_applies_every_sandbox_flag_and_has_no_tcp_port(self):
        text = browsersandbox.unit('/usr/bin/chromium', ['10.0.0.5/32'])
        for flag in browsersandbox.SANDBOX + ('NoNewPrivileges=yes', 'ProtectHome=yes', 'PrivateTmp=yes', 'ProtectSystem=strict',
                                              'User=tutti-browser', 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK'):
            self.assertIn(flag + '\n', text)
        self.assertIn('IPAddressDeny=localhost link-local', text)
        self.assertIn('IPAddressAllow=127.0.0.53/32 10.0.0.5/32\n', text)
        self.assertIn('--remote-debugging-pipe', text)
        self.assertNotIn('--remote-debugging-port', text)
        self.assertIn(browsersandbox.SOCKET, text)

    def test_one_root_command_creates_the_user_and_the_unit(self):
        state = tmp()
        cmd = browsersandbox.write_setup(state, '/usr/bin/chromium', 'owner')
        path = os.path.join(state, 'tutti-browser-setup.sh')
        self.assertEqual(cmd, f'sudo bash {path}')
        with open(path, encoding='utf8') as f:
            text = f.read()
        for part in ('useradd --system', 'groupadd --system tutti-broker', 'usermod -aG tutti-broker owner', 'bridge.py',
                     'enable --now tutti-browser.service', '-m 2770 /var/lib/tutti-browser/downloads'):
            self.assertIn(part, text)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'the isolated mode and its bridge are Linux only (systemd)')
    def test_the_bridge_relays_the_pipe_on_a_0660_socket(self):
        import grp
        import stat
        import subprocess
        d = tmp()
        bridge, chrome, sock = os.path.join(d, 'bridge.py'), os.path.join(d, 'chrome.py'), os.path.join(d, 'devtools.sock')
        with open(bridge, 'w', encoding='utf8') as f:
            f.write(browsersandbox.BRIDGE)
        with open(chrome, 'w', encoding='utf8') as f:   # a fake Chrome: DevTools on fds 3 and 4, NUL-separated
            f.write('import json, os\nbuf = b""\nwhile True:\n    c = os.read(3, 65536)\n    if not c:\n        break\n    buf += c\n'
                    '    while b"\\0" in buf:\n        m, buf = buf.split(b"\\0", 1)\n        m = json.loads(m)\n'
                    '        os.write(4, json.dumps({"id": m["id"], "result": {"echo": m["method"]}}).encode() + b"\\0")\n')
        p = subprocess.Popen([sys.executable, bridge, sock, grp.getgrgid(os.getgid()).gr_name, sys.executable, chrome])
        try:
            for _ in range(100):
                if os.path.exists(sock):
                    break
                time.sleep(0.05)
            self.assertEqual(stat.S_IMODE(os.stat(sock).st_mode), 0o660)
            cdp = browser.open_cdp('unix:' + sock)
            self.assertEqual(cdp.call('Browser.getVersion'), {'echo': 'Browser.getVersion'})
            cdp.close()
        finally:
            p.terminate()
            p.wait(timeout=5)

    def test_isolate_refuses_a_chrome_the_sandbox_cannot_run(self):
        out = []
        cfg = {'browser': {'path': '/snap/bin/chromium'}, 'project': {'state': tmp()}}
        with unittest.mock.patch.object(browser, 'find', return_value=('/snap/bin/chromium', None)):
            rc = browsercmd.isolate(cfg, out=types.SimpleNamespace(write=out.append, flush=lambda: None))
        self.assertEqual(rc, 2)
        self.assertIn('system Chrome', ''.join(out))

    def test_isolated_mode_connects_on_the_unix_socket(self):
        f = Fake()
        b = f.browser({'browser': {'mode': 'isolated', 'socket': '/run/x.sock'}})
        self.assertEqual(b.endpoint(), 'unix:/run/x.sock')
        with self.assertRaises(browser.BrowserError):
            b.sign_in('https://cursor.example/')
        self.assertEqual(f.launches, [])


class Commands(unittest.TestCase):
    def setUp(self):
        self.cfg = {'accounts': {'c': {'usage_url': 'https://cursor.example/spending'}},
                    'machines': {'nuc': {'transport': 'ssh', 'host': 'nuc.example'}},
                    'browser': {'mode': 'existing', 'sites': ['https://cursor.example/spending']},
                    'project': {'state': tmp()}}

    def test_api_refuses_a_site_outside_the_admins_list(self):
        brk = browserbroker.Broker(types.SimpleNamespace(sign_in=lambda u: None), {'sign-in': ['cursor.example']},
                                   os.path.join(tmp(), 'l'))
        self.assertEqual(browsercmd.act(self.cfg, {'email': 'bo@example'}, {'action': 'sign-in', 'url': 'https://evil.example/'},
                                        brk)[0], 403)
        code, answer = browsercmd.act(self.cfg, None, {'action': 'sign-in', 'url': 'https://cursor.example/spending'}, brk)
        self.assertEqual(code, 200)
        self.assertIn('shares it', answer['message'])

    def test_a_remote_worker_gets_the_broker_on_127_0_0_1_never_devtools(self):
        brk = browserbroker.Broker(None, {'usage-reading': ['cursor.example']}, os.path.join(tmp(), 'l'))
        seen = {}

        def sh(m, script, **kw):
            seen.update(script=script, opts=kw['ssh_opts'], stdin=kw['stdin'])
            return types.SimpleNamespace(returncode=0, stdout='{}', stderr='')
        with self.assertRaises(browserbroker.OutOfScope):
            browsercmd.remote_task(self.cfg, 'nuc', 'usage-reading', 'https://evil.example/', sh=sh, brk=brk)
        self.assertEqual(seen, {})
        browsercmd.remote_task(self.cfg, 'nuc', 'usage-reading', 'https://cursor.example/spending', sh=sh, brk=brk)
        forward = seen['opts'][seen['opts'].index('-R') + 1]
        self.assertTrue(forward.startswith('127.0.0.1:') and ':127.0.0.1:' in forward)
        self.assertEqual(seen['script'], 'bash -s')   # the token never reaches a command line
        self.assertIn('export TUTTI_BROWSER_TOKEN=', seen['stdin'])
        self.assertNotIn('devtools', seen['stdin'])

    def test_targets_parse(self):
        a = browsercmd.parse_target(types.SimpleNamespace(action='remote', target=['nuc', 'https://x.example/', '--', 'cmd', 'a']))
        self.assertEqual((a.machine, a.url, a.command), ('nuc', 'https://x.example/', ['cmd', 'a']))


if __name__ == '__main__':
    unittest.main()
