"""`tutti dashboard`: a local web dashboard (an installable PWA) over the state Tutti already keeps.
Standard library only: a threaded HTTP server bound to 127.0.0.1, static files from tutti/web, and /api/state as JSON.
Machines and workers are probed in a background thread (ssh and model servers can be slow); the rest is read per request."""
import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import remote, state, workers

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
BIND = '127.0.0.1'
JSON_TYPE = 'application/json'
TYPES = {'.webmanifest': 'application/manifest+json', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
         '.html': 'text/html; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': JSON_TYPE}
# Never echoes the request path back: nothing the browser sent is reflected into a response.
NOT_FOUND = b'{"error": "this path is not part of the dashboard: open / instead"}'
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "manifest-src 'self'; worker-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class Collector:
    """Holds the latest probes and refreshes them every `probe_seconds` in a daemon thread."""

    def __init__(self, cfg, probe_fn=remote.probe, available_fn=workers.available, clock=time.time):
        self.cfg, self.probe_fn, self.available_fn, self.clock = cfg, probe_fn, available_fn, clock
        self.probes = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def refresh(self):
        fresh = state.probe_all(self.cfg, self.probe_fn, self.available_fn, clock=self.clock)
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


def route(collector, path):
    """(status, body, content type, cache control) for a GET of `path`."""
    if path == '/api/state':
        return 200, json.dumps(collector.snapshot()).encode(), JSON_TYPE, 'no-store'
    if path == '/api/health':
        return 200, b'{"ok": true}', JSON_TYPE, 'no-store'
    full = static_path(path)
    if not full:
        return 404, NOT_FOUND, JSON_TYPE, 'no-store'
    with open(full, 'rb') as f:
        body = f.read()
    # no-cache: revalidated every time, so a new version reaches the service worker and the page at once.
    return 200, body, TYPES.get(os.path.splitext(full)[1]) or mimetypes.guess_type(full)[0] or 'application/octet-stream', 'no-cache'


def make_handler(collector):
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
            self.send(*route(collector, self.path.split('?', 1)[0]))

    return Handler


def server(cfg, port, collector=None):
    """The HTTP server on 127.0.0.1:<port> (0 picks a free port). Not started: call serve_forever()."""
    collector = collector or Collector(cfg)
    httpd = ThreadingHTTPServer((BIND, port), make_handler(collector))
    httpd.daemon_threads = True
    httpd.collector = collector
    return httpd
