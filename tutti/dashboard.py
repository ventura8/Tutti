"""`tutti dashboard`: a local web dashboard (an installable PWA) over the state Tutti already keeps.
Standard library only: a threaded HTTP server bound to 127.0.0.1, static files from tutti/web, and /api/state as JSON.
Machines and workers are probed in a background thread (ssh and model servers can be slow); the rest is read per request."""
import gzip
import hashlib
import hmac
import html
import json
import math
import mimetypes
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import (
    __version__,
    authweb,
    autocompact,
    autopilot,
    conduct,
    config,
    controls,
    dashtelemetry,
    fill,
    handover,
    helper,
    owner,
    pacing,
    projects,
    readers,
    releases,
    remote,
    scouts,
    state,
    telemetry,
    transfer,
    trial,
    update,
    workers,
)

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
BIND = '127.0.0.1'
JSON_TYPE = 'application/json'
TYPES = {'.webmanifest': 'application/manifest+json', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
         '.html': 'text/html; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': JSON_TYPE}
# Never echoes the request path back: nothing the browser sent is reflected into a response.
NOT_FOUND = b'{"error": "this path is not part of the dashboard: open / instead"}'
TOKEN_SLOT = b'__TUTTI_TOKEN__'   # index.html carries this server's token in <meta name="tutti-token">
INDEX = 'index.html'
VERSION_SLOT = b'__TUTTI_VERSION__'   # index.html's <meta name="tutti-version"> and sw.js's cache name: the running version
MAX_BODY = 4096
NOTE_MAX = 200
RESET_PAST = timedelta(days=1)
RESET_AHEAD = timedelta(days=62)
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "manifest-src 'self'; worker-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class Collector:
    """Holds the latest probes and refreshes them every `probe_seconds` in a daemon thread."""

    def __init__(self, cfg, probe_fn=remote.probe, available_fn=workers.available, clock=time.time, reader=None, stored_fn=None,
                 activity_fn=projects.probe_activity, prs_fn=releases.all_prs, readings_fn=remote.device_readings):
        self.cfg, self.probe_fn, self.available_fn, self.clock = cfg, probe_fn, available_fn, clock
        self.readings_fn = readings_fn
        self.activity_fn, self.prs_fn = activity_fn, prs_fn
        self.stored_fn = stored_fn or (lambda m: transfer.stored(cfg, m))
        self.reader = reader or readers.Readers(cfg, clock=clock)
        self.probes = {}
        self.lock = threading.Lock()
        self.read_lock = threading.Lock()   # the probe thread and a Refresh readings click never run the readers at once
        self.stop = threading.Event()
        self.env = os.environ   # the scouts' webhook variables (names only ever leave this process)
        self.update_error = None
        self.updating = threading.Lock()   # one update round at a time in this dashboard (the daily one or a click)

    def read_usage(self, force=False):
        """The automatic readings: each account's reader (cached per command unless `force`) and limit messages in recent job
        logs, appended to the usage log when they say something new."""
        with self.read_lock:
            now = datetime.fromtimestamp(self.clock(), timezone.utc)
            found = self.reader.read_all(now, force)
            readers.record(state.usage_log_path(self.cfg), found, self.reader.scan_jobs(now), now)
            readers.record_files(state.usage_log_path(self.cfg), self.cfg['accounts'])
            return found

    def refresh_readings(self):
        """Refresh readings (the dashboard button): the readers now, without waiting for the next probe or their interval."""
        found = self.read_usage(force=True)
        with self.lock:
            self.probes = {**self.probes, 'readers': found}
        return found

    def refresh(self):
        fresh = state.probe_all(self.cfg, self.probe_fn, self.available_fn, clock=self.clock, stored_fn=self.stored_fn,
                                readings_fn=self.readings_fn)
        fresh['activity'] = projects.probe_all(self.cfg, self.activity_fn)
        fresh['prs'] = self.prs_fn(self.cfg)
        try:
            fresh['readers'] = self.read_usage()
        except (OSError, ValueError) as e:   # a broken usage log must not hide machines and workers
            fresh['readers'] = {}
            fresh['error'] = f'automatic usage readings failed ({e}): fix the usage log, the next probe tries again'
        with self.lock:
            self.probes = fresh
        return fresh

    def autopilot_tick(self):
        """One autopilot round after each probe while it is on: the probes just taken decide (pace, slots, heat)."""
        if not autopilot.enabled(self.cfg):
            return None
        snap = self.snapshot()
        states = {a['name']: a for a in snap['usage']['accounts']}
        from . import sharing  # a household member's heartbeat rides on the round; shared machines offer its grant only
        machines = sharing.limit_items(self.cfg, [dict(m) for m in snap['machines']['items']], self.clock())
        beat = sharing.beat(self.cfg, datetime.fromtimestamp(self.clock(), timezone.utc), machines)
        return autopilot.tick(self.cfg, states, machines, beat=beat)

    def update_round(self, force=False):
        """`tutti update` in the background: the daily round (update.auto, update.every_hours) or a click on Update now.
        Returns False when a round is already running here or none is due."""
        if not (force or update.due(self.cfg, self.clock(), update.load(self.cfg))) or not self.updating.acquire(blocking=False):
            return False

        def go():
            try:
                update.run(self.cfg, clock=self.clock, when_due=not force)
            except Exception as e:  # Busy (another round runs), or a failure update.run already noted in the round's file
                self.update_error = None if isinstance(e, update.Busy) else f'{type(e).__name__}: {e}'
            finally:
                self.updating.release()
        threading.Thread(target=go, name='tutti-update', daemon=True).start()
        return True

    def scouts_tick(self):
        """One scouts round after each probe: answers collected, due watches and queued questions sent within the allowance."""
        if not self.cfg.get('scouts'):
            return []
        states = {a['name']: a for a in self.snapshot()['usage']['accounts']}
        try:
            events, error = scouts.tick(self.cfg, states, env=self.env), None
        except scouts.ScoutError as e:
            events, error = [], str(e)
        with self.lock:
            self.probes = {**self.probes, 'scout_error': error}
        return events

    def loop(self):
        while not self.stop.is_set():
            try:
                self.refresh()
                self.update_round()
                self.autopilot_tick()
                self.scouts_tick()
                dashtelemetry.tick(self.cfg)
            except Exception as e:  # a probe bug must not end the loop; the page shows the last good data and its age
                telemetry.capture_exception(type(e), e, e.__traceback__, where='dashboard-loop')
                state_error = f'probing failed ({type(e).__name__}: {e}); the dashboard keeps the last results'
                with self.lock:
                    self.probes = {**self.probes, 'error': state_error}
            self.stop.wait(float(self.cfg['dashboard']['probe_seconds']))

    def start(self):
        threading.Thread(target=self.loop, name='tutti-probes', daemon=True).start()

    def snapshot(self):
        with self.lock:
            probes = dict(self.probes)
        out = state.assemble(self.cfg, probes)
        state.with_readers(out['usage'], probes.get('readers'))
        out['main'] = conduct.main_state(self.cfg, out['usage']['accounts'])
        projects.tag(self.cfg, out['jobs']['items'])
        out['projects'] = projects.projects_state(self.cfg, out['jobs']['items'], out['machines']['items'], probes.get('activity'),
                                                  out['main']['running'], out['generated_at'])
        releases.attach(out['projects'], probes.get('prs'))
        out['probe_error'] = probes.get('error')
        out['autopilot'] = autopilot.view(self.cfg)
        accounts = {a['name']: a for a in out['usage']['accounts']}
        out['scouts'] = {**scouts.view(self.cfg, accounts, self.env), 'error': probes.get('scout_error'), 'trial': trial.view(self.cfg)}
        out['controls'] = controls.view(self.cfg, controls.read(config.state_dir(self.cfg)))
        out['telemetry'] = dashtelemetry.view(self.cfg)
        out['controls']['opt_in'].append({'kind': 'autopilot', 'name': 'fill_room', 'on': out['autopilot']['fill_room'],
                                          'why': 'free room on the trigger accounts is filled from the repos\' sources by itself'})
        out['controls']['autocompact'] = autocompact.view(self.cfg)
        autocompact.tag_jobs(self.cfg, out['jobs']['items'])
        last = update.load(self.cfg)
        out['updates'] = {**update.view(self.cfg, last), 'running': self.updating.locked(), 'error': (last or {}).get('error')}
        return out

    def reload(self):
        """The config read again after a switch, so routing, workers and the page see it at once."""
        if self.cfg.get('_path'):
            fresh = config.load(self.cfg['_path'])
            with self.read_lock:   # the usage readers follow the switch too: a resumed account is read, a paused one not
                self.cfg = fresh
                if hasattr(self.reader, 'cfg'):
                    self.reader.cfg = fresh
        return self.cfg


