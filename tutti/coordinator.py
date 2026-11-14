"""The household coordinator and its client (docs/HOUSEHOLD.md): a small HTTP service on one always-on computer that keeps
the members, their leases and the shared backlogs, and the members' calls to it.

Standard library only. The state is one JSON file (mode 600: it holds the members' tokens) in
`<state>/household/coordinator.json`, changed under one lock of this process. Every request but a join is signed with the
member's token; a join is signed with the join code's secret, and its answer lets both sides derive the token without
sending it. Bodies carry no secret and nothing private (docs/HOUSEHOLD.md, *What crosses the network*)."""
import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, fairshare, household, netpolicy, telemetry, users

FOLDER = 'household'
FILE = 'coordinator.json'
MAX_BODY = 64 * 1024
JOIN_TRIES = 10            # wrong join codes from one address within JOIN_WINDOW close the join to it for JOIN_BLOCK
JOIN_WINDOW = 600
JOIN_BLOCK = 3600
JSON_TYPE = 'application/json'
JOIN = '/hh/join'
SCHEME = 'http'   # the coordinator is on the LAN: requests are signed, carry nothing secret (docs/HOUSEHOLD.md, threat model)


class Unreachable(Exception):
    """The coordinator did not answer in time (or at all): the member works alone (docs/HOUSEHOLD.md, *When the coordinator
    cannot be reached*)."""


# ---- the coordinator's state file


def folder_of(cfg):
    return os.path.join(config.state_dir(cfg), FOLDER)


def state_path(cfg):
    return os.path.join(folder_of(cfg), FILE)


