"""The dashboard's Queued list: everything waiting, beside Running jobs, grouped by project in priority order. Each item says
its project and priority, its size, where it goes (an account, a device, a machine), its estimated start and why it waits:
  - autopilot backlog tasks (the last round's reason for each, e.g. the binding limit, the adaptive parallel limit);
  - test and device shards of Keep busy (docs/DISPATCH.md) still queued, and why (a device held by another tool, a machine
    asleep);
  - scout questions queued and watches that are due (they go at the next round within the week's line);
  - moves waiting for a condition (Tutti's own move waits for its running jobs).
An admin acts on an autopilot task (Run now, Move to, Cancel, Priority up or down) and cancels a queued scout question; the
others are read-only here. Pure but for the reads of the state files in `view`."""
import json
import time

from . import autopilot, backlog, config, dispatch, scouts

ACTIONS = ('run', 'move', 'cancel', 'up', 'down', 'set-rank')


def _project(cfg, name):
    return {'project': name or 'no project', 'priority': config.project_priority(cfg, name)}


def task_items(cfg, tasks, last):
    """The autopilot's queued tasks with the last round's reason for each (its waiting map, else the round's reason)."""
    waiting = (last or {}).get('waiting') or {}
    reason = (last or {}).get('reason') or 'the autopilot has not run a round yet'
    out = []
    for t in tasks:
        if t.get('status') != 'queued' or backlog.is_question(t):
            continue
        steps = autopilot.candidates(cfg, t) if t['repo'] in autopilot.settings(cfg)['repos'] else []
        to = t.get('prefer') or (steps[0] if steps else 'no ladder step')
        out.append({**_project(cfg, config.project_of_repo(cfg, t['repo'])), 'kind': 'task', 'id': t['id'], 'title': t['title'][:160],
                    'size': t.get('size') or 'S', 'to': to, 'eta': 'the next round' if not waiting.get(t['id']) else 'when it clears',
                    'why': waiting.get(t['id']) or reason, 'rank': t.get('rank', 0), 'urgent': bool(t.get('urgent')),
                    'actions': list(ACTIONS)})
    return out


def shard_items(cfg, st):
    out = []
    for pname, p in (st.get('projects') or {}).items():
        for key, sh in (p.get('shards') or {}).items():
            if sh.get('status') != 'queued':
                continue
            out.append({**_project(cfg, pname), 'kind': 'shard', 'id': key, 'title': f'test shard {key}', 'size': 'shard',
                        'to': sh.get('target') or 'the next free device or machine', 'eta': 'when a device or machine is idle',
                        'why': sh.get('why') or 'waits for an idle device or machine (Keep busy)', 'rank': 0, 'actions': []})
    return out


def scout_items(cfg, requests, now):
    out = []
    for r in requests:
        if r.get('status') == 'queued':
            out.append({**_project(cfg, r.get('project')), 'kind': 'scout', 'id': r['id'], 'title': r['task'][:160], 'size': '1 run',
                        'to': f'scout {r["scout"]}', 'eta': 'the next round with room',
                        'why': 'waits for room on its weekly line (or its reset)', 'rank': 0, 'actions': ['cancel']})
    for name in cfg.get('scouts') or {}:
        try:
            sc = scouts.settings(cfg, name)
        except scouts.ScoutError:
            continue
        for w, spec in (sc.get('watches') or {}).items():
            if scouts.due(sc, requests, w, spec, None, now):
                out.append({**_project(cfg, spec.get('project')), 'kind': 'watch', 'id': w, 'title': spec['task'][:160],
                            'size': '1 run', 'to': f'scout {name}', 'eta': 'the next round',
                            'why': f'due (every {spec["every"]}): goes at the next round', 'rank': 0, 'actions': []})
    return out


def move_items(where):
    """Tutti's own move while its journal says it is waiting for something (its running jobs to finish)."""
    cur = (where or {}).get('current') or {}
    waits = [line for line in (cur.get('log') or [])[-1:] if 'waiting for' in str(line)]
    if cur.get('status') != 'running' or not waits:
        return []
    return [{'project': 'Tutti', 'priority': 0, 'kind': 'move', 'id': 'move', 'title': f'move Tutti to {cur.get("to")}',
             'size': 'move', 'to': cur.get('to') or 'the new machine', 'eta': 'when that is done', 'why': str(waits[0])[:200],
             'rank': 0, 'actions': []}]


def group(items):
    """[{project, priority, items}] by priority, then name; inside a project urgent first, then rank."""
    by = {}
    for i in items:
        by.setdefault((i['priority'], i['project']), []).append(i)
    return [{'project': p, 'priority': pr, 'items': sorted(xs, key=lambda i: (not i.get('urgent'), i.get('rank', 0)))}
            for (pr, p), xs in sorted(by.items())]


def view(cfg, where=None, running=0, can_act=True, now=None):
    """{groups, queued, running, header, can_act}."""
    now = now or time.time()
    items = task_items(cfg, backlog.load(cfg)['tasks'], autopilot.read_state(cfg).get('last'))
    items += shard_items(cfg, dispatch.read_state(cfg))
    items += scout_items(cfg, scouts.load(cfg)['requests'] if cfg.get('scouts') else [], now)
    items += move_items(where)
    if not can_act:
        items = [{**i, 'actions': []} for i in items]
    return {'groups': group(items), 'queued': len(items), 'running': running, 'can_act': can_act,
            'header': f'{running} running · {len(items)} queued'}


def act(cfg, data):
    """(status, answer) of an admin's action: {kind: task, id, action: run|move|cancel|up|down, to?} or {kind: scout, id,
    action: cancel}."""
    kind, tid, action = (data or {}).get('kind'), (data or {}).get('id'), (data or {}).get('action')
    if kind == 'scout' and action == 'cancel':
        with scouts.locked(cfg) as d:
            r = next((x for x in d['requests'] if x['id'] == tid and x['status'] == 'queued'), None)
            if not r:
                return 404, {'error': f'no queued scout question {tid}: it may have gone out already (refresh the page)'}
            r.update(status='dropped', why='cancelled from the dashboard')
        return 200, {'ok': True}
    if kind != 'task' or action not in ACTIONS:
        return 400, {'error': 'send {"kind": "task", "id": "t12", "action": "run|move|cancel|up|down", "to": "<account>"} '
                              'or {"kind": "scout", "id": "s3", "action": "cancel"}'}
    with backlog.locked(cfg) as d:
        t = next((x for x in d['tasks'] if x['id'] == tid and x['status'] in ('queued', 'running', 'claimed')), None)
        if not t:
            return 404, {'error': f'no backlog task {tid}: it may be done or dropped (refresh the page)'}
        if action == 'move':
            names = [n for n in autopilot.candidates(cfg, {**t, 'prefer': None})]   # only where the task may really go now
            if data.get('to') not in names:
                return 400, {'error': f'move {tid} to one of {", ".join(dict.fromkeys(names)) or "(no ladder step)"}'}
            t['prefer'] = data['to']
        elif action == 'cancel':
            t['status'] = 'dropped'
            t['notes'] = [*t.get('notes', []), 'cancelled from the dashboard\'s Queued list']
        elif action == 'run':
            t['urgent'] = True
        elif action == 'set-rank':
            t['rank'] = int(data.get('rank', 0))
        else:
            t['rank'] = t.get('rank', 0) + (-1 if action == 'up' else 1)
    return 200, {'ok': True, 'task': tid, 'action': action}


def post(collector, body):
    try:
        data = json.loads(body or b'{}')
    except ValueError:
        data = None
    return act(collector.cfg, data)