LOOPBACK = ('127.0.0.1', 'localhost', '::1')


def allowed_host(host, port, origins=()):
    """Only the loopback names with our port, and the hosts of dashboard.origins (sign-in required for those): a page on
    another site that rebinds its DNS to 127.0.0.1 is refused."""
    return host in (f'127.0.0.1:{port}', f'localhost:{port}', f'[::1]:{port}') or host in {urlsplit(o).netloc for o in origins}


def bind_of(cfg):
    return (cfg.get('dashboard') or {}).get('bind') or BIND


def exposed(cfg):
    """True when others may reach the dashboard (a non-loopback bind, or dashboard.origins): sign-in is then required."""
    return bind_of(cfg) not in LOOPBACK or bool(authweb.origins(cfg))


def static_path(url_path):
    """The file under tutti/web for a request path, or None when it is outside it or missing."""
    rel = url_path.split('?', 1)[0].lstrip('/') or INDEX
    full = os.path.realpath(os.path.join(WEB, rel))
    if not full.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(full):
        return None
    return full


def same_origin(origin, host, origins=()):
    """The page's own origin: plain HTTP on the request's host, or one of dashboard.origins (an HTTPS name) for that host."""
    parts = urlsplit(origin or '')
    if (origin or '').rstrip('/') in origins and parts.netloc == host:
        return True
    return parts.scheme == 'http' and parts.netloc == host and parts.path == '' and not parts.query


