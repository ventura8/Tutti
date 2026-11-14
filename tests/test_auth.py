"""Sign-in with Google, sessions, roles and invites (tutti/auth.py, tutti/authweb.py, tutti/invites.py), with a fake JWKS."""
import base64
import hashlib
import http.client
import io
import json
import multiprocessing
import os
import re
import secrets
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace

from tutti import auth, authcmd, authweb, config, dashboard, invites, routing

CLIENT = '1234567890-abcdefgh12345678.apps.googleusercontent.com'


def _probable_prime(n):
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(24):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    while True:
        c = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if _probable_prime(c):
            return c


def rsa_key():
    """(n, e, d) of a fresh 2048-bit test key, made with the standard library only."""
    e = 65537
    while True:
        p, q = _prime(1024), _prime(1024)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            return p * q, e, pow(e, -1, phi)


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def sign(key, message):
    n, _e, d = key
    k = (n.bit_length() + 7) // 8
    t = auth.SHA256_INFO + hashlib.sha256(message).digest()
    em = b'\x00\x01' + b'\xff' * (k - len(t) - 3) + b'\x00' + t
    return pow(int.from_bytes(em, 'big'), d, n).to_bytes(k, 'big')


def token(key, claims, kid='k1', alg='RS256'):
    head = b64(json.dumps({'alg': alg, 'kid': kid, 'typ': 'JWT'}).encode())
    body = b64(json.dumps(claims).encode())
    return f'{head}.{body}.{b64(sign(key, f"{head}.{body}".encode()))}'


def claims(sub='s1', email='a@example.com', now=None, **over):
    now = now or time.time()
    return {'iss': 'https://accounts.google.com', 'aud': CLIENT, 'sub': sub, 'email': email, 'email_verified': True, 'name': 'A',
            'iat': int(now), 'exp': int(now) + 3600, **over}


