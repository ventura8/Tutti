"""The quota planner: spend what expires soonest first, so as little quota as possible expires unused.

An account with `limits` (agy's 5 h windows under their weekly quota) is planned on the timeline of its windows left until
the limit's target (its reset minus the lead). The limit's room (its budget minus the owner's `reserve` minus what is used)
is shared over the windows that can run (not in `autopilot.quiet_hours`), evenly, except that a window must take whatever the
windows after it cannot hold: allowance = max(room / runnable windows, room - capacity of the later runnable windows),
never more than the window itself has left. A window that cannot run gets nothing and its share moves to those that can.
An account without limits has one window: its room until its target. Re-planned at every probe from fresh readings.

Pure: datetimes in, plain dicts out. Points are percentages of the limit's quota unless named `own`."""
from datetime import timedelta

from tutti import tz
from tutti.pacing import TASK_PCT, parse_time


def quiet(at, hours, zone=None):
    """Whether `at` (UTC) falls in one of `hours`, local-time spans (time.zone, daylight saving included) such as
    ['22:00-06:00'] (a span past midnight wraps)."""
    if not hours:
        return False
    lt = tz.local(at, zone if zone is not None else tz.zone())
    minute = lt.hour * 60 + lt.minute
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
    budget = float(lim_cfg.get('target_pct', 100)) * lim['share'] - float(lim_cfg.get('reserve', 0)) - kept
    at, known = used_at(lim, start)
    at_start = min(budget, at)
    per = window_points(own, lim, at_start, a.get('window_points'), known)
    windows = timeline(own, lim, hours, per)
    room = max(0.0, budget - at_start)
    caps = [c for _, _, c in windows]
    give = allowance(room, caps)
    spent = max(0.0, lim['used'] - at_start)
    on_pace = at_or_behind(lim)
    # on or behind its pace line the weekly limit is projected along that line, never from this window's short burst
    headroom = max(0.0, float(lim['expected']) - lim['used']) if on_pace else projection_room(lim, budget, now)
    if headroom is not None and headroom > 0:   # joint: more than an even share while the limit's projection still fits
        give = min(room, max(give, spent + headroom), caps[0] if caps[0] is not None else room)
    left = give - spent
    own_left = 100 - (own['used'] or 0)
    left_own = own_left if per is None else min(own_left, left * 100 / per) if per else 0.0
    # use it or lose it: the window's unused room is spent while the week is at or behind its pace
    use_room = on_pace and room > spent and own_left > 0 and caps[0] != 0
    if use_room:   # bounded by the limit's weekly room (and the window's capacity), never past the budget
        weekly = min(room, caps[0]) if caps[0] is not None else room
        left = weekly - spent
        left_own = own_left if not per else min(own_left, left * 100 / per)
    task = float(a.get('task_pct', TASK_PCT))
    tasks = max(0, int(left_own // task)) if left > 0 and task > 0 else 0
    return {'limit': lim['name'], 'expires': own['target'], 'resets': own.get('reset'),
            'windows': len(windows), 'runnable': sum(1 for *_, c in windows if c != 0),
            'per_window': round(room / max(1, len(windows)), 1), 'allowance': round(give, 1), 'this_window': round(spent, 1),
            'left': round(left, 1), 'tasks': tasks, 'lose': round(max(0.0, left_own), 1), 'window_points': per,
            'owner_reserve': round(kept, 1) if owner else None, 'mode': owner['mode'] if owner else None,
            'projected': None if headroom is None else round(budget - headroom, 1), 'limit_verdict': lim.get('verdict'),
            'limit_reset': lim.get('reset'),
            'slow_limit': {'name': lim['name'], 'left': max(0.0, budget - lim['used']),
                           'per_window': per, 'expires': lim['target'], 'runnable': caps[0] != 0}}


def at_or_behind(lim):
    """True when the limit's used % is at or under its pace line (`expected`), so it cannot be what holds a window."""
    return lim.get('expected') is not None and lim.get('used') is not None and float(lim['used']) <= float(lim['expected'])


def projection_room(lim, budget, now):
    """Points the limit can still take on top of its projected end (its last day's rate carried to the reset), or None when
    there is no projection yet. Positive: a window may spend over its even share and the limit still ends within its budget."""
    from tutti import owner as owner_mod
    if not now or not lim.get('reset') or lim.get('used') is None:
        return None
    end = owner_mod.projection(lim.get('readings') or [], lim['used'], now, parse_time(lim['reset']))
    return None if end is None else budget - end


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
    target = parse_time(s['target'])
    hours_left = max(0.0, (target - now).total_seconds() / 3600)
    background = (rate or 0.0) * hours_left
    mine = owner_fields(owner, now, target)
    budget = float(a.get('target_pct', 100)) * s['share'] - float(a.get('reserve', 0)) - background - (mine['owner_reserve'] or 0.0)
    left = max(0.0, budget - (s['used'] or 0))
    task = float(a.get('task_pct', TASK_PCT))
    return {'limit': None, 'expires': s['target'], 'resets': s.get('reset'), 'left': round(left, 1), 'lose': round(left, 1),
            'tasks': single_tasks(s, left, task, owner, now, target), 'allowance': round(left, 1), 'this_window': None,
            'windows': 1, 'runnable': 1, 'background': None if rate is None else round(background, 1), **mine,
            'run_out': run_out_of(s, owner, now), 'slow_left': left}


def run_out_of(s, owner, now):
    """ISO time the owner's account reaches 100 % at the owner's pace alone, before its reset; None when it does not.
    Hard safety: the workers then get nothing on it (the reserve already covers it) and the dashboard warns early."""
    if not owner or s.get('used') is None or not s.get('reset') or not owner.get('reserve'):
        return None
    from tutti import ownerday
    factor = (owner.get('today') or {}).get('factor', 1.0)   # today's pace carried forward over the plan
    t = ownerday.run_out(float(s['used']), now, parse_time(s['reset']), owner.get('planned_reserve') or owner['reserve'],
                         owner.get('offset', 0.0), factor)
    return t.isoformat() if t else None


def owner_fields(owner, now, target):
    """The plan's owner keys: the reserve kept until `target`, the days it is for, the mode, whether it is learned."""
    if not owner:
        return {'owner_reserve': None, 'mode': None, 'owner_days': None, 'learned': None}
    from tutti import owner as owner_mod
    return {'owner_reserve': round(owner_mod.reserve_until(owner, now, target), 1), 'mode': owner['mode'],
            'owner_days': ', '.join(owner_mod.reserve_days(owner, now, target)), 'learned': owner.get('learned')}


def single_tasks(s, left, task, owner, now, target):
    """Tasks now for an account without limits: the owner's account spreads `left` evenly to the target; any other takes
    the pace's own room (behind its line), never more than `left` holds."""
    if task <= 0:
        return 0
    if owner:
        from tutti import owner as owner_mod
        return int(owner_mod.spendable(left, now, target) // task)
    return min(s.get('room_tasks') or 0, int(left // task))


def _at(t, now):
    hours = max(0.0, (t - now).total_seconds() / 3600)
    return tz.show(t, now=now, date=hours >= 24)


def points_line(p):
    """'used 6.3 of its 2.3 weekly points (over by 4)': what this window spent of the weekly limit against its fair share."""
    used, share = p['this_window'], p['allowance']
    over = f' (over by {used - share:.1f})' if round(used - share, 1) > 0 else ''
    return f'used {used:g} of its {share:g} weekly points{over}'


def explain_limited(name, p, when):
    head = (f'{name}: spend by {when}; this window {points_line(p)} '
            f'({p["runnable"]} of {p["windows"]} windows left can run)')
    if p.get('owner_reserve'):
        head += f'; {p["owner_reserve"]:g} weekly points kept for the owner ({p["mode"]})'
    if p['tasks']:
        return f'{head}; {p["lose"]:g} % would be lost at its end → {p["tasks"]} task(s) now'
    return f'{head} → nothing more this window'


def explain_single(name, p, when):
    head = f'{name}: {p["left"]:g} points left, spend by {when}'
    if p.get('background'):
        head += f', after {p["background"]:g} expected outside Tutti'
    if p.get('owner_reserve') is not None:
        head += f', {p["owner_reserve"]:g} kept for the owner ({p["mode"]})'
        return f'{head} → {p["tasks"]} task(s) now, spread evenly to the target' if p['tasks'] else f'{head} → nothing more now'
    return f'{head} → {p["tasks"]} task(s) now, behind its pace' if p['tasks'] else f'{head} → no extra tasks (not behind its pace)'


def _decision_single(p):
    if p.get('owner_reserve') is not None:
        return (f'{p["tasks"]} task(s) now', 'spread evenly to the target') if p['tasks'] else ('nothing more now', '')
    return (f'{p["tasks"]} task(s) now', 'behind its pace') if p['tasks'] else ('no extra tasks', 'not behind its pace')


def kept_text(p):
    unit = 'weekly points' if p['limit'] else 'points'
    days = f' for {p["owner_days"]}' if p.get('owner_days') else ''
    how = ''
    if p.get('learned') is not None:
        how = ', learned' if p['learned'] else ', from pacing.schedule until learned'
    return f'{p["owner_reserve"]:g} {unit}{days} ({p["mode"]}{how})'


def _decision_limited(p):
    if p['tasks']:
        return f'{p["tasks"]} task(s) now', f'{p["lose"]:g} % would be lost at its end'
    return 'nothing more this window', ''


def rows(p):
    """The plan as label/value rows for the account card (the card names the account): {label, value} or {label, at} for a
    time (ISO, UTC), which the page shows in the viewer's own time with UTC in a tooltip."""
    out = []
    if p['limit']:
        out.append({'label': 'This window', 'value': points_line(p)})
        out.append({'label': 'Windows left', 'value': f'{p["runnable"]} of {p["windows"]} can run'})
    else:
        out.append({'label': 'Points left', 'value': f'{p["left"]:g}'})
    out.append({'label': 'Spend by', 'at': p['expires']})
    if p.get('resets'):
        out.append({'label': 'Resets', 'at': p['resets']})
    if not p['limit'] and p.get('background'):
        out.append({'label': 'Outside Tutti', 'value': f'{p["background"]:g} points expected'})
    if p.get('owner_reserve'):
        out.append({'label': 'Kept for the owner', 'value': kept_text(p)})
    decision, why = _decision_limited(p) if p['limit'] else _decision_single(p)
    out.append({'label': 'Decision', 'value': decision})
    if why:
        out.append({'label': 'Why', 'value': why})
    return out


def explain(name, p, now):
    """One line for the dashboard: the spend-by time (the reset minus the lead: what `expires` is), the reset itself (as the
    meter says it), what it may still spend, what would be lost and what Tutti does."""
    when = _at(parse_time(p['expires']), now)
    if p.get('resets'):
        when += f', resets {_at(parse_time(p["resets"]), now)}'
    return explain_limited(name, p, when) if p['limit'] else explain_single(name, p, when)


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
    p['slow_limits'] = [x['slow_limit'] for x in plans] if len(ok) == len(limits) else []
    p['why'], p['rows'] = explain(own['name'], p, now), rows(p)
    # the plan decides the room: the window's own pace line says nothing about its weekly limit (each badge keeps its own verdict)
    out.update(quota_plan=p, room_tasks=p['tasks'] or None, binding=binding(own, p, now))
    if p['left'] <= 0:
        out.update(bound_by=p['limit'], room_tasks=None)
    out['action'] = agree(out, p, a, now)
    return out


def binding(own, p, now):
    """Which of the account's windows sets its room now and why: {name, verdict, why}."""
    if p['tasks']:
        return {'name': own['name'], 'verdict': own['verdict'], 'why': f'{p["limit"]} has room for it ({_proj(p)})'}
    lim = f'{p["limit"]} ({p.get("limit_verdict") or "?"} of its pace{"; " + _proj(p) if p.get("projected") is not None else ""})'
    return {'name': p['limit'], 'verdict': p.get('limit_verdict'), 'why': lim}


def _proj(p):
    if p.get('projected') is None:
        return 'no projection yet'
    return 'projected to reach 100 % before its reset' if p['projected'] >= 100 else f'projected to end at {p["projected"]:g} %'


def agree(s, p, a, now):
    """The Do line from the plan, so it never contradicts the Decision: room → how much goes where; no room while the
    account's own line is behind → what holds it (the binding limit, the owner's reserve); else the pace's own action."""
    if p['tasks']:
        return f'spend: about {p["tasks"]} bounded task(s) can go to {a.get("worker") or s["name"]} now ({_spend_why(p)})'
    if s.get('panic') or s.get('verdict') == 'spent':
        return s['action']
    if p['limit']:
        return _held_by_limit(s, p, now)
    if p.get('owner_reserve'):
        days = f' ({p["owner_days"]})' if p.get('owner_days') else ''
        return (f'nothing more now: {p["owner_reserve"]:g} points are kept for the owner\'s working days before the reset'
                f'{days}; {p["left"]:g} left for the workers, spread evenly')
    return s['action']


def _spend_why(p):
    if p['limit']:
        return f'this window {points_line(p)}; {_proj(p)}'
    if p.get('owner_reserve') is not None:
        return f'{p["left"]:g} points left, spread evenly to the target'
    return 'behind its pace'


def _held_by_limit(s, p, now):
    if not p['allowance']:
        return 'this window is in autopilot.quiet_hours: its share moves to the windows that can run'
    reset = _at(parse_time(p['resets']), now) if p.get('resets') else 'its reset'
    return (f'{s.get("window") or "this"} window has room; the limit {binding(s, p, now)["why"]} is binding: '
            f'new work waits for the {reset} reset (this window {points_line(p)}; the next window gets a new share)')


def plan_alone(s, a, now, rate=None, owner=None):
    """The plan of an account with no limits, for the expiry order and the dashboard's line; `rate`: its background spend;
    `owner`: the owner's context when it is the main agent's account."""
    if s['state'] != 'ok':
        return s
    p = plan_single(s, a, now, rate, owner)
    p['why'], p['rows'] = explain(s['name'], p, now), rows(p)
    out = {**s, 'quota_plan': p}
    if owner:   # the owner's account: the plan (reserve kept, the rest spread evenly) decides, not the even line
        out.update(room_tasks=p['tasks'] or None, action=agree(out, p, a, now))
    return out
