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

from . import tz

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
            'settings': dict(data.get('settings') or {})}


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
        tmp = f'{path(folder)}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf8') as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path(folder))


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


def set_paused(folder, kind, name, paused, source='dashboard', note='', clock=time.time):
    with changed(folder) as data:
        k = key(kind, name)
        if paused:
            data['paused'][k] = {'by': who(source), 'at': clock(), 'note': note}
        else:
            data['paused'].pop(k, None)


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
    for field in ('enabled', 'paused'):
        for k, v in (data.get(field) or {}).items():
            kind, _, name = k.partition(':')
            section = (raw.get(SECTIONS.get(kind, '')) or {}) if isinstance(raw, dict) else {}
            if name not in section:
                continue
            entry = dict(section[name] or {})
            entry[field] = v['on'] if field == 'enabled' else v
            section[name] = entry
    return raw


def paused_note(entry):
    """'paused by <who> at <time>: <note>' for an entry the owner paused, else ''."""
    p = (entry or {}).get('paused')
    if not p:
        return ''
    at = time.strftime('%Y-%m-%d %H:%M', tz.localtime(p.get('at') or 0))
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
    paused = [{'kind': k.partition(':')[0], 'name': k.partition(':')[2], **v} for k, v in data['paused'].items()]
    return {'opt_in': opt_in, 'paused': paused}