def load(path):
    try:
        with open(path, encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save(path, data):
    users._write(path, data, users.SECRET_MODE)


def lan_address(ifaces=None):
    """This computer's address on the LAN or the VPN that members reach it at: the address of its default route when that
    is private, else its first LAN or VPN address; never a public one (tutti/netpolicy.py)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('192.0.2.1', 9))   # TEST-NET-1: never routed; connect() on UDP only picks the interface
        route = s.getsockname()[0]
    except OSError:
        route = ''
    finally:
        s.close()
    ifaces = netpolicy.interfaces() if ifaces is None else ifaces
    if route and netpolicy.private(route, netpolicy.vpn_addresses(ifaces)):
        return route
    found = netpolicy.private_addresses(ifaces)
    if not found:
        raise household.HouseholdError('this computer has no LAN or VPN address members could reach (only a public one, '
                                       f'or none): {netpolicy.WHY}. Connect it to the LAN or the household VPN first')
    return found[0]


def new_code(data, host, port, now):
    """A fresh join code (the old one stops working); kept with its expiry in the state."""
    secret = household.new_secret()
    data['code'] = {'secret': secret, 'until': now + household.CODE_DAYS * 86400, 'host': host, 'port': port}
    return household.make_code(host, port, secret)


def init(cfg, name, host=None, port=None, clock=time.time):
    """Makes this computer the household's coordinator: the state, a join code, and this user's own membership.
    Returns the code. Refuses when this login is already in a household."""
    if users.read_household():
        raise household.HouseholdError('this login is already in a household: `tutti household leave` first, or '
                                       '`tutti household code` for a new join code')
    port = port or cfg['household']['port']
    if host:
        netpolicy.check(host, 'the join code\'s address (--host)')
    host = host or lan_address()
    now = clock()
    data = household.new_state(name, now)   # a fresh household: an old coordinator file's members and tokens are gone
    code = new_code(data, host, port, now)
    me = users.identity(cfg)
    client_nonce, server_nonce = secrets.token_hex(16), secrets.token_hex(16)
    token = household.derive_token(data['code']['secret'], me['id'], client_nonce, server_nonce)
    final = household.join(data, me['id'], me['hash'], me['name'], telemetry.tool_of(me['main'] or ''), token, now)
    data['admin'] = me['id']
    save(state_path(cfg), data)
    users.write_household({'household': name, 'coordinator': f'127.0.0.1:{port}', 'user_id': me['id'], 'name': final,
                           'token': token, 'role': 'coordinator', 'joined': now})
    return code


def set_port(cfg, port):
    """`household serve --port`: new join codes name the new port."""
    data = load(state_path(cfg))
    if data and data.get('code'):
        data['code']['port'] = port
        save(state_path(cfg), data)


def invite_join(body):
    try:
        return bool(json.loads(body or b'{}').get('invite'))
    except (ValueError, AttributeError):
        return False


def auth_on(cfg):
    """Whether this computer's dashboard signs people in with Google (docs/AUTH.md): its admin's invites can then add
    household members, and the dashboard serves the household with no `household init`."""
    try:
        from . import auth
    except ImportError:
        return False
    return auth.enabled(cfg)


def founding(cfg, now):
    """The household the dashboard's admin starts by inviting someone: named after the admin, this login its coordinator
    and the admin (by their Google identity) its first member. Returns the state."""
    from . import auth
    folder = config.state_dir(cfg)
    admins = sorted((u.get('created') or 0, sub, u) for sub, u in auth.Users(folder).all().items() if u.get('role') == 'admin')
    sub, who = (admins[0][1], admins[0][2]) if admins else (users.identity(cfg)['id'], {})
    first = (who.get('name') or users.identity(cfg)['name']).split()[0][:24]
    data = household.new_state(f'{first}\'s household' if not users.name_error(first) else 'home', now)
    token = secrets.token_hex(32)
    final = household.join(data, sub, users.id_hash(sub), first if not users.name_error(first) else 'admin',
                           telemetry.tool_of((cfg.get('main') or {}).get('agent') or ''), token, now)
    data['members'][sub].update(email=who.get('email'), sub=sub)
    data['admin'] = sub
    save(state_path(cfg), data)
    users.write_household({'household': data['household'], 'coordinator': f'127.0.0.1:{cfg["household"]["port"]}',
                           'user_id': sub, 'sub': sub, 'email': who.get('email'), 'name': final, 'token': token,
                           'role': 'coordinator', 'joined': now})
    return data


# ---- the service


class Service:
    """The coordinator's logic over its state file: one lock, every change saved before the answer."""

    def __init__(self, cfg, clock=time.time):
        self.cfg, self.clock, self.path = cfg, clock, state_path(cfg)
        self.lock = threading.Lock()
        self.join_fails = {}

    def minutes(self):
        return float(self.cfg['household']['lease_minutes'])

    def handle(self, path, headers, body, addr):
        """(status, answer) of a POST."""
        with self.lock:
            data = load(self.path)
            if not data and path == JOIN and invite_join(body):
                data = founding(self.cfg, self.clock())   # the dashboard's admin invited someone: the household starts here
            if not data:
                return 503, {'error': 'this computer is not a household coordinator: run `tutti household init` here'}
            now = self.clock()
            if path == JOIN:
                code, answer = self.join(data, headers, body, addr, now)
            else:
                uid = headers.get('X-Tutti-User', '')
                why = self.verify(data, uid, path, headers, body, now)
                if why:
                    return 401, {'error': why}
                try:
                    code, answer = self.member_call(data, uid, path, json.loads(body or b'{}'), now)
                except household.HouseholdError as e:
                    code, answer = 409, {'error': str(e)}
            if code == 200:
                save(self.path, data)
            return code, answer

    def verify(self, data, uid, path, headers, body, now):
        m = data['members'].get(uid)
        if not m or m.get('left') or not m.get('token'):
            return 'not a member of this household: join with `tutti household join <code>`'
        return household.check_signed(m['token'], 'POST', path, headers.get('X-Tutti-Time'), headers.get('X-Tutti-Nonce'), body,
                                      headers.get('X-Tutti-Sign'), now, data.setdefault('nonces', {}))

    def join_blocked(self, addr, now):
        """JOIN_TRIES wrong codes from one address within JOIN_WINDOW close the join to it for JOIN_BLOCK."""
        tries = [t for t in self.join_fails.get(addr, []) if now - t < JOIN_BLOCK]
        self.join_fails[addr] = tries
        return any(sum(1 for u in tries if 0 <= u - t < JOIN_WINDOW) >= JOIN_TRIES for t in tries)

    def join(self, data, headers, body, addr, now):
        if self.join_blocked(addr, now):
            return 429, {'error': 'too many wrong join codes from this computer: wait an hour, or ask for a new code'}
        if invite_join(body):
            return self.join_invite(data, headers, body, addr, now)
        code = data.get('code') or {}
        if not code.get('secret') or code.get('until', 0) < now:
            return 403, {'error': 'the join code expired: run `tutti household code` on the coordinator for a new one'}
        why = household.check_signed(code['secret'], 'POST', JOIN, headers.get('X-Tutti-Time'), headers.get('X-Tutti-Nonce'),
                                     body, headers.get('X-Tutti-Sign'), now, data.setdefault('nonces', {}))
        if why:
            self.join_fails.setdefault(addr, []).append(now)
            return 403, {'error': 'the join code is wrong or old: copy it again from the coordinator (`tutti household code` '
                                  f'makes a new one) ({why})'}
        ask = json.loads(body or b'{}')
        uid, nonce = str(ask.get('user_id') or ''), str(ask.get('nonce') or '')
        why = users.name_error(ask.get('name'))
        if not (len(uid) == 16 and len(nonce) == 32) or why:
            return 400, {'error': why or 'the join request is incomplete: update Tutti on this computer'}
        server_nonce = secrets.token_hex(16)
        token = household.derive_token(code['secret'], uid, nonce, server_nonce)
        final = household.join(data, uid, users.id_hash(uid), ask['name'], str(ask.get('agent') or '')[:32], token, now)
        return 200, {'household': data['household'], 'nonce': server_nonce, 'name': final}

    def join_invite(self, data, headers, body, addr, now):
        """A join with the dashboard's invite (docs/AUTH.md): proved with the secret both sides derive from the invite; the
        member is the Google user who signed in with that invite, so it answers 428 until someone has."""
        from . import auth, invites
        ask = json.loads(body or b'{}')
        ident, nonce = str(ask.get('invite') or ''), str(ask.get('nonce') or '')
        folder = config.state_dir(self.cfg)
        inv = invites.Invites(folder, clock=self.clock)
        try:
            secret = inv.household_secret_for(ident)
        except invites.InviteError as e:
            self.join_fails.setdefault(addr, []).append(now)
            return 403, {'error': str(e)}
        why = household.check_signed(secret, 'POST', JOIN, headers.get('X-Tutti-Time'), headers.get('X-Tutti-Nonce'), body,
                                     headers.get('X-Tutti-Sign'), now, data.setdefault('nonces', {}))
        if why or len(nonce) != 32:
            self.join_fails.setdefault(addr, []).append(now)
            return 403, {'error': f'the invite link was not proved: copy the whole link again ({why or "incomplete request"})'}
        sub = inv.signed_in_with(ident)
        if not sub:
            return 428, {'error': 'sign in with Google on the invite page first (it opened in your browser)', 'waiting': True}
        user = auth.Users(folder, clock=self.clock).get(sub) or {}
        server_nonce = secrets.token_hex(16)
        token = household.derive_token(secret, sub, nonce, server_nonce)
        name = (user.get('name') or (user.get('email') or 'member').split('@')[0])[:32]
        name = name if not users.name_error(name) else 'member'
        final = household.join(data, sub, users.id_hash(sub), name, str(ask.get('agent') or '')[:32], token, now)
        data['members'][sub].update(email=user.get('email'), sub=sub, accounts=[str(x)[:32] for x in ask.get('accounts') or []][:20])
        inv.redeem(ident, 'household', sub=sub)
        return 200, {'household': data['household'], 'nonce': server_nonce, 'name': final, 'sub': sub, 'email': user.get('email')}

    def member_call(self, data, uid, path, ask, now):
        if path == '/hh/sync':
            return 200, self.sync(data, uid, ask, now)
        if path == '/hh/status':
            return 200, household.status(data, uid, now)
        if path == '/hh/leave':
            household.leave(data, uid)
            return 200, {'left': True}
        if path == '/hh/machine':
            return 200, self.machine(data, uid, ask, now)
        if path == '/hh/code':
            if data.get('admin') != uid:
                return 403, {'error': 'only the household\'s admin (who ran `tutti household init`) makes join codes'}
            c = data.get('code') or {}
            return 200, {'code': new_code(data, c.get('host') or lan_address(), c.get('port') or self.cfg['household']['port'], now)}
        return 404, {'error': 'not a household call: update Tutti on this computer'}

    def weight_at(self, now):
        from datetime import datetime, timedelta, timezone
        utc = datetime.fromtimestamp(now, timezone.utc)
        return lambda m: fairshare.hour_weight(m.get('weight', 1), m.get('profile'), utc + timedelta(hours=m.get('utc_offset') or 0))

    def sync(self, data, uid, ask, now):
        household.heartbeat(data, uid, ask.get('beat') or {}, now)
        projects = {}
        for pkey, p in (ask.get('projects') or {}).items():
            if isinstance(p, dict) and household.project_key(f'https://{pkey}') == pkey:
                projects[pkey] = household.sync_project(data, pkey, p, uid, now, self.minutes())
        household.machine_round(data, now, float(self.cfg['household'].get('starve_minutes', 20)), self.weight_at(now))
        answer = {'projects': projects, 'status': household.status(data, uid, now), 'machines': household.machines_view(data, uid)}
        if (ask.get('beat') or {}).get('spare'):
            answer['offers'] = household.offers(data, set(projects), now)
        return answer

    def machine(self, data, uid, ask, now):
        name = str(ask.get('name') or '')
        action = ask.get('action')
        if action == 'share':
            household.share_machine(data, uid, name, ask.get('info') or {}, now)
        elif action == 'unshare':
            household.unshare_machine(data, uid, name)
        elif action in ('pause', 'resume'):
            household.pause_machine(data, uid, name, action == 'pause', now)
        else:
            raise household.HouseholdError('say share, unshare, pause or resume')
        return {'machines': household.machines_view(data, uid)}


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'tutti-household'
        sys_version = ''

        def log_message(self, fmt, *args):
            return

        def answer(self, code, data):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header('Content-Type', JSON_TYPE)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/hh/health':
                return self.answer(200, {'ok': True})
            return self.answer(404, {'error': 'the household coordinator answers signed POSTs only'})

        def do_POST(self):
            size = int(self.headers.get('Content-Length') or 0)
            if size > MAX_BODY:
                return self.answer(413, {'error': f'the request is over {MAX_BODY // 1024} KB'})
            body = self.rfile.read(size)
            try:
                code, data = service.handle(self.path, self.headers, body, self.client_address[0])
            except ValueError:
                code, data = 400, {'error': 'the request is not JSON: update Tutti on this computer'}
            except Exception as e:
                telemetry.capture_exception(type(e), e, e.__traceback__, where='household-coordinator')
                code, data = 500, {'error': 'the coordinator failed on this request; it is reported and the next round tries again'}
            self.answer(code, data)

    return Handler


def server(cfg, port=None, bind=None, service=None, clock=time.time, ifaces=None):
    """One HTTP server of the coordinator on `bind` (checked: never a public address or a wildcard). Not started."""
    bind = bind or '127.0.0.1'
    if not bind.startswith('127.'):
        netpolicy.check(bind, 'household.bind', ifaces)
    service = service or Service(cfg, clock)
    httpd = ThreadingHTTPServer((bind, cfg['household']['port'] if port is None else port), make_handler(service))
    httpd.daemon_threads = True
    httpd.service = service
    return httpd


def servers(cfg, port=None, service=None, clock=time.time, ifaces=None):
    """The coordinator on every address `household.bind` allows (auto: localhost and each LAN and VPN address of this
    computer), sharing one service; a public bind raises netpolicy.PublicAddress before anything listens."""
    addrs = netpolicy.binds(cfg['household'].get('bind', 'auto'), 'household.bind', ifaces)
    service = service or Service(cfg, clock)
    out = []
    for a in addrs:
        try:
            out.append(server(cfg, port if not out else out[0].server_address[1], a, service, clock, ifaces))
        except OSError:
            if not out:
                raise
    return out


def start_in_background(cfg):
    """The dashboard of the coordinator's computer serves the household too; None when this login is not the coordinator
    or its port is taken (then `tutti household serve` is probably running)."""
    m = users.read_household()
    if m.get('role') != 'coordinator' and (m or not auth_on(cfg)):
        return None   # a member, or a household of one without sign-in: nothing to serve
    port = int(m['coordinator'].rsplit(':', 1)[1]) if m.get('coordinator') else cfg['household']['port']
    try:
        all_ = servers(cfg, port)
    except OSError:
        return None
    for httpd in all_:
        threading.Thread(target=httpd.serve_forever, name='tutti-household', daemon=True).start()
    return all_


# ---- the member's client


class Client:
    """Signed calls to the coordinator. No proxy (the coordinator is on the LAN), a short timeout, never a retry inside a
    round: the next round tries again."""

    def __init__(self, membership, timeout=3.0, clock=time.time, opener=None):
        self.m, self.timeout, self.clock = membership, timeout, clock
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def url(self, path):
        return urllib.parse.urlunsplit((SCHEME, self.m['coordinator'], path, '', ''))

    def post(self, path, body, key, user=''):
        raw = json.dumps(body).encode()
        ts, nonce = str(int(self.clock())), secrets.token_hex(12)
        headers = {'Content-Type': JSON_TYPE, 'X-Tutti-Time': ts, 'X-Tutti-Nonce': nonce,
                   'X-Tutti-Sign': household.signature(key, 'POST', path, ts, nonce, raw)}
        if user:
            headers['X-Tutti-User'] = user
        req = urllib.request.Request(self.url(path), data=raw, headers=headers, method='POST')
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            try:
                why = json.loads(e.read() or b'{}').get('error')
            except ValueError:
                why = None
            finally:
                e.close()
            raise household.HouseholdError(why or f'the coordinator answered {e.code}') from e
        except (OSError, ValueError) as e:
            raise Unreachable(f'the household coordinator at {self.m["coordinator"]} did not answer ({type(e).__name__})') from e

    def call(self, path, body=None):
        return self.post(path, body or {}, self.m['token'], self.m['user_id'])


def join(cfg, code, name, agent, client_fn=Client, clock=time.time):
    """`tutti household join`: the signed join, the token derived here, the membership saved (600)."""
    if users.read_household():
        raise household.HouseholdError('this login is already in a household: `tutti household leave` first')
    host, port, secret = household.parse_code(code)
    netpolicy.check(host, 'the join code\'s address')
    me = users.identity(cfg)
    nonce = secrets.token_hex(16)
    c = client_fn({'coordinator': f'{host}:{port}'}, timeout=max(5.0, float(cfg['household']['timeout_seconds'])), clock=clock)
    answer = c.post(JOIN, {'user_id': me['id'], 'nonce': nonce, 'name': name, 'agent': agent}, secret)
    token = household.derive_token(secret, me['id'], nonce, answer['nonce'])
    users.write_household({'household': answer['household'], 'coordinator': f'{host}:{port}', 'user_id': me['id'],
                           'name': answer['name'], 'token': token, 'role': 'member', 'joined': clock()})
    return answer


INVITE_WAIT = 120     # seconds `household join <invite-url>` waits for the Google sign-in in the browser
INVITE_POLL = 3


def coordinator_of(origin, cfg, override=None):
    """host:port of the household coordinator behind an invite's dashboard origin: the same host, household.port."""
    if override:
        return override if ':' in override else f'{override}:{cfg["household"]["port"]}'
    return f'{urllib.parse.urlsplit(origin).hostname}:{cfg["household"]["port"]}'


def join_invite(cfg, link, agent, accounts, override=None, client_fn=Client, clock=time.time, sleep=time.sleep, open_fn=None,
                say=None):
    """`tutti household join <invite-url>`: the join proved with the secret derived from the invite (the token never
    travels); while nobody has signed in with the invite, the link opens in the browser and the join is asked again every
    3 s for up to 2 minutes. The membership is saved (600) with the Google identity the coordinator names."""
    from . import invites
    if users.read_household():
        raise household.HouseholdError('this login is already in a household: `tutti household leave` first')
    try:
        origin, token = invites.parse_url(link)
    except invites.InviteError as e:
        raise household.HouseholdError(str(e)) from e
    where = coordinator_of(origin, cfg, override)
    netpolicy.check(where.rsplit(':', 1)[0].strip('[]'), 'the invite\'s household address')
    c = client_fn({'coordinator': where}, timeout=max(5.0, float(cfg['household']['timeout_seconds'])), clock=clock)
    opened, start = False, clock()
    while True:
        nonce = secrets.token_hex(16)
        try:
            answer = c.post(JOIN, {'invite': invites.invite_id(token), 'nonce': nonce, 'agent': agent, 'accounts': accounts},
                            invites.household_secret(token))
            break
        except household.HouseholdError as e:
            if 'sign in with Google' not in str(e) or clock() - start > INVITE_WAIT:
                raise
            if not opened:
                (say or (lambda m: None))('sign in with Google in the page that opened (the invite link); waiting...')
                (open_fn or _open_browser)(link)
                opened = True
            sleep(INVITE_POLL)
    tok = household.derive_token(invites.household_secret(token), answer['sub'], nonce, answer['nonce'])
    users.write_household({'household': answer['household'], 'coordinator': where, 'user_id': answer['sub'], 'sub': answer['sub'],
                           'email': answer.get('email'), 'name': answer['name'], 'token': tok, 'role': 'member', 'joined': clock()})
    return answer


def _open_browser(link):
    import webbrowser
    try:
        webbrowser.open(link)
    except webbrowser.Error:
        pass


def client_of(cfg, membership=None):
    m = membership if membership is not None else users.read_household()
    if not m.get('token'):
        return None
    return Client(m, timeout=float(cfg['household']['timeout_seconds']))
