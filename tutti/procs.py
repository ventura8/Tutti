"""Tutti's long-lived helper processes and the reaper that stops the strays (docs/SELFHEAL.md, *Stray processes*).

Tutti starts three kinds of helpers that outlive a command: a device lease's keep-awake loop (tutti/prep.py), a worker's
ssh tunnel loop (scripts/tunnel.sh) and the sleep inhibitor (tutti/sleepwatch.py). Each carries the marker `TUTTI_PROC`
in its environment. One is a stray when nothing owns it any more:

- a test's helper (TUTTI_TEST_ROOT set) whose test root is gone, or any helper whose HOME is gone;
- an inhibitor whose owner (TUTTI_PROC_OWNER, else its parent) is not a live process;
- a tunnel loop whose pid file (TUTTI_STATE/tunnel-<name>.pid) names another pid;
- a keep-awake loop whose awake.pid names another pid.

The reaper stops each by its exact pid (SIGTERM), only processes of this user, and logs every one with its reason. On
2026-11-15 the laptop sat at load 60 with 161 keep-awake loops, 38 tunnel loops and 45 inhibitors left behind."""
import os
import re
import signal
import time

from . import secretenv

MARK = 'TUTTI_PROC'
NO_DAEMONS = 'TUTTI_NO_DAEMONS'   # tests and scripts/check.sh: no helper process is started at all
CLAIM = re.compile(r'devices/([^/"\s]+)/users/claim-')
TUNNEL = 'loop "$@"'
PROC = '/proc'
YOUNG_SECONDS = 30   # a helper this young may not have written its pid file yet: never a stray


def daemons_allowed(env=None):
    """False in tests and checks (TUTTI_NO_DAEMONS set): a helper that would outlive the command is not started."""
    return not (env if env is not None else os.environ).get(NO_DAEMONS)


def _read(path, binary=False):
    try:
        with open(path, 'rb' if binary else 'r', encoding=None if binary else 'utf8') as f:
            return f.read()
    except (OSError, ValueError):
        return None


def info(pid, proc=PROC):
    """{pid, ppid, uid, argv, env} of one process, or None when it is gone or unreadable."""
    base = os.path.join(proc, str(pid))
    status = _read(os.path.join(base, 'status'))
    cmd, env = _read(os.path.join(base, 'cmdline'), True), _read(os.path.join(base, 'environ'), True)
    if status is None or cmd is None or env is None:
        return None
    fields = {k: v for k, _, v in (ln.partition(':') for ln in status.splitlines() if ':' in ln)}
    try:
        uid, ppid = int(fields['Uid'].split()[0]), int(fields['PPid'].strip())
    except (KeyError, ValueError, IndexError):
        return None
    pairs = (e.split(b'=', 1) for e in env.split(b'\0') if b'=' in e)
    return {'pid': int(pid), 'ppid': ppid, 'uid': uid, 'argv': [a.decode('utf8', 'replace') for a in cmd.split(b'\0') if a],
            'env': secretenv.mask({k.decode('utf8', 'replace'): v.decode('utf8', 'replace') for k, v in pairs})}   # t116


def kind(p):
    """'claim', 'tunnel', 'inhibit' or None: by the marker, else by the argv of the helpers started before it existed."""
    mark = p['env'].get(MARK, '')
    if mark:
        return mark.split(':', 1)[0]
    text = ' '.join(p['argv'])
    if p['argv'][:1] == ['systemd-inhibit'] and '--who=tutti' in p['argv']:
        return 'inhibit'
    if p['argv'][:2] == ['bash', '-c'] and TUNNEL in text and 'TUTTI_STATE' in p['env']:
        return 'tunnel'
    if p['argv'][:2] == ['bash', '-c'] and CLAIM.search(text) and 'awake.pid' in text:
        return 'claim'
    return None


def _alive(pid, proc):
    return pid > 0 and os.path.exists(os.path.join(proc, str(pid)))


def _owned_by_pidfile(pid, files):
    return any((_read(f) or '').strip() == str(pid) for f in files)


def _age(pid, proc):
    try:
        return time.time() - os.stat(os.path.join(proc, str(pid))).st_mtime
    except OSError:
        return 0.0


def _gone(env):
    root = env.get('TUTTI_TEST_ROOT')
    if root and not os.path.isdir(root):
        return f'its test run is over ({root} is gone)'
    if env.get('HOME') and not os.path.isdir(env['HOME']):
        return f'its home folder {env["HOME"]} is gone'
    return ''


def _inhibit(p, proc):
    owner = int(p['env'].get('TUTTI_PROC_OWNER') or 0) or p['ppid']
    own = info(owner, proc)
    return '' if own and any('tutti' in a for a in own['argv']) else f'its Tutti process {owner} is gone'


def _name(p):
    mark = p['env'].get(MARK, '')
    return mark.split(':', 1)[1] if ':' in mark else ''


def _tunnel(p, proc):
    state, name = p['env'].get('TUTTI_STATE', ''), _name(p)
    if name:
        files = [os.path.join(state, f'tunnel-{name}.pid')]
    else:   # a loop started before the marker: any tunnel pid file of its state folder
        names = os.listdir(state) if os.path.isdir(state) else []
        files = [os.path.join(state, f) for f in names if f.startswith('tunnel-') and f.endswith('.pid')]
    return '' if _owned_by_pidfile(p['pid'], files) else 'no tunnel pid file names it (a second loop of the same tunnel)'


def _claim(p, proc):
    m = CLAIM.search(' '.join(p['argv']))
    device = _name(p) or (m.group(1) if m else '')
    f = os.path.join(p['env'].get('HOME', ''), '.tutti', 'devices', device, 'awake.pid')
    return '' if _owned_by_pidfile(p['pid'], [f]) else f'awake.pid of {device} names another loop (or none)'


RULES = {'inhibit': _inhibit, 'tunnel': _tunnel, 'claim': _claim}


def why_stray(p, proc=PROC):
    """The reason this Tutti helper is a stray, or '' when something still owns it (or it is too young to tell)."""
    gone = _gone(p['env'])
    if gone:
        return gone
    rule = RULES.get(kind(p))
    if rule is None or _age(p['pid'], proc) < YOUNG_SECONDS:
        return ''
    return rule(p, proc)


def strays(uid=None, proc=PROC):
    """[(info, kind, reason)] of this user's Tutti helpers that nothing owns, oldest pid first."""
    uid = os.getuid() if uid is None else uid
    out = []
    for name in sorted((n for n in os.listdir(proc) if n.isdigit()), key=int):
        p = info(name, proc)
        if p is None or p['uid'] != uid or p['pid'] == os.getpid():
            continue
        k = kind(p)
        reason = why_stray(p, proc) if k else ''
        if reason:
            out.append((p, k, reason))
    return out


def reap(log, uid=None, proc=PROC, kill=os.kill, dry=False):
    """Stops each stray by its exact pid and logs it. Returns the lines logged."""
    lines = []
    for p, k, reason in strays(uid, proc):
        verb = 'would stop' if dry else 'stopped'
        line = f'{verb} {k} {p["pid"]}: {reason}'
        if not dry:
            try:
                kill(p['pid'], signal.SIGTERM)
            except ProcessLookupError:
                continue
            except PermissionError:
                line = f'could not stop {k} {p["pid"]} (not allowed): {reason}'
        log(line)
        lines.append(line)
    return lines
