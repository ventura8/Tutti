"""Sign-in with Google and the dashboard's roles (docs/AUTH.md).

- Google Identity Services gives the page an ID token (a JWT signed RS256 by Google). The server checks its signature against
  Google's JWKS (cached as long as Google's Cache-Control says), then `iss`, `aud` (the OAuth client ID), `exp`, `iat`, the
  nonce bound to this browser, and that the e-mail is verified. No client secret: the ID-token flow has none.
- Users: <state>/users.json, mode 600, keyed by Google's `sub`. The first user to sign in becomes admin (under a file lock, so
  two first sign-ins cannot both win); every later one is pending until an admin approves them as viewer. The last admin can
  be neither removed nor demoted.
- Sessions: <state>/sessions.json, mode 600, keyed by the SHA-256 of the cookie's random id (the file holds no usable cookie),
  each with its own CSRF token and an absolute expiry.

Pure apart from the small JSON files, the lock and the JWKS fetch at the edge. Nothing here logs a token or an e-mail."""
import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.request

from . import config

JWKS_URL = 'https://www.googleapis.com/oauth2/v3/certs'
ISSUERS = ('accounts.google.com', 'https://accounts.google.com')
CLIENT_RX = re.compile(r'[0-9]{4,}-[a-z0-9]{8,}\.apps\.googleusercontent\.com')
SKEW = 60                 # seconds of clock difference allowed on exp and iat
MIN_RSA_BITS = 2048
JWKS_DEFAULT_AGE = 3600   # seconds, when Google's answer names no max-age
JWKS_RETRY = 60           # an unknown kid fetches the keys again at most this often
ROLES = ('pending', 'viewer', 'admin')
AUTH_FILE, USERS_FILE, SESSIONS_FILE, LOCK_FILE = 'auth.json', 'users.json', 'sessions.json', 'auth.lock'
SECRET_MODE = 0o600
SEEN_EVERY = 300          # seconds: last_seen is written at most this often per user
# DigestInfo of SHA-256 (RFC 8017, 9.2 note 1): the bytes before the hash in an RS256 signature's encoded message
SHA256_INFO = bytes.fromhex('3031300d060960864801650304020105000420')


class TokenError(Exception):
    """The ID token is not acceptable; the message says which check failed (never the token or its e-mail)."""


class AuthError(Exception):
    """A user or role change is refused; the message names the reason and the fix."""


# ---- files: mode 600, atomic replace


def _read(path):
    try:
        with open(path, encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SECRET_MODE)
    with os.fdopen(fd, 'w', encoding='utf8') as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, SECRET_MODE)
    os.replace(tmp, path)


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


_LOCAL = threading.local()
_THREAD_LOCK = threading.RLock()


@contextlib.contextmanager
def locked(folder):
    """One writer at a time across threads and processes (the dashboard and `tutti auth ...` in a shell). Re-entrant in a
    thread, so a sign-in can redeem an invite and save the user under one lock."""
    with _THREAD_LOCK:
        depth = getattr(_LOCAL, 'depth', 0)
        if depth:
            _LOCAL.depth = depth + 1
            try:
                yield
            finally:
                _LOCAL.depth = depth
            return
        os.makedirs(folder, exist_ok=True)
        fd = os.open(os.path.join(folder, LOCK_FILE), os.O_RDWR | os.O_CREAT, SECRET_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _LOCAL.depth = 1
            yield
        finally:
            _LOCAL.depth = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


# ---- the client ID (a setting: public, not a secret)


def valid_client_id(value):
    return isinstance(value, str) and bool(CLIENT_RX.fullmatch(value.strip()))


def client_id(cfg):
    """The OAuth client ID: `auth.client_id` in the config, else the one `tutti auth setup` saved; '' when sign-in is off."""
    own = ((cfg.get('auth') or {}).get('client_id') or '').strip()
    return own or (_read(os.path.join(config.state_dir(cfg), AUTH_FILE)).get('client_id') or '').strip()


def save_client_id(cfg, value):
    value = (value or '').strip()
    if not valid_client_id(value):
        raise AuthError('that is not a Google OAuth client ID: it looks like 1234567890-abc123def456.apps.googleusercontent.com '
                        '(Google Cloud console, APIs & Services, Credentials, the Web application client)')
    folder = config.state_dir(cfg)
    with locked(folder):
        data = _read(os.path.join(folder, AUTH_FILE))
        data['client_id'] = value
        _write(os.path.join(folder, AUTH_FILE), data)
    return value


def enabled(cfg):
    return bool(client_id(cfg))


# ---- the ID token


def b64url(text):
    if not isinstance(text, str) or not re.fullmatch(r'[A-Za-z0-9_-]*', text):
        raise TokenError('the token is not base64url')
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def rsa_verify(n, e, message, signature):
    """RSASSA-PKCS1-v1_5 with SHA-256: the whole encoded message is rebuilt and compared, never parsed."""
    k = (n.bit_length() + 7) // 8
    if n.bit_length() < MIN_RSA_BITS or len(signature) != k:
        return False
    s = int.from_bytes(signature, 'big')
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, 'big')
    t = SHA256_INFO + hashlib.sha256(message).digest()
    expected = b'\x00\x01' + b'\xff' * (k - len(t) - 3) + b'\x00' + t
    return hmac.compare_digest(em, expected)


