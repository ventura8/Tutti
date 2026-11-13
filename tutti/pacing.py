"""Usage pacing: spend every quota fully a few hours before its reset. Not earlier (work stalls), not later (quota is wasted)."""
import calendar
import json
import os
import re
from datetime import datetime, timedelta, timezone

UNITS = {'h': 'hours', 'd': 'days', 'w': 'weeks'}
TOLERANCE = 5  # percentage points either side of the line that still count as on pace


def parse_time(s):
    """ISO 8601; a bare time is UTC. Accepts a trailing Z and `2026-01-31 10:00`."""
    if isinstance(s, datetime):
        dt = s
    else:
        dt = datetime.fromisoformat(str(s).strip().replace('Z', '+00:00'))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _add_months(dt, n):
    month = dt.month - 1 + n
    year, month = dt.year + month // 12, month % 12 + 1
    return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1]))


def shift(anchor, window, n):
    """The anchor moved by n windows. window: `7d`, `5h`, `2w` or `monthly` (same day every month)."""
    if window == 'monthly':
        return _add_months(anchor, n)
    m = re.fullmatch(r'(\d+)([hdw])', str(window))
    if not m:
        raise ValueError(f'window {window!r}: use a number with h, d or w (7d, 5h) or "monthly"')
    return anchor + n * timedelta(**{UNITS[m.group(2)]: int(m.group(1))})


def window_at(anchor, window, now):
    """(start, reset) of the window that holds `now`, given any one known reset time as the anchor."""
    n = 0
    while shift(anchor, window, n) <= now:
        n += 1
    while shift(anchor, window, n - 1) > now:
        n -= 1
    return shift(anchor, window, n - 1), shift(anchor, window, n)


def pace(now, start, reset, used, lead_hours=3, share=1.0):
    """Where a quota stands. `used` is the percentage of the whole quota spent; `share` is this session's part of it.
    Returns the target time (reset minus the lead), the percentage that should be spent by now and a verdict with its action."""
    target = reset - timedelta(hours=lead_hours)
    budget = 100.0 * share
    span = (target - start).total_seconds()
    done = 1.0 if span <= 0 else min(1.0, max(0.0, (now - start).total_seconds() / span))
    expected = budget * done
    if used >= budget:
        verdict, action = 'spent', 'nothing new on this account until the reset: send work to the other workers'
    elif now >= target:
        verdict, action = 'late', f'{budget - used:.0f} points will be lost at the reset: start parallel work now'
    elif used > expected + TOLERANCE:
        verdict, action = 'ahead', 'spending too fast: shift work to cheaper workers and local models'
    elif used < expected - TOLERANCE:
        verdict, action = 'behind', 'spending too slowly: start more parallel work from the backlog'
    else:
        verdict, action = 'on pace', 'keep going'
    return {'target': target, 'budget': budget, 'expected': expected, 'verdict': verdict, 'action': action}


def read_log(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf8') as f:
        return [json.loads(line) for line in f if line.strip()]


def append_log(path, account, used, at, note=''):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'a', encoding='utf8') as f:
        f.write(json.dumps({'at': at.isoformat(), 'account': account, 'used': used, 'note': note}) + '\n')


def latest(entries, account, since=None):
    """The newest reading of an account, ignoring readings from before `since` (an earlier window says nothing about this one)."""
    best = None
    for e in entries:
        at = parse_time(e['at'])
        if e['account'] == account and (since is None or at >= since) and (best is None or at > parse_time(best['at'])):
            best = e
    return best


def _fmt(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')


def report(accounts, entries, now):
    """One row per account: used, reset, target, verdict and what to do. Times in UTC."""
    rows = [('account', 'used', 'by now', 'resets (UTC)', 'spend by (UTC)', 'verdict', 'do')]
    # A reading is the whole account's use, whoever spent it, so the pace is judged against the whole quota; an account shared
    # with a peer session shows this session's part beside the verdict.
    for name, a in accounts.items():
        a = a or {}
        src = a.get('source', 'its dashboard')
        if not a.get('resets') or not a.get('window'):
            rows.append((name, '?', '?', '?', '?', 'unknown', f'read the reset time at {src} and set accounts.{name}.resets and .window'))
            continue
        start, reset = window_at(parse_time(a['resets']), a['window'], now)
        e = latest(entries, name, start)
        if not e:
            target = reset - timedelta(hours=a.get('lead_hours', 3))
            rows.append((name, '?', '?', _fmt(reset), _fmt(target), 'no reading',
                         f'read it at {src}, then: tutti usage log {name} <percent>'))
            continue
        p = pace(now, start, reset, float(e['used']), a.get('lead_hours', 3))
        share = a.get('share', 1.0)
        mine = '' if share >= 1 else f' (shared: this session\'s part is {100 * share:g} % of the quota)'
        rows.append((name, f'{float(e["used"]):g} %', f'{p["expected"]:.0f} %', _fmt(reset), _fmt(p['target']), p['verdict'],
                     p['action'] + mine))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return '\n'.join('  '.join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows)
