"""How the owner really works today, read from the live readings of the main agent's accounts, so the reserve follows the
owner's day and not only the plan (tutti/owner.py keeps the plan: the seeded or learned 7 x 24 profile).

- **Real start.** The first local hour today whose rise stands above the workers' baseline by more than START_SHARE of the
  planned owner rate. An earlier start than the plan protects from that hour on: today's reserve window moves to start there.
- **Real stop.** After a start, no owner rise for `stop_after_minutes` (60) inside the planned hours: the rest of today's
  reserve is released to the workers at once (not at the day's end).
- **Burn.** The owner's rate over the last BURN_HOURS against the plan's: above it, the reserve until the target is scaled by
  that factor (the workers' share drops straight away and their load moves to other accounts and local models, which the
  planner does by itself when this account has no room); a light day frees the reserve of every hour that passed unused.
- **Run-out warning.** When the main agent would hit 100 % before its reset at today's pace even with no worker on it, the
  dashboard says so early ("at today's pace you run out on Thu 19 10:00"), with what Tutti already did.

The learned profile learns from the same readings, so repeated early starts move the schedule on their own. Pure."""
from datetime import timedelta

from tutti.pacing import parse_time

START_SHARE = 0.5     # a rise above the baseline of half the planned owner rate is the owner at work
STOP_MINUTES = 60     # no owner rise for this long inside the planned hours: stopped for today
BURN_HOURS = 2        # the owner's live rate is read over the last hours


def _local(t, offset):
    """`t` (UTC) in the owner's local time: `offset` is an IANA zone, a tzinfo or fixed hours (tutti/tz.py)."""
    from tutti import tz
    return tz.local(t, offset)


def owner_rates(readings, now, offset):
    """{local hour start (naive): owner points per hour above the workers' baseline} for today, from the readings."""
    from tutti import owner
    rates = {k: r / n for k, (r, n) in owner.hourly(readings, offset).items() if n}
    lnow = _local(now, offset).replace(tzinfo=None)
    base = owner.baseline([x for k, x in rates.items() if lnow - timedelta(hours=24) <= k < lnow])
    day = lnow.replace(hour=0, minute=0, second=0, microsecond=0)
    return {k: max(0.0, x - base) for k, x in rates.items() if day <= k <= lnow}


def planned_hours(row):
    hours = [h for h in range(24) if row[h] > 0]
    return (hours[0], hours[-1] + 1, sum(row) / len(hours)) if hours else (None, None, 0.0)


def last_rise(readings, now, offset, rate):
    """The last time today the account rose faster than START_SHARE x the planned rate between two readings."""
    pts = sorted((parse_time(r['at']), float(r['used'])) for r in readings)
    day = _local(now, offset).date()
    best = None
    for (t0, u0), (t1, u1) in zip(pts, pts[1:], strict=False):
        hours = (t1 - t0).total_seconds() / 3600
        if _local(t1, offset).date() == day and hours > 0 and (u1 - u0) / hours > START_SHARE * rate:
            best = t1
    return best


def today(reserve, readings, now, offset, stop_minutes=STOP_MINUTES):
    """{start, stop, planned_start, planned_stop, factor, row}: today's real start and stop (local hours, or None), the burn
    factor, and today's reserve row shifted to match."""
    lnow = _local(now, offset)
    row = list(reserve[lnow.weekday()])
    p_start, p_stop, rate = planned_hours(row)
    out = {'start': None, 'stop': None, 'planned_start': p_start, 'planned_stop': p_stop, 'factor': 1.0, 'row': row}
    if p_start is None or rate <= 0:
        return out
    rates = owner_rates(readings, now, offset)
    working = sorted(k for k, x in rates.items() if x > START_SHARE * rate)
    if not working:
        return out
    start = working[0].hour
    out['start'] = start
    width = p_stop - p_start
    if start < p_start:   # an earlier start protects from then on, for the planned width
        row = [rate if start <= h < start + width else 0.0 for h in range(24)]
    last = last_rise(readings, now, offset, rate)
    quiet = last is not None and (now - last).total_seconds() / 60 >= stop_minutes and lnow.hour < max(p_stop, start + width)
    if quiet:
        lt = _local(last, offset)
        stop = lt.hour + (1 if lt.minute or lt.second else 0)   # the hour the last rise ended in
        out['stop'] = stop
        row = [x if h < stop else 0.0 for h, x in enumerate(row)]   # released after the real stop
    end = max((h + 1 for h in range(24) if row[h] > 0), default=0)
    if not quiet and last is not None and (now - last).total_seconds() / 60 < stop_minutes and lnow.hour >= end:
        for h in (lnow.hour, (lnow.hour + 1) % 24):   # a late stop: still at work after the plan, protected an hour at a time
            row[h] = max(row[h], rate)
        out['late'] = True
    recent = [x for k, x in rates.items() if k >= lnow.replace(tzinfo=None) - timedelta(hours=BURN_HOURS)]
    live = sum(recent) / len(recent) if recent else 0.0
    if not quiet and live > rate:
        out['factor'] = round(live / rate, 2)
    out['row'] = row
    return out


def shifted(reserve, day, now, offset):
    """The reserve with today's row replaced by the day's shifted row, all scaled by the burn factor until the target."""
    wd = _local(now, offset).weekday()
    return [[round(x * day['factor'], 3) for x in (day['row'] if d == wd else reserve[d])] for d in range(7)]


def run_out(used, now, reset, ctx_reserve, offset, factor):
    """When the account reaches 100 % at the owner's pace alone (workers at zero), if before `reset`; else None."""
    left, t = 100.0 - used, now
    while t < reset and left > 0:
        step = min(reset, t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)) - t
        lt = _local(t, offset)
        left -= ctx_reserve[lt.weekday()][lt.hour] * factor * step.total_seconds() / 3600
        t += step
    return t if left <= 0 and t < reset else None


def line(day):
    """The dashboard's line, e.g. "Today: started 07:40 (planned 09:00), stopped 15:00 (planned 17:00); burning 1.6x"."""
    if day['planned_start'] is None:
        return 'Today: no owner hours planned'
    parts = [f'started {day["start"]:02d}:00 (planned {day["planned_start"]:02d}:00)' if day['start'] is not None
             else f'not started yet (planned {day["planned_start"]:02d}:00)']
    if day.get('late'):
        parts.append(f'still working after the planned {day["planned_stop"]:02d}:00: protected an hour at a time')
    if day['stop'] is not None:
        parts.append(f'stopped {day["stop"]:02d}:00 (planned {day["planned_stop"]:02d}:00): the rest of today goes to the workers')
    if day['factor'] > 1:
        parts.append(f'burning {day["factor"]:g}x the plan: the workers\' share on this account is cut to match')
    return 'Today: ' + '; '.join(parts)
