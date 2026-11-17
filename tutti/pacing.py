"""Usage pacing: spend every quota fully about an hour before its reset. Not earlier (work stalls), not later (quota is wasted)."""
import calendar
import json
import re
from datetime import datetime, timedelta, timezone

from . import durable

UNITS = {'h': 'hours', 'd': 'days', 'w': 'weeks'}
TOLERANCE = 5  # at most this many percentage points either side of the line still count as on pace
TOLERANCE_PART = 0.25  # early in a window the band is a quarter of the line (at least 1 point): 10 % against 6 % is ahead


def tolerance(expected):
    """The on-pace band around the line: a quarter of it, at least 1 point and at most TOLERANCE points."""
    return min(TOLERANCE, max(1.0, expected * TOLERANCE_PART))


def parse_time(s):
    """ISO 8601; a bare time is UTC. Accepts a trailing Z, an offset, `2026-01-31 10:00` and a stated IANA zone,
    `2026-01-31 10:00 Europe/Bucharest` (that zone's wall time, daylight saving included)."""
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    text, zone = str(s).strip(), None
    head, _, last = text.rpartition(' ')
    if head and '/' in last:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            text, zone = head.strip(), ZoneInfo(last)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f'{last!r} is not a time zone: write an IANA name such as Europe/Bucharest') from e
    dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if zone is not None and not dt.tzinfo:
        return dt.replace(tzinfo=zone).astimezone(timezone.utc)
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


def target_of(name, a, owner=None):
    """The % of its quota an account aims to have used by its spend-by time (reset minus lead): accounts.<name>.target_pct,
    else main.target_pct for the main agent's accounts, else 100. A lower target leaves headroom on purpose."""
    v = (a or {}).get('target_pct')
    if v is None and owner and name in (owner.get('accounts') or ()):
        v = owner.get('target_pct')
    return float(100 if v is None else v)


def pace(now, start, reset, used, lead_hours=1, share=1.0):
    """Where a quota stands. `used` is the percentage of the whole quota spent; `share` is this session's part of it.
    Returns the target time (reset minus the lead), the percentage that should be spent by now and a verdict with its action."""
    target = reset - timedelta(hours=lead_hours)
    budget = 100.0 * share
    span = (target - start).total_seconds()
    done = 1.0 if span <= 0 else min(1.0, max(0.0, (now - start).total_seconds() / span))
    expected = budget * done
    band = tolerance(expected)
    if used >= budget:
        verdict, action = 'spent', 'nothing new on this account until the reset: send work to the other workers'
    elif now >= target:
        verdict, action = 'late', f'{budget - used:.0f} points will be lost at the reset: start parallel work now'
    elif used > expected + band:
        verdict, action = 'ahead', 'spending too fast: shift work to other workers and local models'
    elif used < expected - band:
        verdict, action = 'behind', 'spending too slowly: start more parallel work from the backlog'
    else:
        verdict, action = 'on pace', 'keep going'
    return {'target': target, 'budget': budget, 'expected': expected, 'tolerance': band, 'verdict': verdict, 'action': action}


def read_log(path):
    return durable.read_jsonl(path)   # a line torn by a power-off is skipped (tutti/durable.py)


def append_log(path, account, used, at, note='', resets=None, source=None, ref=None):
    """Appends one reading. `resets`: the reset time the reading names (it then anchors the account's window); `source`: who
    read it (`reader`, `limit message`, `dashboard`; none for `tutti usage log`); `ref`: where a limit message was seen."""
    entry = {'at': at.isoformat(), 'account': account, 'used': used, 'note': note}
    if resets is not None:
        entry['resets'] = parse_time(resets).isoformat()
    entry.update({k: v for k, v in (('source', source), ('ref', ref)) if v})
    durable.append_line(path, json.dumps(entry))
    return entry


def reading_anchor(entries, account):
    """The reset time named by the account's newest reading that names one, or None. It beats the config's `resets`: a
    reading (the CLI's own answer, a limit message, the owner's form) knows the real window, the config only a first guess."""
    best = None
    for e in entries:
        if e.get('account') == account and e.get('resets') and (best is None or parse_time(e['at']) > parse_time(best['at'])):
            best = e
    return parse_time(best['resets']) if best else None


def latest(entries, account, since=None):
    """The newest reading of an account, ignoring readings from before `since` (an earlier window says nothing about this one)."""
    best = None
    for e in entries:
        at = parse_time(e['at'])
        if e['account'] == account and (since is None or at >= since) and (best is None or at > parse_time(best['at'])):
            best = e
    return best


STALE_HOURS = 2   # a reading typed or written by hand is old after this; the dashboard and `tutti usage` remind
NO_READING = 'no reading'
PANIC_AT = 95  # percent of the whole quota: at or over it before the target time, the account is in panic mode


def _fmt(dt):
    from tutti import tz  # local time with its offset: 'Sun 15 Nov 02:00 (UTC+2)' (stored times stay UTC)
    return tz.show(dt)


