"""Usage pacing: spend every quota fully about an hour before its reset. Not earlier (work stalls), not later (quota is wasted)."""
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


def pace(now, start, reset, used, lead_hours=1, share=1.0):
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


NO_READING = 'no reading'
PANIC_AT = 95  # percent of the whole quota: at or over it before the target time, the account is in panic mode


def _fmt(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')


def panic(used, now, target, threshold=PANIC_AT):
    """Quota panic: the account is at or over `threshold` % while the target time is still ahead. Nothing new goes to it;
    bounded work moves to accounts with room and to local models until the reset."""
    return used >= threshold and now < target


def readings(entries, account, since):
    """The account's readings in the current window, oldest first: [{at, used}]."""
    out = [(parse_time(e['at']), float(e['used'])) for e in entries if e['account'] == account and parse_time(e['at']) >= since]
    return [{'at': at.isoformat(), 'used': used} for at, used in sorted(out)]


def account_state(name, a, entries, now):
    """Everything known about one account now, as plain data: the window, the latest reading, the pace and panic.
    `state` is `ok`, `no reset` (the reset time or window is not in the config) or `no reading` (none in this window)."""
    a = a or {}
    src = a.get('source', 'its dashboard')
    out = {'name': name, 'source': src, 'window': a.get('window'), 'share': a.get('share', 1.0),
           'lead_hours': a.get('lead_hours', 1), 'panic_at': a.get('panic_at', PANIC_AT), 'used': None, 'expected': None,
           'verdict': 'unknown', 'panic': False, 'reset': None, 'target': None, 'window_start': None, 'reading_at': None, 'readings': []}
    if not a.get('resets') or not a.get('window'):
        return {**out, 'state': 'no reset', 'action': f'read the reset time at {src} and set accounts.{name}.resets and .window'}
    start, reset = window_at(parse_time(a['resets']), a['window'], now)
    target = reset - timedelta(hours=out['lead_hours'])
    out.update(reset=reset.isoformat(), target=target.isoformat(), window_start=start.isoformat(),
               readings=readings(entries, name, start))
    e = latest(entries, name, start)
    if not e:
        return {**out, 'state': NO_READING, 'verdict': NO_READING,
                'action': f'read it at {src}, then: tutti usage log {name} <percent>'}
    used = float(e['used'])
    p = pace(now, start, reset, used, out['lead_hours'])
    is_panic = panic(used, now, target, out['panic_at'])
    action = p['action']
    if is_panic and p['verdict'] != 'spent':
        action = (f'panic: {used:g} % used before the target time: nothing new on {name}; '
                  'move bounded work to other accounts and local models')
    return {**out, 'state': 'ok', 'used': used, 'expected': round(p['expected'], 1), 'verdict': p['verdict'], 'action': action,
            'panic': is_panic, 'reading_at': parse_time(e['at']).isoformat()}


def report(accounts, entries, now):
    """One row per account: used, reset, target, verdict and what to do. Times in UTC."""
    rows = [('account', 'used', 'by now', 'resets (UTC)', 'spend by (UTC)', 'verdict', 'do')]
    # A reading is the whole account's use, whoever spent it, so the pace is judged against the whole quota; an account shared
    # with a peer session shows this session's part beside the verdict.
    for name, a in accounts.items():
        s = account_state(name, a, entries, now)
        if s['state'] == 'no reset':
            rows.append((name, '?', '?', '?', '?', 'unknown', s['action']))
            continue
        reset, target = _fmt(parse_time(s['reset'])), _fmt(parse_time(s['target']))
        if s['state'] == NO_READING:
            rows.append((name, '?', '?', reset, target, NO_READING, s['action']))
            continue
        mine = '' if s['share'] >= 1 else f' (shared: this session\'s part is {100 * s["share"]:g} % of the quota)'
        rows.append((name, f'{s["used"]:g} %', f'{s["expected"]:.0f} %', reset, target, s['verdict'], s['action'] + mine))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return '\n'.join('  '.join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows)
