"""Telemetry: every command and feature use recorded on this computer (always), and sent only with the owner's consent.

Local: `<state>/telemetry/events.jsonl` (rotated at MAX_BYTES, KEEP old files). One JSON object a line: `v`, `ts`, `kind`
(command, feature, crash, dashboard), `name`, and fields that passed `scrub.fields` (categories and numbers only).
Sending (opt-in, `<state>/telemetry/consent.json`): what was recorded with consent on is also queued in `outbox.jsonl` and
flushed in batches (`flush`), to Sentry's envelope API (sentry.py) or to `telemetry.endpoint`; a failed send keeps the
queue for the next try, so being offline loses nothing (the queue is capped at OUTBOX_MAX lines, the oldest go first).
Recording never raises and never slows a command: an unwritable folder only means nothing is recorded.
docs/TELEMETRY.md lists exactly what is collected and what never is."""
import json
import os
import platform
import re
import secrets
import socket
import sys
import time
import traceback
from datetime import datetime, timezone

from . import __version__, scrub

DIR = 'telemetry'
EVENTS = 'events.jsonl'
OUTBOX = 'outbox.jsonl'
CONSENT = 'consent.json'
SENDS = 'send.json'
MAX_BYTES = 2_000_000
KEEP = 3
OUTBOX_MAX = 2000
FLUSH_SECONDS = 600
ENV_DSN = 'TUTTI_SENTRY_DSN'
ENV_OFF = 'TUTTI_TELEMETRY'   # `off`: record nothing at all, not even locally (tests, a locked-down machine)
PACKAGE = os.path.dirname(os.path.abspath(__file__))
_ctx = {'folder': None, 'hosts': (), 'environment': 'production', 'command': ''}


def env_file(env=None):
    """~/.config/tutti/telemetry.env: where `tutti telemetry setup` keeps the DSN (mode 600)."""
    env = os.environ if env is None else env
    base = env.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(base, 'tutti', 'telemetry.env')


def folder_of(state_dir):
    return os.path.join(state_dir, DIR)


def personal_hosts(cfg=None):
    """Names that must never leave this computer: its own host name and every machine's host and user from the config."""
    names = {socket.gethostname(), platform.node()}
    try:
        names.add(os.getlogin())
    except OSError:
        pass
    for m in ((cfg or {}).get('machines') or {}).values():
        names.update(str(m.get(k) or '') for k in ('host', 'user', 'name'))
    return tuple(sorted(n for n in names if n and len(n) > 2))


def configure(state_dir, cfg=None, command='', env=None):
    """Turns recording on for this process (the CLI's entry and the dashboard server). Without it record() does nothing,
    so tests and library use never write telemetry."""
    env = os.environ if env is None else env
    if str(env.get(ENV_OFF, '')).lower() in ('off', '0', 'false', 'no'):
        _ctx['folder'] = None
        return None
    _ctx.update(folder=folder_of(state_dir), hosts=personal_hosts(cfg), command=scrub.category(command) or '',
                environment=scrub.category(((cfg or {}).get('telemetry') or {}).get('environment') or 'production'))
    return _ctx['folder']


TOOLS = ('claude', 'cursor', 'agy', 'antigravity', 'copilot', 'gemini', 'codex', 'aider', 'ollama', 'grok', 'qwen', 'llama',
         'deepseek', 'gpt')


def tool_of(name):
    """A worker or account name reduced to the tool it is (`claude`, `cursor`, `agy`...) or `other`: a name the owner made
    up never leaves as it is."""
    n = str(name or '').lower()
    return next((t for t in TOOLS if n.startswith(t) or f'-{t}' in n), 'other') if n else 'none'


def machine_kind(machine):
    """A machine reduced to its kind: its transport (ssh, local, wsl, android), never its name or host."""
    m = machine or {}
    return scrub.category('android' if m.get('android') else m.get('transport') or 'ssh')


def unconfigure():
    _ctx['folder'] = None


def now_iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).isoformat(timespec='seconds')


# ---- consent

