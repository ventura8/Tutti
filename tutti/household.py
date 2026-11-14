"""The household's shared state, pure (docs/HOUSEHOLD.md): the join code, the per-user token derived on both sides, signed
requests, members and their heartbeats, task leases, and the shared backlogs of shared projects with their dedupe.

Every function takes the coordinator's state (a plain dict) and returns plain data; tutti/coordinator.py keeps it in a file
and serves it over HTTP. Nothing here reads a clock, a file or the network."""
import base64
import hashlib
import hmac
import os
import re
import secrets

from . import config

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
    if 'machines' in beat:   # a status look sends no demand: the last real one stays
        m['demand'] = clean_demand(beat.get('machines'), state.get('machines') or {})
    w = beat.get('weight')
    if isinstance(w, (int, float)) and not isinstance(w, bool) and 0 < w <= 10:
        m['weight'] = w
    if 'profile' in beat:
        from tutti import fairshare
        m['profile'] = fairshare.normalise_profile(beat.get('profile') or [])
    off = beat.get('utc_offset')
    if isinstance(off, (int, float)) and not isinstance(off, bool) and -14 <= off <= 14:
        m['utc_offset'] = off
    from tutti import tz
    zone = beat.get('zone')   # the member's own IANA zone (their Tutti host's time.zone): their hours keep daylight saving
    if isinstance(zone, str) and tz.valid(zone):
        m['zone'] = zone


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
    if user_id in ((state.get('moved') or {}).get(key) or {}).get('users', []):
        return False, None   # an admin moved it away from this member (reassign)
    state['leases'][key] = {'user': user_id, 'until': now + minutes * 60, 'at': (lease or {}).get('at', now)}
    return True, None


def release(state, key, user_id):
    lease = state['leases'].get(key)
    if lease and lease['user'] == user_id:
        del state['leases'][key]
        return True
    return False


def reassign(state, pkey, key, to_user, now, minutes=None):
    """An admin's move of a member's task (tutti/jobmove.py): the lease taken from whoever holds it and given to `to_user`
    (or left free for the first member with room). The old holder's next sync sees the task held by another, keeps its local
    work on a backup branch and never runs it again. Returns what was done."""
    lk = lease_key(pkey, key)
    lease = state['leases'].pop(lk, None)
    if not lease and not to_user:
        return f'{key}: nobody held it; it is free for the first member with room'
    was = holder_name(state, lease['user']) if lease else 'nobody'
    if lease:   # the old holders never get it back: their claims are refused, their syncs are told it moved
        m = state.setdefault('moved', {}).setdefault(lk, {'users': [], 'at': now})
        m['users'] = sorted({*m['users'], lease['user']})
    if to_user:
        if to_user not in state['members']:
            raise HouseholdError(f'no member "{to_user}" in this household: `tutti household status` lists them')
        state['leases'][lk] = {'user': to_user, 'until': now + (minutes or RESERVE_MINUTES) * 60, 'at': now, 'moved_from': was}
        return f'{key}: moved from {was} to {holder_name(state, to_user)}'
    return f'{key}: taken from {was}; the first member with room takes it'


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


def offers(state, asked, now):
    """Shared projects with queued tasks nobody holds, other than the ones this member asked about: what its spare room
    may take ({key, check, queued})."""
    expire(state, now)
    out = []
    for pkey, p in state['projects'].items():
        if pkey in asked or not (p.get('meta') or {}).get('check'):
            continue
        free = [k for k, t in p['tasks'].items() if t['status'] == 'queued' and lease_key(pkey, k) not in state['leases']]
        if free:
            out.append({'key': pkey, 'check': p['meta']['check'], 'queued': len(free)})
    return out


def reserve(state, pkey, project, ask, user_id, now):
    """Reserves, in the member's order of `want`, up to `room` tasks nobody else holds (all of `want` without a room)."""
    room = ask.get('room')
    room = len(ask.get('want') or []) if not isinstance(room, int) else max(0, room)
    for key in ask.get('want') or []:
        if room <= 0:
            break
        t = project['tasks'].get(key)
        if t and t['status'] not in CLOSED and claim(state, lease_key(pkey, key), user_id, now, RESERVE_MINUTES)[0]:
            room -= 1


