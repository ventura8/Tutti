"""Recurring blocked hours (docs/DASHBOARD.md, *Controls*): daily local windows such as `22-07,12-13`, on all days or the
listed ones, when a device (`devices.<d>.hours`) or a machine at 0 % (`machines.<m>.limit` with pct 0) is not used at all.
A window past midnight belongs to the day it starts on. They work as planned absences do (tutti/controls.py): no new work
from LEAD minutes before, running work handed over at the start, and a device's own settings restored when its run ends.
Pure: parse, errors, window."""
import re
from datetime import datetime, timedelta, timezone

from . import tz

DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
ONE = re.compile(r'([01]\d|2[0-3])-([01]\d|2[0-4])')


def parse(spec):
    """[(start hour, end hour)] of 'HH-HH[,HH-HH]'; ValueError with the form when it is wrong."""
    out = []
    for part in str(spec or '').replace(' ', '').split(','):
        m = ONE.fullmatch(part)
        if not m or m.group(1) == m.group(2):
            raise ValueError(f'"{part}" is not a window: local hours HH-HH such as 22-07 (past midnight wraps); several with commas')
        out.append((int(m.group(1)), int(m.group(2))))
    return out


def parse_days(text):
    """['mon', ...] from 'mon-fri', 'sat,sun' or 'mon-wed,sat'; ValueError when a name is not a day."""
    out = []
    for part in str(text or '').lower().replace(' ', '').split(','):
        a, _, b = part.partition('-')
        if a not in DAYS or (b and b not in DAYS):
            raise ValueError(f'"{part}" is not a day: use {", ".join(DAYS)}, a range such as mon-fri, or a list such as sat,sun')
        i, j = DAYS.index(a), DAYS.index(b or a)
        out += [DAYS[k % 7] for k in range(i, i + (j - i) % 7 + 1)]
    return sorted(set(out), key=DAYS.index)


def errors(where, h):
    """Errors of `<where>.hours`: {window: 'HH-HH[,HH-HH]', days: [mon, ...]}."""
    if h is None:
        return []
    if not isinstance(h, dict):
        return [f'{where}.hours is {h!r}: write {{window: "22-07", days: [mon, tue]}} (days: every day unless listed)']
    errs = []
    try:
        parse(h.get('window'))
    except ValueError as e:
        errs.append(f'{where}.hours.window: {e}')
    if any(d not in DAYS for d in h.get('days') or []):
        errs.append(f'{where}.hours.days is {h.get("days")!r}: a list of {", ".join(DAYS)}')
    return errs


def window(spec, days, now, lead=0, zone=None):
    """(start, end) epoch seconds of the window that holds `now` or starts within `lead` seconds, else None."""
    z = zone if zone is not None else tz.zone()
    local = tz.local(datetime.fromtimestamp(now, timezone.utc), z).replace(tzinfo=None)
    days = days or list(DAYS)
    found = []
    for a, b in parse(spec):
        for shift in (-1, 0, 1):
            day = (local + timedelta(days=shift)).replace(hour=0, minute=0, second=0, microsecond=0)
            if DAYS[day.weekday()] not in days:
                continue
            start = day + timedelta(hours=a)
            end = day + timedelta(hours=b if b > a else b + 24)
            s, e = tz.to_utc(start, z).timestamp(), tz.to_utc(end, z).timestamp()
            if s - lead <= now < e:
                found.append((s, e))
    return min(found) if found else None
