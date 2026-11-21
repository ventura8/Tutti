"""A machine's share for Tutti (docs/FLEET.md, *The machine's share*): `machines.<m>.use_pct` (default 100) caps how much of
the machine's slots, cores and model threads Tutti's work uses. The share drops by itself while:
- someone uses the machine (a person at this computer's keyboard: in_use_pct, default 0: no new work there, it goes to the other machines);
- it is in its quiet hours (machines.<m>.quiet_hours, else autopilot.quiet_hours: quiet_pct, default 50);
- it runs hot (its heat cap reached: hot_pct, default 50).
Its yield flag still takes it out entirely (the PC is RoRadio's), as the governor's memory floor does.
Pure: pct, scaled, cap_free; the I/O: idle_seconds (this computer's keyboard and mouse)."""
import os
import re
import shutil
import subprocess
import time

from . import planner

DEFAULTS = {'use_pct': 100, 'in_use_pct': 0, 'quiet_pct': 50, 'hot_pct': 50, 'in_use_minutes': 5}
LIMIT_PCT = 60   # machines.<m>.limit.pct when a window is set without one
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
WINDOW = re.compile(r'([01]\d|2[0-3])-([01]\d|2[0-4])')


def limit_errors(name, limit):
    """Errors of machines.<m>.limit {window: HH-HH, days: [mon, ...], pct: 1-100, main: bool}."""
    if limit is None:
        return []
    key = f'machines.{name}.limit'
    if not isinstance(limit, dict):
        return [f'{key} is {limit!r}: write {{window: "18-01", pct: 60}} (days: every day unless listed)']
    errs = []
    w = str(limit.get('window', ''))
    if not WINDOW.fullmatch(w):
        errs.append(f'{key}.window is {limit.get("window")!r}: local hours HH-HH such as 18-01 (past midnight wraps)')
    elif w[:2] == w[3:]:
        errs.append(f'{key}.window is {w!r}: start and end are the same; the whole day is 00-24')
    if any(d not in DAYS for d in limit.get('days') or []):
        errs.append(f'{key}.days is {limit.get("days")!r}: a list of {", ".join(DAYS)}')
    pct = limit.get('pct', LIMIT_PCT)
    if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct <= 100:
        errs.append(f'{key}.pct is {pct!r}: a share from 0 to 100 (0: not used at all in that window)')
    return errs


def limited(machine, now):
    """The cap (percent) of the machine's limited-usage window when `now` (a UTC datetime) is inside it, else None. A window
    past midnight belongs to the day it starts on (18-01 on Monday runs Monday 18:00 to Tuesday 01:00). The hours are in Tutti's
    time zone (time.zone), the one the dashboard shows."""
    from datetime import timedelta

    from . import tz
    lim = (machine or {}).get('limit')
    if not isinstance(lim, dict) or not WINDOW.fullmatch(str(lim.get('window', ''))) or now is None:
        return None
    a, b = (int(x) for x in lim['window'].split('-'))
    lt = tz.local(now, tz.zone())
    days = lim.get('days') or list(DAYS)
    if a < b:
        inside, start_day = a <= lt.hour < b, lt
    elif lt.hour >= a:
        inside, start_day = True, lt
    else:
        inside, start_day = lt.hour < b, lt - timedelta(days=1)
    if not inside or DAYS[start_day.weekday()] not in days:
        return None
    return int(lim.get('pct', LIMIT_PCT))


def setting(machine, key):
    v = (machine or {}).get(key)
    return DEFAULTS[key] if v is None else v


def pct(machine, row=None, now=None, idle_s=None, quiet_hours=None):
    """(the share in percent now, why it is lower than use_pct or ''). `idle_s`: seconds since a person last used the machine
    (None: not known); `now` a UTC datetime for the quiet hours."""
    share, why = int(setting(machine, 'use_pct')), []
    if idle_s is not None and idle_s < setting(machine, 'in_use_minutes') * 60:
        share, why = min(share, int(setting(machine, 'in_use_pct'))), why + ['someone is at its keyboard']
    hours = (machine or {}).get('quiet_hours') or quiet_hours
    if now is not None and hours and planner.quiet(now, hours):
        share, why = min(share, int(setting(machine, 'quiet_pct'))), why + ['quiet hours']
    cap = limited(machine, now)
    if cap is not None:
        share, why = min(share, cap), why + [f'limited hours {machine["limit"]["window"]} at {cap} %']
    if row and re.search(r'\bhot\b|too hot', row.get('reason') or ''):
        share, why = min(share, int(setting(machine, 'hot_pct'))), why + ['it runs hot']
    return max(0, min(100, share)), ', '.join(why)


def scaled(n, share):
    """`n` (slots, cores, threads) at `share` percent: at least 1 while the share is above 0."""
    if not n or share <= 0:
        return 0
    return max(1, int(n * share / 100))


def cap_free(slots, free, share):
    """The free slots Tutti may use at its share: the capped slots less the busy ones."""
    if free is None:
        return None
    busy = max(0, slots - free)
    return max(0, min(free, scaled(slots, share) - busy))


