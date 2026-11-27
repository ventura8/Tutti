"""The dashboard's sign-in gate (docs/AUTH.md): which request needs which role, the login and invite pages, the session
cookie, CSRF, and the Users card's API. tutti/auth.py holds the checks and the files; this is the HTTP edge.

Default-deny: every GET needs a signed-in, approved user except PUBLIC_GET; every POST needs an admin except VIEWER_POST and
the sign-in itself; ADMIN_GET (Settings, the reading helper, the Users list) needs an admin. A route another change adds is
admin-only for POST by itself."""
import hmac
import html
import json
import os
import secrets
import time
from http.cookies import CookieError, SimpleCookie

from . import __version__, auth, authdevice, config, invites, myschedule, orgs

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
JSON_TYPE = 'application/json'
HTML_TYPE = 'text/html; charset=utf-8'
PUBLIC_GET = ('/api/health', '/api/auth/me')
PUBLIC_STATIC = ('.js', '.css', '.png', '.svg', '.webmanifest')   # the app's code and icons: no data, no token
LOGIN_PAGES = ('/login', '/invite')
USERS, LOGOUT, LOGIN, INVITES, ORGS = '/api/auth/users', '/api/auth/logout', '/api/auth/login', '/api/auth/invites', '/api/orgs'
DEVICE_START, DEVICE_POLL = '/api/auth/device/start', '/api/auth/device/poll'
SCHEDULE = '/api/me/schedule'   # every user's own schedule (viewers included); anyone's for an admin
ADMIN_GET = ('/api/settings', '/settings', '/helper', USERS)
# a viewer may POST these; each handler checks its own target (auth.may_act_for: one's own, or an admin for anyone)
# /api/chat: every user's own conversations; each action has its own role checks (tutti/chat.py)
VIEWER_POST = (LOGOUT, '/api/telemetry', '/api/report', '/api/github', SCHEDULE, '/api/user/main-machine', '/api/users/manage',
               '/api/chat')
