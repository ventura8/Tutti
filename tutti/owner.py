"""The owner's own prompting, so the planner reaches every limit without starving the main agent.

The owner prompts the main agent (`main.agent`; its accounts, or `pacing.accounts`) part of the day on weekdays, and its use
is higher then; they sleep, take a day off, go on holiday, or let the agents work alone. `pacing.schedule` seeds the profile
(default: Mon-Fri, `hours` 8 within the local window "01-14", `rate` points per hour; Sat-Sun away); the readings of those
accounts teach it per weekday x hour once there are enough of them. On an owner day the planner keeps a reserve on the
main agent's accounts: the owner's expected burn for the owner hours left before the window's target. Outside them, on a
weekend, while away (`auto` infers it: under a quarter of the expected owner burn above the workers' baseline in the
last `away_after_hours` of today's owner hours) or on vacation, nothing is
reserved and the workers (Claude-backed autopilot too) spend the rest, spread evenly so the account ends near 100 % about
an hour before its reset without a burst that would block the owner when they come back.

The mode (`auto`, `active`, `away`, `vacation-until-YYYY-MM-DD`) is a state and lives in <state>/owner.json. The
dashboard's edits of the schedule (days, window, hours) are settings: written into the config's pacing.schedule when the
dashboard can write the file, else kept in owner.json, which wins until the key is saved. Pure, except `read` and `write`."""
import json
import os
import re
from datetime import datetime, timedelta

from tutti import tz
from tutti.pacing import parse_time

from . import durable

FILE = 'owner.json'
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
MODES = ('auto', 'active', 'away')
VACATION = re.compile(r'vacation-until-(\d{4}-\d{2}-\d{2})')
WINDOW = re.compile(r'([01]\d|2[0-3])-([01]\d|2[0-4])')
LEARN_HOURS = 48      # hours of readings (on at least LEARN_DAYS weekdays) before the learned profile replaces the seed
LEARN_DAYS = 3
AWAY_SHARE = 0.25    # auto: under this share of the expected owner burn in the last away_after owner hours is away
BLOCK = 5             # hours: the workers may spend left x BLOCK / hours left now (even, never a burst)
DEFAULT = {'days': list(DAYS[:5]), 'hours': 8, 'window': '01-14', 'rate': 1.0, 'utc_offset': None}


def vacation_day(mode):
    """The date of `vacation-until-YYYY-MM-DD`, or None (not that mode, or not a real date)."""
    m = VACATION.fullmatch(mode) if isinstance(mode, str) else None
    try:
        return datetime.strptime(m.group(1), '%Y-%m-%d') if m else None
    except ValueError:
        return None


def valid_mode(mode):
    return mode in MODES or vacation_day(mode) is not None


def schedule_errors(p):
    """Config errors of `pacing` (schedule, mode, away_after_hours, accounts), each naming the key and the fix."""
    s, errs = p.get('schedule') or {}, []
    if not isinstance(s, dict):
        return [f'pacing.schedule is {s!r}: write {{days: [mon, ...], hours: 8, window: "01-14", rate: 1.0}}']
    if not isinstance(s.get('days', []), list) or any(d not in DAYS for d in s.get('days', [])):
        errs.append(f'pacing.schedule.days is {s.get("days")!r}: a list of {", ".join(DAYS)}')
    if not WINDOW.fullmatch(str(s.get('window', '01-14'))):
        errs.append(f'pacing.schedule.window is {s.get("window")!r}: local hours HH-HH, e.g. "01-14"')
    for key, lo, hi in (('hours', 1, 24), ('rate', 0, 100)):
        v = s.get(key, DEFAULT[key])
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            errs.append(f'pacing.schedule.{key} is {v!r}: a number from {lo} to {hi}')
    accounts, away = p.get('accounts'), p.get('away_after_hours', 3)
    if accounts is not None and not (isinstance(accounts, list) and all(isinstance(a, str) for a in accounts)):
        errs.append(f'pacing.accounts is {accounts!r}: a list of account names, e.g. [claude] (or leave it out: the main agent\'s)')
    if isinstance(away, bool) or not isinstance(away, (int, float)) or away < 1:
        errs.append(f'pacing.away_after_hours is {away!r}: a number of hours, 1 or more')
    stop = p.get('stop_after_minutes', 60)
    if isinstance(stop, bool) or not isinstance(stop, (int, float)) or stop < 10:
        errs.append(f'pacing.stop_after_minutes is {stop!r}: minutes with no rise before today\'s reserve is released, 10 or more')
    errs += date_errors(p.get('dates'))
    if not valid_mode(p.get('mode', 'auto')):
        errs.append(f'pacing.mode is {p.get("mode")!r}: auto, active, away or vacation-until-YYYY-MM-DD')
    return errs