def _fetch_google(url=JWKS_URL):
    """(keys document, max-age seconds) from Google."""
    with urllib.request.urlopen(url, timeout=5) as res:   # a fixed https URL of Google's
        cache = res.headers.get('Cache-Control') or ''
        doc = json.loads(res.read(256 * 1024))
    m = re.search(r'max-age=(\d+)', cache)
    return doc, int(m.group(1)) if m else JWKS_DEFAULT_AGE


class Jwks:
    """Google's signing keys, cached for their max-age; an unknown kid fetches again (at most every JWKS_RETRY seconds)."""

    def __init__(self, fetch=_fetch_google, clock=time.time):
        self.fetch, self.clock, self.lock = fetch, clock, threading.Lock()
        self.keys, self.until, self.fetched = {}, 0.0, -JWKS_RETRY

    def _refresh(self, now):
        doc, age = self.fetch()
        keys = {}
        for k in (doc or {}).get('keys') or []:
            if isinstance(k, dict) and k.get('kty') == 'RSA' and k.get('kid') and k.get('alg', 'RS256') == 'RS256':
                try:
                    keys[k['kid']] = (int.from_bytes(b64url(k['n']), 'big'), int.from_bytes(b64url(k['e']), 'big'))
                except (TokenError, KeyError, ValueError):
                    continue
        self.keys, self.until, self.fetched = keys, now + max(60, age), now

    def key(self, kid):
        with self.lock:
            now = self.clock()
            stale = now >= self.until
            if stale or (kid not in self.keys and now - self.fetched >= JWKS_RETRY):
                try:
                    self._refresh(now)
                except (OSError, ValueError) as e:
                    if stale and not self.keys:
                        raise TokenError(f'Google\'s signing keys could not be fetched ({type(e).__name__}): check the internet '
                                         'connection, then sign in again') from e
            return self.keys.get(kid)


def nonce_of(login_secret):
    """The nonce the login page gives Google: the hash of the HttpOnly login cookie, so a token only signs in this browser."""
    return hashlib.sha256(login_secret.encode()).hexdigest()


def verify(token, client, jwks, now, nonce=None):
    """The checked claims {sub, email, name} of a Google ID token, else TokenError."""
    parts = token.split('.') if isinstance(token, str) else []
    if len(parts) != 3 or not client:
        raise TokenError('not a JWT, or no client ID is set')
    try:
        header, claims = json.loads(b64url(parts[0])), json.loads(b64url(parts[1]))
    except ValueError as e:
        raise TokenError('the token\'s header or claims are not JSON') from e
    if not isinstance(header, dict) or not isinstance(claims, dict) or header.get('alg') != 'RS256':
        raise TokenError('the token is not signed with RS256')
    key = jwks.key(header.get('kid'))
    if not key or not rsa_verify(key[0], key[1], f'{parts[0]}.{parts[1]}'.encode(), b64url(parts[2])):
        raise TokenError('bad signature: the token is not signed by Google')
    return _claims(claims, client, now, nonce)


def _claims(claims, client, now, nonce):
    if claims.get('iss') not in ISSUERS:
        raise TokenError('wrong issuer: the token is not from accounts.google.com')
    if claims.get('aud') != client:
        raise TokenError('wrong audience: the token is for another OAuth client ID')
    exp, iat = claims.get('exp'), claims.get('iat')
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or now >= exp + SKEW:
        raise TokenError('the token has expired: sign in again')
    if isinstance(iat, (int, float)) and iat > now + SKEW:
        raise TokenError('the token is issued in the future: check this computer\'s clock')
    if nonce is not None and not hmac.compare_digest(str(claims.get('nonce') or ''), nonce):
        raise TokenError('the sign-in was started in another tab or page: reload the page and sign in again')
    if not isinstance(claims.get('sub'), str) or not claims['sub'] or claims.get('email_verified') is not True:
        raise TokenError('the Google account has no verified e-mail address')
    return {'sub': claims['sub'], 'email': str(claims.get('email') or ''), 'name': str(claims.get('name') or '')[:80]}


# ---- users and roles


