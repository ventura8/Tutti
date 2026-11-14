"""Move work off a target that is away or asleep, now (docs/PRESENCE.md, "Move work now").

A job is a dispatch shard (`shard:<project>:<suite/shard>`), an autopilot task (`task:<id>`) or, on the household
coordinator, a member's task lease (`lease:<project key>:<task key>`). A job waits on a target when its run is on that target,
or when the only targets it may run on are away or asleep. Moving it:

- a shard: its lease (if any) stopped, queued again with no attempt counted, pinned to the chosen target (or any: `auto`);
- an autopilot task: its run stopped, a last WIP checkpoint taken, queued again with no attempt counted and a handoff note; it
  resumes from the last synced checkpoint (tutti/autopilot.py: `refs/tutti/wip/<name>` on the project's remote);
- a household lease: released by the coordinator (or given to the chosen member); the member who held it sees on its next
  sync that the task moved, keeps its local WIP on a backup branch and never runs it again.

`--bump` puts the job first in its queue. Only an admin may move work: the dashboard's POST /api/move passes the auth hook.
The automatic option (presence.move_after_minutes, off by default) moves urgent jobs (`projects.<p>.urgent: true` or a task's
`urgent`) that waited that long on an absent target."""
import time

from . import autopilot, backlog, dispatch, presence, telemetry


class MoveError(Exception):
    """A move that cannot be done, with why and what to do instead."""


def _target_of_task(cfg, t):
    """The machine an autopilot task's run uses (its worker's machine), or None."""
    w = (cfg.get('workers') or {}).get(t.get('worker') or '') or {}
    return f'machine:{w["machine"]}' if w.get('machine') else None


def waiting(cfg, now=None, pst=None, st=None, data=None):
    """[{id, kind, title, target, label, since, urgent}] of the jobs waiting on an away or asleep target."""
    now = now or time.time()
    pst = pst if pst is not None else presence.read(cfg)
    off = presence.offset(cfg)
    gone = presence.absent(pst)
    out = _shards(cfg, st if st is not None else dispatch.read_state(cfg), gone)
    for t in (data if data is not None else backlog.load(cfg))['tasks']:
        tid = _target_of_task(cfg, t)
        if t['status'] in ('running', 'queued') and tid in gone:
            out.append((f'task:{t["id"]}', 'task', t['title'], tid, bool(t.get('urgent')), t.get('started') or 0))
    return [_row(*x, gone, off) for x in out]


def _shards(cfg, st, gone):
    """(id, kind, title, target, urgent) of the shards running on, or only allowed on, absent targets."""
    out = [(f'shard:{x["project"]}:{x["key"]}', 'shard', f'{x["project"]} {x["key"]}', x['target'], _urgent_project(cfg, x['project']),
            x.get('started') or 0) for x in st['leases'].values() if x['target'] in gone]
    for w in dispatch.pending(cfg, st):
        names = _allowed(w, w.get('pin'))
        at = (((st['projects'].get(w['project']) or {}).get('shards') or {}).get(w['key']) or {}).get('at') or 0
        if names and all(n in gone for n in names):
            out.append((f'shard:{w["project"]}:{w["key"]}', 'shard', f'{w["project"]} {w["key"]}', names[0],
                        _urgent_project(cfg, w['project']), at))
    return out


def _allowed(w, pin):
    """The targets a pending shard may go to ([] for any of its kind)."""
    if pin == dispatch.ANY:
        return []
    if pin:
        return [pin]
    return [f'device:{n}' for n in w['devices'] or []] + [f'machine:{n}' for n in w['machines'] or []]


def _urgent_project(cfg, project):
    return bool(((cfg.get('projects') or {}).get(project) or {}).get('urgent'))


def _row(jid, kind, title, target, urgent, at, gone, off):
    """`since`: when this job started waiting on the absent target (the later of its departure and the job's own time)."""
    rec = gone[target]
    since = max(float(rec.get('since') or 0), float(at or 0)) or None
    return {'id': jid, 'kind': kind, 'title': title, 'target': target, 'label': presence.label(rec, off), 'since': since,
            'urgent': urgent}


def check_target(cfg, to):
    """'' for auto, else the target id (`device:<d>`, `machine:<m>`, or a plain name of either)."""
    if to in (None, '', 'auto', 'best'):
        return ''
    if to.split(':', 1)[0] in ('device', 'machine', 'member'):
        return to
    if to in (cfg.get('devices') or {}):
        return f'device:{to}'
    if to in (cfg.get('machines') or {}):
        return f'machine:{to}'
    raise MoveError(f'no device or machine named "{to}": use a name from `tutti machines` / `tutti devices`, or --to auto')


