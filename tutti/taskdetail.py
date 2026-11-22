"""Everything a backlog task has, for its expanded row on the dashboard (t237, docs/DASHBOARD.md, *Queued*).

Pure: a task dict in, short labelled facts out. Bounded (the last notes and attempts, each cut), credentials replaced
(scrub.secrets_only): every signed-in viewer sees the Queued list."""
from datetime import datetime, timezone

from . import scrub

NOTES, ATTEMPTS, CUT = 8, 10, 400


def _when(ts):
    try:
        from . import tz
        return tz.show(datetime.fromtimestamp(float(ts), timezone.utc))   # local, with its offset (never bare UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return ''


def _text(value, cut=CUT):
    return scrub.secrets_only(str(value))[:cut]


def _join(value):
    return ', '.join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value or '')


def facts(t):
    """[[label, value]] of the task's fields that are set, in reading order."""
    rows = [('Id', t.get('id')), ('Repository', t.get('repo')), ('Project', t.get('project')), ('Priority', t.get('priority')),
            ('Size', t.get('size')), ('Source', t.get('source')), ('Status', t.get('status')), ('Added', _when(t.get('added'))),
            ('Scope', _join(t.get('scope') or t.get('local_scope'))), ('Check', t.get('check')),
            ('Claimed by', t.get('claimed_by')), ('Last worker', t.get('worker')), ('Last step', t.get('step')),
            ('Model', t.get('model')), ('Last run', t.get('run')), ('Started', _when(t.get('started'))),
            ('Not before', _when(t.get('not_before'))), ('Branch', t.get('branch')), ('Parked', t.get('parked'))]
    return [[k, _text(v)] for k, v in rows if v not in (None, '', [], 0) or k == 'Priority' and v == 0]


def attempts(t):
    """The last attempts, newest first: when, where, ok and why."""
    out = []
    for a in list(t.get('attempts') or [])[-ATTEMPTS:][::-1]:
        if isinstance(a, dict):
            out.append({'at': _when(a.get('at')), 'step': _text(a.get('step') or a.get('worker') or '', 60), 'ok': bool(a.get('ok')),
                        'why': _text(a.get('why') or '', CUT)})
    return out


def detail(t):
    """{facts, criteria, notes, attempts, attempts_total, pr}: what the expanded row shows."""
    pr = t.get('pr')
    return {'facts': facts(t), 'criteria': [_text(c, 300) for c in (t.get('criteria') or [])][:12],
            'notes': [_text(n) for n in (t.get('notes') or [])[-NOTES:]], 'notes_total': len(t.get('notes') or []),
            'attempts': attempts(t), 'attempts_total': len(t.get('attempts') or []),
            'pr': pr if isinstance(pr, str) and pr.startswith('https://') and scrub.secrets_only(pr) == pr and '@' not in pr.split('/')[2]
            else None}


API = '/api/task/'


def get(cfg, path):
    """(status, answer) for GET /api/task/<id>: one backlog task's detail, read when its row is opened (not in every state
    poll: hundreds of queued tasks would make each poll megabytes)."""
    from urllib.parse import unquote

    from . import backlog
    tid = unquote(path[len(API):])
    tasks = backlog.load(cfg)['tasks']
    task = next((t for t in tasks if t.get('id') == tid), None)
    if not task:
        return 404, {'error': f'task-detail: no task "{tid}" in the backlog: it was removed; refresh the dashboard'}
    from . import taskdeps
    return 200, {'id': tid, **detail(task), 'links': taskdeps.describe(task, tasks)}


BOARD_API = '/api/board'


def get_board(cfg):
    """(status, answer) for GET /api/board: the queued tasks' Blocked, Ready and Grouped lanes with their links (t243)."""
    from . import backlog, taskdeps
    return 200, taskdeps.board(backlog.load(cfg)['tasks'])
