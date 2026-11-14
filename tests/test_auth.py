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
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest import mock

from tutti import auth, authcmd, authdevice, authweb, config, dashboard, invites, routing

CLIENT = '1234567890-abcdefgh12345678.apps.googleusercontent.com'
TV = '1234567890-tvdevice12345678.apps.googleusercontent.com'


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
        other = token(OTHER, claims())
        with self.assertRaisesRegex(auth.TokenError, 'bad signature'):
            self.check(other)
        good = token(KEY, claims())
        head, body, sig = good.split('.')
        forged = f'{head}.{b64(json.dumps(claims(sub="admin")).encode())}.{sig}'
        with self.assertRaisesRegex(auth.TokenError, 'bad signature'):
            self.check(forged)

    def test_wrong_audience_issuer_and_alg_are_refused(self):
        cases = [('wrong audience', token(KEY, claims(aud='999-other.apps.googleusercontent.com'))),
                 ('wrong issuer', token(KEY, claims(iss='https://evil.example'))), ('RS256', token(KEY, claims(), alg='none'))]
        for why, tok in cases:
            with self.subTest(why), self.assertRaisesRegex(auth.TokenError, why):
                self.check(tok)

    def test_expired_token_is_refused(self):
        old = token(KEY, claims(now=self.now - 7200))
        with self.assertRaisesRegex(auth.TokenError, 'expired'):
            self.check(old)

    def test_nonce_and_unverified_email_are_refused(self):
        other_nonce, unverified = token(KEY, claims(nonce='other')), token(KEY, claims(email_verified=False))
        with self.assertRaisesRegex(auth.TokenError, 'another tab'):
            self.check(other_nonce, nonce='mine')
        with self.assertRaisesRegex(auth.TokenError, 'verified'):
            self.check(unverified)

    def test_keys_are_cached_and_an_unknown_kid_fetches_at_most_once_a_minute(self):
        for _ in range(3):
            self.check(token(KEY, claims()))
        self.assertEqual(self.fake.calls, 1)
        unknown = token(KEY, claims(), kid='new')
        with self.assertRaises(auth.TokenError):
            self.check(unknown)
        self.assertEqual(self.fake.calls, 1)   # fetched less than a minute ago
        self.now += auth.JWKS_RETRY
        for _ in range(3):
            with self.assertRaises(auth.TokenError):
                self.check(unknown)
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

    def test_may_act_for_admin_anyone_member_self(self):
        admin, viewer, pending = {'sub': 'a', 'role': 'admin'}, {'sub': 'v', 'role': 'viewer'}, {'sub': 'p', 'role': 'pending'}
        self.assertEqual([auth.may_act_for(admin, 'v'), auth.may_act_for(viewer, 'v'), auth.may_act_for(viewer, 'a'),
                          auth.may_act_for(pending, 'p'), auth.may_act_for(None, 'v')], [True, True, False, False, False])
        e = auth.act_for(self.folder, {**admin, 'email': 'a@example.com'}, {'sub': 'v', 'email': 'v@example.com'}, 'main agent',
                         detail='codex')
        self.assertEqual((e['by'], e['target'], e['action']), ('a', 'v', 'main agent'))
        with self.assertRaises(auth.AuthError):
            auth.act_for(self.folder, viewer, {'sub': 'a'}, 'main agent')
        self.assertEqual(len(auth.audit_log(self.folder)), 1)

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

    def test_a_bad_invite_fails_only_who_needed_it(self):
        tok, rec = self.inv.create()
        self.inv.revoke(rec['id'])
        self.assertEqual(self.users.sign_in({'sub': 'owner', 'email': 'o@example.com', 'name': 'O'}, invite=tok,
                                            invites=self.inv)['role'], 'admin')
        with self.assertRaisesRegex(invites.InviteError, 'revoked'):
            self.join('new', tok)

    def test_an_invite_stays_revocable_until_its_household_use(self):
        tok, rec = self.inv.create()
        self.join('v', tok)
        self.assertTrue(self.inv.listing()[0]['live'])
        self.inv.revoke_all()
        with self.assertRaisesRegex(invites.InviteError, 'revoked'):
            self.inv.redeem(tok, 'household')

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
        flags = r.getheader('Set-Cookie')
        assert 'HttpOnly' in flags
        assert 'SameSite=Strict' in flags
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
        self.assertFalse(json.loads(self.get('/api/auth/me')[1])['signed_in'])
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
        body = {'mode': 'auto'}
        self.assertEqual(self.post('/api/owner', body, self.admin)[0].status, 200)
        no_token = {**self.admin, 'csrf': ''}
        self.assertEqual(self.post('/api/owner', body, no_token)[0].status, 403)
        self.assertEqual(self.post('/api/owner', body, {**self.admin, 'csrf': self.viewer['csrf']})[0].status, 403)
        self.assertEqual(self.post('/api/owner', body, self.admin, {'Origin': 'http://evil.example'})[0].status, 403)
        self.assertEqual(self.post('/api/auth/users', {'action': 'approve', 'sub': 'cy'}, no_token)[0].status, 403)
        self.assertEqual(self.post('/api/auth/login', {'credential': 'x'}, headers={'Origin': 'http://evil.example'})[0].status, 403)

    def test_owner_only_actions_and_the_last_admin(self):
        r, data = self.get('/api/auth/users', self.admin)
        self.assertGreaterEqual({u['sub'] for u in json.loads(data)['users']}, {'owner', 'bo', 'cy'})
        r, data = self.post('/api/auth/users', {'action': 'role', 'sub': 'owner', 'role': 'viewer'}, self.admin)
        self.assertEqual(r.status, 409)
        self.assertEqual(self.post('/api/auth/users', {'action': 'remove', 'sub': 'owner'}, self.admin)[0].status, 409)
        who = self.httpd.collector.who
        self.assertEqual(who({'Cookie': self.admin['cookie']}), {'sub': 'owner', 'email': 'owner@example.com', 'role': 'admin'})
        self.assertIsNone(who({}))
        self.assertIsNone(authweb.who(None, {}))

    def test_promote_demote_self_and_the_audit_log(self):
        def role(sub, r, who):
            return self.post('/api/auth/users', {'action': 'role', 'sub': sub, 'role': r}, who)[0].status
        self.assertEqual(role('bo', 'admin', self.viewer), 403)   # a viewer cannot change roles
        self.assertEqual(role('owner', 'viewer', self.admin), 409)   # the last admin
        try:
            self.assertEqual(role('bo', 'admin', self.admin), 200)   # promote
            r, data = self.post('/api/auth/users', {'action': 'remove', 'sub': 'owner'}, self.admin)
            self.assertEqual(r.status, 409)
            self.assertIn('yourself', json.loads(data)['error'])   # never oneself, even with another admin
            self.assertEqual(role('owner', 'viewer', self.admin), 200)   # demote oneself: another admin exists
            self.assertEqual(role('bo', 'viewer', self.admin), 403)   # now a viewer: refused on the server
            self.assertEqual(role('bo', 'viewer', self.viewer), 409)   # bo is the last admin now
        finally:
            self.assertEqual(role('owner', 'admin', self.viewer), 200)
            self.assertEqual(role('bo', 'viewer', self.admin), 200)
        log = json.loads(self.get('/api/auth/users', self.admin)[1])['audit']
        self.assertEqual([(e['by'], e['target'], e['role']) for e in log[:4]],
                         [('owner', 'bo', 'viewer'), ('bo', 'owner', 'admin'), ('owner', 'owner', 'viewer'), ('owner', 'bo', 'admin')])
        self.assertEqual(os.stat(os.path.join(config.state_dir(self.cfg), auth.AUDIT_FILE)).st_mode & 0o777, 0o600)

    def test_my_schedule_is_my_own_for_a_viewer_and_anyones_for_an_admin(self):
        mine = {'days': ['mon', 'wed'], 'window': '09-17', 'hours': 6, 'mode': 'away'}
        r, data = self.post('/api/me/schedule', mine, self.viewer)
        self.assertEqual(r.status, 200, data)
        self.assertEqual(json.loads(self.get('/api/me/schedule', self.viewer)[1])['schedule'], mine)
        self.assertEqual(self.post('/api/me/schedule', {**mine, 'sub': 'owner'}, self.viewer)[0].status, 403)
        self.assertEqual(self.post('/api/me/schedule', {'mode': 'vacation-until-2026-13-40'}, self.viewer)[0].status, 400)
        self.assertEqual(self.post('/api/me/schedule', {'mode': 'auto'}, self.pending)[0].status, 403)
        r, data = self.post('/api/me/schedule', {'sub': 'bo', 'mode': 'vacation-until-2026-12-24'}, self.admin)
        self.assertEqual(r.status, 200, data)
        self.assertEqual(json.loads(self.get('/api/me/schedule', self.viewer)[1])['schedule']['mode'], 'vacation-until-2026-12-24')
        self.assertEqual(self.get('/api/me/schedule', self.admin)[0].status, 200)

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