def move_shard(cfg, project, key, to, bump, now, stop=dispatch.stop_pid):
    with dispatch.locked(cfg) as st:
        sh = ((st['projects'].get(project) or {}).get('shards') or {}).get(key)
        if not sh:
            raise MoveError(f'no shard {project} {key}: `tutti dispatch status` lists them')
        for lid in [i for i, x in st['leases'].items() if x['project'] == project and x['key'] == key]:
            lease = st['leases'].pop(lid)
            stop(lease['pid'])
            dispatch.requeue(st, lease, 'moved by an admin: queued again, not counted as a failure', now)
        sh.update(status='queued', pin=to or dispatch.ANY, bump=bool(bump) or sh.get('bump', False), lease=None,
                  why=f'moved by an admin to {to or "the best available target"}')
        dispatch.event(st, now, f'{project} {key} moved to {to or "the best available target"}')
    return f'{project} {key}: queued for {to or "the best available target"}'


def move_task(cfg, task_id, to, bump, now, is_alive=None, kill=dispatch.stop_pid, snap=None):
    with backlog.locked(cfg) as data:
        try:
            t = backlog.find(data, task_id)
        except backlog.BacklogError:
            t = None
        if not t:
            raise MoveError(f'no backlog task {task_id}: `tutti backlog list` lists them')
        if t['status'] not in ('running', 'queued'):
            raise MoveError(f'task {task_id} is {t["status"]}: only a queued or running task moves')
        if to and not [n for n in autopilot.ladder(cfg) if f'machine:{autopilot.on_machine(cfg, n)}' == to]:
            raise MoveError(f'no worker of the autopilot\'s ladder runs on {to}: pick one that has a worker, or --to auto')
        note = autopilot.move_out(cfg, t, to, now, is_alive, kill, snap)
        if bump:
            data['tasks'].remove(t)
            data['tasks'].insert(0, t)
            t['urgent'] = True
    return f'task {task_id}: {note}'


def move(cfg, jid, to='auto', bump=False, now=None, **io):
    """Moves one job; returns what was done. MoveError says why it could not."""
    now = now or time.time()
    to = check_target(cfg, to)
    kind, _, rest = jid.partition(':')
    if kind == 'shard':
        project, _, key = rest.partition(':')
        out = move_shard(cfg, project, key, to, bump, now, **{k: v for k, v in io.items() if k == 'stop'})
    elif kind == 'task':
        out = move_task(cfg, rest, to, bump, now, **{k: v for k, v in io.items() if k in ('is_alive', 'kill', 'snap')})
    elif kind == 'lease':
        out = move_lease(cfg, rest, to, now)
    else:
        raise MoveError(f'"{jid}" is not a job id: use shard:<project>:<suite/shard>, task:<id> or lease:<project>:<task>')
    telemetry.record('feature', 'jobs.move', job_kind=kind, auto=not to, bump=bool(bump))
    return out


def move_lease(cfg, rest, to, now):
    from . import coordinator, household
    path = coordinator.state_path(cfg)
    state = coordinator.load(path)
    if not state:
        raise MoveError('this computer is not the household coordinator: move a member\'s task from the coordinator\'s dashboard')
    pkey, _, key = rest.partition(':')
    if to and not to.startswith('member:'):
        raise MoveError('a member\'s task moves to another member: --to member:<id> (`tutti household status`), or --to auto')
    try:
        note = household.reassign(state, pkey, key, to.partition(':')[2] if to else None, now)
    except household.HouseholdError as e:
        raise MoveError(str(e)) from e
    coordinator.save(path, state)
    return note


def evacuate(cfg, target, now=None, **io):
    """Moves every job waiting on `target` (a name or id) to the best available target. Returns the lines."""
    tid = check_target(cfg, target)
    jobs = [j for j in waiting(cfg, now) if j['target'] == tid]
    if not jobs:
        return [f'nothing waits on {target}']
    return [move(cfg, j['id'], 'auto', False, now, **io) for j in jobs]


def auto_moves(cfg, now=None, **io):
    """presence.move_after_minutes (opt-in): urgent jobs waiting that long on an absent target are moved. Returns lines."""
    after = presence.settings(cfg).get('move_after_minutes')
    if not after:
        return []
    now = now or time.time()
    due = [j for j in waiting(cfg, now) if j['urgent'] and j.get('since') and now - j['since'] >= float(after) * 60]
    return [move(cfg, j['id'], 'auto', False, now, **io) for j in due]
