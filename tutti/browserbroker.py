"""The work browser's broker: the one way a user or a worker drives the work browser (docs/BROWSER.md).

Every task has a type, and each type has its allowed sites (browser.scopes, merged over the defaults below); a URL outside
the task's scope is refused before a tab opens, and a page that ends up outside it returns no content. Every action is
logged (<state>/browser-actions.jsonl: when, who, task type, action, the site and path without the query, the result);
never a cookie, a password, a token or a page's content. Each task's tab is closed after it.

A worker on another machine reaches the broker, never the browser: `tutti browser remote <machine>` serves one task type
on 127.0.0.1 with a one-time token for as long as the worker's command runs, forwarded to 127.0.0.1 on that machine over
Tutti's ssh link. Files: downloads land in one scoped downloads directory, and a worker gets a file only by naming it in a
task whose type may take files (browser.file_tasks)."""
import base64
import contextlib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import browser, helper

DEFAULT_TYPES = ('usage-reading', 'sign-in', 'sign-in-check', 'page')
LOG = 'browser-actions.jsonl'
DOWNLOADS = '/var/lib/tutti-browser/downloads'   # isolated mode; the other modes use <state>/browser-downloads
FILE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._ -]{0,200}')


class OutOfScope(browser.BrowserError):
    pass


def host_of(url):
    return (urllib.parse.urlparse(url).hostname or '').lower()


def in_scope(hosts, url):
    """True for an https URL on one of `hosts` or a subdomain of one."""
    u = urllib.parse.urlparse(url)
    h = (u.hostname or '').lower()
    return u.scheme == 'https' and any(h == x or h.endswith('.' + x) for x in (str(y).lower().lstrip('.') for y in hosts))


def scopes(cfg, helper_pages, sites):
    """{task type: [hosts]}: the usage pages for readings, the admin's sites for sign-ins and pages; a type in
    browser.scopes replaces its defaults (so it can widen or narrow them)."""
    usage = sorted({host_of(u) for u in helper_pages})
    chosen = sorted({host_of(u) for u in sites})
    out = {'usage-reading': usage, 'sign-in': chosen, 'sign-in-check': chosen, 'page': chosen}
    for kind, hosts in (browser.settings(cfg).get('scopes') or {}).items():
        out[kind] = [str(h).lower() for h in hosts or []]
    return out


def shown_url(url):
    """The site and path, never the query or fragment (they may carry a code or a token)."""
    u = urllib.parse.urlparse(url or '')
    return f'{u.scheme}://{u.hostname or ""}{u.path or ""}'