def jwks_doc(key, kid='k1'):
    n, e, _d = key
    return {'keys': [{'kty': 'RSA', 'alg': 'RS256', 'kid': kid, 'n': b64(n.to_bytes((n.bit_length() + 7) // 8, 'big')),
                      'e': b64(e.to_bytes(3, 'big'))}]}


KEY = OTHER = None


def setUpModule():
    global KEY, OTHER
    KEY, OTHER = rsa_key(), rsa_key()


class FakeJwks:
    def __init__(self, key):
        self.calls = 0
        self.key_ = key

    def fetch(self):
        self.calls += 1
        return jwks_doc(self.key_), 3600


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.fake = FakeJwks(KEY)
        self.jwks = auth.Jwks(fetch=self.fake.fetch, clock=lambda: self.now)

    def check(self, tok, nonce=None):
        return auth.verify(tok, CLIENT, self.jwks, self.now, nonce=nonce)

    def test_a_good_token_gives_the_user(self):
        who = self.check(token(KEY, claims(nonce='n1')), nonce='n1')
        self.assertEqual((who['sub'], who['email']), ('s1', 'a@example.com'))

    def test_bad_signature_is_refused(self):
        with self.assertRaisesRegex(auth.TokenError, 'bad signature'):
            self.check(token(OTHER, claims()))
        good = token(KEY, claims())
        head, body, sig = good.split('.')
        forged = b64(json.dumps(claims(sub='admin')).encode())
        with self.assertRaisesRegex(auth.TokenError, 'bad signature'):
            self.check(f'{head}.{forged}.{sig}')

    def test_wrong_audience_issuer_and_alg_are_refused(self):
        with self.assertRaisesRegex(auth.TokenError, 'wrong audience'):
            self.check(token(KEY, claims(aud='999-other.apps.googleusercontent.com')))
        with self.assertRaisesRegex(auth.TokenError, 'wrong issuer'):
            self.check(token(KEY, claims(iss='https://evil.example')))
        with self.assertRaisesRegex(auth.TokenError, 'RS256'):
            self.check(token(KEY, claims(), alg='none'))

    def test_expired_token_is_refused(self):
        with self.assertRaisesRegex(auth.TokenError, 'expired'):
            self.check(token(KEY, claims(now=self.now - 7200)))

    def test_nonce_and_unverified_email_are_refused(self):
        with self.assertRaisesRegex(auth.TokenError, 'another tab'):
            self.check(token(KEY, claims(nonce='other')), nonce='mine')
        with self.assertRaisesRegex(auth.TokenError, 'verified'):
            self.check(token(KEY, claims(email_verified=False)))

    def test_keys_are_cached_and_an_unknown_kid_fetches_at_most_once_a_minute(self):
        for _ in range(3):
            self.check(token(KEY, claims()))
        self.assertEqual(self.fake.calls, 1)
        with self.assertRaises(auth.TokenError):
            self.check(token(KEY, claims(), kid='new'))
        self.assertEqual(self.fake.calls, 1)   # fetched less than a minute ago
        self.now += auth.JWKS_RETRY
        for _ in range(3):
            with self.assertRaises(auth.TokenError):
                self.check(token(KEY, claims(), kid='new'))
        self.assertEqual(self.fake.calls, 2)

    def test_small_keys_are_refused(self):
        self.assertFalse(auth.rsa_verify(3233, 17, b'x', b'\x00\x01'))


def _sign_in_process(folder, sub, out):
    out.put(auth.Users(folder).sign_in({'sub': sub, 'email': f'{sub}@example.com', 'name': sub})['role'])


class UsersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def who(self, sub):
        return {'sub': sub, 'email': f'{sub}@example.com', 'name': sub}

    def test_first_admin_race_between_threads_has_one_winner(self):
        start, roles = threading.Barrier(8), []

        def go(i):
            start.wait()
            roles.append(auth.Users(self.folder).sign_in(self.who(f'u{i}'))['role'])
        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(roles), ['admin'] + ['pending'] * 7)

    def test_first_admin_race_between_processes_has_one_winner(self):
        ctx = multiprocessing.get_context('spawn')
        out = ctx.Queue()
        procs = [ctx.Process(target=_sign_in_process, args=(self.folder, f'p{i}', out)) for i in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
        self.assertEqual(sorted(out.get(timeout=5) for _ in procs), ['admin', 'pending', 'pending', 'pending'])

    def test_files_are_mode_600(self):
        auth.Users(self.folder).sign_in(self.who('a'))
        auth.Sessions(self.folder, 1).create('a')
        for name in (auth.USERS_FILE, auth.SESSIONS_FILE):
            self.assertEqual(os.stat(os.path.join(self.folder, name)).st_mode & 0o777, 0o600)

    def test_the_last_admin_cannot_be_removed_or_demoted(self):
        users = auth.Users(self.folder)
        users.sign_in(self.who('a'))
        users.sign_in(self.who('b'))
        with self.assertRaisesRegex(auth.AuthError, 'last admin'):
            users.set_role('a', 'viewer')
        with self.assertRaisesRegex(auth.AuthError, 'last admin'):
            users.remove('a')
        users.set_role('b', 'admin')
        users.set_role('a', 'viewer')
        self.assertEqual(users.get('a')['role'], 'viewer')

    def test_sessions_expire(self):
        now = [1000.0]
        s = auth.Sessions(self.folder, 1, clock=lambda: now[0])
        sid, csrf = s.create('a')
        self.assertEqual(s.get(sid)['csrf'], csrf)
        now[0] += 3601
        self.assertIsNone(s.get(sid))
        self.assertIsNone(s.get('made-up'))

    def test_the_sessions_file_holds_no_cookie(self):
        sid, _ = auth.Sessions(self.folder, 1).create('a')
        with open(os.path.join(self.folder, auth.SESSIONS_FILE), encoding='utf8') as f:
            self.assertNotIn(sid, f.read())


class InvitesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [1_000_000.0]
        self.inv = invites.Invites(self.tmp.name, clock=lambda: self.now[0])
        self.users = auth.Users(self.tmp.name, clock=lambda: self.now[0])
        self.users.sign_in({'sub': 'owner', 'email': 'o@example.com', 'name': 'O'})

    def tearDown(self):
        self.tmp.cleanup()

    def join(self, sub, tok):
        return self.users.sign_in({'sub': sub, 'email': f'{sub}@example.com', 'name': sub}, invite=tok, invites=self.inv)

    def test_the_role_is_applied_at_once_and_only_the_hash_is_kept(self):
        tok, _ = self.inv.create('viewer')
        self.assertEqual(self.join('v', tok)['role'], 'viewer')
        tok2, _ = self.inv.create('admin')
        self.assertEqual(self.join('a2', tok2)['role'], 'admin')
        with open(self.inv.path, encoding='utf8') as f:
            self.assertNotIn(tok, f.read())
        self.assertEqual(self.inv.signed_in_with(invites.invite_id(tok)), 'v')

    def test_single_use(self):
        tok, _ = self.inv.create()
        self.join('v', tok)
        with self.assertRaisesRegex(invites.InviteError, 'already used'):
            self.join('w', tok)
        self.assertIsNone(self.users.get('w'))
        self.assertEqual(self.inv.redeem(tok, 'household', sub='v'), 'viewer')   # its household use is separate, once too
        with self.assertRaisesRegex(invites.InviteError, 'already used'):
            self.inv.redeem(tok, 'household')

    def test_expiry_and_revoke(self):
        tok, rec = self.inv.create()
        self.now[0] += invites.DAYS * 86400 + 1
        with self.assertRaisesRegex(invites.InviteError, 'expired'):
            self.join('v', tok)
        tok, rec = self.inv.create()
        self.inv.revoke(rec['id'])
        with self.assertRaisesRegex(invites.InviteError, 'revoked'):
            self.join('v', tok)
        with self.assertRaisesRegex(invites.InviteError, 'not known'):
            self.join('v', 'made-up')

    def test_household_secret_is_the_same_on_both_sides(self):
        tok, rec = self.inv.create()
        self.assertEqual(invites.household_secret(tok), self.inv.household_secret_for(rec['id']))
        self.assertEqual(invites.parse_url(invites.url('https://t.example', tok)), ('https://t.example', tok))
        with self.assertRaises(invites.InviteError):
            invites.parse_url('https://t.example/other#x')


def cfg_of(folder, **dash):
    raw = {'project': {'worktrees': folder, 'state': os.path.join(folder, 'state')}, 'machines': {'here': {'host': 'local'}},
           'auth': {'client_id': CLIENT}, 'dashboard': dash}
    cfg = config.normalise(raw)
    cfg['_path'] = os.path.join(folder, 'tutti.config.yaml')
    return cfg


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cfg = cfg_of(cls.tmp.name)
        collector = dashboard.Collector(cls.cfg, probe_fn=lambda m: routing.Probe(True, 1, 0.5, 4, False),
                                        available_fn=lambda w: (True, 'ok'), stored_fn=lambda m: None)
        collector.refresh = lambda: None
        cls.httpd = dashboard.server(cls.cfg, 0, collector)
        cls.port = cls.httpd.server_address[1]
        cls.fake = FakeJwks(KEY)
        cls.httpd.auth.jwks = auth.Jwks(fetch=cls.fake.fetch)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.admin = cls.sign_in('owner')
        cls.viewer = cls.sign_in('bo')
        cls.post('/api/auth/users', {'action': 'approve', 'sub': 'bo'}, cls.admin)
        cls.pending = cls.sign_in('cy')

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    @classmethod
    def request(self, method, path, body=None, who=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Host': f'127.0.0.1:{self.port}', 'Origin': f'http://127.0.0.1:{self.port}', 'Content-Type': 'application/json'}
        if who:
            h['Cookie'] = who['cookie']
            h['X-Tutti-Token'] = who['csrf']
        h.update(headers or {})
        c.request(method, path, body=None if body is None else json.dumps(body), headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    @classmethod
    def get(self, path, who=None, headers=None):
        return self.request('GET', path, who=who, headers=headers)

    @classmethod
    def post(self, path, body, who=None, headers=None):
        return self.request('POST', path, body, who, headers)

    @classmethod
    def sign_in(self, sub, invite=None, tok=None):
        r, page = self.get('/')
        login = re.search(r'(tutti_login_\d+)=([^;]+)', r.getheader('Set-Cookie')).group(0)
        nonce = re.search(rb'name="tutti-nonce" content="([0-9a-f]+)"', page).group(1).decode()
        cred = tok or token(KEY, claims(sub=sub, email=f'{sub}@example.com', nonce=nonce))
        body = {'credential': cred, **({'invite': invite} if invite else {})}
        r, data = self.post('/api/auth/login', body, headers={'Cookie': login})
        if r.status != 200:
            return {'status': r.status, 'error': json.loads(data)['error']}
        cookie = re.search(r'(tutti_session_\d+=[^;]+)', r.getheader('Set-Cookie')).group(1)
        assert 'HttpOnly' in r.getheader('Set-Cookie') and 'SameSite=Strict' in r.getheader('Set-Cookie')
        r, page = self.request('GET', '/', headers={'Cookie': cookie})
        csrf = re.search(rb'name="tutti-token" content="([^"]*)"', page).group(1).decode()
        return {'cookie': cookie, 'csrf': csrf, 'role': json.loads(data)['role']}

    def test_roles_from_the_first_sign_ins(self):
        self.assertEqual((self.admin['role'], self.viewer['role'], self.pending['role']), ('admin', 'pending', 'pending'))

    def test_without_a_session_only_the_sign_in_page_and_the_code_answer(self):
        r, page = self.get('/')
        self.assertIn(b'tutti-nonce', page)
        self.assertIn('accounts.google.com/gsi/client', r.getheader('Content-Security-Policy'))
        self.assertEqual(self.get('/api/state')[0].status, 401)
        self.assertEqual(self.get('/app.css')[0].status, 200)
        self.assertEqual(json.loads(self.get('/api/auth/me')[1])['signed_in'], False)
        self.assertEqual(self.post('/api/autopilot', {'enabled': True})[0].status, 401)

    def test_a_pending_user_is_blocked(self):
        self.assertEqual(self.get('/api/state', self.pending)[0].status, 403)
        r, page = self.get('/', self.pending)
        self.assertIn(b'tutti-nonce', page)   # the waiting page, not the dashboard
        self.assertNotIn(b'id="usage"', page)
        self.assertEqual(self.post('/api/autopilot', {'enabled': True}, self.pending)[0].status, 403)

    def test_a_viewer_sees_the_dashboard_uncached(self):
        r, _ = self.get('/api/state', self.viewer)
        self.assertEqual((r.status, r.getheader('Cache-Control')), (200, 'no-store'))
        r, page = self.get('/', self.viewer)
        self.assertEqual(r.getheader('Cache-Control'), 'no-store')
        self.assertIn(b'id="usage"', page)

    def test_a_viewer_gets_403_on_every_mutating_route_and_on_settings(self):
        for path in [*dashboard.POSTS, '/api/auth/users', '/api/auth/invites']:
            if path == '/api/telemetry':
                continue
            r, _ = self.post(path, {'action': 'create', 'enabled': True}, self.viewer)
            self.assertEqual(r.status, 403, path)
        for path in ('/api/settings', '/settings', '/helper', '/api/auth/users'):
            self.assertEqual(self.get(path, self.viewer)[0].status, 403, path)
        self.assertEqual(self.post('/api/telemetry', {'consent': True}, self.viewer)[0].status, 403)
        self.assertEqual(self.post('/api/telemetry', {'use': {'panel': 'usage'}}, self.viewer)[0].status, 200)

    def test_csrf_and_origin_are_checked_on_every_post(self):
        body = {'threshold_pct': 50}
        self.assertEqual(self.post('/api/autocompact', body, self.admin)[0].status, 200)
        no_token = {**self.admin, 'csrf': ''}
        self.assertEqual(self.post('/api/autocompact', body, no_token)[0].status, 403)
        self.assertEqual(self.post('/api/autocompact', body, {**self.admin, 'csrf': self.viewer['csrf']})[0].status, 403)
        self.assertEqual(self.post('/api/autocompact', body, self.admin, {'Origin': 'http://evil.example'})[0].status, 403)
        self.assertEqual(self.post('/api/auth/users', {'action': 'approve', 'sub': 'cy'}, no_token)[0].status, 403)
        self.assertEqual(self.post('/api/auth/login', {'credential': 'x'}, headers={'Origin': 'http://evil.example'})[0].status, 403)

    def test_owner_only_actions_and_the_last_admin(self):
        r, data = self.get('/api/auth/users', self.admin)
        self.assertEqual({u['sub'] for u in json.loads(data)['users']} >= {'owner', 'bo', 'cy'}, True)
        r, data = self.post('/api/auth/users', {'action': 'role', 'sub': 'owner', 'role': 'viewer'}, self.admin)
        self.assertEqual(r.status, 409)
        self.assertEqual(self.post('/api/auth/users', {'action': 'remove', 'sub': 'owner'}, self.admin)[0].status, 409)

    def test_invite_signs_in_with_its_role_once(self):
        r, data = self.post('/api/auth/invites', {'action': 'create', 'role': 'viewer'}, self.admin)
        answer = json.loads(data)
        _origin, tok = invites.parse_url(answer['url'])
        self.assertTrue(answer['join'].startswith('tutti household join http'))
        dee = self.sign_in('dee', invite=tok)
        self.assertEqual(dee['role'], 'viewer')
        self.assertEqual(self.get('/api/state', dee)[0].status, 200)
        again = self.sign_in('eve', invite=tok)
        self.assertEqual(again['status'], 403)
        self.assertIn('already used', again['error'])

    def test_a_forged_or_expired_token_does_not_sign_in(self):
        self.assertEqual(self.sign_in('x', tok=token(OTHER, claims(sub='x')))['status'], 401)
        self.assertEqual(self.sign_in('x', tok=token(KEY, claims(sub='x', now=time.time() - 9000)))['status'], 401)
        self.assertEqual(self.sign_in('x', tok=token(KEY, claims(sub='x')))['status'], 401)   # no nonce of this browser

    def test_sign_out_and_session_expiry(self):
        who = self.sign_in('fay')
        self.assertEqual(self.post('/api/auth/logout', {}, who)[0].status, 200)
        self.assertEqual(self.get('/api/auth/me', who)[1].count(b'"signed_in": false'), 1)
        sessions = self.httpd.auth.sessions
        try:
            sessions.clock = lambda: time.time() + sessions.ttl + 5
            self.assertEqual(self.get('/api/state', self.admin)[0].status, 401)
        finally:
            sessions.clock = time.time
        self.assertEqual(self.get('/api/state', self.admin)[0].status, 200)


class SafetyTest(unittest.TestCase):
    def test_a_reachable_dashboard_needs_sign_in(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = cfg_of(folder, bind='0.0.0.0')
            cfg['auth']['client_id'] = ''
            with self.assertRaises(dashboard.AuthRequired):
                dashboard.server(cfg, 0, dashboard.Collector(cfg))
            cfg = cfg_of(folder, origins=['https://tutti.example'])
            cfg['auth']['client_id'] = ''
            with self.assertRaises(dashboard.AuthRequired):
                dashboard.server(cfg, 0, dashboard.Collector(cfg))

    def test_without_a_client_id_the_dashboard_is_as_before(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = cfg_of(folder)
            cfg['auth']['client_id'] = ''
            httpd = dashboard.server(cfg, 0, dashboard.Collector(cfg))
            self.assertIsNone(httpd.auth)
            code, body, *_ = authweb.get(None, '/api/auth/me', {}, lambda t: None)
            self.assertEqual(json.loads(body), {'enabled': False, 'setup': 'tutti auth setup'})
            httpd.server_close()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_of(self.tmp.name)
        self.cfg['auth']['client_id'] = ''

    def tearDown(self):
        self.tmp.cleanup()

    def run_auth(self, action, **kw):
        out, err = io.StringIO(), io.StringIO()
        a = SimpleNamespace(action=action, email=kw.get('email'), client_id=kw.get('client_id'), yes=kw.get('yes', False))
        with redirect_stderr(err):
            rc = authcmd.cmd_auth(self.cfg, a, out, {'isatty': lambda: False})
        return rc, out.getvalue() + err.getvalue()

    def test_setup_prints_the_console_steps_and_saves_the_client_id(self):
        rc, text = self.run_auth('setup')
        self.assertEqual(rc, 2)
        self.assertIn('http://127.0.0.1:4777', text)
        self.assertIn('Web application', text)
        self.assertEqual(self.run_auth('setup', client_id='not-an-id')[0], 2)
        self.assertEqual(self.run_auth('setup', client_id=CLIENT)[0], 0)
        self.assertEqual(auth.client_id(self.cfg), CLIENT)

    def test_promote_and_reset_admin(self):
        users = auth.Users(config.state_dir(self.cfg))
        users.sign_in({'sub': 'a', 'email': 'a@example.com', 'name': 'A'})
        users.sign_in({'sub': 'b', 'email': 'B@example.com', 'name': 'B'})
        self.assertEqual(self.run_auth('promote', email='b@example.com')[0], 0)
        self.assertEqual(users.get('b')['role'], 'admin')
        self.assertEqual(self.run_auth('promote', email='nobody@example.com')[0], 2)
        self.assertEqual(self.run_auth('reset-admin')[0], 2)
        self.assertEqual(self.run_auth('reset-admin', yes=True)[0], 0)
        self.assertEqual(users.all(), {})
        rc, text = self.run_auth('users')
        self.assertIn('first sign-in becomes admin', text)


if __name__ == '__main__':
    unittest.main()
