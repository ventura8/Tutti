"""Self-healing (docs/SELFHEAL.md): each round, on every machine that runs Tutti's failover round or `tutti selfheal`, a list
of checks, each with its own repair. A repair is done, not reported: it is logged (<state>/selfheal.jsonl) and the dashboard
shows "Self-healed: N today" with the details. Only what a person must do (sign in, a command that needs root) becomes one
item in the needs-input queue, once.

The checks: declared needs installed (tutti/deps.py); worker CLIs present and signed in; ollama up with its models; tunnels
up; the adb server up and wireless devices reconnected; disk and inodes (Tutti's own temp files and caches cleaned, which
replaces the stopgap /tmp cron: backlog t86); the dashboard alive; state files valid (from their snapshot); the config valid
(the last good backup); self-update applied. Every probe and action is injected, so tests run them on fakes."""
import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

from . import config, deps, durable, telemetry

LOG = 'selfheal.jsonl'
KEEP = 2000                      # log lines kept
STALE_SECONDS = 2 * 3600         # a Tutti temp folder untouched this long is left over
# what a Tutti test or run leaves in an anonymous tmpXXXXXXXX folder: only such folders are removed
TUTTI_ENTRIES = {'state', 'tutti.config.yaml', 'app', 'xdg', '.gemini', 'home', 'config.yaml', 'repo', 'wt', '.tutti-state', 'tutti'}
TUTTI_PREFIXES = ('tutti-tests-', 'tutti-t-', 'tutti-review-')
CACHE_TEMP = '~/.cache/tutti-tmp'


class Ctx:
    """What a check may touch: the config, a shell runner, an HTTP reader, the clock, the needs queue."""

    def __init__(self, cfg, run=None, http=None, now=None, ask=None, which=shutil.which):
        self.cfg, self.now, self.which = cfg, now or time.time(), which
        self.run = run or (lambda script, timeout=600: subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                                                                     timeout=timeout))
        self.http = http or _http_json
        self.ask = ask or _ask


def _http_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _ask(cfg, key, question, why):
    """One needs-input item per key (asking again replaces its text): the person's one step."""
    from . import needs
    task = {'id': f'heal:{key}', 'title': question, 'repo': 'tutti'}
    return needs.ask(cfg, task, question, why, ['done', 'later'], 'done')


# ---- the log

def log_path(cfg):
    return os.path.join(config.state_dir(cfg), LOG)


def record(cfg, check, what, now=None):
    os.makedirs(config.state_dir(cfg), exist_ok=True)
    row = {'at': now or time.time(), 'host': socket.gethostname(), 'check': check, 'what': what}
    durable.append_line(log_path(cfg), json.dumps(row))
    telemetry.record('feature', 'selfheal.repair', check=check)
    return row


def trim(cfg):
    rows = durable.read_jsonl(log_path(cfg))
    if len(rows) > KEEP:
        with open(log_path(cfg) + '.tmp', 'w', encoding='utf8') as f:
            f.writelines(json.dumps(r) + '\n' for r in rows[-KEEP:])
        os.replace(log_path(cfg) + '.tmp', log_path(cfg))


def view(cfg, now=None):
    """{today: N, items: [the latest 30 of today]}: what the dashboard shows."""
    now = now or time.time()
    start = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    today = [r for r in durable.read_jsonl(log_path(cfg)) if isinstance(r, dict) and r.get('at', 0) >= start]
    return {'today': len(today), 'items': today[-30:][::-1]}


# ---- the checks: each returns lines (repairs done); a person's step goes to ctx.ask

def check_needs(ctx):
    """Every declared need of this machine's workers and projects, probed and installed (tutti/deps.py)."""
    me = _me(ctx.cfg)
    needs = [n for w in my_workers(ctx.cfg, me).values() for n in deps._list(w.get('needs'))]
    for pname, p in deps._map(ctx.cfg.get('projects')).items():
        p = deps._map(p)
        if not p.get('machines') or me in p['machines']:
            needs += deps._list(p.get('needs'))
        for sname, s in deps._map(p.get('tests')).items():   # a suite's own needs, so a root step is asked here too
            if not deps._map(s).get('machines') or me in s['machines']:
                needs += deps.for_suite(ctx.cfg, pname, sname)[len(deps._list(p.get('needs'))):]
    needs = [n for i, n in enumerate(needs) if n not in needs[:i]]
    if not needs:
        return []
    r = ctx.run(deps.script(needs))
    got = deps.parse(r.stdout)
    for cmd in got['root']:
        ctx.ask(ctx.cfg, f'root:{cmd}', f'Run once as an admin on {me}: {cmd}',
                'Tutti installs what work needs by itself, but this one needs root (administrator) rights.')
    for name in got['failed']:
        ctx.ask(ctx.cfg, f'dep:{name}', f'{name} would not install on {me}: look at `tutti selfheal --once` for the reason',
                'The install command ran but the program is still missing afterwards.')
    return [f'installed {n}' for n in got['installed']]