def read_json(path, default):
    try:
        with open(path, encoding='utf8') as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def write_json(path, data, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(data, f, indent=1)
    if mode:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def consent(folder):
    """{'send': True | False | None (never asked), 'at', 'by', 'install_id'}. The install id is random, made here once:
    it counts installs and links one install's events, and says nothing about the person or the computer."""
    data = read_json(os.path.join(folder, CONSENT), {})
    data.setdefault('send', None)
    if not data.get('install_id'):
        data['install_id'] = secrets.token_hex(16)
        try:
            write_json(os.path.join(folder, CONSENT), data)
        except OSError:
            pass
    return data


def set_consent(folder, on, by='cli'):
    data = consent(folder)
    data.update(send=bool(on), at=now_iso(), by=scrub.category(by))
    write_json(os.path.join(folder, CONSENT), data)
    return data


def sending(folder):
    return bool(folder) and consent(folder)['send'] is True


# ---- the DSN

def read_env_file(path):
    """KEY=value lines (an optional `export `); quotes around a value are dropped."""
    out = {}
    try:
        with open(path, encoding='utf8') as f:
            for line in f:
                m = re.match(r'\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$', line)
                if m:
                    out[m.group(1)] = m.group(2).strip('\'"')
    except OSError:
        pass
    return out


def dsn(cfg=None, env=None):
    """(the DSN, where it came from): $TUTTI_SENTRY_DSN, then ~/.config/tutti/telemetry.env, then telemetry.sentry_dsn."""
    env = os.environ if env is None else env
    if env.get(ENV_DSN):
        return env[ENV_DSN].strip(), ENV_DSN
    found = read_env_file(env_file(env)).get(ENV_DSN)
    if found:
        return found, env_file(env)
    own = ((cfg or {}).get('telemetry') or {}).get('sentry_dsn') or ''
    return (own.strip(), 'telemetry.sentry_dsn') if own else ('', '')


def save_dsn(value, env=None):
    """Writes the DSN to ~/.config/tutti/telemetry.env, readable by this user only."""
    from . import sentry
    sentry.parse_dsn(value)   # refuses a malformed DSN before anything is written
    path = env_file(env)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf8') as f:
        f.write(f'# Tutti telemetry: the Sentry DSN (tutti telemetry setup). Keep this file private.\n{ENV_DSN}={value.strip()}\n')
    os.chmod(path + '.tmp', 0o600)
    os.replace(path + '.tmp', path)
    return path


# ---- recording

def rotate(path, max_bytes=MAX_BYTES, keep=KEEP):
    try:
        if os.path.getsize(path) < max_bytes:
            return
    except OSError:
        return
    for i in range(keep - 1, 0, -1):
        if os.path.exists(f'{path}.{i}'):
            os.replace(f'{path}.{i}', f'{path}.{i + 1}')
    os.replace(path, f'{path}.1')
    if os.path.exists(f'{path}.{keep + 1}'):
        os.remove(f'{path}.{keep + 1}')


def append(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf8') as f:
        f.write(json.dumps(record, separators=(',', ':')) + '\n')


def make_event(kind, name, data=None, ts=None):
    return {'v': 1, 'ts': now_iso(ts), 'kind': scrub.category(kind), 'name': scrub.category(name), **scrub.fields(data)}


def record(kind, name, **data):
    """Records one event when this process is configured; returns it (or None). Never raises."""
    folder = _ctx['folder']
    if not folder:
        return None
    try:
        ev = make_event(kind, name, data)
        path = os.path.join(folder, EVENTS)
        rotate(path)
        append(path, ev)
        if sending(folder):
            queue(folder, {'type': 'usage', 'event': ev})
        return ev
    except (OSError, ValueError, TypeError):
        return None


def queue(folder, item):
    path = os.path.join(folder, OUTBOX)
    append(path, item)
    cap(path)


def cap(path, limit=OUTBOX_MAX):
    try:
        with open(path, encoding='utf8') as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) > limit:
        with open(path, 'w', encoding='utf8') as f:
            f.writelines(lines[-limit:])


def read_events(folder, since=None):
    """Every recorded event, oldest first, the rotated files included; `since`: an ISO time."""
    out = []
    path = os.path.join(folder, EVENTS)
    for p in [f'{path}.{i}' for i in range(KEEP, 0, -1)] + [path]:
        try:
            with open(p, encoding='utf8') as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict) and (not since or str(ev.get('ts', '')) >= since):
                        out.append(ev)
        except OSError:
            continue
    return out


# ---- crashes and failures

def frames(tb):
    """A stack trace's allowlisted frames: Tutti's own file (relative to the package) or the last two parts of another,
    the function and the line number. Never the source line or a local variable."""
    out = []
    for fr in traceback.extract_tb(tb)[-40:]:
        out.append({'filename': scrub.path(fr.filename, PACKAGE), 'function': scrub.category(fr.name) or '?',
                    'lineno': fr.lineno or 0, 'in_app': os.path.abspath(fr.filename).startswith(PACKAGE + os.sep)})
    return out


def base_event(level, tags=None):
    """The fields every event sent to Sentry carries: release, environment, OS and Python. No host name (server_name),
    no user name, no IP: the user id is the random install id."""
    folder = _ctx['folder']
    tags = {'command': _ctx['command'] or 'none', 'os': scrub.category(sys.platform),
            'python': f'{sys.version_info.major}.{sys.version_info.minor}', **scrub.fields(tags)}
    ev = {'event_id': secrets.token_hex(16), 'timestamp': time.time(), 'platform': 'python', 'level': level,
          'logger': 'tutti', 'release': f'tutti@{__version__}', 'environment': _ctx['environment'], 'tags': tags,
          'contexts': {'os': {'name': platform.system() or sys.platform, 'version': scrub.category(platform.release())},
                       'runtime': {'name': platform.python_implementation(), 'version': platform.python_version()}}}
    if folder:
        ev['user'] = {'id': consent(folder)['install_id']}
    return ev