def check_post(headers, host, token, origins=()):
    """None when a POST may write, else (status, why). Same origin only: the Origin header must be this dashboard's, a
    browser's Sec-Fetch-Site must say same-origin, the body is JSON and the page's token is sent back (with sign-in on, the
    session's CSRF token)."""
    if not same_origin(headers.get('Origin'), host, origins):
        return 403, 'only the dashboard page itself may save a reading (the Origin header is not this dashboard)'
    if headers.get('Sec-Fetch-Site', 'same-origin') != 'same-origin':
        return 403, 'only the dashboard page itself may save a reading (the request is cross-site)'
    if not (headers.get('Content-Type') or '').startswith(JSON_TYPE):
        return 415, 'send the reading as application/json'
    if not hmac.compare_digest(headers.get('X-Tutti-Token') or '', token):
        return 403, 'the dashboard restarted since this page loaded: reload the page, then save again'
    return None


def _number(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def parse_resets(resets, now):
    """None, or the posted reset time when it is a time from a day ago to two months ahead; else a ValueError."""
    if resets is None:
        return None
    try:
        resets = pacing.parse_time(resets)
    except (TypeError, ValueError) as e:
        raise ValueError('resets must be a date and time such as 2026-12-01T00:00Z') from e
    if not now - RESET_PAST <= resets <= now + RESET_AHEAD:
        raise ValueError('resets must be the next reset: from a day ago to two months ahead')
    return resets


def parse_reading(cfg, data, now):
    """(account, used, resets or None, note) from a posted reading, or a ValueError that says what to fix."""
    if not isinstance(data, dict):
        raise ValueError('send {"account": ..., "used": 0-100, "resets": ISO time (optional), "note": text (optional)}')
    account = data.get('account')
    if account not in cfg['accounts']:
        raise ValueError(f'no account "{account}" in the config: the accounts are {", ".join(cfg["accounts"]) or "(none)"}')
    used = _number(data.get('used'))
    if used is None or not 0 <= used <= 100:
        raise ValueError('used must be a percentage from 0 to 100')
    resets = parse_resets(data.get('resets') or None, now)
    note = data.get('note') or ''
    if not isinstance(note, str) or len(note) > NOTE_MAX:
        raise ValueError(f'note must be text of at most {NOTE_MAX} characters')
    if 'key' in data and not helper.key_ok(config.state_dir(cfg), data['key']):
        raise PermissionError('this reading helper is from another install or an old key: drag the button from /helper again')
    return account, float(used), resets, note


def save_reading(cfg, raw, now=None):
    """(status, answer) for a POST /api/reading body that passed check_post. Writes the same usage log as `tutti usage log`."""
    now = now or datetime.now(timezone.utc)
    try:
        data = json.loads(raw or b'null')
        account, used, resets, note = parse_reading(cfg, data, now)
    except PermissionError as e:
        return 403, {'error': str(e)}
    except ValueError as e:
        return 400, {'error': str(e)}
    source = 'helper' if isinstance(data, dict) and 'key' in data else 'dashboard'
    entry = pacing.append_log(state.usage_log_path(cfg), account, used, now, note or f'from the {source}', resets=resets, source=source)
    return 200, {'ok': True, 'reading': entry}


def save_main(cfg, raw):
    """(status, answer) for POST /api/main {"agent": <worker>}: the owner's switch of the main agent."""
    try:
        data = json.loads(raw or b'null')
        agent = data.get('agent') if isinstance(data, dict) else None
        conduct.choose(cfg, agent)
    except ValueError:
        return 400, {'error': 'send {"agent": "<worker>"} as JSON'}
    except conduct.ConductError as e:
        return 400, {'error': str(e)}
    return 200, {'ok': True, 'agent': agent}


def save_owner(cfg, raw):
    """(status, answer) for POST /api/owner: the owner's mode ("I'm away until…") and schedule edits (tutti/owner.py)."""
    try:
        data = json.loads(raw or b'null')
    except ValueError:
        data = None
    return owner.change(cfg, config.state_dir(cfg), data)


def save_autopilot(cfg, raw):
    """(status, answer) for POST /api/autopilot {"enabled": true|false, "fill_room": true|false}: the dashboard's switches
    (either key, or both: "Let autopilot fill it" sends both)."""
    try:
        data = json.loads(raw or b'null')
    except ValueError:
        data = None
    keys = [k for k in ('enabled', 'fill_room') if isinstance(data, dict) and k in data]
    if not keys or not all(isinstance(data[k], bool) for k in keys):
        return 400, {'error': 'send {"enabled": true|false} and/or {"fill_room": true|false} as JSON'}
    if 'enabled' in keys:
        autopilot.set_enabled(cfg, data['enabled'])
    if 'fill_room' in keys:
        fill.set_fill(cfg, data['fill_room'])
    return 200, {'ok': True, **{k: data[k] for k in keys}}


def save_control(collector, raw, stop_fn=handover.stop_now):
    """(status, answer) for POST /api/control {"action": "pause"|"resume"|"optin", "kind": "machine"|"device"|"worker"|"account",
    "name": ..., "on": bool (optin), "stop": bool (pause: hand running jobs over, then stop them), "note": str}."""
    try:
        data = json.loads(raw or b'null')
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get('action') not in ('pause', 'resume', 'optin') or data.get('kind') not in controls.SECTIONS:
        return 400, {'error': 'send {"action": "pause"|"resume"|"optin", "kind": "machine"|"device"|"worker"|"account", "name": ...}'}
    cfg, kind, name = collector.cfg, data['kind'], data.get('name')
    section = controls.SECTIONS[kind]
    if name not in ((cfg.get('raw_entries') or {}).get(section) or cfg.get(section) or {}):
        return 404, {'error': f'no {kind} "{name}" in the config: reload the page, the config may have changed'}
    folder = config.state_dir(cfg)
    if data['action'] == 'optin':
        if not isinstance(data.get('on'), bool):
            return 400, {'error': 'send "on": true or false with an optin'}
        controls.set_enabled(folder, kind, name, data['on'])
    else:
        controls.set_paused(folder, kind, name, data['action'] == 'pause', note=str(data.get('note') or '')[:200])
    try:
        cfg = collector.reload()
    except config.ConfigError as e:
        return 500, {'error': f'saved, but the config no longer loads: {e}'}
    handed = stop_fn(cfg, kind, name) if data['action'] == 'pause' and data.get('stop') is True else []
    return 200, {'ok': True, 'handed': [{'log': log, 'to': to} for log, to in handed]}


def save_autocompact(collector, raw):
    """(status, answer) for POST /api/autocompact {"threshold_pct": 10-95, or null for the config's value}."""
    try:
        data = json.loads(raw or b'null')
    except ValueError:
        data = None
    pct = data.get('threshold_pct', 'missing') if isinstance(data, dict) else 'missing'
    if pct is not None and not autocompact.valid_pct(pct):
        return 400, {'error': f'send {{"threshold_pct": <a whole number from {autocompact.MIN_PCT} to {autocompact.MAX_PCT}>}}, '
                              'or {"threshold_pct": null} for the config\'s value'}
    controls.set_setting(config.state_dir(collector.cfg), 'autocompact_pct', pct)
    return 200, {'ok': True, 'threshold_pct': autocompact.threshold(collector.cfg)}


POSTS = ('/api/reading', '/api/refresh', '/api/main', '/api/autopilot', '/api/control', '/api/telemetry', '/api/self-improve',
         '/api/owner', '/api/autocompact', '/api/update')


def post(collector, headers, path, port, token, read):
    """(status, answer) for a POST. `read(n)` reads n bytes of the body, only once the request passed every check."""
    host = headers.get('Host', '')
    origins = authweb.origins(collector.cfg)
    if not allowed_host(host, port, origins):
        return 403, {'error': 'open the dashboard at http://127.0.0.1:<port>/ (other host names are refused)'}
    path = path.split('?', 1)[0]
    if path not in POSTS:
        return 404, {'error': 'POST a reading to /api/reading, {} to /api/refresh to read usage now, {"agent": ...} to /api/main, '
                          '{"enabled": ...} to /api/autopilot or /api/self-improve, {"action": ...} to /api/control, '
                          '{"mode": ...} to /api/owner, {"threshold_pct": ...} to /api/autocompact, '
                          '{"consent"|"error"|"use": ...} to /api/telemetry, or {} to /api/update'}
    refused = check_post(headers, host, token, origins)
    if refused:
        return refused[0], {'error': refused[1]}
    try:
        size = int(headers.get('Content-Length') or 0)
    except ValueError:
        size = -1
    if not 0 < size <= MAX_BODY:
        return 413, {'error': f'a reading is a small JSON object (at most {MAX_BODY} bytes)'}
    return dispatch(collector, path, read(size))


def start_update(collector, _body):
    if collector.update_round(force=True):
        return 202, {'ok': True, 'started': True}
    return 409, {'error': 'an update round is already running: wait for it, the Versions list shows when it is done'}


def refresh_now(collector, _body):
    try:
        found = collector.refresh_readings()
    except (OSError, ValueError) as e:
        return 500, {'error': f'reading usage failed ({e}): fix the usage log, then refresh again'}
    return 200, {'ok': True, 'read': sorted(found), 'problems': {n: r['problem'] for n, r in found.items() if r['problem']}}


HANDLERS = {
    '/api/main': lambda c, b: save_main(c.cfg, b),
    '/api/autopilot': lambda c, b: save_autopilot(c.cfg, b),
    '/api/owner': lambda c, b: save_owner(c.cfg, b),
    '/api/control': save_control,
    '/api/autocompact': save_autocompact,
    '/api/refresh': refresh_now,
    '/api/reading': lambda c, b: save_reading(c.cfg, b),
    '/api/telemetry': dashtelemetry.save,
    '/api/self-improve': dashtelemetry.save_self_improve,
    '/api/update': start_update,
}


def dispatch(collector, path, body):
    return HANDLERS[path](collector, body)


def safe_json(data):
    """JSON with &, < and > written as \\u escapes (the parsed values are unchanged), so no record read from a file (a goal,
    a job's title) can ever form markup; html.escape then finds nothing left to change and only states that."""
    text = json.dumps(data).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    return html.escape(text, quote=False).encode()


STATE_FRESH = 1.0   # seconds one assembled /api/state serves every tab that asks (a phone and a laptop polling together)
GZIP_FROM = 1024    # bytes: a smaller answer goes as it is


class StateCache:
    """The last /api/state body with its ETag, shared for STATE_FRESH seconds. Counts what it served, for measurements."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.lock = clock, threading.Lock()
        self.at, self.body, self.etag, self.zipped = None, b'', '', None
        self.stats = {'built': 0, 'served': 0, 'not_modified': 0, 'gzip': 0}

    def get(self, collector):
        with self.lock:
            if self.at is None or self.clock() - self.at >= STATE_FRESH:
                snap = collector.snapshot()
                self.body = safe_json(snap)
                # the ETag leaves out generated_at, so an answer whose data did not change keeps its tag (a 304)
                stable = safe_json({k: v for k, v in snap.items() if k != 'generated_at'})
                self.etag = 'W/"' + hashlib.sha256(stable).hexdigest()[:20] + '"'
                self.zipped, self.at = None, self.clock()
                self.stats['built'] += 1
            return self.body, self.etag

    def gzipped(self, etag):
        """The gzipped body of the answer `etag` names, or None when another request built a newer one in between."""
        with self.lock:
            if etag != self.etag:
                return None
            if self.zipped is None:
                self.zipped = gzip.compress(self.body, compresslevel=6, mtime=0)
            return self.zipped


def takes_gzip(accept):
    """True when Accept-Encoding takes gzip: named (or *) with a quality above zero (`gzip;q=0` refuses it)."""
    found = {}
    for part in (accept or '').lower().split(','):
        name, _, params = part.strip().partition(';')
        q = 1.0
        for p in params.split(';'):
            k, _, v = p.strip().partition('=')
            if k == 'q':
                try:
                    q = float(v)
                except ValueError:
                    q = 0.0
        found[name.strip()] = q
    return found.get('gzip', found.get('*', 0.0)) > 0


def state_response(collector, headers):
    """(status, body, extra headers) for GET /api/state: 304 when the page already has this answer (If-None-Match), the
    body gzipped when the browser takes it. The page then only parses and renders an answer that changed."""
    cache = collector.__dict__.setdefault('state_cache', StateCache())
    body, etag = cache.get(collector)
    cache.stats['served'] += 1
    extra = {'ETag': etag, 'Vary': 'Accept-Encoding'}
    if etag in [t.strip() for t in (headers.get('If-None-Match') or '').split(',')]:
        cache.stats['not_modified'] += 1
        return 304, b'', extra
    zipped = cache.gzipped(etag) if len(body) >= GZIP_FROM and takes_gzip(headers.get('Accept-Encoding')) else None
    if zipped is not None:   # never a newer body under this answer's ETag
        cache.stats['gzip'] += 1
        return 200, zipped, {**extra, 'Content-Encoding': 'gzip'}
    return 200, body, extra


def route(collector, path, token=''):
    """(status, body, content type, cache control) for a GET of `path`."""
    if path == '/api/state':
        return 200, safe_json(collector.snapshot()), JSON_TYPE, 'no-store'
    if path == '/api/health':
        return 200, b'{"ok": true}', JSON_TYPE, 'no-store'
    if path in ('/helper', '/helper.html'):
        port = getattr(collector, 'port', None) or collector.cfg['dashboard']['port']
        return 200, helper.page(collector.cfg, config.state_dir(collector.cfg), port), 'text/html; charset=utf-8', 'no-store'
    full = static_path(path)
    if not full:
        return 404, NOT_FOUND, JSON_TYPE, 'no-store'
    with open(full, 'rb') as f:
        body = f.read()
    if full.endswith(INDEX):
        body = body.replace(TOKEN_SLOT, token.encode())
    if full.endswith((INDEX, 'sw.js')):
        # a new release changes sw.js byte for byte, so the browser installs the new worker; the page compares its version
        # with /api/state's and reloads itself once (web/update.js)
        body = body.replace(VERSION_SLOT, __version__.encode())
    # no-cache: revalidated every time, so a new version reaches the service worker and the page at once.
    return 200, body, TYPES.get(os.path.splitext(full)[1]) or mimetypes.guess_type(full)[0] or 'application/octet-stream', 'no-cache'


def reported(where, fn, *args):
    """fn(*args), an exception reported to telemetry (sent only with consent) before the server's own error handling."""
    try:
        return fn(*args)
    except Exception as e:
        telemetry.capture_exception(type(e), e, e.__traceback__, where=where)
        raise


def get(collector, path, token, headers):
    """(status, body, content type, cache control, extra headers) for a GET. /api/state is no-cache: the browser keeps it and
    asks with If-None-Match, and an unchanged answer is a 304."""
    if path == '/api/state':
        code, body, extra = state_response(collector, headers)
        return code, body, JSON_TYPE, 'no-cache', extra
    return (*route(collector, path, token), None)


def response_headers(code, body, ctype, cache, extra=None):
    """[(name, value)] of every answer. A 304 states no Content-Length: it would have to be the full answer's."""
    extra = extra or {}
    out = [(k, x) for k, v in extra.items() for x in (v if isinstance(v, list) else [v])] + [('Content-Type', ctype)]
    out += [] if code == 304 else [('Content-Length', str(len(body)))]
    # the sign-in page sets its own CSP (Google's script and frame) and referrer policy
    return out + [('Cache-Control', cache), ('X-Content-Type-Options', 'nosniff')] + \
        [(k, v) for k, v in (('Referrer-Policy', 'no-referrer'), ('Content-Security-Policy', CSP)) if k not in extra]


def signed_post(rt, collector, headers, path, port, read):
    """(status, answer, extra headers) of a POST with sign-in on: the gate (authweb.post), then the dashboard's own POST with
    the session's CSRF token in place of the per-start token."""
    host = headers.get('Host', '')
    if not allowed_host(host, port, authweb.origins(collector.cfg)):
        return 403, {'error': 'open the dashboard at one of its addresses (other host names are refused)'}, None
    path = path.split('?', 1)[0]
    if path not in POSTS and path not in authweb.AUTH_POSTS:
        return 404, {'error': 'this path is not part of the dashboard'}, None
    return authweb.post(rt, headers, path, host, read, lambda csrf, rd: post(collector, headers, path, port, csrf, rd))


def any_post(rt, collector, headers, path, port, token, read):
    """(status, answer, extra headers): the per-start token without sign-in, the session's gate with it."""
    if rt is None:
        return (*post(collector, headers, path, port, token, read), None)
    return signed_post(rt, collector, headers, path, port, read)


class AuthRequired(Exception):
    """The dashboard would be reachable by others without sign-in."""


def make_handler(collector, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'tutti'
        sys_version = ''

        def log_message(self, fmt, *args):  # quiet: the dashboard polls every few seconds
            return

        def send(self, code, body, ctype, cache='no-store', extra=None):
            self.send_response(code)
            for k, v in response_headers(code, body, ctype, cache, extra):
                self.send_header(k, v)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def fail(self, code, text):
            self.send(code, json.dumps({'error': text}).encode(), JSON_TYPE)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not allowed_host(self.headers.get('Host', ''), self.server.server_address[1], authweb.origins(collector.cfg)):
                return self.fail(403, 'open the dashboard at http://127.0.0.1:<port>/ (other host names are refused)')
            path = self.path.split('?', 1)[0]
            self.send(*reported('dashboard-get', authweb.get, getattr(self.server, 'auth', None), path, self.headers,
                                lambda tok: get(collector, path, tok, self.headers), token))

        def do_POST(self):
            code, answer, extra = reported('dashboard-post', any_post, getattr(self.server, 'auth', None), collector, self.headers,
                                           self.path, self.server.server_address[1], token, self.rfile.read)
            self.send(code, json.dumps(answer).encode(), JSON_TYPE, extra=extra)

    return Handler


def server(cfg, port, collector=None):
    """The HTTP server on 127.0.0.1:<port> (0 picks a free port). Not started: call serve_forever()."""
    collector = collector or Collector(cfg)
    from . import auth
    if exposed(cfg) and not auth.enabled(cfg):
        raise AuthRequired('dashboard.bind or dashboard.origins lets others reach the dashboard, and sign-in is off: run '
                           '`tutti auth setup` first, or remove dashboard.bind and dashboard.origins to keep it on this computer')
    token = secrets.token_urlsafe(24)   # a new one per start: the page loaded from this server sends it back with a POST
    httpd = ThreadingHTTPServer((bind_of(cfg), port), make_handler(collector, token))
    httpd.token = token
    httpd.auth = authweb.Runtime(cfg, httpd.server_address[1]) if auth.enabled(cfg) else None
    httpd.daemon_threads = True
    httpd.collector = collector
    collector.port = httpd.server_address[1]   # the reading helper's bookmarklets open this exact address
    return httpd
