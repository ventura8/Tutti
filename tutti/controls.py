"""The owner's switches, from the dashboard or `tutti pause|resume|optin`: opt-in items turned on or off, and machines, devices
and workers paused. Kept in <project.state>/controls.json and applied when the config loads, so routing, the autopilot,
conduct's fallbacks and device locks all see them. Pausing is a state and stays here; the dashboard's opt-in switch is a
setting and writes `enabled` into the config file when it can (as its Settings tab does), so only `tutti optin` and a
config the dashboard cannot write keep `enabled` switches here.

A paused item takes nothing new; what already runs finishes, unless the owner chose "stop now", which ends the tutti jobs
bound to it (`<job log>.target` names them: `machine:<m>`, `device:<d>`, `worker:<w>`)."""
import contextlib
import getpass
import glob
import json
import os
import time

from . import durable, tz

try:
    import fcntl
except ImportError:   # Windows: no flock; the dashboard is the only writer there
    fcntl = None

FILE = 'controls.json'
SECTIONS = {'machine': 'machines', 'device': 'devices', 'worker': 'workers', 'account': 'accounts'}
JOB_FOLDERS = ('tests', 'devices', 'runs', 'files')


def path(folder):
    return os.path.join(os.path.expanduser(folder), FILE)


def read(folder):
    try:
        with open(path(folder), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return {'enabled': dict(data.get('enabled') or {}), 'paused': dict(data.get('paused') or {}),
            'settings': dict(data.get('settings') or {}), 'handed': dict(data.get('handed') or {})}


@contextlib.contextmanager
def changed(folder):
    """controls.json read and written back under one lock: two clicks never overwrite each other."""
    folder = os.path.expanduser(folder)
    os.makedirs(folder, exist_ok=True)
    with open(path(folder) + '.lock', 'w', encoding='utf8') as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        data = read(folder)
        yield data
        durable.dump_json(path(folder), data, indent=1)


def who(source):
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = 'owner'
    return f'{user} ({source})'


def key(kind, name):
    if kind not in SECTIONS:
        raise ValueError(f'kind is {kind!r}: use one of {", ".join(SECTIONS)}')
    return f'{kind}:{name}'


def set_enabled(folder, kind, name, on, source='dashboard', clock=time.time):
    with changed(folder) as data:
        data['enabled'][key(kind, name)] = {'on': bool(on), 'by': who(source), 'at': clock()}


LEAD_MINUTES = 30   # a planned absence takes no new work this long before it starts, so nothing is cut at the start


def set_paused(folder, kind, name, paused, source='dashboard', note='', clock=time.time, start=None, until=None):
    """Pause or resume. With `start` (a time) the pause is a planned absence: from `start` (no new work LEAD_MINUTES
    before it, running work handed over at it) until `until`, or with `until` None until presence sees it back."""
    with changed(folder) as data:
        k = key(kind, name)
        if paused:
            data['paused'][k] = {'by': who(source), 'at': clock(), 'note': note}
            if start is not None:
                data['paused'][k].update({'from': float(start), 'until': None if until is None else float(until)})
        else:
            data['paused'].pop(k, None)


def parse_when(text, now=None, after=None):
    """Epoch seconds of a local time: "now", "YYYY-MM-DD HH:MM" (or with T), "tomorrow HH:MM", "HH:MM" (the next one,
    after `after` when given). ValueError with the forms that work."""
    from datetime import datetime, timedelta
    now = time.time() if now is None else now
    t = (text or '').strip().lower()
    if t == 'now':
        return now
    base = tz.local_now(after if after is not None else now)
    try:
        if t.startswith('tomorrow'):
            hm = datetime.strptime(t.split(None, 1)[1], '%H:%M')
            local = (base + timedelta(days=1)).replace(hour=hm.hour, minute=hm.minute, second=0, microsecond=0)
        elif len(t) <= 5:
            hm = datetime.strptime(t, '%H:%M')
            local = base.replace(hour=hm.hour, minute=hm.minute, second=0, microsecond=0)
            local += timedelta(days=1) if local <= base else timedelta()
        else:
            local = datetime.strptime(t.replace('t', ' '), '%Y-%m-%d %H:%M')
    except (ValueError, IndexError) as e:
        raise ValueError(f'"{text}" is not a time Tutti reads: use "2026-11-16 07:00", "tomorrow 07:00", "07:00" or "now" '
                         '(local time)') from e
    return tz.to_utc(local, tz.zone_name()).timestamp()

SEEN_BACK_MINUTES = 120   # an open-ended absence ends once seen here this long after it started (it never looked away)


def window(v, now):
    """'planned' (it starts later), 'active' or 'over' for a pause; one without `from` is always active."""
    if 'from' not in v:
        return 'active'
    if v.get('until') is not None and now >= v['until']:
        return 'over'
    return 'active' if now >= v['from'] - LEAD_MINUTES * 60 else 'planned'


def planned_round(cfg, pst, now, stop_fn=None, say=lambda m: None):
    """Each dispatch round: a planned absence that started hands its running work over once; one that is over, or
    whose end was left open and whose target presence saw away and then back, is removed. Returns what it did."""
    from . import config
    folder, done = config.state_dir(cfg), []
    with changed(folder) as data:
        for k, v in list(data['paused'].items()):
            if 'from' not in v:
                continue
            state, back = window(v, now), False
            rec = (pst.get('targets') or {}).get(k) or {}
            if state == 'active' and now >= v['from']:
                if rec.get('state') in ('away', 'asleep'):
                    v['seen_away'] = True
                # open-ended ("until it is seen again"): back once presence saw it away and then here, or saw it here for
                # SEEN_BACK_MINUTES after the absence started (it never looked away: the owner kept it, or came back first)
                here_since = max(v['from'], rec.get('since') or 0)
                back = v.get('until') is None and rec.get('state') == 'present' and (
                    v.get('seen_away') or now - here_since >= SEEN_BACK_MINUTES * 60)
                if not v.get('handed'):
                    v['handed'] = True
                    kind, _, name = k.partition(':')
                    if stop_fn:
                        stop_fn(kind, name)
                    done.append(f'{k}: planned absence started; its running work was handed over')
            if state == 'over' or back:
                data['paused'].pop(k)
                done.append(f'{k}: {"back early: presence saw it again" if back else "planned absence over"}; it takes work again')
    for line in done:
        say(line)
    return done


def set_setting(folder, name, value):
    """A setting the owner changed on the dashboard (settings.autocompact_pct); None takes it back to the config's."""
    with changed(folder) as data:
        if value is None:
            data['settings'].pop(name, None)
        else:
            data['settings'][name] = value


def apply(raw, data):
    """The switches written into the raw config (before defaults and opt-outs): `enabled: <on>` and `paused: {by, at, note}`
    on each named entry. A switch for an entry the config no longer has is ignored."""
    now = time.time()
    blocked_hours(raw, now)
    for field in ('enabled', 'paused'):
        for k, v in (data.get(field) or {}).items():
            if field == 'paused' and window(v, now) != 'active':
                continue   # a planned absence not started yet (or over): the entry works as usual
            kind, _, name = k.partition(':')
            section = (raw.get(SECTIONS.get(kind, '')) or {}) if isinstance(raw, dict) else {}
            if name not in section:
                continue
            entry = dict(section[name] or {})
            entry[field] = v['on'] if field == 'enabled' else v
            section[name] = entry
    return raw


def blocked_spec(kind, entry):
    """(window spec, days) of an entry's recurring blocked hours, or None: a device's `hours`, a machine's `limit` at 0 %."""
    e = entry or {}
    if kind == 'device' and isinstance(e.get('hours'), dict):
        return e['hours'].get('window'), e['hours'].get('days')
    lim = e.get('limit')
    if kind == 'machine' and isinstance(lim, dict) and lim.get('pct') == 0:
        return lim.get('window'), lim.get('days')
    return None


def blocked_hours(raw, now):
    """Marks every device or machine inside its blocked hours (or within LEAD_MINUTES of their start) as paused, as a
    planned absence: {by: 'blocked hours', from, until, blocked: true}. An owner's own pause stays as it is."""
    from . import hours
    if not isinstance(raw, dict):
        return raw
    for kind, section in (('device', 'devices'), ('machine', 'machines')):
        for name, entry in (raw.get(section) or {}).items():
            spec = blocked_spec(kind, entry)
            if not spec or (entry or {}).get('paused'):
                continue
            try:
                w = hours.window(spec[0], spec[1], now, lead=LEAD_MINUTES * 60)
            except ValueError:
                continue   # the config check names a bad window
            if w:
                raw[section][name] = {**entry, 'paused': {'by': 'blocked hours', 'at': now, 'from': w[0], 'until': w[1],
                                                          'blocked': True, 'note': f'not used {spec[0]} (local time)'}}
    return raw


def blocked_round(cfg, now, stop_fn=None):
    """A blocked-hours window that started hands its running work over once (as a planned absence's start does)."""
    done = []
    with changed(config_folder(cfg)) as data:
        handed = data.setdefault('handed', {})
        for kind, section in (('device', 'devices'), ('machine', 'machines')):
            for name, entry in (cfg.get(section) or {}).items():
                p = (entry or {}).get('paused') or {}
                k = f'{kind}:{name}@{int(p.get("from") or 0)}'
                if not p.get('blocked') or now < p['from'] or k in handed:
                    continue
                handed[k] = now
                if stop_fn:
                    stop_fn(kind, name)
                done.append(f'{kind}:{name}: its blocked hours started; its running work was handed over')
        for k in [k for k, at in handed.items() if now - at > 2 * 86400]:
            handed.pop(k)
    return done


def config_folder(cfg):
    from . import config
    return config.state_dir(cfg)


def paused_note(entry):
    """'paused by <who> at <time>: <note>' for an entry the owner paused, else ''."""
    p = (entry or {}).get('paused')
    if not p:
        return ''
    at = time.strftime('%Y-%m-%d %H:%M', tz.localtime(p.get('at') or 0))
    if p.get('blocked'):
        end = time.strftime('%H:%M', tz.localtime(p['until']))
        return f'blocked hours: {p.get("note", "not used")}; free again at {end}; change them in Settings or with `tutti devices hours`'
    if 'from' in p:
        start = time.strftime('%a %d %b %H:%M', tz.localtime(p['from']))
        end = time.strftime('%a %d %b %H:%M', tz.localtime(p['until'])) if p.get('until') is not None else 'it is seen again'
        return (f'away (planned by {p.get("by") or "the owner"}) from {start} until {end}' + (f': {p["note"]}' if p.get('note') else '')
                + '; no new work starts on it; resume it from the dashboard or with `tutti resume`')
    return f'paused by {p.get("by") or "the owner"} at {at}' + (f': {p["note"]}' if p.get('note') else '') + \
        '; resume it from the dashboard or with `tutti resume`'


def alive(pid):
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def is_tutti(pid, proc='/proc'):
    """The pid is alive and runs tutti: a pid the system gave to another program since is never taken for a job."""
    if not alive(pid):
        return False
    try:
        with open(os.path.join(proc, str(pid), 'cmdline'), 'rb') as f:
            return b'tutti' in f.read()
    except OSError:
        return False


def running_jobs(folder, kind, name, is_alive=None):
    """[(job log, pid)] of the running tutti jobs bound to the item (`<log>.target` names it, `<log>.pid` the process)."""
    want = key(kind, name)
    out = []
    for target in sorted(glob.glob(os.path.join(os.path.expanduser(folder), '*', '*.log.target'))):
        if os.path.basename(os.path.dirname(target)) not in JOB_FOLDERS:
            continue
        log = target[:-len('.target')]
        if os.path.exists(log + '.rc'):
            continue
        with open(target, encoding='utf8') as f:
            if want not in f.read().split():
                continue
        try:
            with open(log + '.pid', encoding='utf8') as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            continue
        if (is_alive or is_tutti)(pid):
            out.append((log, pid))
    return out


OPT_IN_WHY = {'costs_money': 'costs money per use', 'android': 'Android lint and unit runs (Termux)', 'opt_in': 'opt-in'}


def opt_in_reason(kind, entry):
    """Why an entry is opt-in ('costs_money', 'android', 'opt_in'), or '' when it is not."""
    if entry.get('costs_money'):
        return 'costs_money'
    if kind == 'machine' and entry.get('transport') == 'android':
        return 'android'
    return 'opt_in' if entry.get('opt_in') else ''


def view(cfg, data):
    """The dashboard's controls: every opt-in item with its state, and every pausable item with who paused it and when."""
    opt_in = []
    for kind, section in (('worker', 'workers'), ('account', 'accounts'), ('machine', 'machines')):
        for name, e in (cfg.get('raw_entries', {}).get(section) or {}).items():
            why = opt_in_reason(kind, e or {})
            if why:
                on = (data['enabled'].get(key(kind, name)) or {}).get('on', (e or {}).get('enabled') is True)
                opt_in.append({'kind': kind, 'name': name, 'on': bool(on), 'why': OPT_IN_WHY[why]})
    from . import dispatch  # dispatch imports this module
    if any(s['kind'] in dispatch.LIGHT and 'android' in s['targets'] for s in dispatch.suites(cfg)):
        for name, d in (cfg.get('devices') or {}).items():
            on = (data['enabled'].get(key('device', name)) or {}).get('on', (d or {}).get('enabled') is True)
            opt_in.append({'kind': 'device', 'name': name, 'on': bool(on), 'why': 'Android lint and unit runs (adb)'})
    now = time.time()
    paused = [{'kind': k.partition(':')[0], 'name': k.partition(':')[2], **v, 'window': window(v, now)} for k, v in data['paused'].items()]
    return {'opt_in': opt_in, 'paused': paused}
