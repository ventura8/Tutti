"""The household's work browser: the one Chrome that every Tutti browser task uses, users' and workers' alike.

One machine is the browser machine (browser.machine, '' is the one Tutti runs on); every task, from any user or any
worker on any machine, runs in a tab of the Chrome there and its tab closes when the task ends. browser.mode picks which
Chrome:

- `existing`: the owner's own Chrome profile, with the sign-ins it already has. Tutti never starts or restarts it with
  flags; the owner turns on remote debugging once (chrome://inspect/#remote-debugging), Chrome then listens on 127.0.0.1
  and writes its address to <profile>/DevToolsActivePort, the one file of the profile Tutti reads;
- `dedicated`: a Chrome Tutti starts with its own profile (browser.profile, default ~/.local/share/tutti/browser) and a
  DevTools port on 127.0.0.1, signed in only to the sites Tutti's work needs (browser.sites), each opened once for a
  sign-in. Closed after browser.idle_minutes with no task.

- `isolated`: a fresh Chrome used only by Tutti, run as its own system user in a systemd sandbox with no TCP DevTools
  port: Tutti reaches it on a Unix socket (tutti/browsersandbox.py).

One DevTools connection, a queue and at most browser.max_tabs tabs at once. Users and workers drive it only through the
broker (tutti/browserbroker.py: per-task-type sites, every action logged). Tutti never reads or exports cookies,
passwords or tokens from the profile, and the debugging port is never exposed: it listens on 127.0.0.1 (or is a Unix
socket), and a remote browser machine is reached through an ssh tunnel bound to 127.0.0.1 on both ends."""
import base64
import contextlib
import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse

DEFAULTS = {'machine': '', 'mode': 'dedicated', 'path': '', 'profile': '', 'user_profile': '', 'headless': True,
            'idle_minutes': 5, 'port': 9333, 'max_tabs': 4, 'sites': [],
            'socket': '', 'scopes': {}, 'allow_local': [], 'file_tasks': []}
MODES = ('existing', 'dedicated', 'isolated')
SOCKET = '/run/tutti-browser/devtools.sock'   # isolated mode: the bridge's Unix socket (tutti/browsersandbox.py)
HOST = '127.0.0.1'   # DevTools is reached here only: nothing on the network can drive the work browser
ENV_WS = 'TUTTI_BROWSER_WS'   # a worker's tunnelled DevTools address (`tutti browser remote`)
ACTIVE_PORT = 'DevToolsActivePort'
START_SECONDS = 20
NAMES = ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'chrome')
EXISTING_HELP = ('turn on remote debugging in your Chrome once: open chrome://inspect/#remote-debugging and switch on '
                 '"Allow remote debugging" (it listens on 127.0.0.1 only); Tutti never starts or restarts your Chrome')


class BrowserError(Exception):
    pass


def settings(cfg):
    return {**DEFAULTS, **((cfg or {}).get('browser') or {})}


def system_name(system=None):
    return system or platform.system()


def profile_dir(cfg=None, system=None, env=None, home=None):
    """Tutti's own profile folder (dedicated mode): browser.profile, else the platform's data folder + tutti/browser."""
    env = os.environ if env is None else env
    home = home or os.path.expanduser('~')
    own = settings(cfg)['profile']
    if own:
        return os.path.abspath(os.path.expanduser(own))
    s = system_name(system)
    if s == 'Darwin':
        return os.path.join(home, 'Library', 'Application Support', 'tutti', 'browser')
    if s == 'Windows':
        return os.path.join(env.get('LOCALAPPDATA') or os.path.join(home, 'AppData', 'Local'), 'tutti', 'browser')
    return os.path.join(env.get('XDG_DATA_HOME') or os.path.join(home, '.local', 'share'), 'tutti', 'browser')


def user_profiles(system=None, env=None, home=None):
    """The owner's own Chrome and Chromium profile folders. Dedicated mode never starts on one; existing mode only reads
    their DevToolsActivePort."""
    env = os.environ if env is None else env
    home = home or os.path.expanduser('~')
    s = system_name(system)
    if s == 'Darwin':
        base = os.path.join(home, 'Library', 'Application Support')
        return [os.path.join(base, 'Google', 'Chrome'), os.path.join(base, 'Chromium')]
    if s == 'Windows':
        local = env.get('LOCALAPPDATA') or os.path.join(home, 'AppData', 'Local')
        return [os.path.join(local, 'Google', 'Chrome', 'User Data'), os.path.join(local, 'Chromium', 'User Data')]
    conf = env.get('XDG_CONFIG_HOME') or os.path.join(home, '.config')
    return [os.path.join(conf, n) for n in ('google-chrome', 'google-chrome-beta', 'chromium', 'BraveSoftware')]