def _ask_project(state, pkey, project, ask, user_id, now, minutes):
    meta = ask.get('meta') or {}
    if isinstance(meta.get('check'), str) and meta['check'].strip():
        project['meta'] = {'check': meta['check'][:500]}
    for task in ask.get('publish') or []:
        publish(project, task, user_id, now)
    for c in ask.get('closed') or []:
        lease = state['leases'].get(lease_key(pkey, c.get('key'))) or {}
        close(project, c.get('key'), c.get('status'), user_id, now, lease.get('user'))
        release(state, lease_key(pkey, c.get('key')), user_id)
    for key in ask.get('held') or []:
        claim(state, lease_key(pkey, key), user_id, now, minutes)
    reserve(state, pkey, project, ask, user_id, now)


def _answer_project(state, pkey, project, user_id):
    mine, held, gone = [], {}, []
    for key in project['tasks']:
        lease = state['leases'].get(lease_key(pkey, key))
        moved = user_id in ((state.get('moved') or {}).get(lease_key(pkey, key)) or {}).get('users', [])
        if lease and lease['user'] == user_id:
            mine.append(key)
        elif lease:
            held[key] = holder_name(state, lease['user'])
        elif moved:
            held[key] = 'another member (an admin moved it)'
        if moved:
            gone.append(key)
    tasks = []
    for k, t in project['tasks'].items():
        by = t.get('closed_by')
        tasks.append({'key': k, **{x: t[x] for x in TASK_KEYS}, 'status': t['status'],
                      'closed_by': holder_name(state, by) if by and by != user_id else None})
    return {'tasks': tasks, 'mine': mine, 'held': held, 'moved': gone}


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


# ---- shared machines (increment 3)

# on each shared machine: the slot locks every member's login takes, next to the default lock files (mode 1777 like /tmp:
# nobody can remove or replace another login's file; the locks are opened read-only, docs/HOUSEHOLD.md)
SHARED_DIR = os.path.join(os.path.dirname(config.MACHINE_DEFAULTS['lock_prefix']), 'tutti-household')
MACHINE_KEYS = ('host', 'slots', 'kind', 'gpu', 'ollama_port', 'lock_prefix')


def share_machine(state, user_id, name, info, now):
    """A member shares one of its machines with the household. Another member's machine of that name is refused."""
    m = state.setdefault('machines', {}).get(name)
    if m and m['owner'] != user_id:
        raise HouseholdError(f'the household already has a machine "{name}" ({holder_name(state, m["owner"])}\'s): rename yours '
                             'under machines: in your config, then share it under the new name')
    clean = {k: info.get(k) for k in MACHINE_KEYS}
    clean['slots'] = max(1, min(64, int(clean.get('slots') or 1)))
    state['machines'][name] = {**(m or {}), **clean, 'owner': user_id, 'shared_at': (m or {}).get('shared_at', now)}
    return state['machines'][name]


def may_manage(state, user_id, name):
    m = state.get('machines', {}).get(name)
    if not m:
        raise HouseholdError(f'the household has no machine "{name}": `tutti household status` lists them')
    if user_id not in (m['owner'], state.get('admin')):
        raise HouseholdError(f'only {holder_name(state, m["owner"])} (its owner) or the household\'s admin may change {name}; '
                             'you can stop your own runs on it with `tutti pause machine ' + name + '` (only for you)')
    return m


def unshare_machine(state, user_id, name):
    may_manage(state, user_id, name)
    del state['machines'][name]
    state.get('grants', {}).pop(name, None)


def pause_machine(state, user_id, name, paused, now):
    m = may_manage(state, user_id, name)
    if paused:
        m['paused'] = {'by': user_id, 'at': now}
    else:
        m.pop('paused', None)


def capacity(m, demands, now, stale):
    """The slots the household may share on a machine now: none while paused; the owner's last fresh report of its fit
    slots (yield, heat, offline), else all of them."""
    if m.get('paused'):
        return 0
    own = demands.get(m['owner']) or {}
    if own.get('capacity') is not None and now - own.get('at', 0) <= stale:
        return max(0, min(int(m['slots']), int(own['capacity'])))
    return int(m['slots'])