QR_DATA, QR_BLOCKS = [0, 19, 34, 55, 80, 108, 136], [0, 1, 1, 1, 1, 1, 2]


def read_qr(m):
    """The text of a QR matrix from web/qr.js (byte mode, level L, mask 0, versions 1 to 6): an independent reader."""
    n = len(m)
    version = (n - 17) // 4

    def function(x, y):
        corner = (x < 9 and y < 9) or (x >= n - 8 and y < 9) or (x < 9 and y >= n - 8)
        align = version > 1 and abs(x - (n - 7)) <= 2 and abs(y - (n - 7)) <= 2
        return corner or align or x == 6 or y == 6
    bits = []
    for right in [r - 1 if r <= 6 else r for r in range(n - 1, 0, -2)]:
        for vert in range(n):
            y = n - 1 - vert if ((right + 1) & 2) == 0 else vert
            bits += [int(m[y][x]) ^ ((x + y) % 2 == 0) for x in (right, right - 1) if not function(x, y)]
    words = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits) - 7, 8)]
    blocks, size = QR_BLOCKS[version], QR_DATA[version] // QR_BLOCKS[version]
    data = [words[i * blocks + k] for k in range(blocks) for i in range(size)]
    stream = ''.join(f'{w:08b}' for w in data)
    assert stream[:4] == '0100', 'byte mode'
    count = int(stream[4:12], 2)
    return bytes(int(stream[12 + 8 * i:20 + 8 * i], 2) for i in range(count)).decode()


