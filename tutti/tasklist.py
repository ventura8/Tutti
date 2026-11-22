"""The Tasks tab (t307, docs/DASHBOARD.md, *Tasks*): every backlog task in one status bucket per subtab, P0 first and then
by id, each row opening to its detail (taskdetail). Pure: tasks in, buckets out; `get` reads the backlog for the route."""
import re

from . import priority

# subtab: (label, the statuses it holds). Done holds only the newest, the rest every task.
BUCKETS = (('active', 'Active', ('running', 'claimed')), ('queued', 'Queued', ('queued',)),
           ('needs-person', 'Needs a person', ('needs-person',)), ('review', 'To review', ('review',)),
           ('candidate', 'Suggested', ('candidate',)), ('done', 'Done', ('done', 'dropped')))
DONE_SHOWN, CUT = 50, 160
API = '/api/tasks'


def _num(tid):
    m = re.fullmatch(r't(\d+)', str(tid))
    return int(m.group(1)) if m else 0


def row(t, why=''):
    """What a list row shows: no notes or attempts (the row opens to those)."""
    return {'id': t.get('id'), 'title': str(t.get('title') or '')[:CUT], 'repo': t.get('repo'), 'status': t.get('status'),
            'priority': f'P{priority.task_level(t)}' if 'priority' in t else 'untriaged', 'size': t.get('size'),
            'who': t.get('claimed_by') or t.get('worker') or '', 'why': why or t.get('parked') or ''}


def buckets(tasks, held=None):
    """[{key, label, count, items}] in subtab order; P0 first, then by number (newest first under Done)."""
    held = held or {}
    out = []
    for key, label, statuses in BUCKETS:
        mine = [t for t in tasks if t.get('status') in statuses]
        if key == 'done':
            mine = sorted(mine, key=lambda t: -_num(t.get('id')))[:DONE_SHOWN]
        else:
            mine = sorted(mine, key=lambda t: (priority.task_level(t), _num(t.get('id'))))
        out.append({'key': key, 'label': label, 'count': sum(1 for t in tasks if t.get('status') in statuses),
                    'items': [row(t, held.get(t.get('id'), '')) for t in mine]})
    return out


def get(cfg):
    """(status, answer) for GET /api/tasks: the buckets, a blocked task saying why (taskdeps)."""
    from . import backlog, taskdeps
    tasks = backlog.load(cfg)['tasks']
    return 200, {'buckets': buckets(tasks, taskdeps.holds(tasks))}
