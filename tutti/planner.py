"""The quota planner: spend what expires soonest first, so as little quota as possible expires unused.

An account with `limits` (agy's 5 h windows under their weekly quota) is planned on the timeline of its windows left until
the limit's target (its reset minus the lead). The limit's room (its budget minus the owner's `reserve` minus what is used)
is shared over the windows that can run (not in `autopilot.quiet_hours`), evenly, except that a window must take whatever the
windows after it cannot hold: allowance = max(room / runnable windows, room - capacity of the later runnable windows),
never more than the window itself has left. A window that cannot run gets nothing and its share moves to those that can.
An account without limits has one window: its room until its target. Re-planned at every probe from fresh readings.

Pure: datetimes in, plain dicts out. Points are percentages of the limit's quota unless named `own`."""
from datetime import timedelta

from tutti.pacing import TASK_PCT, parse_time


def quiet(at, hours):
    """Whether `at` (UTC) falls in one of `hours`, e.g. ['22:00-06:00'] (a span past midnight wraps)."""
    minute = at.hour * 60 + at.minute
    for span in hours or []:
        a, b = (int(x[:2]) * 60 + int(x[3:5]) for x in span.split('-'))
        if (a <= minute < b) if a <= b else (minute >= a or minute < b):
            return True
    return False


def runnable(start, end, hours):
    """A window can run when some hour of it is outside the quiet hours (checked every 30 minutes)."""
    t = start
    while t < end:
        if not quiet(t, hours):
            return True
        t += timedelta(minutes=30)
    return False


def used_at(lim, at):
    """(the limit's % used when a window started, known): its newest reading at or before `at`, else 0 when its own window
    began after `at` (it reset since); else its first reading after `at`, the best guess, not known."""
    rs = lim['readings']
    before = [r for r in rs if parse_time(r['at']) <= at]
    if before:
        return before[-1]['used'], True
    if parse_time(lim['window_start']) >= at or not rs:
        return 0.0, True
    return rs[0]['used'], False


def window_points(own, lim, at_start, configured=None, known=True):
    """What a whole short window costs of the limit, in its points: `accounts.<name>.window_points` when set, else measured in
    this window (the limit's rise over the window's own rise, once the window rose 5 points), else None (not known yet)."""
    if configured:
        return float(configured)
    rise = own['used'] or 0
    if known and rise >= 5 and lim['used'] > at_start:
        return max(0.0, lim['used'] - at_start) * 100 / rise
    return None


def allowance(room, capacities, now_index=0):
    """The points window `now_index` may spend: room spread over the runnable windows from it on, at least what the later
    runnable windows cannot hold, at most its own capacity. capacities: one per window, None = unknown (no cap), 0 = cannot run."""
    later = capacities[now_index + 1:]
    runs = 1 + sum(1 for c in later if c != 0)
    if capacities[now_index] == 0:
        return 0.0
    hold = None if any(c is None for c in later) else sum(later)
    share = max(room / runs, room - hold if hold is not None else 0.0, 0.0)
    cap = capacities[now_index]
    return min(share, cap) if cap is not None else share


def timeline(own, lim, hours, per_window):
    """The windows from the current one to the limit's target: [(start, end, capacity)], each at its full capacity."""
    start, reset = parse_time(own['window_start']), parse_time(own['reset'])
    span = reset - start
    target = parse_time(lim['target'])
    out, t = [], start
    while t < target:
        end = min(t + span, target)
        cap = None if per_window is None else per_window * (end - t) / span   # whole windows: what is spent is subtracted once, later
        out.append((t, end, cap if runnable(max(t, start), end, hours) else 0.0))
        t += span
    return out or [(start, reset, None)]


