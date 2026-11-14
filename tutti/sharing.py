"""A member's side of shared projects (docs/HOUSEHOLD.md, *Same project, several users*): which repos are shared, their
project keys, one sync per autopilot round with the coordinator, and what it means for the local backlog.

The round: `ask` is built from the local backlog (new tasks to publish, finished ones to close, held ones to renew, the
next ones wanted); one signed call (outside the backlog's lock); `apply` mirrors the shared tasks into the local backlog,
closes the ones another member finished, and returns the tasks the plan must not start now (leased by another member, or
any shared task while the coordinator cannot be reached: fail closed). A single user never gets here."""
import subprocess
import time

from . import backlog, config, coordinator, fairshare, household, telemetry, users, worktree

SOURCE = 'household'
_keys = {}


def shared_repos(cfg):
    """The repos of autopilot.repos shared with the household: `household: true`, or `tutti household project share`."""
    saved = set(users.read(config.state_dir(cfg)).get('shared_repos') or [])
    return sorted(n for n, r in (cfg['autopilot'].get('repos') or {}).items() if (r or {}).get('household') or n in saved)


def set_shared(cfg, repo, on):
    if repo not in (cfg['autopilot'].get('repos') or {}):
        names = ', '.join(cfg['autopilot'].get('repos') or {}) or '(none)'
        raise users.UserError(f'no repo "{repo}" under autopilot.repos in {cfg.get("_path")}: the repos are {names}')
    folder = config.state_dir(cfg)
    saved = set(users.read(folder).get('shared_repos') or [])
    saved = saved | {repo} if on else saved - {repo}
    users.write(folder, shared_repos=sorted(saved))


def key_of_repo(cfg, name, run=subprocess.run):
    """The project key of a repo (its origin remote, normalised), cached per path; '' when it has none."""
    path = config.expand(cfg['autopilot']['repos'][name]['path'])
    if path not in _keys:
        try:
            out = run([worktree.git_bin(), '-C', path, 'remote', 'get-url', 'origin'], capture_output=True, text=True, timeout=10)
            _keys[path] = household.project_key(out.stdout.strip()) if out.returncode == 0 else ''
        except (OSError, subprocess.SubprocessError):
            _keys[path] = ''
    return _keys[path]


def ask_for(tasks, repo, room=6, check=''):
    """What this member tells the coordinator about one shared repo's local tasks (and its gates, for a member who adopts
    the project for spare room)."""
    mine = [t for t in tasks if t['repo'] == repo]
    publish = [{k: t.get(k) for k in household.TASK_KEYS} for t in mine
               if t['status'] in ('queued', 'running') and t.get('source') != SOURCE]
    closed = [{'key': household.fold(t['title']), 'status': t['status']} for t in mine if t['status'] in household.CLOSED]
    held = [household.fold(t['title']) for t in mine if t['status'] in household.HELD]
    want = [household.fold(t['title']) for t in mine if t['status'] == 'queued'][:50]
    return {'publish': publish, 'closed': closed, 'held': held, 'want': want, 'room': room, 'meta': {'check': check}}


def ask(cfg, data, keys, beat):
    """The whole round's request: the heartbeat and every shared repo with a project key. A member reserves only as many
    next tasks as it could start now (max_parallel minus its runs), so it never keeps tasks from the others."""
    running = sum(1 for t in data['tasks'] if t['status'] == 'running')
    room = max(0, int(cfg['autopilot'].get('max_parallel', 1)) - running)
    repos = cfg['autopilot']['repos']
    return {'beat': beat, 'projects': {keys[r]: ask_for(data['tasks'], r, room, (repos[r] or {}).get('check') or '') for r in keys
                                       if keys[r]}}


def _close_here(t, st, events):
    """A task another member finished, still queued here: closed here too."""
    if t and t['status'] in ('queued', 'candidate') and st.get('closed_by'):
        t['status'] = st['status']
        t['notes'] = [*t.get('notes', []), f'{st["status"]} by {st["closed_by"]} (household)']
        events.append(f'{t["id"]} {st["status"]} by {st["closed_by"]}: closed here too')


