"""`tutti dashboard`: a local web dashboard (an installable PWA) over the state Tutti already keeps.
Standard library only: a threaded HTTP server bound to 127.0.0.1, static files from tutti/web, and /api/state as JSON.
Machines and workers are probed in a background thread (ssh and model servers can be slow); the rest is read per request."""
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

from . import conduct, config, helper, pacing, projects, readers, remote, state, transfer, workers

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
BIND = '127.0.0.1'
JSON_TYPE = 'application/json'
TYPES = {'.webmanifest': 'application/manifest+json', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
         '.html': 'text/html; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': JSON_TYPE}
# Never echoes the request path back: nothing the browser sent is reflected into a response.
NOT_FOUND = b'{"error": "this path is not part of the dashboard: open / instead"}'
TOKEN_SLOT = b'__TUTTI_TOKEN__'   # index.html carries this server's token in <meta name="tutti-token">
MAX_BODY = 4096
NOTE_MAX = 200
RESET_PAST = timedelta(days=1)
RESET_AHEAD = timedelta(days=62)
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "manifest-src 'self'; worker-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class Collector:
    """Holds the latest probes and refreshes them every `probe_seconds` in a daemon thread."""

    def __init__(self, cfg, probe_fn=remote.probe, available_fn=workers.available, clock=time.time, reader=None, stored_fn=None,
                 activity_fn=projects.probe_activity):
        self.cfg, self.probe_fn, self.available_fn, self.clock = cfg, probe_fn, available_fn, clock
        self.activity_fn = activity_fn
        self.stored_fn = stored_fn or (lambda m: transfer.stored(cfg, m))
        self.reader = reader or readers.Readers(cfg, clock=clock)
        self.probes = {}
        self.lock = threading.Lock()
        self.read_lock = threading.Lock()   # the probe thread and a Refresh readings click never run the readers at once
        self.stop = threading.Event()

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
        fresh = state.probe_all(self.cfg, self.probe_fn, self.available_fn, clock=self.clock, stored_fn=self.stored_fn)
        fresh['activity'] = projects.probe_all(self.cfg, self.activity_fn)
        try:
            fresh['readers'] = self.read_usage()
        except (OSError, ValueError) as e:   # a broken usage log must not hide machines and workers
            fresh['readers'] = {}
            fresh['error'] = f'automatic usage readings failed ({e}): fix the usage log, the next probe tries again'
        with self.lock:
            self.probes = fresh
        return fresh

    def loop(self):
        while not self.stop.is_set():
            try:
                self.refresh()
            except Exception as e:  # a probe bug must not end the loop; the page shows the last good data and its age
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
        out['probe_error'] = probes.get('error')
        return out


def allowed_host(host, port):
    """Only the loopback names with our port: a page on another site that rebinds its DNS to 127.0.0.1 is refused."""
    return host in (f'127.0.0.1:{port}', f'localhost:{port}', f'[::1]:{port}')


def static_path(url_path):
    """The file under tutti/web for a request path, or None when it is outside it or missing."""
    rel = url_path.split('?', 1)[0].lstrip('/') or 'index.html'
    full = os.path.realpath(os.path.join(WEB, rel))
    if not full.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(full):
        return None
    return full


def same_origin(origin, host):
    """The dashboard is served over plain HTTP on loopback only (allowed_host), so its Origin is that scheme and host."""
    parts = urlsplit(origin or '')
    return parts.scheme == 'http' and parts.netloc == host and parts.path == '' and not parts.query


def check_post(headers, host, token):
    """None when a POST may write, else (status, why). Same origin only: the Origin header must be this dashboard's, a
    browser's Sec-Fetch-Site must say same-origin, the body is JSON and the page's token is sent back."""
    if not same_origin(headers.get('Origin'), host):
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


POSTS = ('/api/reading', '/api/refresh', '/api/main')


def post(collector, headers, path, port, token, read):
    """(status, answer) for a POST. `read(n)` reads n bytes of the body, only once the request passed every check."""
    host = headers.get('Host', '')
    if not allowed_host(host, port):
        return 403, {'error': 'open the dashboard at http://127.0.0.1:<port>/ (other host names are refused)'}
    path = path.split('?', 1)[0]
    if path not in POSTS:
        return 404, {'error': 'POST a reading to /api/reading, {} to /api/refresh to read usage now, or {"agent": ...} to /api/main'}
    refused = check_post(headers, host, token)
    if refused:
        return refused[0], {'error': refused[1]}
    try:
        size = int(headers.get('Content-Length') or 0)
    except ValueError:
        size = -1
    if not 0 < size <= MAX_BODY:
        return 413, {'error': f'a reading is a small JSON object (at most {MAX_BODY} bytes)'}
    body = read(size)
    if path == '/api/main':
        return save_main(collector.cfg, body)
    if path == '/api/refresh':
        try:
            found = collector.refresh_readings()
        except (OSError, ValueError) as e:
            return 500, {'error': f'reading usage failed ({e}): fix the usage log, then refresh again'}
        return 200, {'ok': True, 'read': sorted(found), 'problems': {n: r['problem'] for n, r in found.items() if r['problem']}}
    return save_reading(collector.cfg, body)


def safe_json(data):
    """JSON with &, < and > written as \\u escapes (the parsed values are unchanged), so no record read from a file (a goal,
    a job's title) can ever form markup; html.escape then finds nothing left to change and only states that."""
    text = json.dumps(data).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    return html.escape(text, quote=False).encode()


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
    if full.endswith('index.html'):
        body = body.replace(TOKEN_SLOT, token.encode())
    # no-cache: revalidated every time, so a new version reaches the service worker and the page at once.
    return 200, body, TYPES.get(os.path.splitext(full)[1]) or mimetypes.guess_type(full)[0] or 'application/octet-stream', 'no-cache'


def make_handler(collector, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'tutti'
        sys_version = ''

        def log_message(self, fmt, *args):  # quiet: the dashboard polls every few seconds
            return

        def send(self, code, body, ctype, cache='no-store'):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', cache)
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', CSP)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def fail(self, code, text):
            self.send(code, json.dumps({'error': text}).encode(), JSON_TYPE)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not allowed_host(self.headers.get('Host', ''), self.server.server_address[1]):
                return self.fail(403, 'open the dashboard at http://127.0.0.1:<port>/ (other host names are refused)')
            self.send(*route(collector, self.path.split('?', 1)[0], token))

        def do_POST(self):
            code, answer = post(collector, self.headers, self.path, self.server.server_address[1], token, self.rfile.read)
            self.send(code, json.dumps(answer).encode(), JSON_TYPE)

    return Handler


def server(cfg, port, collector=None):
    """The HTTP server on 127.0.0.1:<port> (0 picks a free port). Not started: call serve_forever()."""
    collector = collector or Collector(cfg)
    token = secrets.token_urlsafe(24)   # a new one per start: the page loaded from this server sends it back with a POST
    httpd = ThreadingHTTPServer((BIND, port), make_handler(collector, token))
    httpd.token = token
    httpd.daemon_threads = True
    httpd.collector = collector
    collector.port = httpd.server_address[1]   # the reading helper's bookmarklets open this exact address
    return httpd
