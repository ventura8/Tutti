"""Invites: one link that lets a person in, shared by the dashboard's sign-in and the household's join (docs/AUTH.md).

An admin creates an invite with a role (viewer by default, or admin). The link is `<dashboard origin>/invite#<token>`: the
token is in the fragment, so it never reaches a server log, a proxy or a Referer. Only its SHA-256 is stored, in
<state>/invites.json (mode 600). An invite expires after 7 days, can be revoked, and each purpose uses it once:

- `signin`: the person opens the link and signs in with Google; they are approved at once with the invite's role.
- `household`: their own Tutti runs `tutti household join <invite-url>`. The token never travels: both sides derive the
  same secret, `household_secret`, the joiner from the token, the coordinator from the stored hash, and prove it with the
  household's signed, nonced requests (tutti/household.py). The coordinator calls `redeem(id, 'household')` once proved.

Pure apart from the JSON file and the lock it shares with tutti/auth.py."""
import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import urlsplit

from . import auth

FILE = 'invites.json'
DAYS = 7
PURPOSES = ('signin', 'household')
ROLES = ('viewer', 'member', 'admin', 'owner')
TOKEN_BYTES = 32
ID_HEX = 16   # an invite's id: the first hex digits of its hash (shown, revoked, sent by the joiner)


class InviteError(Exception):
    """The invite cannot be used; the message says why and what to do (never the token)."""


def token_hash(token):
    return hashlib.sha256((token or '').encode()).hexdigest()


def invite_id(token):
    return token_hash(token)[:ID_HEX]


def household_secret_from_hash(hashed):
    return hmac.new(bytes.fromhex(hashed), b'tutti-household-join', hashlib.sha256).hexdigest()


def household_secret(token):
    """The joiner's side: the secret its signed join requests prove, derived from the invite token it holds."""
    return household_secret_from_hash(token_hash(token))


def url(origin, token):
    return f'{origin.rstrip("/")}/invite#{token}'


def parse_url(link):
    """(origin, token) of an invite link, else InviteError."""
    parts = urlsplit((link or '').strip())
    token = parts.fragment
    if parts.scheme not in ('http', 'https') or not parts.netloc or parts.path != '/invite' or not token:
        raise InviteError('that is not a Tutti invite link: it looks like https://<dashboard>/invite#<code>; '
                          'ask the admin for a new one from the dashboard\'s Users card')
    return f'{parts.scheme}://{parts.netloc}', token


class Invites:
    """<state>/invites.json: {"invites": {hash: {role, created, expires, by, used: {purpose: at}, revoked}}}."""

    def __init__(self, folder, clock=time.time):
        self.folder, self.clock = folder, clock
        self.path = os.path.join(folder, FILE)

    def _all(self):
        data = auth._read(self.path).get('invites')
        return data if isinstance(data, dict) else {}

    def _save(self, invites):
        auth._write(self.path, {'invites': invites})

    def create(self, role='viewer', by=''):
        """(token, record): the token is shown once, in the link; only its hash is kept."""
        if role not in ROLES:
            raise InviteError('an invite\'s role is viewer, member, admin or owner')
        token, now = secrets.token_urlsafe(TOKEN_BYTES), int(self.clock())
        rec = {'role': role, 'created': now, 'expires': now + DAYS * 86400, 'by': by, 'used': {}, 'revoked': False}
        with auth.locked(self.folder):
            invites = {h: v for h, v in self._all().items() if v.get('expires', 0) > now - 30 * 86400}   # old ones pruned
            invites[token_hash(token)] = rec
            self._save(invites)
        return token, {**rec, 'id': invite_id(token)}

    def listing(self):
        now = self.clock()
        return [{'id': h[:ID_HEX], 'role': v.get('role'), 'created': v.get('created'), 'expires': v.get('expires'),
                 'used': sorted(v.get('used') or {}), 'revoked': bool(v.get('revoked')),
                 'live': not v.get('revoked') and v.get('expires', 0) > now and len(v.get('used') or {}) < len(PURPOSES)}
                for h, v in sorted(self._all().items(), key=lambda kv: -kv[1].get('created', 0))]

    def _find(self, invites, key):
        """The hash for a token or an id."""
        h = token_hash(key)
        if h in invites:
            return h
        found = [x for x in invites if len(key or '') == ID_HEX and hmac.compare_digest(x[:ID_HEX], key)]
        return found[0] if found else None

    def revoke(self, ident):
        with auth.locked(self.folder):
            invites = self._all()
            h = next((x for x in invites if x[:ID_HEX] == ident), None)
            if not h:
                raise InviteError('no such invite: reload the Users card')
            invites[h] = {**invites[h], 'revoked': True}
            self._save(invites)

    def revoke_all(self):
        """Every invite revoked (`tutti auth reset-admin`): none raises the next sign-in after a recovery."""
        with auth.locked(self.folder):
            self._save({h: {**v, 'revoked': True} for h, v in self._all().items()})

    def check(self, key, purpose):
        """The invite's record when `key` (a token, or an id for the household side) may be used for `purpose` now."""
        if purpose not in PURPOSES:
            raise InviteError(f'an invite is used for {" or ".join(PURPOSES)}')
        invites = self._all()
        if purpose == 'household':
            h = self._find(invites, key)
        else:
            h = token_hash(key) if token_hash(key) in invites else None
        rec = invites.get(h) if h else None
        if not rec:
            raise InviteError('this invite link is not known here: ask the admin for a new one')
        if rec.get('revoked'):
            raise InviteError('this invite was revoked: ask the admin for a new one')
        if rec.get('expires', 0) <= self.clock():
            raise InviteError('this invite has expired (they last 7 days): ask the admin for a new one')
        if purpose in (rec.get('used') or {}):
            raise InviteError('this invite was already used: ask the admin for a new one')
        return h, rec

    def redeem(self, key, purpose, sub=None):
        """Marks the invite used for `purpose` and returns its role; InviteError when it may not be used. Call it inside
        auth.locked(folder) together with the change it grants, so the use and the grant are one step."""
        with auth.locked(self.folder):
            h, rec = self.check(key, purpose)
            invites = self._all()
            invites[h] = {**rec, 'used': {**(rec.get('used') or {}), purpose: int(self.clock())},
                          'used_by': {**(rec.get('used_by') or {}), purpose: sub}}
            self._save(invites)
        return rec['role']

    def signed_in_with(self, ident):
        """The Google sub of who signed in with this invite (by id or token), else None: the household ties a joining Tutti
        to that user."""
        invites = self._all()
        h = self._find(invites, ident)
        return ((invites.get(h) or {}).get('used_by') or {}).get('signin') if h else None

    def household_secret_for(self, ident):
        """The coordinator's side: the join secret of a live invite, by the id the joiner sends."""
        h, _rec = self.check(ident, 'household')
        return household_secret_from_hash(h)