def my_workers(cfg, me):
    """The workers that run on this machine: those naming it (`machine:`), and those naming none on the main machine."""
    from . import failover
    main = failover.main_of(cfg) if cfg.get('failover') is not None else me
    return {n: w for n, w in deps._map(cfg.get('workers')).items()
            if isinstance(w, dict) and (w.get('machine') or main) == me}


def _me(cfg):
    from . import failover
    try:
        return failover.me(cfg)
    except Exception:   # an odd config must not stop the other checks
        return socket.gethostname()


def check_workers(ctx):
    """A CLI worker's program on PATH (installed through its needs) and signed in (its `login_check`)."""
    for name, w in my_workers(ctx.cfg, _me(ctx.cfg)).items():
        if w.get('kind') != 'cli' or not w.get('command'):
            continue
        prog = w['command'][0]
        if not ctx.which(prog):
            ctx.ask(ctx.cfg, f'worker:{name}', f'Install {prog} for the worker {name}, or add needs: to workers.{name}',
                    f'{prog} is not on PATH, and workers.{name} declares no needs: Tutti cannot install it by itself.')
            continue
        probe = w.get('login_check')
        if probe and ctx.run(' '.join(shlex.quote(str(x)) for x in probe), timeout=60).returncode != 0:
            how = w.get('login') or f'{prog} login'
            ctx.ask(ctx.cfg, f'login:{name}', f'Sign in to {name}: run `{how}` on {_me(ctx.cfg)}',
                    f'{name} answered its sign-in check with an error, so no work goes to it until you sign in.')
    return []


def _local(host):
    return str(host).split(':')[0] in ('127.0.0.1', 'localhost', '::1')


def check_ollama(ctx):
    """A local model server up (started when it is down) with each worker's model pulled."""
    out = []
    for name, w in (ctx.cfg.get('workers') or {}).items():
        if (w or {}).get('kind') != 'ollama' or w.get('tunnel') or not _local(w.get('host', '')):
            continue
        url = config.ollama_url(w['host'], '/api/tags')
        try:
            have = {m.get('name') for m in ctx.http(url).get('models') or []}
        except (OSError, ValueError):
            ctx.run('systemctl --user start ollama 2>/dev/null || (command -v ollama >/dev/null && '
                    'setsid nohup ollama serve >/dev/null 2>&1 < /dev/null &)', timeout=30)
            out.append(f'{name}: the model server at {w["host"]} did not answer: started it')
            continue
        model = w['model'] if ':' in w['model'] else w['model'] + ':latest'
        if model not in have:
            r = ctx.run(f'OLLAMA_HOST={shlex.quote(w["host"])} ollama pull {shlex.quote(w["model"])}', timeout=3600)
            got = 'pulled' if r.returncode == 0 else 'the pull failed, trying next round'
            out.append(f'{name}: model {w["model"]} was missing: {got}')
    return out


def check_tunnels(ctx):
    """Each worker's ssh tunnel running (tunnel.sh restarts the link itself; this restarts its loop)."""
    from . import remote
    out = []
    for name, w in (ctx.cfg.get('workers') or {}).items():
        if not (w or {}).get('tunnel'):
            continue
        r = ctx.run(f'{shlex.quote(remote.BIN)} --config {shlex.quote(ctx.cfg.get("_path") or "")} tunnel status {shlex.quote(name)}',
                    timeout=30)
        if 'loop not running' in (r.stdout or '') or r.returncode != 0:
            ctx.run(f'{shlex.quote(remote.BIN)} --config {shlex.quote(ctx.cfg.get("_path") or "")} tunnel start {shlex.quote(name)}',
                    timeout=30)
            out.append(f'the tunnel of {name} was down: started it')
    return out


def check_adb(ctx):
    """The adb server up, and each wireless device of this machine reconnected."""
    me = _me(ctx.cfg)
    mine = [d for d in (ctx.cfg.get('devices') or {}).values() if (d or {}).get('machine') in (me, 'local', None)]
    if not mine or not ctx.which('adb'):
        return []
    out = []
    listed = ctx.run('adb devices', timeout=30)
    if listed.returncode != 0 or 'List of devices' not in (listed.stdout or ''):
        ctx.run('adb kill-server; adb start-server', timeout=60)
        out.append('the adb server did not answer: started it again')
        listed = ctx.run('adb devices', timeout=30)
    seen = {ln.split()[0] for ln in (listed.stdout or '').splitlines()[1:] if ln.strip().endswith('device')}
    for d in mine:
        wifi = d.get('wireless') if isinstance(d.get('wireless'), list) else []
        for addr in [str(x) for x in wifi if ':' in str(x)]:
            if addr not in seen and ctx.run(f'adb connect {shlex.quote(addr)}', timeout=20).returncode == 0:
                out.append(f'{d.get("name", addr)}: reconnected over Wi-Fi at {addr}')
    return out