def check_profile(profile, system=None, env=None, home=None):
    """Raises when a dedicated profile is, or lies inside, one of the owner's own browser profiles."""
    p = os.path.realpath(profile)
    for u in user_profiles(system, env, home):
        u = os.path.realpath(u)
        if p == u or p.startswith(u + os.sep):
            raise BrowserError(f'browser.profile ({profile}) is your own browser\'s profile ({u}): dedicated mode keeps its own; '
                               'clear browser.profile, or set browser.mode to existing to use your Chrome as it is')
    return profile


def active_profile(cfg, system=None, env=None, home=None):
    """The profile whose DevToolsActivePort names the endpoint: the owner's (existing) or Tutti's (dedicated)."""
    s = settings(cfg)
    if s['mode'] == 'existing':
        return os.path.expanduser(s['user_profile']) if s['user_profile'] else user_profiles(system, env, home)[0]
    return profile_dir(cfg, system, env, home)


def parse_active_port(text):
    """(port, browser path) from a DevToolsActivePort file's two lines."""
    lines = [x.strip() for x in (text or '').splitlines() if x.strip()]
    if len(lines) < 2 or not lines[0].isdigit() or not lines[1].startswith('/devtools/browser/'):
        raise BrowserError('the work browser\'s DevToolsActivePort file is not the port and address Chrome writes: '
                           'restart that Chrome, then try again')
    return int(lines[0]), lines[1]