def probe_at_share(machine, probe, now, idle_s=None, quiet_hours=None):
    """A routing probe whose free slots are cut to Tutti's share now (limited hours, quiet, person at keyboard).
    `tutti test` used to probe raw free slots and ignore `machines.<m>.limit`, so heavy check.sh still landed on a
    Plex NUC inside its evening window (t444)."""
    import dataclasses

    from . import routing
    if not probe.reachable:
        return probe
    reason = routing.heat_unfit(machine, probe) or routing.power_unfit(machine, probe)
    share_pct, _why = pct(machine, {'reason': reason}, now, idle_s, quiet_hours)
    free = cap_free(int(machine.get('slots') or 1), probe.free, share_pct)
    return probe if free == probe.free else dataclasses.replace(probe, free=free)

def mutter_idle(run=subprocess.run):
    """Seconds since the last keyboard or mouse input on a GNOME session (X11 or Wayland) from Mutter's idle monitor over
    the session D-Bus, or None (no GNOME, no gdbus, no session bus). GNOME never sets logind's IdleSinceHint, so on a
    Wayland laptop this is the only way to tell (the 2026-11-16 laptop: quiet mode never saw the owner)."""
    if not shutil.which('gdbus'):
        return None
    env = dict(os.environ)
    bus = f'/run/user/{os.getuid()}/bus' if hasattr(os, 'getuid') else ''
    if not env.get('DBUS_SESSION_BUS_ADDRESS') and bus and os.path.exists(bus):
        env['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path={bus}'   # a systemd user service or ssh shell may lack it
    try:
        r = run(['gdbus', 'call', '--session', '--dest', 'org.gnome.Mutter.IdleMonitor', '--object-path',
                 '/org/gnome/Mutter/IdleMonitor/Core', '--method', 'org.gnome.Mutter.IdleMonitor.GetIdletime'],
                capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'uint64 (\d+)', r.stdout or '')
    return int(m.group(1)) / 1000 if r.returncode == 0 and m else None


def graphical_sessions(run=subprocess.run):
    """[{Type, IdleHint, IdleSinceHint, LockedHint}] of this user's x11 and wayland logind sessions (any session, not
    `self`: the dashboard runs outside one)."""
    try:
        r = run(['loginctl', 'list-sessions', '--no-legend'], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for sid in (line.split()[0] for line in (r.stdout or '').splitlines() if line.split()):
        try:
            s = run(['loginctl', 'show-session', sid, '-p', 'Type', '-p', 'IdleHint', '-p', 'IdleSinceHint', '-p', 'LockedHint'],
                    capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        v = dict(line.split('=', 1) for line in (s.stdout or '').splitlines() if '=' in line)
        if v.get('Type') in ('x11', 'wayland'):
            out.append(v)
    return out


def screen_locked(run=subprocess.run):
    """True when every graphical session here is locked (nobody can be using it), False when one is unlocked, None when
    there is none."""
    sessions = graphical_sessions(run)
    return all(s.get('LockedHint') == 'yes' for s in sessions) if sessions else None


def idle_seconds(run=subprocess.run, clock=time.time):
    """Seconds since this computer's keyboard or mouse was last used, or None when it cannot be told: GNOME's idle monitor
    (X11 and Wayland), xprintidle (X11), else a logind session's idle hint. A locked screen reads as idle for a day."""
    sessions = graphical_sessions(run)
    if sessions and all(s.get('LockedHint') == 'yes' for s in sessions):
        return 86400.0
    v = mutter_idle(run)
    if v is not None:
        return v
    if shutil.which('xprintidle'):
        try:
            r = run(['xprintidle'], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                return int(r.stdout.strip()) / 1000
        except (OSError, subprocess.SubprocessError):
            pass
    known = [s for s in sessions if s.get('IdleSinceHint', '').isdigit() and int(s['IdleSinceHint'])]
    if not known:
        return None   # a desktop that never sets the hint: not known (it would read "in use" for ever)
    return min(0.0 if s.get('IdleHint') == 'no' else max(0.0, clock() - int(s['IdleSinceHint']) / 1e6) for s in known)


def limit_change(cfg, name, window, pct=None):
    """[(config path, value or DELETE)] for `tutti machine limit` and the machine card: window `off` takes it away. Raises
    ValueError with the fix when the input is wrong."""
    from . import yamledit
    if name not in cfg['machines']:
        raise ValueError(f'no machine "{name}" under machines: (tutti machines lists them)')
    if window == 'off':
        return [(['machines', name, 'limit'], yamledit.DELETE)]
    old = cfg['machines'][name].get('limit') or {}
    new = {**old, 'window': window, 'pct': LIMIT_PCT if pct is None and 'pct' not in old else (old.get('pct') if pct is None else pct)}
    errs = limit_errors(name, new)
    if errs:
        raise ValueError('; '.join(errs))
    return [(['machines', name, 'limit'], new)]