def qr_of(text):
    src = os.path.join(os.path.dirname(authweb.__file__), 'web', 'qr.js')
    script = f'const {{ qrMatrix }} = require({json.dumps(src)}); console.log(JSON.stringify(qrMatrix({json.dumps(text)})));'
    out = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


class FakeGoogle:
    """Google's device endpoints: a code, then pending until approve(), then an ID token for `sub`."""

    def __init__(self):
        self.calls, self.answer = [], {'error': 'authorization_pending'}

    def approve(self, sub, aud=TV):
        self.answer = {'id_token': token(KEY, claims(sub=sub, email=f'{sub}@example.com', aud=aud)), 'access_token': 'x'}

    def post(self, url, fields):
        self.calls.append(url)
        if url == authdevice.CODE_URL:
            return 200, {'device_code': 'dev-1', 'user_code': 'ABCD-EFGH', 'verification_url': 'https://www.google.com/device',
                         'verification_url_complete': 'https://www.google.com/device?user_code=ABCD-EFGH', 'expires_in': 1800,
                         'interval': 5}
        if 'error' in self.answer:
            return 428, self.answer
        return 200, self.answer


class DeviceLanTest(unittest.TestCase):
    """Sign-in from a plain home LAN address over http (192.168.x.x), reached through any VPN, with no Tailscale or WireGuard."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_of(self.tmp.name)
        self.cfg['auth']['client_id'] = ''   # no button client at all: the device flow alone
        authdevice.save(self.cfg, TV, 'GOCSPX-test-secret')
        collector = dashboard.Collector(self.cfg, probe_fn=lambda m: routing.Probe(True, 1, 0.5, 4, False),
                                        available_fn=lambda w: (True, 'ok'), stored_fn=lambda m: None)
        self.httpd = dashboard.server(self.cfg, 0, collector)
        self.port = self.httpd.server_address[1]
        self.cfg['dashboard']['bind'] = '192.168.1.5'   # served as if on the LAN address (the test socket is loopback)
        self.now = [time.time()]
        self.google = FakeGoogle()
        self.httpd.auth.jwks = auth.Jwks(fetch=FakeJwks(KEY).fetch)
        self.httpd.auth.device = authdevice.Flows(self.cfg, post=self.google.post, clock=lambda: self.now[0])
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.lan = f'192.168.1.5:{self.port}'

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, cookie=''):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Host': self.lan, 'Origin': f'http://{self.lan}', 'Content-Type': 'application/json', 'Cookie': cookie}
        c.request(method, path, body=None if body is None else json.dumps(body), headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def test_sign_in_completes_from_a_lan_address_with_no_vpn_software(self):
        lan_only = [('lo', '', ['127.0.0.1']), ('eth0', '', ['192.168.1.5'])]   # no wg, no tailscale
        self.assertEqual(auth.network_errors(self.cfg, ifaces=lan_only), [])
        r, page = self.call('GET', '/')
        self.assertEqual(r.status, 200)
        self.assertIn(b'login.js', page)
        me = json.loads(self.call('GET', '/api/auth/me')[1])
        self.assertEqual((me['client_id'], me['device']), ('', True))
        r, data = self.call('POST', '/api/auth/device/start', {})
        self.assertEqual((r.status, json.loads(data)['user_code']), (200, 'ABCD-EFGH'))
        flow = re.search(r'(tutti_flow_\d+=[^;]+)', r.getheader('Set-Cookie')).group(1)
        self.assertEqual(json.loads(self.call('POST', '/api/auth/device/poll', {}, flow)[1]), {'pending': True})
        asked = len(self.google.calls)
        self.now[0] += 5
        self.assertEqual(json.loads(self.call('POST', '/api/auth/device/poll', {}, flow)[1]), {'pending': True})
        self.assertEqual(len(self.google.calls), asked + 1)   # asked Google once per interval, not per poll
        self.google.approve('owner')
        self.assertEqual(self.call('POST', '/api/auth/device/poll', {}, flow)[1], b'{"pending": true}')   # interval not up
        self.now[0] += 5
        self.assertEqual(self.call('POST', '/api/auth/device/poll', {}, 'tutti_flow_1=other-browser')[0].status, 401)
        r, data = self.call('POST', '/api/auth/device/poll', {}, flow)
        self.assertEqual((r.status, json.loads(data)['role']), (200, 'admin'))
        session = re.search(r'(tutti_session_\d+=[^;]+)', r.getheader('Set-Cookie')).group(1)
        flags = r.getheader('Set-Cookie')
        self.assertNotIn('Secure', flags)   # plain http on the LAN: a Secure cookie would be dropped
        self.assertNotIn('__Host-', flags)
        self.assertIn('HttpOnly', flags)
        self.assertIn('SameSite=Strict', flags)
        self.assertEqual(self.call('GET', '/api/state', cookie=session)[0].status, 200)
        page = self.call('GET', '/', cookie=session)[1]
        csrf = re.search(rb'name="tutti-token" content="([^"]*)"', page).group(1).decode()
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        for tok, want in (('', 403), ('wrong', 403), (csrf, 200)):   # CSRF stays enforced over http on the LAN
            c.request('POST', '/api/owner', body='{"mode": "auto"}', headers={
                'Host': self.lan, 'Origin': f'http://{self.lan}', 'Content-Type': 'application/json', 'Cookie': session,
                'X-Tutti-Token': tok})
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, want, tok)
        c.close()

    def test_the_qr_code_carries_googles_link_with_the_code(self):
        r, data = self.call('POST', '/api/auth/device/start', {})
        shown = json.loads(data)
        self.assertEqual(shown['qr_url'], 'https://www.google.com/device?user_code=ABCD-EFGH')
        self.assertEqual(shown['verification_url'], 'https://www.google.com/device')
        self.assertEqual(authdevice.google_url('https://evil.example/device', 'https://www.google.com/device'),
                         'https://www.google.com/device')
        if shutil.which('node'):
            self.assertEqual(read_qr(qr_of(shown['qr_url'])), shown['qr_url'])
            long = 'https://tutti.example.ts.net/invite#' + 'A' * 43
            self.assertEqual(read_qr(qr_of(long)), long)   # version 5 and 6, two blocks: the invite links too
            self.assertEqual(read_qr(qr_of('y' * 130)), 'y' * 130)

    def test_cookies_over_https_get_the_host_prefix(self):
        self.assertTrue(authweb.set_cookie('s', 'v', 60, True).startswith('__Host-s=v; Path=/;'))
        self.assertIn('Secure', authweb.set_cookie('s', 'v', 60, True))
        self.assertEqual(authweb.cookie({'Cookie': '__Host-s=a; s=b'}, 's'), 'a')
        self.assertEqual(authweb.cookie({'Cookie': 's=b'}, 's'), 'b')

    def test_a_token_for_another_client_or_a_refusal_does_not_sign_in(self):
        r, _ = self.call('POST', '/api/auth/device/start', {})
        flow = re.search(r'(tutti_flow_\d+=[^;]+)', r.getheader('Set-Cookie')).group(1)
        self.google.approve('x', aud=CLIENT)
        self.now[0] += 5
        r, data = self.call('POST', '/api/auth/device/poll', {}, flow)
        self.assertEqual(r.status, 401)
        self.assertIn('wrong audience', json.loads(data)['error'])
        self.google.answer = {'error': 'access_denied'}
        r, _ = self.call('POST', '/api/auth/device/start', {})
        flow = re.search(r'(tutti_flow_\d+=[^;]+)', r.getheader('Set-Cookie')).group(1)
        self.now[0] += 5
        self.assertIn('refused', json.loads(self.call('POST', '/api/auth/device/poll', {}, flow)[1])['error'])

    def test_clients_from_any_private_subnet_and_never_the_internet(self):
        for addr in ('192.168.1.20', '10.8.0.6', '172.31.5.9', '100.70.1.1', 'fd00::5', '127.0.0.1'):
            self.assertTrue(self.httpd.verify_request(None, (addr, 50000)), addr)
        for addr in ('203.0.113.5', '8.8.8.8', '2001:db8::1'):
            self.assertFalse(self.httpd.verify_request(None, (addr, 50000)), addr)
        self.assertEqual(self.call('GET', '/api/state', cookie='')[0].status, 401)
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        c.request('GET', '/api/state', headers={'Host': f'evil.example:{self.port}'})
        self.assertEqual(c.getresponse().status, 403)   # a LAN bind does not open other host names
        c.close()


class SafetyTest(unittest.TestCase):
    def test_a_reachable_dashboard_needs_sign_in(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(auth, 'network_errors', return_value=[]):
            for dash in ({'bind': '192.168.1.5'}, {'origins': ['https://tutti.example']}):
                cfg = cfg_of(folder, **dash)
                cfg['auth']['client_id'] = ''
                with self.subTest(dash), self.assertRaisesRegex(dashboard.AuthRequired, 'sign-in is off'):
                    dashboard.server(cfg, 0, dashboard.Collector(cfg))

    def test_a_public_bind_or_origin_is_refused_even_with_sign_in(self):
        lan = [('lo', '', ['127.0.0.1']), ('eth0', '', ['192.168.1.5']), ('tailscale0', '', ['100.80.1.2'])]
        with tempfile.TemporaryDirectory() as folder, mock.patch('tutti.netpolicy.interfaces', return_value=lan):
            for bind in ('8.8.8.8', '0.0.0.0', '::', '2001:4860::8888'):
                cfg, collector = cfg_of(folder, bind=bind), dashboard.Collector(cfg_of(folder))
                with self.subTest(bind), self.assertRaisesRegex(dashboard.AuthRequired, 'never exposed to the internet|public'):
                    dashboard.server(cfg, 0, collector)
        for bind in ('192.168.1.5', '10.0.0.2', '172.20.0.1', '100.101.102.103', 'fd7a:115c:a1e0::1', '127.0.0.1'):
            with mock.patch.object(auth, 'saved_origins', return_value=[]):
                self.assertEqual(auth.network_errors({'dashboard': {'bind': bind}}, ifaces=lan), [], bind)

        def public(host):   # a fake resolver with a public answer
            return ['203.0.113.9']

        def vpn(host):
            return ['100.80.1.2']
        cfg = {'dashboard': {'bind': '127.0.0.1', 'origins': ['https://tutti.example']}, 'project': {'state': folder}}
        with mock.patch.object(auth, 'saved_origins', return_value=[]):
            self.assertIn('public address', auth.network_errors(cfg, resolve=public, ifaces=lan)[0])
            self.assertEqual(auth.network_errors(cfg, resolve=vpn, ifaces=lan), [])

    def test_without_a_client_id_the_dashboard_is_as_before(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = cfg_of(folder)
            cfg['auth']['client_id'] = ''
            httpd = dashboard.server(cfg, 0, dashboard.Collector(cfg))
            self.assertIsNone(httpd.auth)
            code, body, *_ = authweb.get(None, '/api/auth/me', {}, lambda t: None)
            self.assertEqual(json.loads(body), {'enabled': False, 'setup': 'tutti auth setup'})
            httpd.server_close()


class ViewerPageTest(unittest.TestCase):
    """A viewer's page shows no control that changes something: one CSS rule hides every control unless it is viewer-safe
    (checked in a browser for 0.1.47: a viewer saw 9 controls, all read-only, an admin 65 of 68)."""

    SAFE = {'.view-ok', '.view-ok *', '.more', '.seg', '.seg *', '#job-filter *', '#job-project', '#refresh', '#install', '#signout'}
    ACTIONS = {'button', 'input', 'select', 'textarea', 'form', '.btn', 'a.link', '[data-admin]', '#settings-view', '#users-view',
               ".tabs__tab[data-view='settings']"}

    def rule(self):
        with open(os.path.join(dashboard.WEB, 'app.css'), encoding='utf8') as f:
            css = f.read()
        m = re.search(r"body\[data-role='viewer'\] :is\(([^)]*)\):not\(([^{]*)\) \{ display: none; \}", css)
        self.assertIsNotNone(m, 'the one viewer rule is in app.css')
        def split(text):
            return {x.strip() for x in ' '.join(text.split()).split(',')}
        return split(m.group(1)), split(m.group(2))

    def test_every_control_is_hidden_for_a_viewer_unless_viewer_safe(self):
        actions, safe = self.rule()
        self.assertEqual(actions, self.ACTIONS)
        self.assertEqual(safe, self.SAFE)   # a new exception is a deliberate, reviewed change here

    def test_only_read_only_controls_are_marked_viewer_safe(self):
        marked = []
        for name in os.listdir(dashboard.WEB):
            if name.endswith(('.js', '.html')):
                with open(os.path.join(dashboard.WEB, name), encoding='utf8') as f:
                    marked += re.findall(r"view-ok'?[^\n]*?text: '([^']+)'", f.read())
        self.assertEqual(sorted(marked), ['Open its log', 'Show its jobs'])

    def test_the_pages_scripts_never_declare_the_same_global(self):
        # classic scripts share one global scope: a second `const DAY_NAMES` stopped users.js from running at all
        seen = {}
        with open(os.path.join(dashboard.WEB, 'index.html'), encoding='utf8') as f:
            scripts = re.findall(r'<script src="([a-z-]+\.js)', f.read())
        for name in scripts:
            with open(os.path.join(dashboard.WEB, name), encoding='utf8') as f:
                for g in re.findall(r'^(?:const|let|class|(?:async )?function) ([A-Za-z_$][\w$]*)', f.read(), re.M):
                    self.assertNotIn(g, seen, f'{g} is declared in {seen.get(g)} and {name}')
                    seen[g] = name

    def test_an_admin_sees_every_control(self):
        with open(os.path.join(dashboard.WEB, 'app.css'), encoding='utf8') as f:
            css = f.read()
        self.assertNotIn("body[data-role='admin']", css)   # nothing is hidden from an admin


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_of(self.tmp.name)
        self.cfg['auth']['client_id'] = ''

    def tearDown(self):
        self.tmp.cleanup()

    def run_auth(self, action, **kw):
        out, err = io.StringIO(), io.StringIO()
        a = SimpleNamespace(action=action, email=kw.get('email'), client_id=kw.get('client_id'), yes=kw.get('yes', False),
                            tailscale=kw.get('tailscale', False), device_client_id=kw.get('device_client_id'),
                            device_secret_stdin=kw.get('stdin') is not None)
        with redirect_stderr(err):
            rc = authcmd.cmd_auth(self.cfg, a, out, {'isatty': lambda: False, 'stdin': io.StringIO(kw.get('stdin') or ''),
                                                     'lan': lambda: ['192.168.1.5']})
        return rc, out.getvalue() + err.getvalue()

    def test_setup_prints_the_console_steps_and_saves_the_client_id(self):
        rc, text = self.run_auth('setup')
        self.assertEqual(rc, 2)
        self.assertIn('http://127.0.0.1:4777', text)
        self.assertIn('Web application', text)
        self.assertEqual(self.run_auth('setup', client_id='not-an-id')[0], 2)
        self.assertEqual(self.run_auth('setup', client_id=CLIENT)[0], 0)
        self.assertEqual(auth.client_id(self.cfg), CLIENT)

    def test_tailscale_name_and_the_origins_to_authorize(self):
        status = SimpleNamespace(returncode=0, stdout=json.dumps({'BackendState': 'Running', 'Self': {'DNSName': 'box.tail1.ts.net.'}}))
        self.assertEqual(auth.tailscale_name(run=lambda: status), 'box.tail1.ts.net')
        stopped = SimpleNamespace(returncode=0, stdout=json.dumps({'BackendState': 'Stopped', 'Self': {'DNSName': 'box.tail1.ts.net.'}}))
        self.assertEqual(auth.tailscale_name(run=lambda: stopped), '')
        self.assertEqual(auth.required_origins(self.cfg), ['http://127.0.0.1:4777', 'http://localhost:4777'])
        calls = []
        ok = authcmd.serve_tailscale(self.cfg, 'box.tail1.ts.net', io.StringIO(),
                                     run=lambda cmd: calls.append(cmd) or SimpleNamespace(returncode=0, stdout='', stderr=''))
        self.assertTrue(ok)
        self.assertNotIn('funnel', calls[0])
        self.assertEqual(auth.required_origins(self.cfg)[-1], 'https://box.tail1.ts.net')

    def test_tailscale_setup_needs_the_client_id_first(self):
        with mock.patch.object(auth, 'tailscale_name', return_value='box.tail1.ts.net'):
            rc, text = self.run_auth('setup', tailscale=True)
        self.assertEqual(rc, 2)
        self.assertIn('client ID first', text)
        self.assertEqual(auth.saved_origins(self.cfg), [])

    def test_a_server_without_sign_in_never_answers_a_saved_origin(self):
        auth.save_origin(self.cfg, 'https://box.tail1.ts.net')
        collector = SimpleNamespace(cfg=self.cfg, signed=False)
        self.assertEqual(dashboard.reach_origins(collector), ())
        self.assertFalse(dashboard.allowed_host('box.tail1.ts.net', 4777, dashboard.reach_origins(collector)))

    def test_setup_takes_both_clients_with_no_prompt(self):
        rc, text = self.run_auth('setup', client_id=CLIENT, device_client_id=TV, stdin='GOCSPX-test-secret\n')
        self.assertEqual(rc, 0, text)
        self.assertIn('TVs and Limited Input devices', text)
        self.assertIn('http://192.168.1.5:4777/', text)
        self.assertNotIn('GOCSPX-test-secret', text)
        self.assertEqual(authdevice.client(self.cfg), (TV, 'GOCSPX-test-secret'))
        self.assertEqual(os.stat(authdevice.path(self.cfg)).st_mode & 0o777, 0o600)
        self.assertEqual(self.run_auth('setup', device_client_id=TV, stdin='')[0], 2)   # a TV client needs its secret

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
        self.assertFalse(any(i['live'] for i in invites.Invites(config.state_dir(self.cfg)).listing()))
        rc, text = self.run_auth('users')
        self.assertIn('first sign-in becomes admin', text)


if __name__ == '__main__':
    unittest.main()
