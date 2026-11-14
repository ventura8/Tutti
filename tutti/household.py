"""The household's shared state, pure (docs/HOUSEHOLD.md): the join code, the per-user token derived on both sides, signed
requests, members and their heartbeats, task leases, and the shared backlogs of shared projects with their dedupe.

Every function takes the coordinator's state (a plain dict) and returns plain data; tutti/coordinator.py keeps it in a file
and serves it over HTTP. Nothing here reads a clock, a file or the network."""
import base64
import hashlib
import hmac
import re
import secrets

CODE_PREFIX = 'tutti-hh'
CODE_DAYS = 7
SKEW = 120               # seconds a signed request's time may be off
NONCE_SECONDS = 300      # how long a seen nonce is remembered (more than twice SKEW)
RESERVE_MINUTES = 5      # a lease asked for a task that may start soon; renewed into lease_minutes once it runs
HELD = ('running', 'review', 'needs-person')   # local statuses whose lease is kept: nobody else may redo the task
CLOSED = ('done', 'dropped')
ACTIVE_MINUTES = 15      # a member whose last heartbeat said "prompting" within this is active
SEEN_MINUTES = 10        # a member heard from within this is online
TASK_KEYS = ('title', 'scope', 'check', 'size', 'source')
MAX_TASKS = 2000         # per shared project: the oldest closed ones leave first


class HouseholdError(Exception):
    """A household request is wrong or refused; the message says why and what to do."""


# ---- the join code and the token


def new_secret():
    """120 random bits as 24 base32 letters and digits."""
    return base64.b32encode(secrets.token_bytes(15)).decode()


def make_code(host, port, secret):
    """`tutti-hh:<host>:<port>:XXXX-XXXX-XXXX-XXXX-XXXX-XXXX`, what a member types or pastes."""
    groups = '-'.join(secret[i:i + 4] for i in range(0, len(secret), 4))
    return f'{CODE_PREFIX}:{host}:{port}:{groups}'


def parse_code(code):
    """(host, port, secret) of a join code; HouseholdError naming what is wrong."""
    parts = (code or '').strip().split(':')
    if len(parts) != 4 or parts[0] != CODE_PREFIX:
        raise HouseholdError('that is not a household join code: it looks like tutti-hh:<host>:<port>:XXXX-XXXX-...; '
                             'run `tutti household code` on the coordinator for a new one')
    _, host, port, groups = parts
    secret = groups.replace('-', '').replace(' ', '').upper()
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise HouseholdError(f'the join code\'s port "{port}" is not a port number: copy the whole code again')
    if not re.fullmatch(r'[A-Z2-7]{24}', secret) or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', host):
        raise HouseholdError('the join code is cut short or mistyped: copy the whole code again, or make a new one with '
                             '`tutti household code` on the coordinator')
    return host, int(port), secret


def derive_token(secret, user_id, client_nonce, server_nonce):
    """The user's token: both sides compute it from the join secret and two nonces; it never travels."""
    msg = f'token|{user_id}|{client_nonce}|{server_nonce}'.encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def body_hash(body):
    return hashlib.sha256(body or b'').hexdigest()


def signature(key, method, path, ts, nonce, body):
    msg = f'{method}|{path}|{ts}|{nonce}|{body_hash(body)}'.encode()
    return hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()


def check_signed(key, method, path, ts, nonce, body, sign, now, seen):
    """None when the request is signed by `key`, fresh and not replayed (the nonce is then remembered in `seen`), else why."""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return 'no request time: update Tutti on this computer'
    if abs(now - ts) > SKEW:
        return f'the request time is {abs(now - ts):.0f} s off: set both computers\' clocks (NTP)'
    if not nonce or len(nonce) > 64 or nonce in seen:
        return 'a repeated request was refused'
    if not hmac.compare_digest(signature(key, method, path, ts, nonce, body), sign or ''):
        return 'the request is not signed by this member: join again with a fresh code (`tutti household join`)'
    for n in [n for n, t in seen.items() if now - t > NONCE_SECONDS]:
        del seen[n]
    seen[nonce] = now
    return None