def temp_roots(cfg):
    extra = ((cfg.get('selfheal') or {}).get('temp_dirs')) or []
    return [tempfile.gettempdir(), config.expand(CACHE_TEMP), *[config.expand(x) for x in extra]]


def _ours(path, name):
    if name.startswith(TUTTI_PREFIXES):
        return True
    if not (name.startswith('tmp') and len(name) >= 11 and os.path.isdir(path)):
        return False
    try:
        inside = os.listdir(path)
    except OSError:
        return False
    return bool(inside) and set(inside) <= TUTTI_ENTRIES


def newest(path, limit=5000):
    """The newest modification time inside a folder (a run working in a subfolder keeps it fresh); at most `limit` entries."""
    t, seen = os.lstat(path).st_mtime, 0
    for root, dirs, files in os.walk(path):
        for n in dirs + files:
            seen += 1
            try:
                t = max(t, os.lstat(os.path.join(root, n)).st_mtime)
            except OSError:
                continue
            if seen >= limit:
                return t
    return t


def janitor(roots, now, uid=None, stale=STALE_SECONDS):
    """Removes Tutti's own left-over temp folders (this user's, untouched for `stale` seconds). Returns how many went."""
    uid = os.getuid() if uid is None else uid
    gone = 0
    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for n in names:
            p = os.path.join(root, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if st.st_uid != uid or now - st.st_mtime < stale or not os.path.isdir(p) or not _ours(p, n) or now - newest(p) < stale:
                continue
            shutil.rmtree(p, ignore_errors=True)
            gone += not os.path.exists(p)
    return gone


def check_disk(ctx, statvfs=os.statvfs):
    """Tutti's own temp folders and caches cleaned every round; still low afterwards, the admin is asked to make room."""
    from . import diskroom
    n = janitor(temp_roots(ctx.cfg), ctx.now)
    out = [f'removed {n} left-over Tutti temp folder(s)'] if n else []
    for line in diskroom.problems(ctx.cfg, statvfs):
        ctx.ask(ctx.cfg, f'disk:{line[:40]}', f'Make room on {_me(ctx.cfg)}: {line}',
                'Tutti already removed its own temp files and caches; the rest is not Tutti\'s to delete.')
    return out


def check_state(ctx):
    """State files that no longer parse put back from their last good snapshot (tutti/watchdog.py)."""
    from . import watchdog
    out = []
    for x in watchdog.round_files(config.state_dir(ctx.cfg)):
        if 'put back' in x:
            out.append(x)
        else:
            ctx.ask(ctx.cfg, f'state:{x.split()[0]}', x, 'It no longer parses and Tutti has no good copy of it to put back.')
    return out


CONFIG_GRACE = 120   # a config must stay unreadable this long (two rounds) before a backup replaces it: a save in progress


def config_fallback(path, backups, load=config.load, now=None, grace=CONFIG_GRACE):
    """When the config at `path` is no longer YAML for `grace` seconds, the newest backup that loads is put back (the broken
    one kept as .bad). A config that parses but fails a check is the person's edit in progress: never replaced. Returns a line,
    or ''."""
    import yaml
    now = now or time.time()
    mark = path + '.unreadable-since'
    try:
        with open(path, encoding='utf8') as f:
            yaml.safe_load(f)
    except yaml.YAMLError:
        pass
    else:
        if os.path.exists(mark):
            os.remove(mark)
        return ''
    if not os.path.exists(mark):
        with open(mark, 'w', encoding='utf8') as f:
            f.write(str(now))
        return ''
    with open(mark, encoding='utf8') as f:
        since = float(f.read() or now)
    if now - since < grace:
        return ''
    for b in sorted((os.path.join(backups, n) for n in os.listdir(backups) if n.startswith(os.path.basename(path) + '.'))
                    if os.path.isdir(backups) else [], key=os.path.getmtime, reverse=True):
        try:
            load(b)
        except Exception:   # this backup does not load either: try an older one
            continue
        shutil.copy2(path, path + '.bad')
        shutil.copy2(b, path + '.tmp')
        os.replace(path + '.tmp', path)
        os.remove(mark)
        return f'{os.path.basename(path)} did not load: put back {os.path.basename(b)} (the broken one is {os.path.basename(path)}.bad)'
    return ''


def check_config(ctx):
    from . import settings
    path = ctx.cfg.get('_path')
    if not path or not os.path.isfile(path) or os.path.islink(path):
        return []
    line = config_fallback(path, os.path.join(config.state_dir(ctx.cfg), settings.BACKUPS), now=ctx.now)
    return [line] if line else []


def check_dashboard(ctx):
    """The dashboard answering on the machine that serves (tutti/watchdog.py starts it again)."""
    from . import failover, move, watchdog
    if not ctx.cfg.get('_path') or not failover.may_write(ctx.cfg) or failover.me(ctx.cfg) != failover.main_of(ctx.cfg):
        return []
    port = ctx.cfg['dashboard']['port']
    return watchdog.round_services(True, lambda: move.local_health(port) is not None, lambda: failover.start_dashboard(ctx.cfg))


UPDATE_EVERY = 6 * 3600   # GitHub is asked for a newer release at most this often


def check_update(ctx, update=None, release=None):
    """A release install on the newest release: a newer one is installed now (the dashboard reloads by itself)."""
    from . import install, remote
    stamp = os.path.join(config.state_dir(ctx.cfg), 'selfheal-update.stamp')
    if not (ctx.cfg.get('self_update') or {}).get('enabled', True):
        return []
    if not (release if release is not None else install.is_release_install(remote.ROOT, install.home())):
        return []
    if os.path.exists(stamp) and ctx.now - os.path.getmtime(stamp) < UPDATE_EVERY:
        return []
    with open(stamp, 'w', encoding='utf8'):
        pass
    code, msg = (update or install.self_update)(install.home())
    return [f'self-update: {msg}'] if code == 0 and '->' in msg else []


def check_strays(ctx):
    """Tutti's own helper processes that nothing owns any more (tutti/procs.py), stopped by exact pid. Never in tests."""
    from . import procs
    reap = getattr(ctx, 'reap', None) or (procs.reap if procs.daemons_allowed() else None)
    return reap(lambda line: None) if reap else []


CHECKS = (('ollama', check_ollama), ('strays', check_strays), ('needs', check_needs), ('workers', check_workers),
          ('tunnels', check_tunnels),
          ('adb', check_adb), ('disk', check_disk), ('state', check_state), ('config', check_config),
          ('dashboard', check_dashboard), ('update', check_update))


EVERY_SECONDS = 300   # how often the failover service's loop runs the checks (selfheal.every_seconds)
# the failover round already runs the state and dashboard checks itself (tutti/failovercmd.py): its loop runs the rest
LOOP_CHECKS = tuple(c for c in CHECKS if c[0] not in ('state', 'dashboard'))
REPAIR_WORDS = ('put back', 'started again')


def every(cfg):
    return float((cfg.get('selfheal') or {}).get('every_seconds', EVERY_SECONDS))


def note_round(cfg, lines, now=None):
    """The failover round's own repairs (a state file put back, the dashboard started again) join the self-heal log."""
    for x in lines:
        if any(w in x for w in REPAIR_WORDS):
            record(cfg, 'dashboard' if 'dashboard' in x else 'state', x, now)


def enabled(cfg):
    return (cfg.get('selfheal') or {}).get('enabled', True) is not False


def errors(cfg):
    sh = cfg.get('selfheal')
    if sh is None:
        return deps.errors(cfg)
    if not isinstance(sh, dict):
        return [f'selfheal is {sh!r}: a mapping, e.g. selfheal: {{enabled: true, every_seconds: 300}}']
    errs = []
    if not isinstance(sh.get('every_seconds', 300), (int, float)) or sh.get('every_seconds', 300) < 30:
        errs.append(f'selfheal.every_seconds is {sh.get("every_seconds")!r}: a number of seconds, 30 or more (default 300)')
    if not isinstance(sh.get('temp_dirs', []), list):
        errs.append('selfheal.temp_dirs is not a list: write the extra temp folders Tutti cleans, e.g. [~/.cache/my-tmp]')
    return errs + deps.errors(cfg)


def round_(ctx, checks=CHECKS):
    """Every check once; a check that raises is logged and the others go on. Returns the lines for the log."""
    lines = []
    for name, fn in checks:
        try:
            done = fn(ctx)
        except Exception as e:   # one broken check must never stop the others or the round
            lines.append(f'{name}: the check failed ({type(e).__name__}: {e}); it runs again next round')
            continue
        for what in done:
            record(ctx.cfg, name, what, ctx.now)
            lines.append(f'{name}: {what}')
    trim(ctx.cfg)
    return lines
