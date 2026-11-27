"""Display facts shared by the dashboard and terminal. Inputs are task, shard or job records."""
from datetime import datetime

FIELDS = (
    ('ID', 'id'), ('Title', 'title'), ('Kind', 'kind'), ('Status', 'status'),
    ('Repo / Project', 'repo project'), ('Priority', 'priority'), ('Size', 'size'), ('Source', 'source'),
    ('Scope paths', 'scope local_scope'), ('Acceptance check', 'check gates'), ('Claimed by', 'claimed_by'), ('Claimed at', 'claimed_at'),
    ('Goes to', 'to target'), ('Waiting', 'why parked'), ('Worker', 'worker'), ('Model', 'model'), ('Account', 'account step'),
    ('Machine', 'machine'), ('Device', 'device'), ('Target', 'target'), ('Shard', 'key'),
    ('Branch', 'branch'), ('Commit', 'commit'), ('Worktree', 'worktree'), ('PR', 'pr'), ('CI', 'ci'),
    ('Rank', 'rank'), ('Urgent', 'urgent'), ('Added', 'added'), ('Start', 'started'),
    ('End', 'closed_at ended'), ('Updated', 'updated at'),
    ('Duration (seconds)', 'duration elapsed seconds elapsed_seconds'), ('ETA', 'eta eta_seconds'),
    ('Tokens', 'tokens'), ('Credits', 'credits'), ('Watts', 'watts'), ('Cost', 'cost'),
    ('Related', 'related related_tasks depends_on dependencies'), ('Merged into', 'merged_into'), ('Together with', 'together_with'),
    ('Run', 'run'), ('Lease', 'lease'), ('PID', 'pid'), ('Exit code', 'rc'), ('Context used', 'context_pct'),
    ('Result', 'result landing'), ('Progress', 'label'), ('Question', 'question'), ('Review notes', 'publish_note'),
    ('Tests', 'tests'), ('Checks', 'checks'), ('Files', 'files'), ('Copied', 'copied'),
    ('Log', 'log'), ('Log tail', 'tail last_line'), ('Report', 'report'), ('Handoff', 'handoff'),
)


def text_of(value):
    if isinstance(value, dict):
        return ', '.join(f'{k}: {text_of(v)}' for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return ', '.join(map(str, value))
    return str(value)


def _values(item, keys):
    values = (item.get(key) for key in keys.split())
    return dict.fromkeys(text_of(v) for v in values if v not in (None, '', [], {}))


def _attempt(a):
    result = 'passed' if a.get('ok') else f'failed: {a.get("why", "")}'
    keys = ('step', 'worker', 'model', 'started', 'at', 'class', 'signature', 'gates', 'stat')
    meta = ', '.join(f'{key}: {text_of(a[key])}' for key in keys if a.get(key))
    log = f' (log: {a["log"]})' if a.get('log') else ''
    tail = f' | tail: {a["tail"]}' if a.get('tail') else ''
    return f'run {a.get("run", "?")}: {result}{log}{tail}' + (f'\n{meta}' if meta else '')


def duration(start, end):
    if isinstance(start, str) and isinstance(end, str):
        try:
            start, end = (datetime.fromisoformat(value).timestamp() for value in (start, end))
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
        return max(0, round(end - start))
    return None


def facts(item):
    """Show recorded fields and retain the complete notes and attempt histories."""
    data = {**(item.get('facts') or {}), **(item.get('progress') or {}), **item}
    if isinstance(data.get('tail'), list):
        data['tail'] = '\n'.join(data['tail'])
    if data.get('duration') is None:
        data['duration'] = duration(data.get('started'), data.get('closed_at') or data.get('ended'))
    out = [(label, ' · '.join(values)) for label, keys in FIELDS if (values := _values(data, keys))]
    if notes := item.get('notes'):
        out.append((f'Notes history ({len(notes)})', '\n'.join(map(str, notes))))
    attempts = item.get('attempts') or item.get('history')
    if isinstance(attempts, list) and attempts:
        out.append((f'Attempts and logs ({len(attempts)})', '\n\n'.join(map(_attempt, attempts))))
        out.extend((f'Attempt log · {a["run"]}', a.get('tail') or a['log'], a['run'])
                   for a in attempts if a.get('run') and (a.get('log') or a.get('tail')))
    elif isinstance(attempts, int):
        out.append(('Attempts', str(attempts)))
    return out