def zone_of(schedule, name=None):
    """The owner's zone: a fixed schedule.utc_offset (hours) when one is set, else the IANA name (time.zone, detected on
    the owner's own Tutti host), daylight saving included."""
    if schedule.get('utc_offset') is not None:
        return float(schedule['utc_offset'])
    return name or tz.zone_name()


def offset_of(schedule, now=None, name=None):
    """The owner's UTC offset in hours at `now`: 2 for Bucharest in winter, 3 in summer."""
    return tz.offset_hours(zone_of(schedule, name), now)


def local(at, zone):
    """`at` (UTC) in the owner's local time; `zone` is an IANA name, a tzinfo or fixed hours."""
    return tz.local(at, zone)


def seed(schedule):
    """The seeded profile: 7 x 24 points per hour, `rate` in `window` on `days`, 0 elsewhere."""
    a, b = (int(x) for x in schedule.get('window', DEFAULT['window']).split('-'))
    rate, days = float(schedule.get('rate', DEFAULT['rate'])), schedule.get('days', DEFAULT['days'])
    hours = [h for h in range(24) if (a <= h < b if a <= b else h >= a or h < b)]
    return [[rate if DAYS[d] in days and h in hours else 0.0 for h in range(24)] for d in range(7)]


def hourly(readings, offset):
    """{local hour start: (points risen, hours seen)} of consecutive readings [{at, used}]: each rise spread evenly over the
    hours it spans; a drop (a reset) and a gap over 24 h are skipped."""
    out = {}
    pts = sorted((parse_time(r['at']), float(r['used'])) for r in readings)
    for (t0, u0), (t1, u1) in zip(pts, pts[1:], strict=False):
        span = (t1 - t0).total_seconds() / 3600
        if u1 < u0 or span <= 0 or span > 24:
            continue
        t = t0
        while t < t1:
            step = min(t1, t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)) - t
            part = step.total_seconds() / 3600
            key = local(t, offset).replace(minute=0, second=0, microsecond=0, tzinfo=None)
            r, n = out.get(key, (0.0, 0.0))
            out[key] = (r + (u1 - u0) * part / span, n + part)
            t += step
    return out