def _mirror(data, repo, st, local, events, now):
    """A shared task this member does not have yet: added to its backlog (source household)."""
    task, added = backlog.add(data, {**backlog.new_task(repo, st['title'], st.get('scope') or (), st.get('check') or '',
                                                        st.get('size') or 'S', SOURCE), 'added': now})
    if added:
        local[st['key']] = task
        events.append(f'{task["id"]} from the household\'s backlog of {repo}')


def _holds(local, p):
    held, mine = p.get('held') or {}, set(p.get('mine') or [])
    return {t['id']: f'leased by {held[key]}' if key in held else 'waiting for its lease from the household'
            for key, t in local.items() if t['status'] == 'queued' and key not in mine}


def apply(data, keys, answer, now):
    """Mirrors the answer into the local backlog. Returns ({task id: why it waits}, events)."""
    hold, events = no_key_hold(data, keys), []
    by_key = {pkey: repo for repo, pkey in keys.items() if pkey}
    for pkey, p in (answer.get('projects') or {}).items():
        repo = by_key.get(pkey)
        if not repo:
            continue
        local = {household.fold(t['title']): t for t in data['tasks'] if t['repo'] == repo}
        for st in p.get('tasks') or []:
            t = local.get(st['key'])
            if st['status'] in household.CLOSED:
                _close_here(t, st, events)
            elif not t:
                _mirror(data, repo, st, local, events, now)
        hold.update(_holds(local, p))
    return hold, events


def taken_meanwhile(data, keys, answer):
    """A shared task this member ran while away, whose lease another member took after it expired: said, so the two
    results are compared before either lands (nothing is dropped)."""
    out = []
    by_key = {pkey: repo for repo, pkey in keys.items() if pkey}
    for pkey, p in (answer.get('projects') or {}).items():
        held = p.get('held') or {}
        for t in data['tasks']:
            if t['repo'] == by_key.get(pkey) and t['status'] in household.HELD and household.fold(t['title']) in held:
                who = held[household.fold(t['title'])]
                t['notes'] = [*t.get('notes', []), f'{who} took it while you were away: compare before landing']
                out.append(f'{t["id"]}: {who} took it while you were away; both results are kept')
    return out


def no_key_hold(data, keys):
    """A shared repo without an origin remote cannot be matched across members: its tasks wait, and say why."""
    return {t['id']: f'shared, but {t["repo"]} has no origin remote: give it one (git remote add origin <url>) so the household '
                     'can match it, or `tutti household project unshare ' + t['repo'] + '`'
            for t in data['tasks'] if t['status'] == 'queued' and t['repo'] in keys and not keys[t['repo']]}


def offline_hold(data, keys, why):
    return {t['id']: f'household coordinator not reachable ({why}): shared tasks wait' for t in data['tasks']
            if t['status'] == 'queued' and t['repo'] in keys}


def sync(cfg, data, beat=None, client=None, now=0.0, run=subprocess.run):
    """One round (called outside the backlog's lock with a fresh read of it). Returns {hold, events, answer, error}; for a
    household of one or no shared repo: nothing to hold and no call."""
    repos = shared_repos(cfg)
    client = client or coordinator.client_of(cfg)
    if not client:
        return {'hold': {}, 'events': [], 'answer': None, 'error': None, 'keys': {}}
    keys = {r: key_of_repo(cfg, r, run) for r in repos}
    try:
        answer = client.call('/hh/sync', ask(cfg, data, keys, beat or {}))
    except (coordinator.Unreachable, household.HouseholdError) as e:
        away = isinstance(e, coordinator.Unreachable)
        telemetry.record('feature', 'household.sync', outcome='offline' if away else 'refused')
        fairshare.save_cache(config.state_dir(cfg), None, now or time.time(), error=str(e), away=away)
        return {'hold': offline_hold(data, keys, type(e).__name__), 'events': [], 'answer': None, 'error': str(e), 'keys': keys}
    was_away = fairshare.read_cache(config.state_dir(cfg)).get('away_since')
    events = []
    if was_away:   # back on the home network: this round reported everything that happened meanwhile
        p = ask(cfg, data, keys, beat or {})['projects'].values()
        events.append(f'back home: synced with the household ({sum(len(x["publish"]) for x in p)} task(s) published, '
                      f'{sum(len(x["closed"]) for x in p)} closed, after {((now or time.time()) - was_away) / 60:.0f} min away)')
        telemetry.record('feature', 'household.return', minutes=round(((now or time.time()) - was_away) / 60))
    telemetry.record('feature', 'household.sync', outcome='ok', projects=len(answer.get('projects') or {}),
                     machines=len(answer.get('machines') or {}))
    if answer.get('machines') is not None:
        fairshare.save_cache(config.state_dir(cfg), answer['machines'], now or time.time(), answer.get('status'))
        want = (beat or {}).get('machines') or {}
        limited = [n for n, m in answer['machines'].items()
                   if int(m.get('grant', 0)) < sum(int((want.get(n) or {}).get(k, 0)) for k in ('interactive', 'background'))]
        telemetry.record('feature', 'household.grant', machines=len(answer['machines']), limited=len(limited),
                         granted=sum(int(m.get('grant', 0)) for m in answer['machines'].values()))
    return {'hold': {}, 'events': events, 'answer': answer, 'error': None, 'keys': keys}