# ---- members


def new_state(name, now):
    return {'household': name, 'created': now, 'admin': None, 'code': None, 'members': {}, 'leases': {}, 'projects': {},
            'machines': {}}


def member_view(m, now):
    """What every member may see of a member: name, agent kind, online, active (prompting lately), the shared totals."""
    seen = now - (m.get('last_seen') or 0)
    return {'name': m['name'], 'hash': m['hash'], 'agent': m.get('agent'), 'online': seen <= SEEN_MINUTES * 60,
            'active': now - (m.get('prompting_at') or 0) <= ACTIVE_MINUTES * 60, 'last_seen': m.get('last_seen'),
            'runs_today': (m.get('totals') or {}).get('runs_today', 0), 'left': bool(m.get('left'))}


def join(state, user_id, uid_hash, name, agent, token, now):
    """Adds (or re-adds) a member with its derived token; a name another member uses gets a number."""
    others = {m['name'] for u, m in state['members'].items() if u != user_id and not m.get('left')}
    base, n, final = name, 2, name
    while final in others:
        final, n = f'{base} {n}', n + 1
    state['members'][user_id] = {'name': final, 'hash': uid_hash, 'agent': agent, 'token': token, 'joined': now,
                                 'last_seen': now, 'prompting_at': 0, 'totals': {}}
    if not state.get('admin'):
        state['admin'] = user_id
    return final


def leave(state, user_id):
    """The member leaves: its leases end now, its shared tasks stay for the others."""
    m = state['members'].get(user_id)
    if not m:
        return False
    m.update(left=True, token='')
    for key in [k for k, lease in state['leases'].items() if lease['user'] == user_id]:
        del state['leases'][key]
    if state.get('admin') == user_id:   # the longest-standing member who stays takes over making join codes
        stay = sorted((mm['joined'], u) for u, mm in state['members'].items() if not mm.get('left'))
        state['admin'] = stay[0][1] if stay else None
    return True


def heartbeat(state, user_id, beat, now):
    m = state['members'][user_id]
    m['last_seen'] = now
    if beat.get('prompting'):
        m['prompting_at'] = now
    totals = beat.get('totals')
    if isinstance(totals, dict):
        m['totals'] = {k: v for k, v in totals.items() if k in ('runs_today', 'minutes_shared') and isinstance(v, (int, float))}
    if isinstance(beat.get('agent'), str):
        m['agent'] = beat['agent'][:32]


# ---- leases


def expire(state, now):
    for key in [k for k, lease in state['leases'].items() if lease['until'] <= now]:
        del state['leases'][key]


def claim(state, key, user_id, now, minutes):
    """(True, None) when `user_id` holds the lease on `key` until now + minutes; (False, holder's id) when another does."""
    expire(state, now)
    lease = state['leases'].get(key)
    if lease and lease['user'] != user_id:
        return False, lease['user']
    state['leases'][key] = {'user': user_id, 'until': now + minutes * 60, 'at': (lease or {}).get('at', now)}
    return True, None


def release(state, key, user_id):
    lease = state['leases'].get(key)
    if lease and lease['user'] == user_id:
        del state['leases'][key]
        return True
    return False


def holder_name(state, user_id):
    return (state['members'].get(user_id) or {}).get('name', 'a member who left')


# ---- shared projects


def fold(title):
    """A task's dedupe key: its title without case, punctuation or extra spaces."""
    return ' '.join(re.sub(r'[^0-9a-z]+', ' ', (title or '').lower()).split())[:200]


def project_key(remote):
    """`github.com/owner/name` of a git remote (ssh, https, with or without .git), or '' when it is not one."""
    r = (remote or '').strip().rstrip('/')
    r = r[:-4] if r.endswith('.git') else r
    m = re.fullmatch(r'(?:[a-z+]+://)?(?:[^@/]+@)?([A-Za-z0-9.-]+)(?::\d+)?[:/]([^\s]+)', r)
    return f'{m.group(1).lower()}/{m.group(2).strip("/").lower()}' if m else ''