def machine_round(state, now, starve_minutes, weights_at):
    """Grants for every shared machine from every member's latest demand. `weights_at(member) -> weight now`."""
    from tutti import fairshare
    state.setdefault('starved', {})
    prev, state['grants'] = state.get('grants') or {}, {}
    live = {u: m for u, m in state['members'].items() if not m.get('left') and now - (m.get('last_seen') or 0) <= fairshare.STALE_SECONDS}
    for name, m in state.get('machines', {}).items():
        demands = {u: dict((mm.get('demand') or {}).get(name) or {}, at=mm.get('last_seen', 0)) for u, mm in live.items()}
        demands = {u: d for u, d in demands.items() if d.get('interactive') or d.get('background') or u == m['owner']}
        cap = capacity(m, demands, now, fairshare.STALE_SECONDS)
        weights = {u: weights_at(live[u]) for u in demands}
        last = (prev.get(name) or {}).get('grants') or {}
        starved = fairshare.starving(state['starved'], name, demands, last, now, starve_minutes)
        g = fairshare.grants(cap, demands, weights, starved)
        fairshare.starving(state['starved'], name, demands, g, now, starve_minutes)
        state['grants'][name] = {'capacity': cap, 'grants': g, 'demand': {u: int(d.get('interactive', 0)) + int(d.get('background', 0))
                                                                            for u, d in demands.items()}}


def machines_view(state, user_id):
    """What a member sees of the shared machines: its grant, each member's running count by name, the owner, paused."""
    out = {}
    for name, m in state.get('machines', {}).items():
        g = (state.get('grants') or {}).get(name) or {}
        use = {}
        for mm in state['members'].values():
            running = int(((mm.get('demand') or {}).get(name) or {}).get('running', 0))
            if running and not mm.get('left'):
                use[mm['name']] = running
        out[name] = {**{k: m.get(k) for k in MACHINE_KEYS}, 'owner': holder_name(state, m['owner']), 'mine': m['owner'] == user_id,
                     'grant': (g.get('grants') or {}).get(user_id, 0), 'capacity': g.get('capacity', m['slots']), 'use': use,
                     'fairness': _fairness(g),
                     'paused': bool(m.get('paused')),
                     'paused_by': holder_name(state, m['paused']['by']) if m.get('paused') else None}
    return out


def _fairness(g):
    from tutti import fairshare
    return fairshare.jain(g.get('grants') or {}, g.get('demand') or {})


def clean_demand(raw, shared):
    """A member's demand per shared machine from its heartbeat: counts only."""
    out = {}
    for name, d in (raw or {}).items():
        if name not in shared or not isinstance(d, dict):
            continue
        out[name] = {k: max(0, min(256, int(d.get(k) or 0))) for k in ('interactive', 'background', 'running')}
        if d.get('capacity') is not None:
            out[name]['capacity'] = max(0, min(256, int(d['capacity'])))
    return out


# ---- the dashboard's Users card (docs/AUTH.md, *The interface for the household*)


