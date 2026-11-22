"""Idle with work (t254, docs/DASHBOARD.md, *Queued*): a machine with a free slot, or an account behind its pace, while queued
work waits. Tutti's rules (presence, quiet hours, owner share, heat, battery, pacing) are already in those numbers: a
machine's `free` is what its share allows now, and an account behind its pace has room it should spend. So each case found
here is either a reason the queue gives (shown with it) or a bug to fix; nothing is left silent.

Pure: the dashboard's snapshot in, findings out."""
from collections import Counter

MAX_REASONS = 3


def queued_tasks(snap):
    return [i for g in ((snap.get('queue') or {}).get('groups') or []) for i in g.get('items') or [] if i.get('kind') == 'task']


def reasons(tasks):
    """The most common reasons the queued tasks give, most common first: [(why, count)]."""
    return Counter(str(t.get('why') or 'no reason given') for t in tasks).most_common(MAX_REASONS)


def idle_resources(snap):
    """Machines with a free slot now and accounts behind their pace (room to spend)."""
    out = [{'kind': 'machine', 'name': m['name'], 'room': f'{m["free"]} free slot{"s" if m["free"] != 1 else ""}'}
           for m in ((snap.get('machines') or {}).get('items') or []) if m.get('status') == 'free' and (m.get('free') or 0) > 0]
    out += [{'kind': 'account', 'name': a['name'], 'room': f'behind its pace ({a.get("used", 0):.0f} % used)'}
            for a in ((snap.get('usage') or {}).get('accounts') or [])
            if a.get('verdict') == 'behind' and not a.get('panic') and not a.get('paused') and isinstance(a.get('used'), (int, float))]
    return out


def find(snap):
    """{idle: [...], waiting: n, reasons: [[why, n]], headline} when queued work waits while something has room, else None."""
    tasks = queued_tasks(snap)
    idle = idle_resources(snap)
    if not tasks or not idle:
        return None
    why = reasons(tasks)
    if not (snap.get('autopilot') or {}).get('enabled', True):
        why = [('the autopilot is off: the Autopilot card or `tutti autopilot on` starts it', len(tasks)), *why][:MAX_REASONS]
    names = ', '.join(r['name'] for r in idle[:4]) + (f' and {len(idle) - 4} more' if len(idle) > 4 else '')
    return {'idle': idle, 'waiting': len(tasks), 'reasons': [[w, n] for w, n in why],
            'headline': f'Idle with work: {names} could take some of the {len(tasks)} queued task{"s" if len(tasks) != 1 else ""}'}


LOG_KEEP, LOG_SHOWN = 500, 5


def signature(found):
    """What makes two findings the same case: the idle names and the top reason. A new case is logged; a repeat is not."""
    if not found:
        return ''
    names = sorted(f'{r["kind"]}:{r["name"]}' for r in found['idle'])
    return '|'.join(names) + '#' + (found['reasons'][0][0] if found['reasons'] else '')


def entry(found, at):
    """One line of the idle-with-work log: when, who had room, how many waited and the top reason."""
    return {'at': at, 'idle': [f'{r["name"]} ({r["kind"]})' for r in found['idle']], 'waiting': found['waiting'],
            'why': found['reasons'][0][0] if found['reasons'] else '', 'sig': signature(found)}


def keep(lines, new):
    """The log after `new` (an entry, or None when nothing is idle): a case is appended when it starts (a different case, or
    the same one again after it had ended); `None` marks the last case ended. Bounded to the last LOG_KEEP."""
    lines = [e for e in lines if isinstance(e, dict)]
    last = lines[-1] if lines else None
    if new is None:
        if last and not last.get('ended'):
            lines = [*lines[:-1], {**last, 'ended': True}]
    elif not last or last.get('ended') or last.get('sig') != new['sig']:
        lines = [*lines, new]
    return lines[-LOG_KEEP:]