def lease_key(pkey, key):
    return f'task:{pkey}:{key}'


def clean_task(task):
    out = {k: task.get(k) for k in TASK_KEYS}
    out['title'] = str(out['title'] or '')[:300]
    out['scope'] = [str(s)[:200] for s in (out['scope'] or [])][:20]
    out['check'] = str(out['check'] or '')[:500]
    out['size'] = out['size'] if out['size'] in ('S', 'M', 'L') else 'S'
    out['source'] = str(out['source'] or 'household')[:32]
    return out


def publish(project, task, user_id, now):
    """Adds a task to a shared backlog unless its key is there in any status (dedupe); True when added."""
    key = fold(task.get('title'))
    if not key or key in project['tasks']:
        return False
    project['tasks'][key] = {**clean_task(task), 'status': 'queued', 'by': user_id, 'at': now}
    if len(project['tasks']) > MAX_TASKS:
        closed = sorted((t['at'], k) for k, t in project['tasks'].items() if t['status'] in CLOSED)
        for _at, k in closed[:len(project['tasks']) - MAX_TASKS]:
            del project['tasks'][k]
    return True


def close(project, key, status, user_id, now, holder=None):
    """Closes a shared task for everyone; only the member that published it or holds its lease may."""
    t = project['tasks'].get(key)
    if not t or status not in CLOSED or user_id not in (t.get('by'), holder):
        return False
    t.update(status=status, closed_by=user_id, closed_at=now)
    return True


def _ask_project(state, pkey, project, ask, user_id, now, minutes):
    for task in ask.get('publish') or []:
        publish(project, task, user_id, now)
    for c in ask.get('closed') or []:
        lease = state['leases'].get(lease_key(pkey, c.get('key'))) or {}
        close(project, c.get('key'), c.get('status'), user_id, now, lease.get('user'))
        release(state, lease_key(pkey, c.get('key')), user_id)
    for key in ask.get('held') or []:
        claim(state, lease_key(pkey, key), user_id, now, minutes)
    for key in ask.get('want') or []:
        t = project['tasks'].get(key)
        if t and t['status'] not in CLOSED:
            claim(state, lease_key(pkey, key), user_id, now, RESERVE_MINUTES)


def _answer_project(state, pkey, project, user_id):
    mine, held = [], {}
    for key in project['tasks']:
        lease = state['leases'].get(lease_key(pkey, key))
        if lease and lease['user'] == user_id:
            mine.append(key)
        elif lease:
            held[key] = holder_name(state, lease['user'])
    tasks = []
    for k, t in project['tasks'].items():
        by = t.get('closed_by')
        tasks.append({'key': k, **{x: t[x] for x in TASK_KEYS}, 'status': t['status'],
                      'closed_by': holder_name(state, by) if by and by != user_id else None})
    return {'tasks': tasks, 'mine': mine, 'held': held}


def sync_project(state, pkey, ask, user_id, now, minutes):
    """One member's round on one shared project: publish its new tasks, close its finished ones, renew the leases of the
    tasks it runs (or has in review), reserve the ones it may start next; answer the backlog and who holds what."""
    project = state['projects'].setdefault(pkey, {'tasks': {}})
    _ask_project(state, pkey, project, ask, user_id, now, minutes)
    expire(state, now)
    return _answer_project(state, pkey, project, user_id)


def status(state, user_id, now):
    """The household view a member gets: the members, the shared projects as counts, its own leases and how many others hold."""
    expire(state, now)
    projects = {}
    for pkey, p in state['projects'].items():
        counts = {}
        for t in p['tasks'].values():
            counts[t['status']] = counts.get(t['status'], 0) + 1
        projects[pkey] = counts
    own = sorted(k for k, lease in state['leases'].items() if lease['user'] == user_id)
    return {'household': state['household'], 'admin': state['members'].get(state.get('admin'), {}).get('name'),
            'members': [member_view(m, now) for m in state['members'].values() if not m.get('left')],
            'projects': projects, 'my_leases': own, 'other_leases': len(state['leases']) - len(own)}