def under_lock(result, data, now):
    """(hold, events) for the round's plan, from the backlog read under the lock: the answer mirrored, or every shared
    queued task held while the coordinator cannot be reached."""
    if result['answer'] is not None:
        hold, events = apply(data, result['keys'], result['answer'], now)
        return hold, list(result.get('events') or []) + events + taken_meanwhile(data, result['keys'], result['answer'])
    if result['error']:
        return offline_hold(data, result['keys'], 'unreachable'), []
    return no_key_hold(data, result['keys']), []


def renew(cfg, data, started, client=None, run=subprocess.run):
    """Right after shared tasks started: their leases renewed to the full lease_minutes at once (the round's reservation
    is short), so another member can never take a task that runs here. Never raises: the next round renews too."""
    client = client or coordinator.client_of(cfg)
    repos = sorted({backlog.find(data, i)['repo'] for i in started})
    keys = {r: key_of_repo(cfg, r, run) for r in repos}
    projects = {keys[r]: {'held': [household.fold(backlog.find(data, i)['title']) for i in started
                                   if backlog.find(data, i)['repo'] == r]} for r in repos if keys[r]}
    if not client or not projects:
        return False
    try:
        client.call('/hh/sync', {'beat': {}, 'projects': projects})
        return True
    except (coordinator.Unreachable, household.HouseholdError):
        return False


PROMPTING_MINUTES = 15


def prompting(entries, accounts, now):
    """Whether the owner is prompting now: one of the main agent's accounts rose within the last PROMPTING_MINUTES."""
    from datetime import timedelta

    from .pacing import parse_time
    since = now - timedelta(minutes=PROMPTING_MINUTES)
    for name in accounts:
        rs = [(parse_time(e['at']), float(e['used'])) for e in entries if e.get('account') == name and e.get('used') is not None]
        recent = [u for t, u in rs if t >= since]
        before = [u for t, u in rs if t < since]
        if recent and before and max(recent) > before[-1]:
            return True
    return False


def beat(cfg, now, machines=None):
    """This member's heartbeat: its main agent's kind, prompting or not, runs today, its weight, its 7 x 24 activity
    profile (empty while away or on vacation) and its demand on each shared machine. Never a prompt, a reading or a name
    of an account. `machines`: this round's probed machine items (the capacity of the member's own shared machines)."""
    from . import owner, pacing, state
    try:
        entries = pacing.read_log(state.usage_log_path(cfg))
    except (OSError, ValueError):
        entries = []
    folder = config.state_dir(cfg)
    accounts = owner.owner_of(cfg, folder)['accounts']
    agent = telemetry.tool_of((cfg.get('main') or {}).get('agent') or '')
    active = prompting(entries, accounts, now)
    v = owner.view(cfg, folder, entries, now)
    away = v['effective'] not in ('active', 'auto', 'present')
    return {'agent': agent, 'prompting': active, 'totals': {'runs_today': state.runs_today(cfg)},
            'weight': (cfg.get('user') or {}).get('weight', 1), 'profile': [] if away else v['profile'],
            'utc_offset': v['utc_offset'], 'machines': demand(cfg, active, machines or [])}


def running_on(folder, name):
    """(interactive, background) counts of this member's running or queued jobs bound to a machine."""
    from . import controls
    inter = back = 0
    for log, _pid in controls.running_jobs(folder, 'machine', name):
        try:
            with open(log + '.target', encoding='utf8') as f:
                background = 'class:background' in f.read().split()
        except OSError:
            background = False
        back, inter = (back + 1, inter) if background else (back, inter + 1)
    return inter, back