def members_by_sub(state_dir):
    """{Google sub: {joined, last_seen, agent, online}} of the members who joined with an invite, read from this login's
    coordinator state (empty when this computer is not the coordinator)."""
    import json
    import time
    try:
        with open(os.path.join(state_dir, 'household', 'coordinator.json'), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {m['sub']: {'joined': m.get('joined'), 'last_seen': m.get('last_seen'), 'agent': m.get('agent'),
                       'online': now - (m.get('last_seen') or 0) <= SEEN_MINUTES * 60}
            for m in (data.get('members') or {}).values() if m.get('sub') and not m.get('left')}


# ---- compute jobs on shared machines (a member's tests run there under the owner's Tutti; docs/HOUSEHOLD.md, *Compute*)

JOB_KINDS = ('test',)
JOB_STATES = ('queued', 'running', 'done')
JOB_TAIL = 8000          # characters of a job's output the coordinator keeps for the member
JOB_KEEP = 200           # finished jobs kept (the oldest leave first)
CLAIM_GRACE = 15 * 60    # a claimed job whose owner went quiet past its timeout and this grace goes back to the queue


def submit_job(state, user_id, job, now):
    """A member asks for a run on a shared machine: a repo URL, a WIP ref, a command; never a credential. Returns its id."""
    m = (state.get('machines') or {}).get(job.get('machine'))
    if not m:
        raise HouseholdError(f'the household has no machine "{job.get("machine")}": `tutti household status` lists them')
    if m.get('paused'):
        raise HouseholdError(f'{job["machine"]} is paused for the household by {holder_name(state, m["paused"]["by"])}')
    for key, limit in (('repo', 300), ('ref', 200)):
        if not isinstance(job.get(key), str) or not job[key] or len(job[key]) > limit:
            raise HouseholdError(f'a job needs its {key} (at most {limit} characters): update Tutti on this computer')
    if not job['ref'].startswith('refs/tutti-wip/'):
        raise HouseholdError('a job\'s ref must be a refs/tutti-wip/ ref: update Tutti on this computer')
    if not job['repo'].startswith(('https://', 'ssh://', 'git@')) or project_key(job['repo']) not in (state.get('projects') or {}):
        raise HouseholdError('a shared machine runs only the household\'s shared projects (the owner\'s git fetches them): '
                             'share the project with `tutti household project share <repo>`, or run it on your own machines')
    cmd = job.get('command')
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd) or sum(len(x) for x in cmd) > 4000:
        raise HouseholdError('a job needs its command as a list of words (at most 4000 characters)')
    jobs = state.setdefault('jobs', {})
    jid = f'j{state.get("next_job", 1)}'
    state['next_job'] = state.get('next_job', 1) + 1
    jobs[jid] = {'by': user_id, 'machine': job['machine'], 'repo': job['repo'], 'ref': job['ref'], 'command': cmd,
                 'device': str(job.get('device') or '')[:64] or None, 'timeout': min(int(job.get('timeout') or 3600), 6 * 3600),
                 'kind': 'test', 'state': 'queued', 'at': now}
    done = sorted((j['at'], k) for k, j in jobs.items() if j['state'] == 'done')
    for _at, k in done[:max(0, len(jobs) - JOB_KEEP)]:
        del jobs[k]
    return jid


def claim_jobs(state, user_id, now, room=4):
    """The queued jobs on the machines `user_id` owns (and stale claims of theirs), up to `room`: the owner's Tutti runs
    them. Another member can never claim them."""
    out = []
    for jid, j in sorted((state.get('jobs') or {}).items(), key=lambda kv: kv[1]['at']):
        m = (state.get('machines') or {}).get(j['machine'])
        if not m or m['owner'] != user_id or m.get('paused') or len(out) >= room:
            continue
        stale = j['state'] == 'running' and now - j.get('claimed', 0) > j['timeout'] + CLAIM_GRACE
        if j['state'] == 'queued' or stale:
            j.update(state='running', claimed=now)
            out.append({'id': jid, **{k: j[k] for k in ('machine', 'repo', 'ref', 'command', 'device', 'timeout')},
                        'member': holder_name(state, j['by'])})
    return out


def finish_job(state, user_id, jid, rc, tail, now):
    j = (state.get('jobs') or {}).get(jid)
    if not j or (state.get('machines') or {}).get(j['machine'], {}).get('owner') != user_id:
        raise HouseholdError(f'no job "{jid}" on a machine of yours')
    j.update(state='done', rc=int(rc), tail=str(tail or '')[-JOB_TAIL:], finished=now)


def job_view(state, user_id, jid):
    """A member sees its own job only: its state, and when done its exit code and the end of its output."""
    j = (state.get('jobs') or {}).get(jid)
    if not j or j['by'] != user_id:
        raise HouseholdError(f'no job "{jid}" of yours')
    return {'id': jid, 'state': j['state'], 'machine': j['machine'], 'rc': j.get('rc'), 'tail': j.get('tail', '')}