def candidates(system=None, env=None, home=None):
    env = os.environ if env is None else env
    home = home or os.path.expanduser('~')
    s = system_name(system)
    if s == 'Darwin':
        out = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/Applications/Chromium.app/Contents/MacOS/Chromium']
        cache = os.path.join(home, 'Library', 'Caches', 'ms-playwright')
    elif s == 'Windows':
        out = [os.path.join(env.get(k) or '', 'Google', 'Chrome', 'Application', 'chrome.exe')
               for k in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA') if env.get(k)]
        cache = os.path.join(env.get('LOCALAPPDATA') or home, 'ms-playwright')
    else:
        out = ['/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser', '/snap/bin/chromium']
        cache = os.path.join(home, '.cache', 'ms-playwright')
    return out, cache


def playwright_chromium(cache, listdir=os.listdir, exists=os.path.exists):
    try:
        names = sorted((n for n in listdir(cache) if n.startswith('chromium-')), reverse=True)
    except OSError:
        return None
    for n in names:
        for rel in (('chrome-linux', 'chrome'), ('chrome-mac', 'Chromium.app', 'Contents', 'MacOS', 'Chromium'),
                    ('chrome-win', 'chrome.exe')):
            p = os.path.join(cache, n, *rel)
            if exists(p):
                return p
    return None


def install_hint(system=None):
    s = system_name(system)
    if s == 'Darwin':
        return 'brew install --cask google-chrome'
    if s == 'Windows':
        return 'winget install Google.Chrome'
    return 'sudo apt install chromium (or: npx playwright install chromium)'


def find(cfg=None, system=None, env=None, home=None, which=shutil.which, exists=os.path.exists, listdir=os.listdir):
    """(path, None), or (None, why with the install command)."""
    own = settings(cfg)['path']
    if own:
        return (own, None) if exists(own) else (None, f'browser.path ({own}) does not exist: fix it in Settings, Work browser')
    for n in NAMES:
        hit = which(n)
        if hit:
            return hit, None
    paths, cache = candidates(system, env, home)
    for p in paths:
        if exists(p):
            return p, None
    bundled = playwright_chromium(cache, listdir, exists)
    if bundled:
        return bundled, None
    return None, f'no Chrome or Chromium on the browser machine: install one ({install_hint(system)})'


def launch_argv(path, profile, port, headless, url=None):
    """Dedicated mode's command line: its own profile, DevTools on 127.0.0.1 only, no first-run pages."""
    argv = [path, f'--user-data-dir={profile}', f'--remote-debugging-address={HOST}', f'--remote-debugging-port={port}',
            '--no-first-run', '--no-default-browser-check', '--disable-background-networking']
    if headless:
        argv.append('--headless=new')
    return argv + [url or 'about:blank']


def ws_url(port, path):
    """The DevTools WebSocket on 127.0.0.1: plain ws, since it never leaves this machine (TLS has no meaning on loopback)."""
    return urllib.parse.urlunsplit(('ws', f'{HOST}:{port}', path, '', ''))


def local_ws(url):
    u = urllib.parse.urlparse(url)
    if u.scheme != 'ws' or u.hostname != HOST:
        raise BrowserError(f'the DevTools address {url} is not on {HOST}: Tutti only drives the work browser through 127.0.0.1')
    return u


# ---- a minimal WebSocket client (RFC 6455, text frames) for DevTools

def ws_frame(text, mask=None, opcode=1):
    data = text.encode('utf8') if isinstance(text, str) else text
    mask = mask if mask is not None else os.urandom(4)
    n = len(data)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + n.to_bytes(2, 'big')
    else:
        head += bytes([0x80 | 127]) + n.to_bytes(8, 'big')
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def ws_read(recv):
    """One frame's (opcode, payload) from `recv(n)`, which returns exactly n bytes."""
    op, data, _fin = ws_read_frame(recv)
    return op, data


def ws_read_frame(recv):
    """One frame's (opcode, payload, final) from `recv(n)`."""
    b0, b1 = recv(2)
    n = b1 & 0x7F
    if n == 126:
        n = int.from_bytes(recv(2), 'big')
    elif n == 127:
        n = int.from_bytes(recv(8), 'big')
    mask = recv(4) if b1 & 0x80 else None
    data = recv(n) if n else b''
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return b0 & 0x0F, data, bool(b0 & 0x80)


class Cdp:
    """One DevTools connection to the browser; calls are serialised, events skipped. Subclasses carry the messages."""

    def __init__(self):
        self.next, self.lock = 0, threading.Lock()

    def call(self, method, params=None, session=None):
        with self.lock:
            self.next += 1
            msg = {'id': self.next, 'method': method, 'params': params or {}}
            if session:
                msg['sessionId'] = session
            self.send(json.dumps(msg))
            while True:
                answer = self.receive()
                if answer.get('id') == msg['id']:
                    if 'error' in answer:
                        raise BrowserError(f'{method}: {answer["error"].get("message")}')
                    return answer.get('result') or {}


class WsCdp(Cdp):
    """DevTools over a WebSocket on 127.0.0.1 (existing and dedicated modes)."""

    def __init__(self, ws_url, timeout=60):
        super().__init__()
        u = local_ws(ws_url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=timeout)
        self.rest = b''
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f'GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        while b'\r\n\r\n' not in self.rest:
            self.rest += self.chunk()
        head, self.rest = self.rest.split(b'\r\n\r\n', 1)
        if b' 101 ' not in head.split(b'\r\n', 1)[0]:
            raise BrowserError('the work browser refused the DevTools connection (was "Allow" declined in Chrome?): try again '
                               'and allow it')

    def chunk(self):
        c = self.sock.recv(65536)
        if not c:
            raise BrowserError('the work browser closed the DevTools connection')
        return c

    def recv(self, n):
        while len(self.rest) < n:
            self.rest += self.chunk()
        out, self.rest = self.rest[:n], self.rest[n:]
        return out

    def send(self, text):
        self.sock.sendall(ws_frame(text))

    def receive(self):
        """One whole message: continuation frames joined, pings answered, other control frames skipped."""
        parts = []
        while True:
            op, data, fin = ws_read_frame(self.recv)
            if op == 8:
                raise BrowserError('the work browser closed the DevTools connection')
            if op == 9:
                self.sock.sendall(ws_frame(data, opcode=10))
            elif op in (0, 1):
                parts.append(data)
                if fin:
                    return json.loads(b''.join(parts).decode('utf8'))

    def close(self):
        with contextlib.suppress(OSError):
            self.sock.close()


class UnixCdp(Cdp):
    """DevTools over the isolated browser's Unix socket: its bridge relays Chrome's --remote-debugging-pipe, so no TCP port
    exists at all. Messages end with a NUL byte, as on the pipe."""

    def __init__(self, path, timeout=60):
        super().__init__()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(path)
        except OSError as e:
            self.sock.close()
            raise BrowserError(f'the isolated work browser does not answer on {path} ({e.strerror}): on the browser machine run '
                               '`sudo systemctl start tutti-browser` (the one-time setup: `tutti browser isolate`)') from None
        self.rest = b''

    def send(self, text):
        self.sock.sendall(text.encode('utf8') + b'\0')

    def receive(self):
        while b'\0' not in self.rest:
            c = self.sock.recv(65536)
            if not c:
                raise BrowserError('the isolated work browser closed its socket')
            self.rest += c
        msg, self.rest = self.rest.split(b'\0', 1)
        return json.loads(msg.decode('utf8'))

    def close(self):
        with contextlib.suppress(OSError):
            self.sock.close()


def open_cdp(endpoint):
    return UnixCdp(endpoint[5:]) if endpoint.startswith('unix:') else WsCdp(endpoint)


class Browser:
    """The work browser as this process sees it: one connection, a queue, at most max_tabs tabs."""

    def __init__(self, cfg=None, popen=subprocess.Popen, connect=open_cdp, clock=time.monotonic, sleep=time.sleep, finder=find,
                 read_file=None, tunnel=None, env=None, watch=True, downloads=None):
        self.cfg, self.popen, self.connect, self.clock, self.sleep, self.finder = cfg, popen, connect, clock, sleep, finder
        self.read_file = read_file or (lambda p: open(p, encoding='utf8').read())
        self.tunnel, self.env, self.tunnel_proc = tunnel, os.environ if env is None else env, None
        self.s = settings(cfg)
        self.proc, self.headless, self.cdp, self.last = None, None, None, clock()
        self.tabs, self.waiting, self.sites, self.signing = {}, [], {}, False
        self.cond = threading.Condition(threading.RLock())
        self.watch, self.watcher = watch, None
        self.probe = lambda port: socket.create_connection((HOST, port), timeout=1).close()
        self.downloads = downloads   # the one scoped downloads folder (dedicated and isolated modes; never the owner's Chrome)

    @property
    def mode(self):
        return self.s['mode'] if self.s['mode'] in MODES else 'dedicated'

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def endpoint(self, start=True):
        """The browser's DevTools address on 127.0.0.1 (or its Unix socket): a worker's tunnel, the browser machine through
        Tutti's ssh tunnel, or this machine. `start`: dedicated mode starts Tutti's own Chrome when it does not run."""
        if self.env.get(ENV_WS):
            return local_ws(self.env[ENV_WS]).geturl()
        if self.tunnel:   # the browser machine is another machine: one tunnel per connection, closed with it
            address, self.tunnel_proc = self.tunnel(self.cfg)
            return address
        if self.mode == 'isolated':
            return 'unix:' + (self.s['socket'] or SOCKET)
        port_file = os.path.join(active_profile(self.cfg), ACTIVE_PORT)
        if self.mode == 'dedicated' and start and not self.running() and not self.answers(port_file):
            self.start(bool(self.s['headless']))   # nothing answers: a port file left by a Chrome that is gone is replaced
        try:
            port, path = parse_active_port(self.read_file(port_file))
        except OSError:
            if self.mode == 'existing':
                raise BrowserError(f'your Chrome on this computer does not accept Tutti\'s tasks yet: {EXISTING_HELP}') from None
            raise BrowserError('Tutti\'s own Chrome is not running: `tutti browser status` shows its program') from None
        return ws_url(port, path)

    def answers(self, port_file):
        """True when a Chrome (maybe started by another Tutti process) already listens at the port file's address."""
        try:
            port, _ = parse_active_port(self.read_file(port_file))
            self.probe(port)
            return True
        except (OSError, BrowserError):
            return False

    def start(self, headless, url=None):
        """Dedicated mode only: starts Tutti's own Chrome (restarted in the other mode); returns once its port is written."""
        with self.cond:
            if self.mode != 'dedicated':
                raise BrowserError('Tutti never starts your own Chrome (browser.mode existing): ' + EXISTING_HELP)
            if self.running() and self.headless == headless and url is None:
                return
            port_file = os.path.join(profile_dir(self.cfg), ACTIVE_PORT)
            if not self.running() and headless and self.answers(port_file):
                return   # another Tutti process runs it: attach, never start a second Chrome on the same profile
            self.close()
            path, why = self.finder(self.cfg)
            if not path:
                raise BrowserError(why)
            profile = check_profile(profile_dir(self.cfg))
            os.makedirs(profile, mode=0o700, exist_ok=True)
            with contextlib.suppress(OSError):
                os.remove(os.path.join(profile, ACTIVE_PORT))
            self.proc = self.popen(launch_argv(path, profile, int(self.s['port']), headless, url), stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            self.headless, self.last = headless, self.clock()
            self.wait_ready(os.path.join(profile, ACTIVE_PORT))
            self.start_watch()

    def wait_ready(self, port_file):
        end = self.clock() + START_SECONDS
        while self.clock() < end:
            if not self.running():
                raise BrowserError('the work browser stopped right after it started: `tutti browser status` shows its program')
            with contextlib.suppress(OSError, BrowserError):
                parse_active_port(self.read_file(port_file))
                return
            self.sleep(0.25)
        raise BrowserError(f'the work browser did not open its DevTools port within {START_SECONDS} s: another program may use '
                           f'port {self.s["port"]}; set browser.port in Settings')

    def start_watch(self):
        if not self.watch or (self.watcher and self.watcher.is_alive()):
            return
        self.watcher = threading.Thread(target=self.watch_loop, name='tutti-browser-idle', daemon=True)
        self.watcher.start()

    def watch_loop(self):
        while self.cdp is not None or self.running():
            self.sleep(15)
            self.reap()

    def connection(self):
        with self.cond:
            if self.cdp is None:
                try:
                    self.cdp = self.connect(self.endpoint())
                except (OSError, BrowserError) as e:
                    self.disconnect()   # the tunnel opened for it ends too
                    if isinstance(e, BrowserError):
                        raise
                    raise BrowserError(f'the work browser does not answer ({e}): '
                                       + (EXISTING_HELP if self.mode == 'existing' else 'try again in a minute')) from None
                if self.downloads and self.mode != 'existing':
                    self.cdp.call('Browser.setDownloadBehavior', {'behavior': 'allow', 'downloadPath': self.downloads})
                self.start_watch()
            return self.cdp

    def reap(self, now=None):
        """After idle_minutes with no tab: the connection closes, and a hidden dedicated Chrome too. The owner's Chrome and a
        visible sign-in window stay."""
        with self.cond:
            idle = (now if now is not None else self.clock()) - self.last
            if self.tabs or self.waiting or idle < float(self.s['idle_minutes']) * 60:
                return False
            # a visible sign-in window gets three times as long, then closes too
            if self.running() and not self.headless and idle < float(self.s['idle_minutes']) * 180:
                return False
            if self.cdp is None and not self.running():
                return False
            self.close()
            return True

    def disconnect(self):
        with self.cond:
            if self.cdp is not None:
                self.cdp.close()
            self.cdp = None
            if self.tunnel_proc is not None:
                with contextlib.suppress(OSError):
                    self.tunnel_proc.terminate()
                self.tunnel_proc = None

    def close(self):
        """Closes the connection, and Tutti's own Chrome in dedicated mode. The owner's Chrome is never closed."""
        with self.cond:
            self.disconnect()
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc, self.headless = None, None

    @contextlib.contextmanager
    def task(self, url, who='tutti'):
        """A tab of its own for one task, queued while max_tabs tabs are open, closed when the task ends (also on failure)."""
        ticket = {'who': who, 'url': url, 'since': time.time()}
        with self.cond:
            self.waiting.append(ticket)
            while self.signing or len(self.tabs) >= max(1, int(self.s['max_tabs'])) or self.waiting[0] is not ticket:
                self.cond.wait(1)
            self.waiting.pop(0)
            key = object()
            self.tabs[key] = ticket
        target, slot = None, None
        try:
            slot = self.host_slot()
            try:
                cdp = self.connection()
                target = cdp.call('Target.createTarget', {'url': url, 'background': True})['targetId']
                ticket['target'] = target
                session = cdp.call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
            except OSError:
                self.disconnect()   # the connection itself broke: the next task connects again
                raise
            yield {'target': target, 'session': session}
        finally:
            try:
                self.close_tab(target)   # whatever the task did, its tab is closed
            finally:
                if slot is not None:
                    os.close(slot)   # closing the file releases its flock: another process's task may take the tab
                with self.cond:
                    self.tabs.pop(key, None)
                    self.last = self.clock()
                    self.cond.notify_all()

    def host_slot(self):
        """One of max_tabs lock files in <state>/browser-tabs, held with flock while the task's tab is open: the limit holds for
        every Tutti process on this machine (the dashboard, the CLI, a worker's broker), not only this one. None where flock is
        missing (Windows) or there is no state folder (tests)."""
        try:
            import fcntl
            from . import config
            folder = os.path.join(config.state_dir(self.cfg), 'browser-tabs')
        except (ImportError, KeyError, TypeError):
            return None
        os.makedirs(folder, mode=0o700, exist_ok=True)
        n = max(1, int(self.s['max_tabs']))
        while True:
            for i in range(n):
                fd = os.open(os.path.join(folder, f'slot-{i}.lock'), os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except OSError:
                    os.close(fd)
            self.sleep(0.5)

    def close_tab(self, target):
        """Closes a task's tab; only a connection that cannot even do that is dropped (other tasks keep a working one)."""
        with self.cond:
            cdp = self.cdp
        if not target or cdp is None:
            return
        try:
            cdp.call('Target.closeTarget', {'targetId': target})
        except (OSError, BrowserError, AttributeError):
            self.disconnect()

    def evaluate(self, tab, expression):
        r = self.connection().call('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True},
                                   session=tab['session'])
        return (r.get('result') or {}).get('value')

    def sign_in(self, url):
        """Opens `url` in the work browser, in front, for one sign-in; the tab is the person's to close. Dedicated mode first
        turns a hidden Chrome into a visible one."""
        if self.mode == 'isolated':
            raise BrowserError(f'the isolated work browser has no window: on the browser machine run `tutti browser isolate '
                               f'--sign-in {url}` for the one command that opens a visible window of it for that sign-in')
        if self.mode == 'dedicated' and not self.env.get(ENV_WS) and not self.tunnel and (not self.running() or self.headless):
            if not self.running() and self.answers(os.path.join(profile_dir(self.cfg), ACTIVE_PORT)):
                raise BrowserError('Tutti\'s own Chrome runs hidden in another Tutti process (the dashboard): use Sign in on the '
                                   'Work browser card there, or run `tutti browser close` in it first')
            with self.cond:
                self.signing = True   # goes first: new tasks wait; the ones running keep their tabs until they finish
                try:
                    while self.tabs:
                        self.cond.wait(1)
                    self.start(False)
                finally:
                    self.signing = False
                    self.cond.notify_all()
        self.connection().call('Target.createTarget', {'url': url})
        with self.cond:
            self.last = self.clock()

    def state(self):
        """Tabs and the queue with the site only (never a path or a query: they may carry a code)."""
        def row(t):
            return {'who': t['who'], 'site': urllib.parse.urlparse(t['url']).hostname or '', 'since': t['since']}
        with self.cond:
            return {'mode': self.mode, 'connected': self.cdp is not None, 'tabs': [row(t) for t in self.tabs.values()],
                    'queue': [row(t) for t in self.waiting], 'sites': dict(self.sites)}


LOGIN_SEGMENTS = {'login', 'signin', 'sign-in', 'sign_in', 'log-in'}


def same_host(host, other):
    """cursor.com and www.cursor.com are one host; a sibling such as authenticator.cursor.com is not."""
    a, b = (h.lower().removeprefix('www.') for h in (host or '', other or ''))
    return bool(a) and a == b


def signed_out(url, href):
    """A page that moved to another host (an authenticator.<site> included) or to a login path segment asks for a sign-in."""
    h = urllib.parse.urlparse(href or '')
    segments = {s.lower() for s in (h.path or '').split('/') if s}
    return not same_host(h.hostname, urllib.parse.urlparse(url).hostname) or bool(segments & LOGIN_SEGMENTS)


TEXT = 'JSON.stringify({loading: document.readyState !== "complete", text: document.body ? document.body.innerText.slice(0, 2000) : ""})'


def run_page(b, url, script=None, who='tutti', tries=10, wait=1.5):
    """One task: opens `url` in its own tab, runs `script` (a JSON string expression, default the page's text) until the page
    stops loading, and returns {value, href, signed_out}; the tab is closed after."""
    with b.task(url, who) as t:
        value = None
        for _ in range(tries):
            b.sleep(wait)
            raw = b.evaluate(t, script or TEXT)
            try:
                value = json.loads(raw) if isinstance(raw, str) else {}
            except ValueError:
                value = {}
            value = value if isinstance(value, dict) else {}
            if value.get('readings') or (value and not value.get('loading')):
                break
        href = b.evaluate(t, 'location.href') or ''
    return {'value': value or {}, 'href': href, 'signed_out': signed_out(url, href)}