AUTH_POSTS = (LOGIN, LOGOUT, USERS, INVITES, ORGS, DEVICE_START, DEVICE_POLL, SCHEDULE)
LOCAL_SCHEME = 'http'   # the loopback dashboard's own scheme (other origins are https, dashboard.origins)
MAX_BODY = 8192
LOGIN_COOKIE_AGE = 900
GIS = 'https://accounts.google.com/gsi/'
# the login page only: Google's button injects inline styles (no data is ever rendered as markup on this page)
LOGIN_CSP = (f"default-src 'self'; script-src 'self' {GIS}client; style-src 'self' 'unsafe-inline' {GIS}style; frame-src {GIS}; "
             f"connect-src 'self' {GIS}; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
INSTALL_URL = 'https://github.com/ventura8/Tutti#install'


class Runtime:
    """Sign-in for one dashboard server: the client ID, the users, sessions and invites, Google's keys."""

    def __init__(self, cfg, port, jwks=None, clock=time.time, google_post=authdevice.google_post):
        folder = config.state_dir(cfg)
        self.cfg, self.port, self.clock = cfg, port, clock
        self.client = auth.client_id(cfg)
        hours = (cfg.get('auth') or {}).get('session_hours') or 72
        self.users = auth.Users(folder, clock)
        self.orgs = orgs.Orgs(folder, clock)
        self.sessions = auth.Sessions(folder, hours, clock)
        self.invites = invites.Invites(folder, clock)
        oidc = auth.oidc_config(cfg)
        self.oidc = oidc
        self.jwks = jwks or auth.Jwks(fetch=lambda: auth.fetch_jwks(oidc['jwks_url'] or auth.JWKS_URL), clock=clock)
        self.session_cookie, self.login_cookie = f'tutti_session_{port}', f'tutti_login_{port}'
        self.flow_cookie = authdevice.FLOW_COOKIE.format(port=port)
        self.device = authdevice.Flows(cfg, post=google_post, clock=clock)


def origins(cfg):
    """Extra full origins (https://name) the dashboard is reached at, besides the loopback ones (dashboard.origins and the
    Tailscale name `tutti auth setup --tailscale` saved)."""
    return auth.extra_origins(cfg)


def cookies(headers):
    jar = SimpleCookie()
    try:
        jar.load(headers.get('Cookie') or '')
    except CookieError:
        return {}
    return {k: m.value for k, m in jar.items()}


HOST_PREFIX = '__Host-'   # https only: the browser then keeps the cookie to this exact host, Secure, Path=/


def set_cookie(name, value, age, secure):
    """HttpOnly and SameSite=Strict always; over https also Secure and the __Host- prefix. Plain http on a LAN address
    (the device flow's main case) gets neither: a browser drops a Secure cookie that came over http."""
    if secure:
        return f'{HOST_PREFIX}{name}={value}; Path=/; Max-Age={int(age)}; HttpOnly; SameSite=Strict; Secure'
    return f'{name}={value}; Path=/; Max-Age={int(age)}; HttpOnly; SameSite=Strict'


def cookie(headers, name):
    """The cookie by its name, the __Host- one first (https)."""
    jar = cookies(headers)
    return jar.get(HOST_PREFIX + name) or jar.get(name)


def secure_request(headers):
    return (headers.get('Origin') or '').startswith('https://') or headers.get('X-Forwarded-Proto') == 'https'


def current(rt, headers):
    """(session, user) of the request's cookie, or (None, None). A removed user's session is ended."""
    sid = cookie(headers, rt.session_cookie)
    s = rt.sessions.get(sid)
    if not s:
        return None, None
    user = rt.users.get(s['sub'])
    if not user:
        rt.sessions.drop(sid)
        return None, None
    rt.users.seen(user['sub'])
    return s, user


def wants_page(path, headers):
    return headers.get('Sec-Fetch-Dest') == 'document' or path == '/' or path.endswith('.html') or \
        'text/html' in (headers.get('Accept') or '')


def jsonr(code, data, extra=None):
    return code, json.dumps(data).encode(), JSON_TYPE, 'no-store', extra


def login_page(rt, headers, csrf=''):
    """The sign-in page (also the invite and the waiting-for-approval page): a fresh login cookie whose hash is Google's nonce."""
    secret = secrets.token_urlsafe(24)
    with open(os.path.join(WEB, 'login.html'), 'rb') as f:
        body = f.read()
    for slot, value in ((b'__TUTTI_CLIENT__', rt.client), (b'__TUTTI_NONCE__', auth.nonce_of(secret)), (b'__TUTTI_TOKEN__', csrf),
                        (b'__TUTTI_VERSION__', __version__), (b'__TUTTI_INSTALL__', INSTALL_URL)):
        body = body.replace(slot, html.escape(value).encode())
    extra = {'Set-Cookie': set_cookie(rt.login_cookie, secret, LOGIN_COOKIE_AGE, secure_request(headers)),
             'Content-Security-Policy': LOGIN_CSP, 'Referrer-Policy': 'strict-origin-when-cross-origin'}
    return 200, body, HTML_TYPE, 'no-store', extra


def who(rt, headers):
    """{sub, email, role} of the signed-in user, or None: sign-in is off (the dashboard's owner on this computer, treated as
    admin by the caller) or nobody is signed in. `collector.who(headers)` for handlers outside this module."""
    if rt is None:
        return None
    _s, user = current(rt, headers)
    return {'sub': user['sub'], 'email': user.get('email'), 'role': user['role']} if user else None


def me(rt, headers):
    if rt is None:
        return jsonr(200, {'enabled': False, 'setup': 'tutti auth setup'})
    _s, user = current(rt, headers)
    out = {'enabled': True, 'client_id': rt.client, 'device': authdevice.enabled(rt.cfg), 'first': not rt.users.all(),
           'signed_in': bool(user), 'sso': {'provider': rt.oidc['provider'], 'issuer': rt.oidc['issuers'][0]}}
    if user:
        out.update(role=user['role'], email=user['email'], name=user['name'])
    return jsonr(200, out)


def tutti_joined(rt):
    """{sub: {joined, last_seen}} of household members tied to a Google user (tutti/household.py), else {}."""
    from . import household
    fn = getattr(household, 'members_by_sub', None)
    try:
        return fn(config.state_dir(rt.cfg)) if fn else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def users_view(rt):
    joined = tutti_joined(rt)
    rows = [{'sub': sub, 'email': u.get('email'), 'name': u.get('name'), 'role': u.get('role'), 'created': u.get('created'),
             'last_seen': u.get('last_seen'), 'tutti': joined.get(sub)} for sub, u in rt.users.all().items()]
    rows.sort(key=lambda r: (auth.ROLES.index(r['role']) if r['role'] in auth.ROLES else 0, -(r['last_seen'] or 0)))
    return {'users': rows, 'invites': rt.invites.listing(), 'audit': auth.audit_log(rt.users.folder)}


def get(rt, path, headers, serve, server_token=''):
    """(status, body, type, cache, extra) for a GET. `serve(token)` answers a page or API the gate let through."""
    if path == '/api/auth/me':
        return me(rt, headers)
    if rt is None:
        return serve(server_token)
    if path in LOGIN_PAGES:
        return login_page(rt, headers)
    if path in PUBLIC_GET or (path.endswith(PUBLIC_STATIC) and not path.startswith('/api/')):
        return serve('')
    return signed_in_get(rt, path, headers, serve)


def signed_in_get(rt, path, headers, serve):
    s, user = current(rt, headers)
    page = wants_page(path, headers)
    if not user:
        return login_page(rt, headers) if page else jsonr(401, {'error': 'sign in first: open the dashboard'})
    if user['role'] == 'pending':
        return login_page(rt, headers, s['csrf']) if page else \
            jsonr(403, {'error': 'an admin has not approved you yet: ask them to approve you in the Users card'})
    if path.startswith(ADMIN_GET) and not auth.is_admin(user['role']):
        return jsonr(403, {'error': 'only an admin may open this: ask an admin to make you admin in the Users card'})
    if path == USERS:
        return jsonr(200, users_view(rt))
    if path == ORGS:
        return jsonr(200, {'orgs': rt.orgs.visible_to(user)})
    if path == SCHEDULE:
        return jsonr(200, {'sub': user['sub'], 'schedule': myschedule.read(rt.users.folder, user['sub'], pacing_defaults(rt.cfg))})
    code, body, ctype, cache, extra = serve(s['csrf'])
    private = path.startswith('/api/') or ctype.startswith('text/html')
    return code, body, ctype, 'no-store' if private else cache, extra   # nothing signed-in is kept in a cache


def same_origin(headers, host, allowed):
    origin = (headers.get('Origin') or '').rstrip('/')
    return origin in allowed or origin == f'{LOCAL_SCHEME}://{host}'


def check(headers, host, cfg, csrf):
    """None, or (status, why) when a POST is not from the dashboard page itself with this session's CSRF token."""
    if not same_origin(headers, host, origins(cfg)) or headers.get('Sec-Fetch-Site', 'same-origin') != 'same-origin':
        return 403, 'only the dashboard page itself may change something (the request is cross-site)'
    if not (headers.get('Content-Type') or '').startswith(JSON_TYPE):
        return 415, 'send JSON (Content-Type: application/json)'
    if csrf is not None and not hmac.compare_digest(headers.get('X-Tutti-Token') or '', csrf):
        return 403, 'this page is from an older session: reload it, then try again'
    return None


def body_of(headers, read):
    try:
        size = int(headers.get('Content-Length') or 0)
    except ValueError:
        size = -1
    if not 0 < size <= MAX_BODY:
        return None
    try:
        data = json.loads(read(size))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def login(rt, headers, read):
    data = body_of(headers, read)
    if not data or not isinstance(data.get('credential'), str):
        return 400, {'error': 'send {"credential": <Google ID token>} as JSON'}, None
    secret = cookie(headers, rt.login_cookie) or ''
    if not secret:
        return 400, {'error': 'the sign-in page expired: reload it and sign in again'}, None
    try:
        if rt.oidc['provider'] != 'google':
            who = auth.verify_oidc(
                data['credential'], rt.oidc['client_id'], rt.jwks, rt.clock(),
                issuers=rt.oidc['issuers'], nonce=auth.nonce_of(secret),
                allowed_domains=rt.oidc['allowed_domains'], groups_claim=rt.oidc['groups_claim'],
            )
        else:
            who = auth.verify(data['credential'], rt.client, rt.jwks, rt.clock(), nonce=auth.nonce_of(secret))
    except auth.TokenError as e:
        return 401, {'error': f'Google sign-in was not accepted: {e}'}, None
    return signed_in(rt, headers, who, invite_of(data), rt.login_cookie)


def invite_of(data):
    return data.get('invite') if isinstance(data.get('invite'), str) and data.get('invite') else None


def signed_in(rt, headers, who, invite, done_cookie):
    """The user saved (with the invite redeemed under the same lock), a session made and its cookie set."""
    try:
        user = rt.users.sign_in(who, invite=invite, invites=rt.invites)
    except invites.InviteError as e:
        return 403, {'error': str(e)}, None
    sid, _csrf = rt.sessions.create(user['sub'])
    secure = secure_request(headers)
    extra = {'Set-Cookie': [set_cookie(rt.session_cookie, sid, rt.sessions.ttl, secure), set_cookie(done_cookie, '', 0, secure)]}
    return 200, {'ok': True, 'role': user['role']}, extra


def device_start(rt, headers, read):
    """POST /api/auth/device/start {"invite": optional}: Google's code to enter at google.com/device; the flow's cookie."""
    data = body_of(headers, read) or {}
    try:
        fid, shown = rt.device.start(invite_of(data))
    except authdevice.DeviceError as e:
        return 503, {'error': str(e)}, None
    extra = {'Set-Cookie': set_cookie(rt.flow_cookie, fid, shown['expires_in'], secure_request(headers))}
    return 200, {'ok': True, **shown}, extra


def device_poll(rt, headers):
    """POST /api/auth/device/poll {}: {"pending": true} until the person approved on Google's page, then the session."""
    try:
        done = rt.device.poll(cookie(headers, rt.flow_cookie), rt.jwks)
    except authdevice.DeviceError as e:
        return 401, {'error': str(e)}, {'Set-Cookie': set_cookie(rt.flow_cookie, '', 0, secure_request(headers))}
    if done is None:
        return 200, {'pending': True}, None
    who, invite = done
    return signed_in(rt, headers, who, invite, rt.flow_cookie)


def pacing_defaults(cfg):
    p = cfg.get('pacing') or {}
    return {**(p.get('schedule') or {}), 'mode': p.get('mode', 'auto')}


def change_schedule(rt, data, actor):
    """POST /api/me/schedule {"sub": optional, mode/days/window/hours}: one's own always; another user's only as an admin."""
    target = data.get('sub') or actor['sub']
    if not auth.may_act_for(actor, target):
        return 403, {'error': 'you can change only your own schedule: an admin changes other people\'s'}
    if not rt.users.get(target):
        return 404, {'error': 'no such user: reload the page'}
    code, answer = myschedule.change(rt.users.folder, target, data, pacing_defaults(rt.cfg))
    if code == 200:
        auth.act_for(rt.users.folder, actor, rt.users.get(target), 'schedule', clock=rt.clock)
    return code, answer


def change_user(rt, data, actor):
    """Approve, change the role (Make admin / Make viewer, also one's own while another admin exists) or remove; each change
    goes into the audit log. The last admin is never demoted or removed (409)."""
    action, sub = data.get('action'), data.get('sub')
    target = rt.users.get(sub) if isinstance(sub, str) else None
    try:
        if action == 'approve':
            rt.users.set_role(sub, 'viewer')
        elif action == 'role':
            rt.users.set_role(sub, data.get('role'))
        elif action == 'remove':
            if sub == actor['sub']:
                return 409, {'error': 'you cannot remove yourself: make yourself a viewer (another admin must exist), or ask '
                                      'another admin to remove you'}
            rt.users.remove(sub)
            rt.sessions.drop_user(sub)
        else:
            return 400, {'error': 'send {"action": "approve"|"role"|"remove", "sub": ..., "role": "viewer"|"admin"}'}
    except auth.AuthError as e:
        return 409 if 'last admin' in str(e) else 400, {'error': str(e)}
    role = {'approve': 'viewer', 'role': data.get('role')}.get(action)
    auth.audit(rt.users.folder, actor, action, target, role, clock=rt.clock)
    return 200, {'ok': True, **users_view(rt), 'self': sub == actor['sub']}


def invite_base(rt, headers):
    """Where an invite link points: the first dashboard.origins entry (reachable by others), else the page's own origin."""
    own = origins(rt.cfg)
    return own[0] if own else headers.get('Origin', '').rstrip('/')   # a POST that passed check() has the page's Origin


def change_invites(rt, data, actor, headers):
    try:
        if data.get('action') == 'create':
            token, rec = rt.invites.create(data.get('role') or 'viewer', by=actor['sub'])
            link = invites.url(invite_base(rt, headers), token)
            auth.audit(rt.users.folder, actor, 'invite', {'sub': rec['id']}, rec['role'], clock=rt.clock)
            return 200, {'ok': True, 'url': link, 'id': rec['id'], 'role': rec['role'], 'expires': rec['expires'],
                         'join': f'tutti household join {link}', **users_view(rt)}
        if data.get('action') == 'revoke':
            rt.invites.revoke(str(data.get('id') or ''))
            auth.audit(rt.users.folder, actor, 'revoke invite', {'sub': str(data.get('id'))}, clock=rt.clock)
            return 200, {'ok': True, **users_view(rt)}
    except invites.InviteError as e:
        return 400, {'error': str(e)}
    return 400, {'error': 'send {"action": "create", "role": "viewer"|"admin"} or {"action": "revoke", "id": ...}'}


def viewer_read(read, headers):
    """The body for a viewer's telemetry POST: errors and use counts only, never the consent (an admin setting)."""
    try:
        size = int(headers.get('Content-Length') or 0)
    except ValueError:
        size = 0
    raw = read(size) if 0 < size <= MAX_BODY else b''
    try:
        data = json.loads(raw or b'null')
    except ValueError:
        data = None
    if isinstance(data, dict) and 'consent' in data:
        return None
    return lambda _n: raw


def post(rt, headers, path, host, read, inner):
    """(status, answer, extra headers) for a POST when sign-in is on. `inner(csrf, read)` is the dashboard's own POST."""
    if path in (LOGIN, DEVICE_START, DEVICE_POLL):
        refused = check(headers, host, rt.cfg, None)
        if refused:
            return refused[0], {'error': refused[1]}, None
        return {LOGIN: login, DEVICE_START: device_start}[path](rt, headers, read) if path != DEVICE_POLL else device_poll(rt, headers)
    s, user = current(rt, headers)
    if not user:
        return 401, {'error': 'sign in first: reload the dashboard'}, None
    refused = check(headers, host, rt.cfg, s['csrf'])
    if refused:
        return refused[0], {'error': refused[1]}, None
    if path == LOGOUT:
        rt.sessions.drop(cookie(headers, rt.session_cookie))
        extra = {'Set-Cookie': set_cookie(rt.session_cookie, '', 0, secure_request(headers)), 'Clear-Site-Data': '"cache"'}
        return 200, {'ok': True}, extra
    return allowed_post(rt, headers, path, read, inner, s, user)


def _org_delete(rt, oid, _data, actor, org):
    if orgs.actor_org_role(actor, org) != 'owner':
        raise PermissionError('only an organization owner may delete an organization')
    rt.orgs.delete_org(oid)
    return None, f'delete org {oid}'


def _org_set_member(rt, oid, data, actor, org):
    role = data.get('role')
    if not orgs.may_assign_role(actor, org, role):
        raise PermissionError('only an organization owner may assign the owner role')
    return rt.orgs.set_member(oid, data.get('sub'), role, teams=data.get('teams')), f'{oid} member {data.get("sub")}={role}'


def _org_upsert_team(rt, oid, data, _actor, _org):
    res = rt.orgs.upsert_team(
        oid, data.get('team_id'), data.get('name'),
        priority=data.get('priority', orgs.DEFAULT_PRIORITY),
        budget=data.get('budget'), projects=data.get('projects'),
    )
    return res, f'{oid} team {data.get("team_id")}'


_ORG_ACTIONS = {
    'delete_org': _org_delete,
    'set_member': _org_set_member,
    'remove_member': lambda rt, oid, d, _a, _o: (rt.orgs.remove_member(oid, d.get('sub')), f'{oid} remove {d.get("sub")}'),
    'upsert_team': _org_upsert_team,
    'delete_team': lambda rt, oid, d, _a, _o: (rt.orgs.delete_team(oid, d.get('team_id')), f'{oid} delete team {d.get("team_id")}'),
}


def _org_mutate(rt, data, actor):
    action, oid = data.get('action'), str(data.get('org_id') or '').strip()
    if action == 'create_org':
        return rt.orgs.create_org(oid, data.get('name'), owner_sub=actor.get('sub')), f'org {oid}'
    org = rt.orgs.get(oid)
    if not org:
        raise orgs.OrgError(f'no such organization "{oid}"')
    if not orgs.may_manage_org(actor, org):
        raise PermissionError('only an organization admin or owner may change this')
    fn = _ORG_ACTIONS.get(action)
    if not fn:
        raise orgs.OrgError('action must be create_org, delete_org, set_member, remove_member, upsert_team or delete_team')
    return fn(rt, oid, data, actor, org)


def change_orgs(rt, data, actor):
    try:
        _res, detail = _org_mutate(rt, data, actor)
    except PermissionError as e:
        return 403, {'error': str(e)}
    except orgs.OrgError as e:
        return 409 if 'last owner' in str(e) else 400, {'error': str(e)}
    auth.audit(rt.users.folder, actor, f'org:{data.get("action")}', clock=rt.clock, detail=detail)
    return 200, {'ok': True, 'orgs': rt.orgs.visible_to(actor)}


def allowed_post(rt, headers, path, read, inner, s, user):
    """The role check of a signed-in POST, then the answer."""
    role = user['role']
    if role == 'pending' or (not auth.is_admin(role) and path not in VIEWER_POST):
        return 403, {'error': 'only an admin may change this: you can look, not change (ask an admin in the Users card)'}, None
    if not auth.is_admin(role) and path == '/api/telemetry':   # a viewer's telemetry: errors and use counts only
        read = viewer_read(read, headers)
        if read is None:
            return 403, {'error': 'only an admin may change whether reports are sent'}, None
    if path in (USERS, INVITES, ORGS, SCHEDULE):
        data = body_of(headers, read)
        if data is None:
            return 400, {'error': 'send a small JSON object'}, None
        if path == SCHEDULE:
            return (*change_schedule(rt, data, user), None)
        if path == USERS:
            return (*change_user(rt, data, user), None)
        if path == ORGS:
            return (*change_orgs(rt, data, user), None)
        return (*change_invites(rt, data, user, headers), None)
    return (*inner(s['csrf'], read), None)
