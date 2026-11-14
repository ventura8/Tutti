"""Sign-in with Google from a plain LAN address (http://192.168.x.x:4777): Google's device flow ("TVs and Limited Input
devices"), docs/AUTH.md.

Google Identity Services takes plain http only on localhost, so a page on a LAN address over http cannot show Google's
button. The device flow needs no origin and no redirect: the dashboard asks Google for a short code, the person opens
google.com/device on any device, enters it and approves; the server polls Google and gets an ID token, checked like the
button's (signature against Google's keys, iss, aud = the device client, exp, verified e-mail). The flow is bound to the
browser that started it by an HttpOnly, SameSite=Strict cookie holding a random id; only that browser gets the session.

The device client has a client secret (Google requires it for this flow). It is kept in <state>/auth-device.json, mode
600, never printed and never sent anywhere but Google's token endpoint."""
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import auth, config

DEVICE_FILE = 'auth-device.json'
CODE_URL = 'https://oauth2.googleapis.com/device/code'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
GRANT = 'urn:ietf:params:oauth:grant-type:device_code'
SCOPE = 'openid email profile'
MAX_FLOWS = 32            # flows waiting at once (a page that starts many cannot grow the server's memory)
FLOW_COOKIE = 'tutti_flow_{port}'


class DeviceError(Exception):
    """The device sign-in cannot go on; the message says why and what to do (never a code, token or secret)."""


def path(cfg):
    return os.path.join(config.state_dir(cfg), DEVICE_FILE)


def client(cfg):
    """(client_id, client_secret) of the device client, or ('', '') when it is not set up."""
    data = auth._read(auth.state_path(cfg, DEVICE_FILE))
    return (data.get('client_id') or '').strip(), (data.get('client_secret') or '').strip()


def save(cfg, client_id, secret):
    client_id, secret = (client_id or '').strip(), (secret or '').strip()
    if not auth.valid_client_id(client_id):
        raise auth.AuthError('that is not a Google OAuth client ID: copy the "TVs and Limited Input devices" client\'s ID '
                             '(it ends in .apps.googleusercontent.com)')
    if len(secret) < 8 or any(c.isspace() for c in secret):
        raise auth.AuthError('the client secret is missing or wrong: copy it from the same "TVs and Limited Input devices" client')
    folder = config.state_dir(cfg)
    with auth.locked(folder):
        auth._write(path(cfg), {'client_id': client_id, 'client_secret': secret})


def enabled(cfg):
    return all(client(cfg))


def google_post(url, fields):
    """(status, JSON answer) of a form POST to one of Google's fixed OAuth endpoints."""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method='POST',
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:   # a fixed https URL of Google's
            return res.status, json.loads(res.read(64 * 1024))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read(64 * 1024))
        except ValueError:
            return e.code, {}


DEVICE_PAGE = 'https://www.google.com/device'


def google_url(url, fallback):
    """`url` when it is an https page of Google's, else `fallback`: the page never links or encodes anywhere else."""
    parts = urllib.parse.urlsplit(url or '')
    ok = parts.scheme == 'https' and (parts.hostname or '') in ('google.com', 'www.google.com', 'accounts.google.com')
    return url if ok else fallback


class Flows:
    """The device sign-ins waiting for approval, by the random id in the browser's flow cookie."""

    def __init__(self, cfg, post=google_post, clock=time.time):
        self.cfg, self.post, self.clock = cfg, post, clock
        self.waiting, self.lock = {}, threading.Lock()

    def _prune(self, now):
        for k in [k for k, f in self.waiting.items() if f['expires'] <= now]:
            del self.waiting[k]

    def start(self, invite=None):
        """(flow id, what the page shows: user_code, verification_url, expires_in, interval)."""
        cid, _secret = client(self.cfg)
        if not cid:
            raise DeviceError('sign-in from a LAN address is not set up: the admin runs `tutti auth setup` on the dashboard\'s '
                              'computer and adds the "TVs and Limited Input devices" client')
        now = self.clock()
        with self.lock:
            self._prune(now)
            if len(self.waiting) >= MAX_FLOWS:
                raise DeviceError('too many sign-ins are waiting: try again in a few minutes')
        status, answer = self.post(CODE_URL, {'client_id': cid, 'scope': SCOPE})
        if status != 200 or not answer.get('device_code'):
            raise DeviceError(f'Google did not start the sign-in ({answer.get("error") or status}): check the device client in '
                              'the Google Cloud console, then try again')
        fid = secrets.token_urlsafe(24)
        interval = max(5, int(answer.get('interval') or 5))
        flow = {'device_code': answer['device_code'], 'interval': interval, 'next': now + interval,
                'expires': now + int(answer.get('expires_in') or 1800), 'invite': invite}
        with self.lock:
            self.waiting[fid] = flow
        url = google_url(answer.get('verification_url') or answer.get('verification_uri'), DEVICE_PAGE)
        # the QR code carries the link with the code already filled in, when Google gives one
        qr = google_url(answer.get('verification_url_complete') or answer.get('verification_uri_complete'), url)
        shown = {'user_code': answer.get('user_code'), 'verification_url': url, 'qr_url': qr,
                 'expires_in': int(flow['expires'] - now), 'interval': interval}
        return fid, shown

    def poll(self, fid, jwks):
        """None while the person has not approved yet, else ({sub, email, name}, invite). Google is asked at most once per
        its interval, whatever the page does."""
        now = self.clock()
        with self.lock:
            self._prune(now)
            flow = self.waiting.get(fid or '')
            if not flow:
                raise DeviceError('this sign-in expired or was never started here: start it again')
            if now < flow['next']:
                return None
            flow['next'] = now + flow['interval']
        cid, secret = client(self.cfg)
        status, answer = self.post(TOKEN_URL, {'client_id': cid, 'client_secret': secret, 'device_code': flow['device_code'],
                                               'grant_type': GRANT})
        error = answer.get('error')
        if error == 'authorization_pending':
            return None
        if error == 'slow_down':
            with self.lock:
                flow['interval'] += 5
            return None
        with self.lock:
            self.waiting.pop(fid, None)
        if status != 200 or not answer.get('id_token'):
            why = {'access_denied': 'the sign-in was refused on Google\'s page', 'expired_token': 'the code expired'}.get(error, error)
            raise DeviceError(f'Google did not sign you in ({why or status}): start again')
        try:
            who = auth.verify(answer['id_token'], cid, jwks, self.clock())
        except auth.TokenError as e:
            raise DeviceError(f'Google\'s answer was not accepted: {e}') from e
        return who, flow['invite']