class Broker:
    """Runs tasks by type. A task never brings its own JavaScript: it names an extractor (`text`, `usage`, `check`) whose
    script is Tutti's own, read-only, and returns only what the extractor reads (never document.cookie or storage)."""

    def __init__(self, b, scope_map, log_path, downloads=None, usage_targets=None, file_tasks=(), clock=time.time):
        self.b, self.scope, self.log_path, self.downloads, self.clock = b, scope_map, log_path, downloads, clock
        self.usage_targets, self.file_tasks = usage_targets or {}, set(file_tasks or ())
        self.lock = threading.Lock()

    def log(self, who, kind, action, url, result):
        line = {'at': self.clock(), 'who': who, 'type': kind, 'action': action, 'url': shown_url(url), 'result': result}
        with self.lock:
            os.makedirs(os.path.dirname(self.log_path) or '.', exist_ok=True)
            with open(self.log_path, 'a', encoding='utf8') as f:
                f.write(json.dumps(line) + '\n')

    def check(self, kind, url, who):
        hosts = self.scope.get(kind)
        if hosts is None or not in_scope(hosts, url):
            self.log(who, kind, 'refused', url, 'out of scope')
            allowed = ', '.join(hosts or []) or 'no site'
            raise OutOfScope(f'{host_of(url) or url} is outside what a {kind} task may open ({allowed}): an admin adds sites in '
                             'Settings, Work browser (browser.sites or browser.scopes)')

    def script(self, extract, url):
        if extract == 'text':
            return browser.TEXT
        if extract == 'check':
            return 'JSON.stringify({loading: document.readyState !== "complete"})'
        if extract == 'usage' and url in self.usage_targets:
            return helper.page_script(self.usage_targets[url])
        raise browser.BrowserError(f'"{extract}" is not an extractor for {shown_url(url)}: use text, check, or usage on a usage page')

    def page(self, kind, url, extract='text', who='tutti', tries=10, wait=1.5):
        """One task: the scope check, a tab of its own, Tutti's extractor, the tab closed; content only from an in-scope page."""
        self.check(kind, url, who)
        script = self.script(extract, url)
        self.log(who, kind, 'open', url, f'tab ({extract})')
        try:
            r = browser.run_page(self.b, url, script, who, tries, wait)
        except (browser.BrowserError, OSError, ValueError) as e:
            self.log(who, kind, 'close', url, f'failed: {e}')
            raise
        if not in_scope(self.scope.get(kind) or [], r['href']):
            self.log(who, kind, 'close', r['href'], 'left the scope: content withheld')
            return {'value': {}, 'href': shown_url(r['href']), 'signed_out': True, 'withheld': True}
        self.log(who, kind, 'close', url, 'signed out' if r['signed_out'] else 'ok')
        return {**r, 'href': shown_url(r['href'])}

    def sign_in(self, url, who):
        self.check('sign-in', url, who)
        self.log(who, 'sign-in', 'open', url, 'visible tab')
        self.b.sign_in(url)

    def take(self, kind, name, who):
        """One downloaded file from the scoped downloads folder, for a task type allowed files (browser.file_tasks), by its
        plain name; logged. Returns its bytes."""
        if kind not in self.file_tasks or not self.downloads:
            self.log(who, kind, 'refused', f'file:///{name}', 'no files for this task type')
            raise OutOfScope(f'a {kind} task gets no files: an admin lists the task types that do in browser.file_tasks')
        if not FILE_NAME.fullmatch(name or '') or name in ('.', '..'):
            raise browser.BrowserError(f'"{name}" is not a file name: ask for one downloaded file by its name')
        path = os.path.join(self.downloads, name)
        if not os.path.isfile(path):
            raise browser.BrowserError(f'no downloaded file is named {name}')
        with open(path, 'rb') as f:
            data = f.read()
        self.log(who, kind, 'take', f'file:///{name}', f'{len(data)} bytes')
        return data


def serve(broker, kind, who, token=None, port=0):
    """A one-task-type broker on 127.0.0.1 for a worker: POST /page {url, extract} or /file {name}, with X-Tutti-Task.
    Not started."""
    token = token or secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        timeout = 30   # a silent connection is closed

        def log_message(self, fmt, *args):
            return

        def answer(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if not secrets.compare_digest(self.headers.get('X-Tutti-Task', ''), token) or self.path not in ('/page', '/file'):
                return self.answer(403, {'error': 'this broker serves POST /page and /file with its task token only'})
            try:
                size = int(self.headers.get('Content-Length') or 0)
                if not 0 <= size <= 65536:   # a negative length would read until the connection closes
                    raise ValueError('send a JSON object of at most 64 KB with its Content-Length')
                body = json.loads(self.rfile.read(size) or b'{}')
                if not isinstance(body, dict):
                    raise ValueError('send a JSON object')
                if self.path == '/file':
                    data = broker.take(kind, str(body.get('name')), who)
                    return self.answer(200, {'name': body.get('name'), 'base64': base64.b64encode(data).decode()})
                return self.answer(200, broker.page(kind, str(body.get('url')), str(body.get('extract') or 'text'), who))
            except OutOfScope as e:
                return self.answer(403, {'error': str(e)})
            except (browser.BrowserError, OSError, ValueError) as e:
                return self.answer(502, {'error': str(e)})

    httpd = ThreadingHTTPServer((browser.HOST, port), Handler)
    httpd.daemon_threads = True
    httpd.token = token
    return httpd


# The worker's client: standard library only, sent over ssh on standard input (the worker's machine needs no Tutti).
CLIENT = r'''
import json, os, sys, urllib.request
args = json.loads(os.environ['TUTTI_BROWSER_ARGS'])
req = urllib.request.Request(os.environ['TUTTI_BROWSER_URL'] + '/page', method='POST',
                             data=json.dumps({'url': args[0], 'extract': args[1] if len(args) > 1 else 'text'}).encode(),
                             headers={'X-Tutti-Task': os.environ['TUTTI_BROWSER_TOKEN'], 'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(req, timeout=170) as r:
        print(r.read().decode())
except urllib.error.HTTPError as e:
    print('work browser: ' + json.loads(e.read() or b'{}').get('error', str(e)), file=sys.stderr)
    sys.exit(1)
'''


@contextlib.contextmanager
def running(httpd):
    t = threading.Thread(target=httpd.serve_forever, name='tutti-browser-broker', daemon=True)
    t.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