def crash_event(exc_type, exc, tb, tags=None):
    ev = base_event('fatal' if tags and tags.get('where') == 'cli' else 'error', tags)
    ev['exception'] = {'values': [{'type': scrub.category(exc_type.__name__),
                                   'value': scrub.text(exc, hosts=_ctx['hosts'], limit=300),
                                   'module': scrub.category(getattr(exc_type, '__module__', '')),
                                   'stacktrace': {'frames': frames(tb)}}]}
    return ev


def message_event(level, message, tags=None):
    ev = base_event(level, tags)
    ev['message'] = {'formatted': scrub.text(message, hosts=_ctx['hosts'], limit=300)}
    return ev


def report(ev, kind='crash', name=''):
    """Records a crash or failure locally (type and place only) and queues the Sentry event when sending is on."""
    folder = _ctx['folder']
    if not folder:
        return None
    exc = (ev.get('exception') or {}).get('values') or [{}]
    record(kind, name or exc[0].get('type') or 'message', where=ev['tags'].get('where'), level=ev['level'],
           frame=(exc[0].get('stacktrace', {}).get('frames') or [{}])[-1].get('function'))
    try:
        if sending(folder):
            queue(folder, {'type': 'event', 'event': ev})
    except OSError:
        return None
    return ev


def capture_exception(exc_type, exc, tb, **tags):
    try:
        return report(crash_event(exc_type, exc, tb, tags), 'crash')
    except Exception:   # telemetry must never turn one crash into two
        return None


def capture_failure(command, rc, **tags):
    """A command that ended with a non-zero exit code: its name and the code, never its arguments."""
    try:
        ev = message_event('error', f'tutti {scrub.category(command)} exited {int(rc)}', {'rc': int(rc), **tags})
        ev['fingerprint'] = ['tutti-exit', str(scrub.category(command)), str(int(rc))]
        return report(ev, 'failure', command)
    except Exception:   # never fail the command a second time
        return None


# ---- flushing the queue

def due(folder, clock=time.time, every=FLUSH_SECONDS):
    s = read_json(os.path.join(folder, SENDS), {})
    return clock() - float(s.get('last_try') or 0) >= every


def take_outbox(folder):
    """Moves the queue aside (atomic) so records written meanwhile go to a new one; returns (items, the taken path)."""
    path = os.path.join(folder, OUTBOX)
    taken = f'{path}.{os.getpid()}.sending'
    try:
        os.replace(path, taken)
    except OSError:
        return [], None
    items = []
    with open(taken, encoding='utf8') as f:
        for line in f:
            try:
                items.append(json.loads(line))
            except ValueError:
                continue
    return items, taken


def give_back(folder, items):
    for it in items:
        queue(folder, it)


def flush(folder, cfg=None, env=None, post_fn=None, clock=time.time, force=False):
    """Sends the queue when sending is on, a target is set and it is due (or forced). Returns a short status dict."""
    from . import sentry
    if not folder or not sending(folder):
        return {'sent': 0, 'why': 'sending is off: tutti telemetry on'}
    target = sentry.target(cfg, env)
    if not target:
        return {'sent': 0, 'why': 'no Sentry DSN or endpoint: tutti telemetry setup'}
    if not force and not due(folder, clock):
        return {'sent': 0, 'why': 'sent recently'}
    items, taken = take_outbox(folder)
    status = {'last_try': clock()}
    try:
        sent, left = sentry.send_all(target, items, consent(folder)['install_id'], post_fn)
    except Exception as e:   # one bad item or a broken network never loses the queue
        sent, left = 0, items
        status['last_error'] = scrub.text(f'{type(e).__name__}: {e}', hosts=_ctx['hosts'], limit=200)
    give_back(folder, left)
    if taken:
        os.remove(taken)
    if sent and not left:
        status['last_ok'] = clock()
    elif left and 'last_error' not in status:
        status['last_error'] = f'{len(left)} item(s) not accepted: kept for the next try'
    old = read_json(os.path.join(folder, SENDS), {})
    write_json(os.path.join(folder, SENDS), {**old, **status, 'sent_total': int(old.get('sent_total') or 0) + sent})
    return {'sent': sent, 'left': len(left), 'why': status.get('last_error', 'ok')}


def status(folder, cfg=None, env=None):
    """What `tutti telemetry status` and the dashboard show."""
    from . import sentry
    c = consent(folder)
    value, source = dsn(cfg, env)
    try:
        with open(os.path.join(folder, OUTBOX), encoding='utf8') as f:
            queued = sum(1 for _ in f)
    except OSError:
        queued = 0
    s = read_json(os.path.join(folder, SENDS), {})
    endpoint = ((cfg or {}).get('telemetry') or {}).get('endpoint') or ''
    return {'send': c['send'], 'asked': c['send'] is not None, 'dsn_set': bool(value), 'dsn_from': source if value else '',
            'dsn_host': sentry.host_of(value), 'endpoint_set': bool(endpoint), 'queued': queued, 'folder': folder,
            'last_try': s.get('last_try'), 'last_ok': s.get('last_ok'), 'last_error': s.get('last_error'),
            'sent_total': s.get('sent_total', 0), 'events': os.path.join(folder, EVENTS)}