def plan_limited(own, lim, a, lim_cfg, hours, owner=None, now=None):
    """The plan of one short window under its limit: {expires, allowance, this_window, left, tasks, lose, why}. With `owner`
    (tutti/owner.py) the owner's expected burn until the limit's target is kept out of the budget."""
    from tutti import owner as owner_mod
    start = parse_time(own['window_start'])
    kept = owner_mod.reserve_until(owner, now, parse_time(lim['target'])) if owner and now else 0.0
    budget = 100.0 * lim['share'] - float(lim_cfg.get('reserve', 0)) - kept
    at, known = used_at(lim, start)
    at_start = min(budget, at)
    per = window_points(own, lim, at_start, a.get('window_points'), known)
    windows = timeline(own, lim, hours, per)
    room = max(0.0, budget - at_start)
    give = allowance(room, [c for _, _, c in windows])
    spent = max(0.0, lim['used'] - at_start)
    left = give - spent
    own_left = 100 - (own['used'] or 0)
    left_own = own_left if per is None else min(own_left, left * 100 / per) if per else 0.0
    task = float(a.get('task_pct', TASK_PCT))
    tasks = max(0, int(left_own // task)) if left > 0 and task > 0 else 0
    return {'limit': lim['name'], 'expires': own['target'], 'resets': own.get('reset'),
            'windows': len(windows), 'runnable': sum(1 for *_, c in windows if c != 0),
            'per_window': round(room / max(1, len(windows)), 1), 'allowance': round(give, 1), 'this_window': round(spent, 1),
            'left': round(left, 1), 'tasks': tasks, 'lose': round(max(0.0, left_own), 1), 'window_points': per,
            'owner_reserve': round(kept, 1) if owner else None}


def background_rate(readings, busy):
    """Points per hour an account rises with no Tutti run on it (Cursor's own automatic reviews, the owner's IDE): the rises
    between consecutive readings whose span overlaps no run in `busy` [(start, end) datetimes], over those spans' hours.
    None until at least an hour of such spans is known."""
    rise = hours = 0.0
    pts = [(parse_time(x['at']), x['used']) for x in readings]
    for (t0, u0), (t1, u1) in zip(pts, pts[1:], strict=False):
        if u1 < u0 or any(b0 < t1 and b1 > t0 for b0, b1 in busy):
            continue
        rise += u1 - u0
        hours += (t1 - t0).total_seconds() / 3600
    return rise / hours if hours >= 1 else None


def plan_single(s, a, now, rate=None, owner=None):
    """An account without limits: one window, its room (minus the reserve and the background spend expected until its
    target) until its target. With `owner` (the main agent's account, tutti/owner.py) the owner's expected burn until the
    target is kept too, and the rest is spent evenly (owner.spendable) instead of only while behind the even-pace line."""
    from tutti import owner as owner_mod
    target = parse_time(s['target'])
    hours_left = max(0.0, (target - now).total_seconds() / 3600)
    background = (rate or 0.0) * hours_left
    kept = owner_mod.reserve_until(owner, now, target) if owner else 0.0
    budget = 100.0 * s['share'] - float(a.get('reserve', 0)) - background - kept
    left = max(0.0, budget - (s['used'] or 0))
    task = float(a.get('task_pct', TASK_PCT))
    if owner:
        tasks = int(owner_mod.spendable(left, now, target) // task) if task > 0 else 0
    else:
        room = s.get('room_tasks') or 0   # the pace's own room (behind its line); never more than the left points hold
        tasks = min(room, int(left // task)) if task > 0 else 0
    return {'limit': None, 'expires': s['target'], 'resets': s.get('reset'), 'left': round(left, 1), 'lose': round(left, 1), 'tasks': tasks,
            'allowance': round(left, 1), 'this_window': None, 'windows': 1, 'runnable': 1,
            'background': None if rate is None else round(background, 1), 'owner_reserve': round(kept, 1) if owner else None,
            'mode': owner['mode'] if owner else None}


def _at(t, now):
    hours = max(0.0, (t - now).total_seconds() / 3600)
    return f'{t:%H:%M} UTC (in {hours:.1f} h)' if hours < 24 else f'{t:%a %d %b %H:%M} UTC'


def explain(name, p, now):
    """One line for the dashboard: the spend-by time (the reset minus the lead: what `expires` is), the reset itself (as the
    meter says it), what it may still spend, what would be lost and what Tutti does."""
    when = _at(parse_time(p['expires']), now)
    if p.get('resets'):
        when += f', resets {_at(parse_time(p["resets"]), now)}'
    if p['limit']:
        head = (f'{name}: spend by {when}; this window: {p["this_window"]:g} of {p["allowance"]:g} weekly points '
                f'({p["runnable"]} of {p["windows"]} windows left can run)')
        if p['tasks']:
            return f'{head}; {p["lose"]:g} % would be lost at its end → {p["tasks"]} task(s) now'
        return f'{head} → nothing more this window'
    head = f'{name}: {p["left"]:g} points left, spend by {when}'
    if p.get('background'):
        head += f', after {p["background"]:g} expected outside Tutti'
    if p.get('owner_reserve') is not None:
        head += f', {p["owner_reserve"]:g} kept for the owner ({p["mode"]})'
        return f'{head} → {p["tasks"]} task(s) now, spread evenly to the target' if p['tasks'] else f'{head} → nothing more now'
    return f'{head} → {p["tasks"]} task(s) now, behind its pace' if p['tasks'] else f'{head} → no extra tasks (not behind its pace)'


def bind(own, limits, a, accounts, hours, now, owner=None):
    """The account under its limits: a limit spent or in panic closes it; otherwise the planner sets its room."""
    out = {**own, 'limits': limits, 'bound_by': None, 'quota_plan': None}
    for lim in limits:
        if lim['state'] == 'ok' and (lim['panic'] or lim['verdict'] == 'spent'):
            # panic moves over only with a reading of the window itself; the limit's own check still closes an unread one
            panic = own['panic'] or (lim['panic'] and own['used'] is not None)
            out.update(verdict='spent' if lim['verdict'] == 'spent' else out['verdict'], panic=panic,
                       bound_by=lim['name'], room_tasks=None, action=f'{lim["name"]} ({lim["window"]}) closes it: {lim["action"]}')
            return out
    ok = [lim for lim in limits if lim['state'] == 'ok']
    if own['state'] != 'ok' or not ok:
        return out
    plans = [plan_limited(own, lim, a, accounts.get(lim['name']) or {}, hours, owner, now) for lim in ok]
    p = min(plans, key=lambda x: (x['tasks'], x['left']))   # the fewest tasks: points of different limits do not compare
    p['why'] = explain(own['name'], p, now)
    lim_tasks = p['tasks']
    out.update(quota_plan=p, room_tasks=min(own['room_tasks'], lim_tasks) if own['room_tasks'] else (lim_tasks or None))
    if p['left'] <= 0:
        used = (f'this window used its share of {p["limit"]} ({p["this_window"]:g} of {p["allowance"]:g} points): '
                'nothing new until it resets')
        why = 'this window is in autopilot.quiet_hours: its share moves to the windows that can run' if not p['allowance'] else used
        out.update(bound_by=p['limit'], room_tasks=None, verdict='ahead', action=why)
    return out


def plan_alone(s, a, now, rate=None, owner=None):
    """The plan of an account with no limits, for the expiry order and the dashboard's line; `rate`: its background spend;
    `owner`: the owner's context when it is the main agent's account."""
    if s['state'] != 'ok':
        return s
    p = plan_single(s, a, now, rate, owner)
    p['why'] = explain(s['name'], p, now)
    return {**s, 'quota_plan': p}