class Users:
    """<state>/users.json: {"users": {sub: {email, name, role, created, last_seen}}}. Read again when the file changes."""

    def __init__(self, folder, clock=time.time):
        self.folder, self.clock = folder, clock
        self.path = os.path.join(folder, USERS_FILE)
        self._cache, self._mtime = {}, None

    def _load(self):
        m = _mtime(self.path)
        if m != self._mtime:
            data = _read(self.path).get('users')
            self._cache, self._mtime = (data if isinstance(data, dict) else {}), m
        return self._cache

    def _save(self, users):
        _write(self.path, {'users': users})
        self._cache, self._mtime = users, _mtime(self.path)

    def all(self):
        return {k: dict(v) for k, v in self._load().items()}

    def get(self, sub):
        u = self._load().get(sub)
        return dict(u, sub=sub) if u else None

    def sign_in(self, who, invite=None, invites=None):
        """The user after a verified sign-in. New ones are admin when there is no admin yet, else pending; with an invite
        (a token, redeemed for 'signin' under the same lock) they get its role at once, and a pending or viewer user is raised
        to it. Atomic: two first sign-ins cannot both become admin, and an invite is used once."""
        now = int(self.clock())
        with locked(self.folder):
            self._mtime = None
            users = dict(self._load())
            u = dict(users.get(who['sub']) or {})
            no_admin = not self._admins(users)
            if not u:
                u = {'role': 'admin' if no_admin else 'pending', 'created': now}
            if invite and no_admin:   # the first user is admin anyway; the invite is still used up and tied to them
                invites.redeem(invite, 'signin', sub=who['sub'])
            elif invite:
                granted = invites.redeem(invite, 'signin', sub=who['sub'])
                if ROLES.index(granted) > ROLES.index(u['role']):
                    u['role'] = granted
            u.update(email=who['email'], name=who['name'], last_seen=now)
            users[who['sub']] = u
            self._save(users)
        return dict(u, sub=who['sub'])

    def seen(self, sub):
        u = self._load().get(sub)
        if u and self.clock() - u.get('last_seen', 0) >= SEEN_EVERY:
            with locked(self.folder):
                self._mtime = None
                users = dict(self._load())
                if sub in users:
                    users[sub] = {**users[sub], 'last_seen': int(self.clock())}
                    self._save(users)

    def _admins(self, users):
        return [s for s, u in users.items() if u.get('role') == 'admin']

    def set_role(self, sub, role):
        if role not in ('viewer', 'admin'):
            raise AuthError('a role is viewer or admin (approving a pending user makes them a viewer)')
        with locked(self.folder):
            self._mtime = None
            users = dict(self._load())
            if sub not in users:
                raise AuthError('no such user: reload the Users list')
            if role != 'admin' and self._admins(users) == [sub]:
                raise AuthError('this is the last admin: make another user admin first')
            users[sub] = {**users[sub], 'role': role}
            self._save(users)
        return self.get(sub)

    def remove(self, sub):
        with locked(self.folder):
            self._mtime = None
            users = dict(self._load())
            if sub not in users:
                raise AuthError('no such user: reload the Users list')
            if self._admins(users) == [sub]:
                raise AuthError('this is the last admin: make another user admin first')
            del users[sub]
            self._save(users)

    def by_email(self, email):
        """The sub of the user with this e-mail (case-insensitive), else AuthError."""
        found = [s for s, u in self._load().items() if (u.get('email') or '').lower() == (email or '').strip().lower()]
        if not found:
            raise AuthError('nobody with that e-mail has signed in: they sign in once first (`tutti auth users` lists them)')
        return found[0]

    def reset(self):
        """Every user gone: the next sign-in becomes admin again."""
        with locked(self.folder):
            self._save({})


# ---- sessions


def _sid_key(sid):
    return hashlib.sha256(sid.encode()).hexdigest()


class Sessions:
    """<state>/sessions.json: {sha256(id): {sub, csrf, created, expires}}. Read again when the file changes (a CLI reset)."""

    def __init__(self, folder, hours, clock=time.time):
        self.folder, self.ttl, self.clock = folder, float(hours) * 3600, clock
        self.path = os.path.join(folder, SESSIONS_FILE)
        self._cache, self._mtime = {}, None

    def _load(self):
        m = _mtime(self.path)
        if m != self._mtime:
            data = _read(self.path)
            self._cache, self._mtime = data, m
        return self._cache

    def _change(self, fn):
        with locked(self.folder):
            self._mtime = None
            data = {k: v for k, v in self._load().items() if isinstance(v, dict) and v.get('expires', 0) > self.clock()}
            fn(data)
            _write(self.path, data)
            self._cache, self._mtime = data, _mtime(self.path)

    def create(self, sub):
        """(cookie id, csrf token) of a new session for `sub`."""
        sid, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(24), int(self.clock())
        self._change(lambda d: d.__setitem__(_sid_key(sid), {'sub': sub, 'csrf': csrf, 'created': now, 'expires': now + self.ttl}))
        return sid, csrf

    def get(self, sid):
        if not isinstance(sid, str) or not sid:
            return None
        s = self._load().get(_sid_key(sid))
        if not isinstance(s, dict) or s.get('expires', 0) <= self.clock():
            return None
        return s

    def drop(self, sid):
        self._change(lambda d: d.pop(_sid_key(sid or ''), None))

    def drop_user(self, sub):
        self._change(lambda d: [d.pop(k) for k in [k for k, v in d.items() if v.get('sub') == sub]])

    def clear(self):
        self._change(lambda d: d.clear())


def redact_email(email):
    """a…@example.com: enough to tell users apart in a terminal, never the whole address in a log."""
    name, _, domain = (email or '').partition('@')
    return f'{name[:1]}…@{domain}' if domain else '…'
