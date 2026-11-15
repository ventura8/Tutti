"""`tutti browser` and the Work browser card: the household's one work browser (tutti/browser.py, docs/BROWSER.md).

  tutti browser status                          the browser machine, its mode, the endpoint
  tutti browser sign-in <url>                   opens <url> in the work browser, in front, for a one-time sign-in
  tutti browser sites                           checks the sign-in of every needed site
  tutti browser read                            reads every usage page with no reader, each in its own tab, and records it
  tutti browser page <url> [--type T]           one task through the broker: the page's text, its tab closed after
  tutti browser remote <machine> <url> [--type T] [-- <cmd>]
                                                a worker on <machine> does a task through the broker (ssh -R, 127.0.0.1)
  tutti browser isolate                         isolated mode's one-time root setup: prints the one command to run
  tutti browser close                           closes Tutti's connection (and Tutti's own Chrome in dedicated mode)"""
import base64
import getpass
import json
import os
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone

from . import browser, browserbroker, browsersandbox, config, helper, remote


def say(msg, out=None):
    print(msg, file=out or sys.stderr)


def site(url):
    return urllib.parse.urlparse(url).hostname or url


def browser_machine(cfg):
    """(name, machine) of the browser machine, or ('', None) for the one Tutti runs on."""
    name = browser.settings(cfg)['machine']
    m = (cfg.get('machines') or {}).get(name) if name else None
    if name and m is None:
        raise browser.BrowserError(f'browser.machine is {name}, which is not under machines: pick one in Settings, Work browser')
    return (name, m) if m and m.get('transport') != 'local' else ('', None)


def needed_sites(cfg):
    """Sites the work browser must be signed in to: browser.sites (the admin's list) and every reading helper page."""
    out = list(browser.settings(cfg)['sites'] or [])
    return out + [u for u in helper.helper_pages(cfg) if u not in out]


def remote_port_file(cfg):
    """The port file's path as a shell word for the browser machine: ~ expands there, the rest is quoted."""
    s = browser.settings(cfg)
    own = s['user_profile'] if s['mode'] == 'existing' else s['profile']
    if not own:
        return ('"$HOME/.config/google-chrome"' if s['mode'] == 'existing'
                else '"${XDG_DATA_HOME:-$HOME/.local/share}/tutti/browser"') + f'/{browser.ACTIVE_PORT}'
    if own == '~' or own.startswith('~/'):
        return '"$HOME"' + shlex.quote(own[1:] + '/' + browser.ACTIVE_PORT)
    return shlex.quote(os.path.join(own, browser.ACTIVE_PORT))


FORWARD_ONLY = ('-o', 'ExitOnForwardFailure=yes')


def free_port():
    """A free port on 127.0.0.1 here (the system picks it); ExitOnForwardFailure stops ssh if it is taken meanwhile."""
    with socket.socket() as s:
        s.bind((browser.HOST, 0))
        return s.getsockname()[1]


def remote_port():
    """A port on the worker's machine: if it is taken there, ExitOnForwardFailure ends the task with an error, and the
    broker behind it answers only its one-time token."""
    return 20000 + secrets.randbelow(10000)


def wait_open(test, sleep=time.sleep, tries=40):
    """True once `test()` connects (a tunnel's local end is listening)."""
    for _ in range(tries):
        try:
            test()
            return True
        except OSError:
            sleep(0.25)
    return False


def tcp_test(port):
    return lambda: socket.create_connection((browser.HOST, port), timeout=1).close()


def unix_test(path):
    def test():
        s = socket.socket(socket.AF_UNIX)
        try:
            s.connect(path)
        finally:
            s.close()
    return test


