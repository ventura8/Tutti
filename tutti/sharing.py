"""A member's side of shared projects (docs/HOUSEHOLD.md, *Same project, several users*): which repos are shared, their
project keys, one sync per autopilot round with the coordinator, and what it means for the local backlog.

The round: `ask` is built from the local backlog (new tasks to publish, finished ones to close, held ones to renew, the
next ones wanted); one signed call (outside the backlog's lock); `apply` mirrors the shared tasks into the local backlog,
closes the ones another member finished, and returns the tasks the plan must not start now (leased by another member, or
any shared task while the coordinator cannot be reached: fail closed). A single user never gets here."""
import subprocess

from . import backlog, config, coordinator, household, telemetry, users, worktree

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


def ask_for(tasks, repo, room=6):
    """What this member tells the coordinator about one shared repo's local tasks."""
    mine = [t for t in tasks if t['repo'] == repo]
    publish = [{k: t.get(k) for k in household.TASK_KEYS} for t in mine
               if t['status'] in ('queued', 'running') and t.get('source') != SOURCE]
    closed = [{'key': household.fold(t['title']), 'status': t['status']} for t in mine if t['status'] in household.CLOSED]
    held = [household.fold(t['title']) for t in mine if t['status'] in household.HELD]
    want = [household.fold(t['title']) for t in mine if t['status'] == 'queued'][:room]
    return {'publish': publish, 'closed': closed, 'held': held, 'want': want}


def ask(cfg, data, keys, beat):
    """The whole round's request: the heartbeat and every shared repo with a project key."""
    room = max(2, int(cfg['autopilot'].get('max_parallel', 1)) * 3)
    return {'beat': beat, 'projects': {keys[r]: ask_for(data['tasks'], r, room) for r in keys if keys[r]}}


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
    if not client or not repos:
        return {'hold': {}, 'events': [], 'answer': None, 'error': None, 'keys': {}}
    keys = {r: key_of_repo(cfg, r, run) for r in repos}
    try:
        answer = client.call('/hh/sync', ask(cfg, data, keys, beat or {}))
    except (coordinator.Unreachable, household.HouseholdError) as e:
        telemetry.record('feature', 'household.sync', outcome='offline' if isinstance(e, coordinator.Unreachable) else 'refused')
        return {'hold': offline_hold(data, keys, type(e).__name__), 'events': [], 'answer': None, 'error': str(e), 'keys': keys}
    telemetry.record('feature', 'household.sync', outcome='ok', projects=len(answer.get('projects') or {}))
    return {'hold': {}, 'events': [], 'answer': answer, 'error': None, 'keys': keys}


def under_lock(result, data, now):
    """(hold, events) for the round's plan, from the backlog read under the lock: the answer mirrored, or every shared
    queued task held while the coordinator cannot be reached."""
    if result['answer'] is not None:
        return apply(data, result['keys'], result['answer'], now)
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


def beat(cfg, now):
    """This member's heartbeat: its main agent's kind, prompting or not, runs today. Never a prompt, a reading or a name
    of an account."""
    from . import owner, pacing, state
    try:
        entries = pacing.read_log(state.usage_log_path(cfg))
    except (OSError, ValueError):
        entries = []
    accounts = owner.owner_of(cfg, config.state_dir(cfg))['accounts']
    agent = telemetry.tool_of((cfg.get('main') or {}).get('agent') or '')
    return {'agent': agent, 'prompting': prompting(entries, accounts, now), 'totals': {'runs_today': state.runs_today(cfg)}}