def fit_slots(m, slots, heat):
    """The slots of one's own shared machine that may run now: none while offline, yielding, paused or too hot; a hot
    machine's `heat.hot_slots`."""
    if m.get('status') in ('offline', 'paused', 'held', 'off') or str(m.get('reason', '')).startswith('too hot'):
        return 0
    if str(m.get('reason', '')).startswith('hot:'):
        return min(slots, int((heat or {}).get('hot_slots', 1)))
    return slots


def demand(cfg, active, machines):
    """This member's demand on each household machine it knows (from the last grants): what it runs there; one more
    interactive slot while it prompts, on the first of them in its routing order only (its next test should not wait);
    and its queued autopilot tasks as background, spread once over the machines (each up to its slots), never counted
    twice. For its own shared machines also how many slots are fit now."""
    folder = config.state_dir(cfg)
    known = (fairshare.read_cache(folder).get('machines') or {})
    probed = {m['name']: m for m in machines}
    queued = sum(1 for t in backlog.load(cfg)['tasks'] if t['status'] == 'queued')
    order = [n for n in cfg['routing']['order'] if n in known] + [n for n in known if n not in cfg['routing']['order']]
    out, extra = {}, (1 if active else 0)
    for name in order:
        if name not in cfg['machines']:
            continue
        inter, back = running_on(folder, name)
        slots = int(known[name].get('slots') or 1)
        spread = min(max(0, slots - back), queued)
        queued -= spread
        out[name] = {'interactive': inter + extra, 'background': back + spread, 'running': inter + back}
        extra = 0
        if known[name].get('mine') and name in probed:
            out[name]['capacity'] = fit_slots(probed[name], slots, cfg['machines'][name].get('heat'))
    return out


# ---- the grants applied: what a shared machine leaves free for this member


def mine_shared(cfg):
    return set(users.read(config.state_dir(cfg)).get('machine_overrides') or {})


def limited_free(cfg, name, free, now, cache=None):
    """(free, why) of one machine for this member after its household grant."""
    folder = config.state_dir(cfg)
    cache = fairshare.read_cache(folder) if cache is None else cache
    if name not in (cache.get('machines') or {}) and name not in mine_shared(cfg):
        return free, ''
    inter, back = running_on(folder, name)
    return fairshare.limit(name, free, inter + back, cache, now, mine_shared(cfg))


def limit_items(cfg, items, now):
    """The probed machine items (state.machines_state) with each shared machine's free slots cut to this member's grant."""
    cache = fairshare.read_cache(config.state_dir(cfg))
    for m in items:
        if m.get('free') is None:
            continue
        free, why = limited_free(cfg, m['name'], m['free'], now, cache)
        if why:
            m.update(free=free, household=why)
            if free < 1 and m.get('status') in ('free', 'loaded'):
                m.update(status='busy', reason=why)
    return items


def limited_probe(cfg, name, probe, now):
    """A routing probe with the free slots cut to this member's grant (tutti test's routing)."""
    import dataclasses
    if not probe.reachable:
        return probe
    free, _why = limited_free(cfg, name, probe.free, now)
    return probe if free >= probe.free else dataclasses.replace(probe, free=free)


# ---- spare room shared with the household (accounts.<n>.share_spare)

SPARE_RETRY = 6 * 3600   # a project that could not be cloned is tried again after this


def spare_accounts(cfg, states, now):
    """This member's accounts that share their spare room and have room the planner says would be lost: tasks it would
    give now, and their target within household.share_spare_hours. Never one in panic or spent."""
    from datetime import datetime, timezone

    from .pacing import parse_time
    shares = users.shares(cfg, users.read(config.state_dir(cfg)))
    hours = float(cfg['household'].get('share_spare_hours', 6))
    at = datetime.fromtimestamp(now, timezone.utc) if isinstance(now, (int, float)) else now
    out = []
    for name in shares:
        s = states.get(name) or {}
        p = s.get('quota_plan') or {}
        if s.get('panic') or s.get('verdict') == 'spent' or not p.get('tasks') or not p.get('expires'):
            continue
        if (parse_time(p['expires']) - at).total_seconds() <= hours * 3600:
            out.append(name)
    return out