def baseline(rates):
    """The workers' share of a day's hourly rates: their lower quartile (the owner's hours stand above it)."""
    xs = sorted(rates)
    return xs[len(xs) // 4] if xs else 0.0


def learn(readings, offset):
    """(profile 7 x 24 of the owner's points per hour, hours seen per weekday x hour, weekdays seen) from the readings of
    the owner's accounts. Workers spend the same account, so each day's lower quartile (their even spread) is taken off."""
    days = {}
    for k, (r, n) in hourly(readings, offset).items():
        days.setdefault(k.date(), []).append((k, r / n if n else 0.0, n))
    rise = [[0.0] * 24 for _ in range(7)]
    seen = [[0.0] * 24 for _ in range(7)]
    for rows in days.values():
        base = baseline([x for _, x, _ in rows])
        for k, rate, n in rows:
            rise[k.weekday()][k.hour] += max(0.0, rate - base) * n
            seen[k.weekday()][k.hour] += n
    profile = [[rise[d][h] / seen[d][h] if seen[d][h] else 0.0 for h in range(24)] for d in range(7)]
    weekdays = sum(1 for d in range(5) if sum(seen[d]) >= 12)
    return profile, seen, weekdays


def busiest(row, hours):
    """The `hours` contiguous hours (wrapping past midnight) of a day with the highest sum: the day's reserve window (the
    owner works in one stretch; a scattered peak keeps the stretch that holds most of it)."""
    if sum(1 for x in row if x > 0) <= hours:
        return {h for h in range(24) if row[h] > 0}
    start = max(range(24), key=lambda s: sum(row[(s + i) % 24] for i in range(hours)))
    return {(start + i) % 24 for i in range(hours)}


def profile_of(schedule, readings, offset):
    """{profile, reserve: 7 x 24 points per hour the planner keeps, learned: bool}. Once LEARN_HOURS of readings on
    LEARN_DAYS weekdays exist, a weekday seen for 12 h or more takes its learned row (others keep the seed's); its window,
    if wider than `hours`, keeps its busiest `hours` contiguous hours. Days outside schedule.days (the weekend) reserve nothing."""
    seeded = seed(schedule)
    learned, seen, weekdays = learn(readings, offset)
    total = sum(map(sum, seen))
    use = total >= LEARN_HOURS and weekdays >= LEARN_DAYS
    prof = [learned[d] if use and sum(seen[d]) >= 12 else seeded[d] for d in range(7)]
    days, width = schedule.get('days', DEFAULT['days']), int(schedule.get('hours', DEFAULT['hours']))
    reserve = []
    for d in range(7):
        keep = busiest(prof[d], width) if DAYS[d] in days else set()
        reserve.append([round(prof[d][h], 3) if h in keep else 0.0 for h in range(24)])
    return {'profile': [[round(x, 3) for x in row] for row in prof], 'reserve': reserve, 'learned': use,
            'hours_seen': round(total, 1)}


def owner_excess(readings, now, offset, hours):
    """Points the owner (above the workers' baseline of the last 24 h) spent in the given local hour starts."""
    rates = {k: r / n for k, (r, n) in hourly(readings, offset).items() if n}
    lnow = local(now, offset).replace(tzinfo=None)
    base = baseline([x for k, x in rates.items() if lnow - timedelta(hours=24) <= k < lnow])
    return sum(max(0.0, rates.get(h, 0.0) - base) for h in hours)


def effective(mode, reserve, readings, now, offset, away_after, learned=True):
    """(mode in effect, until: datetime or None). auto becomes away for the rest of today when the last `away_after` of
    today's reserve hours (the owner's busiest, learned: a seeded window says nothing about when they start) rose under
    AWAY_SHARE of their expected burn; vacation-until-<date> is away until that local date's midnight."""
    day = vacation_day(mode)
    if day:
        until = tz.to_utc(day, offset)
        return ('vacation', until) if now < until else ('auto', None)
    if not valid_mode(mode):
        mode = 'auto'   # a hand-edited owner.json with a bad mode: the profile decides, nothing breaks
    lt = local(now, offset)
    midnight = tz.to_utc(lt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None) + timedelta(days=1), offset)
    if mode == 'away':
        return 'away', midnight
    if mode != 'auto' or not learned:
        return mode, None
    passed = [h for h in range(lt.hour) if reserve[lt.weekday()][h] > 0][-int(away_after):]
    if len(passed) < away_after:
        return 'auto', None
    day = lt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    expected = sum(reserve[lt.weekday()][h] for h in passed)
    spent = owner_excess(readings, now, offset, [day + timedelta(hours=h) for h in passed])
    return ('away', midnight) if spent < AWAY_SHARE * expected else ('auto', None)


DATE = re.compile(r'\d{4}-\d{2}-\d{2}')


def date_errors(dates):
    """Errors of dated windows {YYYY-MM-DD: HH-HH or off}: one day's owner window that replaces its weekday's."""
    errs = []
    for d, w in (dates or {}).items():
        try:
            ok = bool(DATE.fullmatch(str(d))) and datetime.strptime(str(d), '%Y-%m-%d')
        except ValueError:
            ok = False
        if not ok:
            errs.append(f'pacing.dates has {d!r}: a date such as 2026-11-16')
        elif w is None:
            continue   # a day taken back from the dashboard over the config's (owner.json)
        elif w != 'off' and not WINDOW.fullmatch(str(w)):
            errs.append(f'pacing.dates.{d} is {w!r}: local hours HH-HH such as 20-24, or off (no owner hours that day)')
        elif w != 'off' and int(str(w)[:2]) >= int(str(w)[3:]):
            errs.append(f'pacing.dates.{d} is {w!r}: a day\'s window ends by 24 (20-24); past midnight, give the next day its '
                        'own window too (00-02)')
    return errs


def window_hours(window):
    a, b = (int(x) for x in window.split('-'))
    return {h for h in range(24) if (a <= h < b if a <= b else h >= a or h < b)}


def dated_row(window, rate):
    """One day's reserve row from a dated window: `rate` points in its hours, none for `off`."""
    hours = set() if window == 'off' else window_hours(window)
    return [float(rate) if h in hours else 0.0 for h in range(24)]


def rate_at(ctx, lt):
    """The reserve (points per hour) at local time `lt`: a dated window for that day wins over its weekday's row."""
    w = (ctx.get('dates') or {}).get(lt.date().isoformat())
    if w is None:
        return ctx['reserve'][lt.weekday()][lt.hour]
    return dated_row(w, ctx.get('rate', DEFAULT['rate']))[lt.hour]


