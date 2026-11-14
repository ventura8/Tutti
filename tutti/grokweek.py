"""A scout's week: what one run really costs of its weekly allowance, and how many runs it may send now so the week ends near
`stop_at` % (95 by default) instead of mostly unused. Pure: readings, requests and times in, numbers out.

Cost. A run's cost is measured, never assumed: between two real readings of the scout's account (not Tutti's own estimates)
in the same window, the rise over the runs sent in between. Pairs are summed (total rise / total runs) once they cover at least
MIN_RUNS runs; until then the account's `task_pct` is the guess. The first reading of a window counts from 0 at its start.

Pace. The allowance is spread evenly over the window: by `now` the scout may have used `stop_at` x the share of the window gone
(an hour ahead, so a round never waits for the line to pass it). In the last `burst_hours` before the target the line is
`stop_at` itself: whatever is left goes out as fast as the caps allow (the scout runs in its own cloud, so parallel runs cost
the household nothing)."""
from .pacing import TASK_PCT, parse_time

MIN_RUNS = 3          # runs between real readings before their rise is trusted as the cost
AHEAD_SECONDS = 3600  # the even line is read an hour ahead
KINDS = ('review', 'triage', 'notes', 'check', 'research', 'second')


def _real(entries, account, since=None):
    out = [e for e in entries if e['account'] == account and e.get('source') != 'estimate']
    out = [e for e in out if since is None or parse_time(e['at']).timestamp() >= since]
    return sorted(out, key=lambda e: parse_time(e['at']))


def _sent_at(r):
    return r.get('sent') if r.get('status') not in ('queued', 'failed') else None


def cost(entries, requests, account, scout, fallback=TASK_PCT, window_start=None):
    """{pct, runs, rise, measured}: the points one run costs. `window_start` (epoch seconds): the current window's start, so
    its first reading counts from 0 there; earlier windows' pairs count too (a run costs the same every week)."""
    reals = _real(entries, account)
    sent = sorted(t for t in (_sent_at(r) for r in requests if r['scout'] == scout) if t)
    points = [(parse_time(e['at']).timestamp(), float(e['used'])) for e in reals]
    if window_start is not None:
        points = sorted([*points, (window_start, 0.0)]) if not any(abs(t - window_start) < 1 for t, _ in points) else points
    rise = runs = 0.0
    for (t0, u0), (t1, u1) in zip(points, points[1:], strict=False):
        if u1 < u0:   # a reset in between: the pair says nothing
            continue
        n = sum(1 for t in sent if t0 < t <= t1)
        if n:
            rise, runs = rise + (u1 - u0), runs + n
    if runs >= MIN_RUNS and rise > 0:
        return {'pct': round(rise / runs, 3), 'runs': int(runs), 'rise': round(rise, 2), 'measured': True}
    return {'pct': float(fallback), 'runs': int(runs), 'rise': round(rise, 2), 'measured': False}


def line(acc, now, stop_at, burst_hours):
    """The % the scout may have used by now (an hour ahead): stop_at x the window gone; stop_at in the burst stretch."""
    start, target = parse_time(acc['window_start']).timestamp(), parse_time(acc['target']).timestamp()
    if now >= target - burst_hours * 3600:
        return float(stop_at)
    share = min(1.0, max(0.0, (now + AHEAD_SECONDS - start) / max(1.0, target - start)))
    return stop_at * share


def budget(acc, now, pct, stop_at, burst_hours):
    """{mode, now, left, line}: runs it may send now (to the line), runs left before stop_at, and the mode: `burst` in the last
    hours, `pace` while under the line, `ahead` when at or over it, `stop` at stop_at, `off` with no reading."""
    if not acc or acc.get('state') != 'ok' or acc.get('used') is None:
        return {'mode': 'off', 'now': 0, 'left': 0, 'line': None}
    used = float(acc['used'])
    left = max(0, int((stop_at - used) / pct + 1e-9)) if pct > 0 else 0
    if used >= stop_at:
        return {'mode': 'stop', 'now': 0, 'left': 0, 'line': stop_at}
    at = line(acc, now, stop_at, burst_hours)
    burst = now >= parse_time(acc['target']).timestamp() - burst_hours * 3600
    room = max(0, int((at - used) / pct + 1e-9)) if pct > 0 else 0
    mode = 'burst' if burst else ('pace' if room > 0 else 'ahead')
    return {'mode': mode, 'now': min(room, left), 'left': left, 'line': round(at, 1)}


def kind_of_request(r):
    """The kind of work a request was: review, triage (Sentry), notes (dependencies), check (live site), research, second."""
    if r.get('kind') == 'review':
        return 'review'
    if r.get('second_opinion'):
        return 'second'
    w = str(r.get('watch') or '')
    if w.startswith('sentry-'):
        return 'triage'
    if w:
        from .spendrest import NOTES_RX  # spendrest imports this module
        return 'notes' if NOTES_RX.search(w) else 'check'
    return 'research'


def today(requests, scout, now):
    """{kind: runs sent} since the start of today (the day of the epoch clock, as the meter counts it)."""
    day = now - now % 86400
    out = {}
    for r in requests:
        t = _sent_at(r) if r['scout'] == scout else None
        if t and t >= day:
            k = kind_of_request(r)
            out[k] = out.get(k, 0) + 1
    return out


def mix(left, days, reviews, triage, notes, checks, research):
    """The week's plan of `left` runs over `days`: what the regular work takes (reviews of open pull requests, a triage per
    Sentry project a day, notes, live checks, the research questions queued), the rest as research and extra checks."""
    want = {'review': reviews, 'triage': int(triage * days), 'notes': int(notes * max(1, days / 7)), 'check': int(checks * days),
            'research': research}
    out, room = {}, left
    for k in ('review', 'triage', 'notes', 'check', 'research'):
        out[k] = min(room, want[k])
        room -= out[k]
    out['research'] += room   # nothing of value is left unasked: the rest goes to research and second opinions
    return out


def text(b, c, mixed, done):
    """The card's line: about N runs left this week with the mix, and what it did today."""
    if b['mode'] == 'off':
        return 'no reading of its allowance yet: read the meter once (Add a reading) and the week is planned from it'
    each = f'{c["pct"]:g} % each, {"measured over " + str(c["runs"]) + " runs" if c["measured"] else "not measured yet"}'
    plan = ' / '.join(f'{n} {k}' for k, n in mixed.items() if n) or 'nothing'
    did = ', '.join(f'{n} {k}' for k, n in done.items()) or 'nothing yet'
    when = {'burst': 'burst before the reset: sends all it can', 'pace': f'{b["now"]} can go now',
            'ahead': 'ahead of its even line: waits', 'stop': 'at its stop: sends nothing more'}[b['mode']]
    return f'about {b["left"]} runs left this week ({each}): {plan}; {when}; today: {did}'
