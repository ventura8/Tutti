"""The dashboard's Queued list: everything waiting, beside Running jobs, grouped by project in priority order. Each item says
its project and priority, its size, where it goes (an account, a device, a machine), its estimated start and why it waits:
  - autopilot backlog tasks (the last round's reason for each, e.g. the binding limit, the adaptive parallel limit);
  - test and device shards of Keep busy (docs/DISPATCH.md) still queued, and why (a device held by another tool, a machine
    asleep);
  - scout questions queued and watches that are due (they go at the next round within the week's line);
  - moves waiting for a condition (Tutti's own move waits for its running jobs).
An admin acts on an autopilot task (Run now, Move to, Cancel, Claim, Priority up or down) and cancels a queued scout question; the
others are read-only here. Pure but for the reads of the state files in `view`."""
import json
import time

from . import autopilot, backlog, config, dispatch, scouts, taskdetail

ACTIONS = ('run', 'move', 'cancel', 'up', 'down', 'claim')
TASK_ACTIONS = {
    'queued': ACTIONS,
    'review': ('retry', 'claim'),
    'candidate': ('cancel',),
}


def task_actions(task):
    return list(TASK_ACTIONS.get(task.get('status'), ()))


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
        item = {**_project(cfg, config.project_of_repo(cfg, t['repo'])), 'kind': 'task', 'id': t['id'], 'title': t['title'][:160],
                    'size': t.get('size') or 'S', 'to': to, 'eta': 'the next round' if not waiting.get(t['id']) else 'when it clears',
                    'why': waiting.get(t['id']) or reason, 'rank': t.get('rank', 0), 'urgent': bool(t.get('urgent')),
                    'actions': task_actions(t)}
        out.append({**item, 'facts': autopilot.detail_facts(cfg, {**t, **item, 'title': t['title']})})
    return out


def shard_items(cfg, st):
    out = []
    for pname, p in (st.get('projects') or {}).items():
        for key, sh in (p.get('shards') or {}).items():
            if sh.get('status') != 'queued':
                continue
            item = {**_project(cfg, pname), 'kind': 'shard', 'id': key, 'title': f'test shard {key}', 'size': 'shard',
                        'to': sh.get('target') or 'the next free device or machine', 'eta': 'when a device or machine is idle',
                        'why': sh.get('why') or 'waits for an idle device or machine (Keep busy)', 'actions': []}
            detail = dispatch.shard_detail(cfg, pname, key, sh, p.get('commit'))
            out.append({**item, 'facts': taskdetail.facts({**detail, **item})})
    return out


def scout_items(cfg, requests, now):
    out = []
    for r in requests:
        if r.get('status') == 'queued':
            out.append({**_project(cfg, r.get('project')), 'kind': 'scout', 'id': r['id'], 'title': r['task'][:160], 'size': '1 run',
                        'to': f'scout {r["scout"]}', 'eta': 'the next round with room',
                        'why': 'waits for room on its weekly line (or its reset)', 'actions': ['cancel']})
    for name in cfg.get('scouts') or {}:
        try:
            sc = scouts.settings(cfg, name)
        except scouts.ScoutError:
            continue
        for w, spec in (sc.get('watches') or {}).items():
            if scouts.due(sc, requests, w, spec, None, now):
                out.append({**_project(cfg, spec.get('project')), 'kind': 'watch', 'id': w, 'title': spec['task'][:160],
                            'size': '1 run', 'to': f'scout {name}', 'eta': 'the next round',
                            'why': f'due (every {spec["every"]}): goes at the next round', 'actions': []})
    return out


def move_items(where):
    """Tutti's own move while its journal says it is waiting for something (its running jobs to finish)."""
    cur = (where or {}).get('current') or {}
    waits = [line for line in (cur.get('log') or [])[-1:] if 'waiting for' in str(line)]
    if cur.get('status') != 'running' or not waits:
        return []
    return [{'project': 'Tutti', 'priority': 0, 'kind': 'move', 'id': 'move', 'title': f'move Tutti to {cur.get("to")}',
             'size': 'move', 'to': cur.get('to') or 'the new machine', 'eta': 'when that is done', 'why': str(waits[0])[:200],
             'actions': []}]


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
    items = [{**i, 'facts': i['facts'] if 'facts' in i else taskdetail.facts(i)} for i in items]
    if not can_act:
        items = [{**i, 'actions': []} for i in items]
    return {'groups': group(items), 'queued': len(items), 'running': running, 'can_act': can_act,
            'header': f'{running} running · {len(items)} queued'}


def scout_cancel(cfg, tid):
    with scouts.locked(cfg) as data:
        req = next((r for r in data['requests'] if r['id'] == tid and r['status'] == 'queued'), None)
        if not req:
            return 404, {'error': f'no queued scout question {tid}: it may have gone out already (refresh the page)'}
        req.update(status='dropped', why='cancelled from the dashboard')
    return 200, {'ok': True}


def change_task(cfg, task, action, data):
    if action == 'move':
        names = list(dict.fromkeys(autopilot.candidates(cfg, {**task, 'prefer': None})))
        if data.get('to') not in names:
            raise backlog.BacklogError(f'move {task["id"]} to one of {", ".join(names) or "(no ladder step)"}')
        task['prefer'] = data['to']
    elif action == 'claim':
        who = data.get('who')
        backlog.claim_task(task, who.strip() if isinstance(who, str) else '', time.time())
    elif action in ('up', 'down'):
        task['rank'] = task.get('rank', 0) + (-1 if action == 'up' else 1)
    else:
        set_task_status(task, action)


def set_task_status(task, action):
    if action == 'cancel':
        task['status'] = 'dropped'
        task.setdefault('notes', []).append('cancelled from the dashboard')
    elif action == 'retry':
        task.update(status='queued', parked=None)
    else:
        task['urgent'] = True


def act(cfg, data):
    """Apply a dashboard action under the state lock after checking the current status."""
    if not isinstance(data, dict):
        return 400, {'error': 'send a JSON object with kind, id and action'}
    kind, tid, action = data.get('kind'), data.get('id'), data.get('action')
    if kind == 'scout' and action == 'cancel':
        return scout_cancel(cfg, tid)
    if kind != 'task' or action not in (*ACTIONS, 'retry'):
        return 400, {'error': 'send kind "task", an id and action run, move, cancel, up, down, claim or retry'}
    return act_on_task(cfg, tid, action, data)


def act_on_task(cfg, tid, action, data):
    with backlog.locked(cfg) as tasks:
        task = next((t for t in tasks['tasks'] if t['id'] == tid), None)
        if task is None or action not in task_actions(task):
            return 404, {'error': f'cannot {action} task {tid}: it may have changed status (refresh the page)'}
        try:
            change_task(cfg, task, action, data)
        except backlog.BacklogError as err:
            return 400, {'error': str(err)}
    return 200, {'ok': True, 'task': tid, 'action': action}


def post(collector, body):
    try:
        data = json.loads(body or b'{}')
    except ValueError:
        data = None
    return act(collector.cfg, data)