def reserve_until(ctx, now, end):
    """The owner's expected burn (points) from `now` to `end` on the reserve profile, none while away or on vacation."""
    t, total = now, 0.0
    skip = ctx.get('until')
    while t < end:
        step = min(end, t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)) - t
        if not (skip and t < skip):
            lt = local(t, ctx['offset'])
            total += rate_at(ctx, lt) * step.total_seconds() / 3600
        t += step
    return total


def reserve_days(ctx, now, end):
    """The owner's local days from `now` to `end` that keep a reserve, e.g. ['Mon 16', 'Tue 17']: the working days the reserve
    is for (a weekend keeps nothing; a day after the reset is not counted)."""
    out, t = [], now
    skip = ctx.get('until')
    while t < end:
        lt = local(t, ctx['offset'])
        name = f'{DAYS[lt.weekday()].title()} {lt.day}'
        if not (skip and t < skip) and rate_at(ctx, lt) > 0 and name not in out:
            out.append(name)
        t = min(end, t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    return out


def context(owner, readings, now):
    """What the planner needs for an owner account: the reserve profile, the mode in effect, the offset. None when off."""
    if not owner:
        return None
    sched = owner['schedule']
    offset = zone_of(sched, owner.get('zone'))
    prof = profile_of(sched, readings, offset)
    dates = owner.get('dates') or {}
    today = local(now, offset).date().isoformat()
    if today in dates:   # a dated window today (pacing.dates): today's row is that window, for the away check and the shift
        wd = local(now, offset).weekday()
        row = dated_row(dates[today], sched.get('rate', DEFAULT['rate']))
        prof = {**prof, 'reserve': [row if d == wd else r for d, r in enumerate(prof['reserve'])]}
    mode, until = effective(owner['mode'], prof['reserve'], readings, now, offset, owner.get('away_after_hours', 3), prof['learned'])
    if mode == 'auto' and owner.get('laptop_away'):   # the owner's laptop is away or asleep (presence.owner_laptop): away
        mode, until = 'away', now + timedelta(hours=1)  # for the next hour, looked at again each round
    from tutti import ownerday  # today's real start, stop and burn move today's reserve (and the burn all of it)
    day = ownerday.today(prof['reserve'], readings, now, offset, owner.get('stop_after_minutes', ownerday.STOP_MINUTES))
    if mode not in ('away', 'vacation'):
        prof = {**prof, 'planned_reserve': prof['reserve'], 'reserve': ownerday.shifted(prof['reserve'], day, now, offset)}
    return {**prof, 'dates': dates, 'rate': sched.get('rate', DEFAULT['rate']),
            'offset': offset, 'zone': offset if isinstance(offset, str) else tz.label(offset),
            'utc_offset': tz.offset_hours(offset, now), 'mode': mode, 'set_mode': owner['mode'], 'until': until, 'today': day,
            'today_line': ownerday.line(day)}


def spendable(left, now, target):
    """What the workers may spend now: `left` spread evenly over the hours to the target, a BLOCK at a time (the last
    block takes all that is left)."""
    hours = (target - now).total_seconds() / 3600
    return left if hours <= BLOCK else left * BLOCK / hours


def projection(readings, used, now, reset):
    """The % the account is on track to end at by its reset: the last 24 h's rate carried on (the window's when shorter)."""
    pts = sorted((parse_time(r['at']), float(r['used'])) for r in readings)
    recent = [p for p in pts if p[0] >= now - timedelta(hours=24)] or pts
    if used is None or len(recent) < 2 or recent[-1][0] <= recent[0][0]:
        return None
    rate = max(0.0, recent[-1][1] - recent[0][1]) / ((recent[-1][0] - recent[0][0]).total_seconds() / 3600)
    return round(min(100.0, used + rate * max(0.0, (reset - now).total_seconds() / 3600)), 1)


# ---- I/O: the mode and the dashboard's edits, in <state>/owner.json


def path(folder):
    return os.path.join(os.path.expanduser(folder), FILE)


def read(folder):
    try:
        with open(path(folder), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def write(folder, **changes):
    """Saves the mode or schedule edits (days, window, hours); a None value takes the key back to the config's."""
    data = read(folder)
    for k, v in changes.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    os.makedirs(os.path.expanduser(folder), exist_ok=True)
    durable.dump_json(path(folder), data, indent=1)
    return data


def change_date(folder, data, config_dates=None):
    """(status, answer) of {"date": "YYYY-MM-DD", "window": "HH-HH" | "off" | null}: null takes the day back to its weekday,
    a day the config sets too included (kept as null in owner.json, which wins over the config)."""
    d, w = str(data.get('date')), data.get('window')
    errs = date_errors({d: w if w is not None else 'off'})
    if errs:
        return 400, {'error': '; '.join(errs)}
    dates = dict(read(folder).get('dates') or {})
    if w is None and d in {str(k) for k in (config_dates or {})}:
        dates[d] = None
    elif w is None:
        dates.pop(d, None)
    else:
        dates[d] = w
    write(folder, dates=dates or None)
    return 200, {'ok': True, 'date': d, 'window': w}


def owner_of(cfg, folder):
    """The planner's owner input from the config and owner.json: {accounts, schedule, mode, away_after_hours}."""
    p = cfg.get('pacing') or {}
    saved = read(folder)
    sched = {**DEFAULT, **(p.get('schedule') or {}), **{k: saved[k] for k in ('days', 'window', 'hours') if k in saved}}
    agent = (cfg.get('main') or {}).get('agent')
    accounts = p.get('accounts')
    if accounts is None:   # no main agent: no account is the owner's
        accounts = [n for n, a in cfg['accounts'].items() if agent and (n == agent or (a or {}).get('worker') == agent)]
    from . import presence  # imported here: config loads this module before presence exists
    # keys as text: YAML reads an unquoted 2026-11-16 as a date. A dashboard null takes a config day back.
    dates = {**{str(d): w for d, w in (p.get('dates') or {}).items()}, **{str(d): w for d, w in (saved.get('dates') or {}).items()}}
    dates = {d: w for d, w in dates.items() if w is not None}
    return {'accounts': list(accounts), 'schedule': sched, 'mode': saved.get('mode') or p.get('mode', 'auto'), 'dates': dates,
            'away_after_hours': p.get('away_after_hours', 3), 'zone': tz.zone_name(cfg, saved.get('browser_zone') or ''),
            'laptop_away': presence.owner_away(cfg),
            'stop_after_minutes': p.get('stop_after_minutes', 60), 'target_pct': (cfg.get('main') or {}).get('target_pct')}


def view(cfg, folder, entries, now):
    """The dashboard's owner panel: the mode set and the one in effect, the schedule, the learned 7 x 24 profile and the
    reserve the planner keeps."""
    from tutti import pacing
    o = owner_of(cfg, folder)
    ctx = context(o, pacing.owner_readings(entries, o['accounts'], now), now)
    until = ctx['until'].isoformat() if ctx['until'] else None
    return {'accounts': o['accounts'], 'mode': o['mode'], 'effective': ctx['mode'], 'until': until, 'schedule': o['schedule'],
            'profile': ctx['profile'], 'reserve': ctx['reserve'], 'learned': ctx['learned'], 'hours_seen': ctx['hours_seen'],
            'utc_offset': ctx['utc_offset'], 'zone': ctx['zone'], 'browser_zone': read(folder).get('browser_zone') or '',
            'days': list(DAYS), 'today': ctx.get('today_line'),
            'dates': {d: w for d, w in sorted(o['dates'].items()) if d >= local(now, ctx['offset']).date().isoformat()}}


def change(cfg, folder, data):
    """(status, answer) of a change from the dashboard or the CLI: {mode, days, window, hours}; null takes one back to the
    config's. Checked with the config's own rules before anything is saved."""
    if isinstance(data, dict) and 'browser_zone' in data:   # the dashboard's browser zone: used when the host's is unknown
        if not tz.valid(data['browser_zone']):
            return 400, {'error': f'browser_zone is {data["browser_zone"]!r}: an IANA zone such as Europe/Bucharest'}
        write(folder, browser_zone=data['browser_zone'])
        return 200, {'ok': True, 'browser_zone': data['browser_zone']}
    if isinstance(data, dict) and 'date' in data:   # one day's own window (pacing.dates): {"date": d, "window": "20-24"|"off"|null}
        return change_date(folder, data, (cfg.get('pacing') or {}).get('dates'))
    keys = [k for k in ('mode', 'days', 'window', 'hours') if isinstance(data, dict) and k in data]
    if not keys:
        return 400, {'error': 'send {"mode": "auto|active|away|vacation-until-YYYY-MM-DD"} or {"days": [...], "window": "HH-HH", '
                              '"hours": n} as JSON (null takes one back to the config\'s)'}
    o = owner_of(cfg, folder)
    trial = {'schedule': {**o['schedule'], **{k: data[k] for k in ('days', 'window', 'hours') if data.get(k) is not None}},
             'mode': data['mode'] if data.get('mode') is not None else o['mode']}
    errs = schedule_errors(trial)
    if errs:
        return 400, {'error': '; '.join(errs)}
    write(folder, **{k: data[k] for k in keys})
    return 200, {'ok': True, **{k: data[k] for k in keys}}