def open_tunnel(cfg, sh=remote.sh, popen=subprocess.Popen, wait=wait_open):
    """(address, ssh process): the browser machine's DevTools forwarded to 127.0.0.1 (or a Unix socket) here over Tutti's ssh
    link, both ends local only. The caller keeps the process and ends it with the connection; returns once it listens."""
    name, m = browser_machine(cfg)
    m = {**m, 'name': name}
    if browser.settings(cfg)['mode'] == 'isolated':
        local = os.path.join(config.state_dir(cfg), f'browser-{name}.sock')
        forward, address, test = f'{local}:{browser.settings(cfg)["socket"] or browser.SOCKET}', 'unix:' + local, unix_test(local)
        extra = ('-o', 'StreamLocalBindUnlink=yes')
    else:
        r = sh(m, f'cat {remote_port_file(cfg)}', capture=True, timeout=20)
        if r.returncode != 0:
            raise browser.BrowserError(f'the work browser on {name} does not accept tasks: '
                                       + (browser.EXISTING_HELP if browser.settings(cfg)['mode'] == 'existing'
                                          else f'Tutti\'s own Chrome does not run there: on {name}, run `tutti browser page <one of '
                                               'the sites>` once (it starts it), or use mode existing'))
        port, path = browser.parse_active_port(r.stdout)
        local = free_port()
        forward, address, test, extra = f'{browser.HOST}:{local}:{browser.HOST}:{port}', browser.ws_url(local, path), tcp_test(local), ()
    proc = popen(remote.argv(m, 'exec sleep 86400', ssh_opts=(*FORWARD_ONLY, *extra, '-L', forward)),
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    if not wait(test):
        proc.terminate()
        raise browser.BrowserError(f'the ssh tunnel to the work browser on {name} did not open: check `ssh {m.get("host", name)} '
                                   'true` from here, then try again')
    return address, proc


_SHARED = {}
_SHARED_LOCK = threading.Lock()


def shared(cfg):
    """The one work browser of this process: the dashboard's tasks and the CLI share it."""
    with _SHARED_LOCK:
        return _shared(cfg)


def _shared(cfg):
    key = json.dumps(browser.settings(cfg), sort_keys=True)
    if _SHARED.get('key') != key:
        if _SHARED.get('b') is not None:
            _SHARED['b'].close()
        mode = browser.settings(cfg)['mode']
        _SHARED['b'] = browser.Browser(cfg, tunnel=open_tunnel if browser_machine(cfg)[1] else None,
                                       downloads=None if browser_machine(cfg)[1] else downloads_of(cfg, mode))
        _SHARED['key'] = key
    return _SHARED['b']


def broker(cfg, b=None):
    b = b or shared(cfg)
    pages = helper.helper_pages(cfg)
    return browserbroker.Broker(b, browserbroker.scopes(cfg, pages, needed_sites(cfg)),
                                os.path.join(config.state_dir(cfg), browserbroker.LOG),
                                None if browser_machine(cfg)[1] else downloads_of(cfg, b.mode), pages,
                                browser.settings(cfg)['file_tasks'],
                                max_file_mb=(cfg.get('browser') or {}).get('max_file_mb'))


def downloads_of(cfg, mode):
    """The one scoped downloads folder; none in existing mode (the owner's own Chrome keeps its own downloads)."""
    if mode == 'isolated':
        return browserbroker.DOWNLOADS
    return os.path.join(config.state_dir(cfg), 'browser-downloads') if mode == 'dedicated' else None


def view(cfg):
    """The Work browser card: machine, mode, tabs per user or worker, the queue, each needed site's sign-in. Starts nothing."""
    b = _SHARED.get('b')
    st = b.state() if b is not None else {'mode': browser.settings(cfg)['mode'], 'connected': False, 'tabs': [], 'queue': [],
                                          'sites': {}}
    try:
        machine = browser_machine(cfg)[0] or 'this computer'
    except browser.BrowserError as e:
        machine, st['problem'] = browser.settings(cfg)['machine'], str(e)
    st['sites'] = [{'url': u, 'site': site(u), **st['sites'].get(u, {'signed_in': None})} for u in needed_sites(cfg)]
    for t in st['tabs'] + st['queue']:   # every signed-in user sees the card: a user's name, never their address
        t['who'] = t['who'] if t['who'].startswith('worker@') or '@' not in t['who'] else t['who'].split('@', 1)[0]
    return {**st, 'machine': machine, 'max_tabs': browser.settings(cfg)['max_tabs']}


def read_pages(cfg, brk, record, who='tutti', pages=None):
    """Reads each helper page in its own tab; `record(json_text)` saves the readings. Returns {url: problem or None}."""
    out = {}
    for url in (pages if pages is not None else helper.helper_pages(cfg)):
        try:
            r = brk.page('usage-reading', url, 'usage', who)
        except (browser.BrowserError, OSError, ValueError) as e:
            out[url] = f'the work browser could not read {site(url)} ({e})'
            continue
        data = r['value']
        if not data.get('readings') and r['signed_out']:
            out[url] = f'{site(url)} asks for a sign-in on the work browser: Sign in to {site(url)} on the Work browser card'
            continue
        out[url] = None if record(json.dumps({k: data.get(k) for k in ('readings', 'missing', 'loading')})) == 0 \
            else f'{site(url)}: some meters were not read (see the lines above)'
    return out


def check_sites(cfg, brk, who):
    for url in needed_sites(cfg):
        try:
            r = brk.page('sign-in-check', url, 'check', who, tries=5, wait=1)
            brk.b.sites[url] = {'signed_in': not r['signed_out'], 'at': datetime.now(timezone.utc).timestamp()}
        except (browser.BrowserError, OSError, ValueError) as e:
            brk.b.sites[url] = {'signed_in': None, 'problem': str(e)}
    return brk.b.sites


def remote_task(cfg, machine_name, kind, url, command=None, sh=remote.sh, brk=None):
    """A worker on another machine does a task through the broker, never the browser: a one-task-type broker on 127.0.0.1
    here, forwarded to 127.0.0.1 on that machine (ssh -R) while the worker's command runs. Without a command the worker
    runs the broker's own client (standard library, sent on standard input) for `url`."""
    m = (cfg.get('machines') or {}).get(machine_name)
    if not m:
        raise browser.BrowserError(f'{machine_name} is not under machines: name one of {", ".join(cfg.get("machines") or {})}')
    brk = brk or broker(cfg)
    brk.check(kind, url, f'worker@{machine_name}')
    httpd = browserbroker.serve(brk, kind, f'worker@{machine_name}')
    port = remote_port()
    client = base64.b64encode(browserbroker.CLIENT.encode()).decode()
    # The token travels on standard input and lives in the environment only: never on a command line (`ps` shows those).
    address = urllib.parse.urlunsplit(('http', f'{browser.HOST}:{port}', '', '', ''))   # loopback only, through ssh
    lines = [f'export TUTTI_BROWSER_URL={shlex.quote(address)}',
             f'export TUTTI_BROWSER_TOKEN={shlex.quote(httpd.token)}', f'export TUTTI_BROWSER_TYPE={shlex.quote(kind)}',
             f'export TUTTI_BROWSER_ARGS={shlex.quote(json.dumps([url]))}',
             command or f'python3 -c "$(printf %s {client} | base64 -d)"']
    with browserbroker.running(httpd):
        return sh({**m, 'name': machine_name}, 'bash -s', capture=True, timeout=200, stdin='\n'.join(lines) + '\n',
                  ssh_opts=(*FORWARD_ONLY, '-R', f'{browser.HOST}:{port}:{browser.HOST}:{httpd.server_address[1]}'))


def snap_wrapper(path):
    """True for a snap's binary or the script some distributions ship in its place (Ubuntu's /usr/bin/chromium)."""
    real = os.path.realpath(path)
    if real.startswith('/snap/') or os.path.basename(real) == 'snap':
        return True
    try:
        with open(real, 'rb') as f:
            head = f.read(4096)
    except OSError:
        return False
    return head.startswith(b'#!') and b'snap' in head


def isolate(cfg, sign_in=None, script=False, out=None):
    path, why = browser.find(cfg)
    if path and (path.startswith(('/home/', '/snap/', os.path.expanduser('~') + os.sep)) or snap_wrapper(path)):
        path, why = None, (f'{path} cannot run in the sandbox (no home folders, no snap confinement there): install a system '
                           'Chrome or Chromium from a .deb (`sudo apt install chromium` on Debian, Google\'s .deb on Ubuntu), or set '
                           'browser.path to one under /usr or /opt')
    if not path:
        say(f'browser isolate: {why}', out)
        return 2
    if sign_in:
        browserbroker.Broker(None, browserbroker.scopes(cfg, helper.helper_pages(cfg), needed_sites(cfg)),
                             os.path.join(config.state_dir(cfg), browserbroker.LOG)).check('sign-in', sign_in, getpass.getuser())
        say(f'sign in to {site(sign_in)} on the isolated work browser: run this, sign in, close the window:\n  '
            + browsersandbox.sign_in_command(path, sign_in), out)
        return 0
    if script:   # `tutti browser isolate --script | sudo bash`: the one command of the move plan
        print(browsersandbox.setup_script(path, getpass.getuser(), browser.settings(cfg)['allow_local']))
        return 0
    cmd = browsersandbox.write_setup(config.state_dir(cfg), path, getpass.getuser(), browser.settings(cfg)['allow_local'])
    say(f'isolated work browser: run this once on this machine (it creates the user {browsersandbox.USER}, the group '
        f'{browsersandbox.GROUP} and the sandboxed unit, then starts it):\n  {cmd}\nthen set browser.mode to isolated in Settings', out)
    return 0


def status(cfg, b, out=None):
    v = view(cfg)
    s = browser.settings(cfg)
    kind = {'existing': 'your own Chrome profile with its sign-ins', 'isolated': f'Tutti\'s sandboxed Chrome as {browsersandbox.USER}'}
    say(f'work browser: {v["machine"]}, mode {v["mode"]} ({kind.get(v["mode"], "its own profile " + browser.profile_dir(cfg))}); '
        f'DevTools on {browser.HOST} or a Unix socket only; at most {s["max_tabs"]} tabs; idle close after {s["idle_minutes"]} min', out)
    try:
        say(f'endpoint: {b.endpoint(start=False)}'.split('/devtools/')[0], out)
    except browser.BrowserError as e:
        say(f'endpoint: {e}', out)
        return 2
    finally:
        b.disconnect()   # a tunnel opened to look is closed again; nothing is started
    return 0


def task_action(cfg, a, brk):
    from . import cli  # cli imports this module
    if a.action == 'sign-in':
        brk.sign_in(a.url, getpass.getuser())
        say(f'sign in to {site(a.url)} in the work browser\'s tab that just opened; close the tab when done')
        return 0
    if a.action == 'sites':
        for u, x in check_sites(cfg, brk, getpass.getuser()).items():
            say(f'{site(u)}: ' + {True: 'signed in', False: f'not signed in: `tutti browser sign-in {u}`'}.get(
                x['signed_in'], f'unknown ({x.get("problem")})'))
        return 0
    if a.action == 'page':
        print(json.dumps(brk.page(a.type, a.url, 'text', getpass.getuser())))
        return 0
    if a.action == 'remote':
        r = remote_task(cfg, a.machine, a.type, a.url, ' '.join(shlex.quote(x) for x in a.command) or None, brk=brk)
        sys.stdout.write(r.stdout)
        if r.stderr.strip():
            say(r.stderr.strip())
        return r.returncode
    now = datetime.now(timezone.utc)
    path = cli.usage_log_path(cfg)
    problems = read_pages(cfg, brk, lambda text: cli.page_reading(cfg, path, text, now), getpass.getuser())
    for p in filter(None, problems.values()):
        say(f'browser read: {p}')
    if not problems:
        say('browser read: no account has a usage_url without its own reader, so there is nothing to read')
    return 1 if any(problems.values()) else 0


def parse_target(a):
    """a.url, a.machine and a.command from the positional words: <url>, or for remote <machine> <url> [<command>...]."""
    t = [x for x in (getattr(a, 'target', None) or []) if x != '--']
    if a.action == 'remote':
        a.machine, a.url, a.command = (t + [None, None])[0], (t + [None, None])[1], t[2:]
    else:
        a.machine, a.url, a.command = None, t[0] if t else None, []
    if a.action == 'isolate':
        a.url = getattr(a, 'sign_in_url', None)
    return a


def cmd_browser(cfg, a, b=None):
    a = parse_target(a)
    if a.action == 'isolate':
        try:
            return isolate(cfg, a.url, getattr(a, 'script', False))
        except browser.BrowserError as e:
            say(f'browser isolate: {e}')
            return 2
    b = b or shared(cfg)
    if a.action == 'status':
        return status(cfg, b)
    if a.action == 'close':
        b.close()
        say('the work browser: Tutti\'s connection is closed' + (' and its own Chrome too' if b.mode == 'dedicated' else ''))
        return 0
    if a.action in ('sign-in', 'page', 'remote') and not a.url:
        say(f'browser {a.action}: name the page, e.g. `tutti browser {a.action} https://cursor.com/dashboard/spending`')
        return 2
    try:
        return task_action(cfg, a, broker(cfg, b))
    except browser.BrowserError as e:
        say(f'browser {a.action}: {e}')
        return 2
    finally:
        if a.action != 'sign-in':   # the sign-in window stays open: the person closes it when done (this process ends now)
            b.close()


def act(cfg, who, body, brk=None, now=None):
    """POST /api/browser: {action: 'sign-in'|'read', url} or {action: 'check'}. Any signed-in user may use the work browser
    for the sites the admin chose; its sign-ins are shared by everyone, so the site list is the admin's (Settings). Each
    task's tab carries who asked."""
    brk = brk or broker(cfg)
    action, url = body.get('action'), body.get('url')
    label = (who or {}).get('email') or 'owner'
    handler = {'sign-in': act_sign_in, 'read': act_read, 'check': act_check}.get(action)
    if handler is None or (action == 'sign-in' and not isinstance(url, str)):
        return 400, {'error': 'send {"action": "sign-in", "url": ...}, {"action": "read"} or {"action": "check"}'}
    if url is not None and not isinstance(url, str):
        return 400, {'error': f'url is {url!r}: give the page\'s address as text, such as "https://example.com/usage", or leave '
                              'it out to read every usage page'}
    try:
        return handler(cfg, brk, url, label, now)
    except browserbroker.OutOfScope as e:
        return 403, {'error': str(e)}
    except browser.BrowserError as e:
        return 503, {'error': str(e)}


def act_sign_in(cfg, brk, url, label, _now):
    brk.sign_in(url, label)
    return 200, {'ok': True, 'message': f'Sign in to {site(url)} in the work browser\'s tab that just opened '
                                        f'(on {view(cfg)["machine"]}); everyone who uses the work browser shares it.'}


def act_read(cfg, brk, url, label, now):
    from . import cli  # cli imports this module
    now = now or datetime.now(timezone.utc)
    path = cli.usage_log_path(cfg)
    pages = {u: t for u, t in helper.helper_pages(cfg).items() if not url or u == url}
    if not pages:
        return 404, {'error': (f'{site(url)} is not a usage page the work browser reads' if url else 'no account has a '
                               'usage page without its own reader') + ': add usage_url to the account in Settings'}
    problems = read_pages(cfg, brk, lambda text: cli.page_reading(cfg, path, text, now), label, pages)
    return 200, {'ok': not any(problems.values()), 'problems': {u: p for u, p in problems.items() if p},
                 'message': f'Read {len(pages)} page(s) in the work browser.'}


def act_check(cfg, brk, _url, label, _now):
    sites = check_sites(cfg, brk, f'sign-in check ({label})')
    problems = {u: f'{site(u)}: {x["problem"]}' for u, x in sites.items() if x.get('problem')}
    return 200, {'ok': not problems, 'problems': problems, 'browser': view(cfg)}


def post(collector, headers, raw):
    """The dashboard's SELF_POST: who asks comes from the sign-in (docs/AUTH.md); none means this computer's owner."""
    try:
        body = json.loads(raw or b'{}')
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return 400, {'error': 'send a JSON object such as {"action": "check"}'}
    who_fn = getattr(collector, 'who', None)
    return act(collector.cfg, who_fn(headers) if callable(who_fn) else None, body)