def spare_name(pkey):
    return 'hh-' + pkey.split('/', 1)[-1].replace('/', '-')[:60]


def _clone(path, key, run):
    """None when the project is (now) at `path`, else why the clone failed."""
    import os
    if os.path.isdir(path):
        return None
    out = run([worktree.git_bin(), 'clone', '-q', f'https://{key}.git', path], capture_output=True, text=True, timeout=600)
    return None if out.returncode == 0 else (out.stderr or '').strip()[:120] or f'git exit {out.returncode}'


def adopt_offers(cfg, result, now, run=subprocess.run):
    """Another member's shared projects offered for this member's spare room: cloned once (with this member's own git
    sign-in) into <state>/household/repos/ and added as spare repos (only spare accounts take their tasks; results go to
    a draft pull request). Returns events."""
    import os
    offers = ((result or {}).get('answer') or {}).get('offers') or []
    if not offers:
        return []
    folder = config.state_dir(cfg)
    saved = users.read(folder)
    spare, failed = dict(saved.get('spare_repos') or {}), dict(saved.get('spare_failed') or {})
    events = []
    for o in offers:
        name = spare_name(o['key'])
        if name in spare or (o['key'] in failed and now - failed[o['key']] < SPARE_RETRY):
            continue
        path = os.path.join(folder, 'household', 'repos', name)
        why = _clone(path, o['key'], run)
        if why:
            failed[o['key']] = now
            events.append(f'spare room: could not clone {o["key"]} with your git sign-in ({why}); tried again in 6 h')
            continue
        spare[name] = {'path': path, 'check': o.get('check') or '', 'household': True, 'spare': True, 'land': 'pr', 'pr': 'draft'}
        cfg['autopilot']['repos'][name] = spare[name]
        events.append(f'spare room: {o["key"]} adopted as {name}; your spare accounts take its shared tasks')
    users.write(folder, spare_repos=spare, spare_failed=failed)
    return events


def spare_hold(cfg, data):
    """A spare repo's queued tasks wait while no account of this member has spare room."""
    if cfg.get('_spare_accounts'):
        return {}
    repos = cfg['autopilot']['repos']
    return {t['id']: 'another member\'s project: only room your accounts would lose at their reset does it (share_spare)'
            for t in data['tasks'] if t['status'] == 'queued' and (repos.get(t['repo']) or {}).get('spare')}


# ---- the dashboard's household view (docs/DASHBOARD.md, *Household*)


def view(cfg, now):
    """The household as the dashboard shows it, from the last answer kept here (no call while the page loads): who is in
    it, active and their main agent's kind, each shared machine's use per member and its fairness, whether the coordinator
    answers. Of the others only aggregates and names: never their jobs, prompts or accounts."""
    m = users.read_household()
    me = users.identity(cfg)
    if not m:
        return {'role': 'alone', 'me': me['name'], 'main': me['main'], 'members': [], 'machines': {}}
    cache = fairshare.read_cache(config.state_dir(cfg))
    status = cache.get('status') or {}
    fresh = now - (cache.get('at') or 0) <= fairshare.STALE_SECONDS
    away = bool(cache.get('away_since')) and not fresh
    members = [{**{k: x.get(k) for k in ('name', 'agent', 'online', 'active', 'runs_today', 'last_seen')}, 'me': x['name'] == m.get('name')}
               for x in status.get('members') or []]
    return {'role': users.role(m), 'household': m.get('household'), 'me': m.get('name'), 'main': me['main'], 'reachable': fresh,
            'error': cache.get('error') if not fresh else None, 'at': cache.get('at'), 'members': members,
            'machines': cache.get('machines') or {}, 'admin': status.get('admin'), 'projects': status.get('projects') or {},
            'away': away, 'away_since': cache.get('away_since') if away else None,
            'away_note': 'away from the household: working on your own until you\'re home' if away else None}


def busy_for(items, hh):
    """The probed machine items with the household's use of each shared machine: who else it is busy for, by name only."""
    for it in items:
        m = (hh.get('machines') or {}).get(it['name'])
        if m:
            others = {n: k for n, k in (m.get('use') or {}).items() if n != hh.get('me')}
            it['household_use'] = {'owner': m.get('owner'), 'grant': m.get('grant'), 'others': others, 'paused': m.get('paused')}
    return items