def panic(used, now, target, threshold=PANIC_AT):
    """Quota panic: the account is at or over `threshold` % while the target time is still ahead. Nothing new goes to it;
    bounded work moves to accounts with room and to local models until the reset."""
    return used >= threshold and now < target


def readings(entries, account, since):
    """The account's readings in the current window, oldest first: [{at, used}]."""
    out = [(parse_time(e['at']), float(e['used'])) for e in entries if e['account'] == account and parse_time(e['at']) >= since]
    return [{'at': at.isoformat(), 'used': used} for at, used in sorted(out)]


def no_reading_action(name, a, src):
    if a.get('reader'):
        return 'read automatically at every probe of `tutti dashboard`; `tutti usage refresh` reads it now'
    tap = ', or Add a reading on the dashboard' if a.get('usage_url') else ''
    return f'read it at {src}, then: tutti usage log {name} <percent>{tap}'


TASK_PCT = 2   # what one bounded delegated task costs, in points of a quota (accounts.<name>.task_pct): a first guess


def room_tasks(p, used, task_pct):
    """How many bounded tasks an account behind its line (or late) can take now without going over the line: the points it
    is short of, divided by the cost of one task. None when it is on pace or ahead."""
    if not task_pct:
        return None
    if p['verdict'] == 'on pace':   # inside the band but under the line: top up to it, so the window ends at its target
        short = p['expected'] - used
        return int(short // task_pct) or None if short >= task_pct else None
    if p['verdict'] not in ('behind', 'late'):
        return None
    short = (p['budget'] - used) if p['verdict'] == 'late' else (p['expected'] - used)
    return max(1, int(short // task_pct))


def staleness(name, a, reading_at, now):
    """(stale, the reminder or '') for an account no reader reads on its own: no reading, or the last one older than
    accounts.<name>.stale_hours (default 2). An account with a reader command is read automatically: never stale (a plan
    command gives only a label, not the % used, so it does not count)."""
    if a.get('reader') or a.get('stale_hours') == 0:
        return False, ''
    hours = float(a.get('stale_hours', STALE_HOURS))
    if reading_at is not None and (now - reading_at).total_seconds() < hours * 3600:
        return False, ''
    age = 'no reading yet' if reading_at is None else f'the last reading is {(now - reading_at).total_seconds() / 3600:.1f} h old'
    where = f' (or write it to {a["reading_file"]})' if a.get('reading_file') else ''
    return True, f'{name}: {age}: tutti usage log {name} <percent>{where}'


AUTO_SOURCES = ('reader', 'file', 'helper')


def freshness(name, a, newest, now):
    """How the account's newest reading came in and whether it needs a reminder: {stale, remind, auto, reading_source}."""
    at = parse_time(newest['at']) if newest else None
    stale, remind = staleness(name, a, at, now)
    source = newest.get('source') if newest else None
    return {'stale': stale, 'remind': remind, 'auto': bool(a.get('reader')) or source in AUTO_SOURCES, 'reading_source': source,
            'needs_reading': needs_reading(a, at, now)}


def needs_reading(a, reading_at, now):
    """True when the dashboard offers Add a reading: nothing reads the account on its own (no reader command and no
    reading_file an orchestrator writes), or its newest reading is older than stale_hours (default 2) or missing."""
    if not (a.get('reader') or a.get('reading_file')):
        return True
    hours = float(a.get('stale_hours') or STALE_HOURS)
    return reading_at is None or (now - reading_at).total_seconds() >= hours * 3600


def account_action(name, a, p, used, is_panic, room):
    """What to do about the account now: the pace's action, the room for bounded tasks, or panic."""
    if is_panic and p['verdict'] != 'spent':
        return (f'panic: {used:g} % used before the target time: nothing new on {name}; '
                'move bounded work to other accounts and local models')
    if room:
        return p['action'] + f'; use it: about {room} bounded task(s) can go to {a.get("worker") or name} now'
    return p['action']


def plan_args(cfg):
    """What account_state needs from the config for the planner: every account (for `limits`), the quiet hours, and the
    owner's schedule and mode (tutti/owner.py; the mode and the dashboard's edits are read from the state folder)."""
    from tutti import config, owner
    return cfg['accounts'], cfg['autopilot'].get('quiet_hours'), owner.owner_of(cfg, config.state_dir(cfg))


def owner_readings(entries, names, now, weeks=4):
    """The owner accounts' readings of the last `weeks`, oldest first, for the learned profile."""
    since = now - timedelta(weeks=weeks)
    return [{'at': e['at'], 'used': float(e['used'])} for e in entries
            if e['account'] in names and e.get('source') != 'estimate' and parse_time(e['at']) >= since]


def account_state(name, a, entries, now, accounts=None, quiet_hours=None, owner=None):
    """Everything known about one account now, as plain data: the window, the latest reading, the pace and panic.
    `state` is `ok`, `no reset` (the reset time or window is not in the config) or `no reading` (none in this window).
    With `accounts` (the config's), the planner (tutti/planner.py) adds its `plan`: an account's `limits` (a 5 h window's
    weekly quota) bound it, and what expires soonest is spent first."""
    a = a or {}
    if accounts is not None:
        from tutti import owner as owner_mod
        from tutti import planner  # planner imports pacing: imported here, when a plan is wanted
        a = {**a, 'target_pct': target_of(name, a, owner)}
        own = account_state(name, {k: v for k, v in a.items() if k != 'limits'}, entries, now)
        ctx = owner_mod.context(owner, owner_readings(entries, owner['accounts'], now), now) \
            if owner and name in owner['accounts'] else None
        if not a.get('limits'):
            return planner.plan_alone(own, a, now, owner=ctx)
        lims = [account_state(n, accounts.get(n), entries, now) for n in a['limits'] if n in accounts]
        return planner.bind(own, lims, a, accounts, quiet_hours, now, owner=ctx)
    src = a.get('source', 'its dashboard')
    out = {'name': name, 'source': src, 'window': a.get('window'), 'share': a.get('share', 1.0), 'usage_url': a.get('usage_url'),
           'tutti_browser': bool(a.get('usage_url') and not a.get('reader')),
           'reset_from': None,
           'lead_hours': a.get('lead_hours', 1), 'panic_at': a.get('panic_at', PANIC_AT), 'used': None, 'expected': None,
           'verdict': 'unknown', 'panic': False, 'reset': None, 'target': None, 'window_start': None, 'reading_at': None, 'readings': [],
           'stale': False, 'remind': '', 'auto': False, 'reading_source': None, 'room_tasks': None, 'note': a.get('note'),
           'needs_reading': True, 'target_pct': target_of(name, a)}
    anchor = reading_anchor(entries, name)
    if anchor:
        out['reset_from'] = 'reading'
    elif a.get('resets'):
        anchor = parse_time(a['resets'])
        out['reset_from'] = 'estimate' if a.get('resets_estimated') else 'config'
    if not anchor or not a.get('window'):
        return {**out, 'state': 'no reset', 'action': f'read the reset time at {src} and set accounts.{name}.resets and .window'}
    start, reset = window_at(anchor, a['window'], now)
    target = reset - timedelta(hours=out['lead_hours'])
    out.update(reset=reset.isoformat(), target=target.isoformat(), window_start=start.isoformat(),
               readings=readings(entries, name, start))
    e = latest(entries, name, start)
    out.update(freshness(name, a, latest([x for x in entries if x.get('source') != 'estimate'], name), now))
    if not e and latest(entries, name):
        # a reset passed since the last reading: the new window starts from 0 at once, so queued work goes on with no gap
        # (and no stale "nothing more this window" decision); the next real reading replaces this
        e, out['after_reset'] = {'at': start.isoformat(), 'used': 0.0}, True
    if not e:
        return {**out, 'state': NO_READING, 'verdict': NO_READING, 'action': no_reading_action(name, a, src)}
    used = float(e['used'])
    p = pace(now, start, reset, used, out['lead_hours'], out['target_pct'] / 100)
    is_panic = panic(used, now, target, out['panic_at'])
    out['room_tasks'] = room_tasks(p, used, a.get('task_pct', TASK_PCT))
    action = account_action(name, a, p, used, is_panic, out['room_tasks'])
    from tutti import owner as owner_mod  # owner imports pacing
    out['projected_end'] = owner_mod.projection(out['readings'], used, now, reset)
    return {**out, 'state': 'ok', 'used': used, 'expected': round(p['expected'], 1), 'tolerance': round(p['tolerance'], 1),
            'verdict': p['verdict'], 'action': action,
            'panic': is_panic, 'reading_at': parse_time(e['at']).isoformat(), 'selected_source': e.get('source')}


def report(accounts, entries, now, quiet_hours=None, states=None):
    """One row per account: used, reset, target, verdict and what to do. Local times with the offset. `states`: the accounts' states as the
    dashboard has them (tutti/state.py: the owner's reserve, scouts), else computed here."""
    by_name = {s['name']: s for s in states or []}
    rows = [('account', 'used', 'by now', 'resets', 'spend by', 'verdict', 'do')]
    # A reading is the whole account's use, whoever spent it, so the pace is judged against the whole quota; an account shared
    # with a peer session shows this session's part beside the verdict.
    for name, a in accounts.items():
        s = by_name.get(name) or account_state(name, a, entries, now, accounts, quiet_hours)
        if s['state'] == 'no reset':
            rows.append((name, '?', '?', '?', '?', 'unknown', s['action']))
            continue
        reset, target = _fmt(parse_time(s['reset'])), _fmt(parse_time(s['target']))
        if s['state'] == NO_READING:
            rows.append((name, '?', '?', reset, target, NO_READING, s['action']))
            continue
        mine = '' if s['share'] >= 1 else (f' (shared: this session\'s part is {100 * s["share"]:g} % of the quota; the line is the '
                                           'whole account\'s, since a reading counts every session)')
        mine += f' [{s["used"]:g} % vs {s["expected"]:g} % by now; the on-pace band is ±{s["tolerance"]:g}]'
        rows.append((name, f'{s["used"]:g} %', f'{s["expected"]:.0f} %', reset, target, s['verdict'], s['action'] + mine))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return '\n'.join('  '.join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows)
