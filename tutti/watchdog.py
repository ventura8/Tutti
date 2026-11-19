"""The watchdog (docs/FAILOVER.md, *Light and self-healing*): restarts a crashed dashboard on the machine that serves, and
repairs corrupt state from the last good snapshot.

Every round, each state JSON file that parses is copied to `snapshots/` (only when it changed); one that no longer parses is
put back from its snapshot, and the broken copy kept beside it as `<name>.corrupt` for a look. Pure where it can be: the file
checks take a folder; restarting takes injected probes."""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

SNAPS = 'snapshots'
WATCHED = ('.json',)
_START_LOCK = threading.RLock()


class WarningLine(str):
    """A dashboard warning to display, not a completed repair."""


def valid(path):
    try:
        with open(path, encoding='utf8') as f:
            json.load(f)
    except (OSError, ValueError):
        return False
    return True


def _same(a, b):
    try:
        return os.path.getsize(a) == os.path.getsize(b) and open(a, 'rb').read() == open(b, 'rb').read()
    except OSError:
        return False


def files(folder):
    try:
        # never failover*.json: an old term put back would undo the term protocol (tutti/failover.py fails closed instead)
        return sorted(n for n in os.listdir(folder) if n.endswith(WATCHED) and not n.startswith('failover')
                      and os.path.isfile(os.path.join(folder, n)))
    except OSError:
        return []


def round_files(folder):
    """Snapshots the good files and repairs the broken ones; returns lines for the log."""
    snaps = os.path.join(folder, SNAPS)
    os.makedirs(snaps, mode=0o700, exist_ok=True)
    out = []
    for n in files(folder):
        p, s = os.path.join(folder, n), os.path.join(snaps, n)
        if valid(p):
            if not _same(p, s):
                shutil.copy2(p, s + '.tmp')
                os.replace(s + '.tmp', s)
        elif os.path.exists(s) and valid(s):
            shutil.copy2(p, p + '.corrupt')
            shutil.copy2(s, p + '.tmp')
            os.replace(p + '.tmp', p)
            out.append(f'{n} was corrupt: put back from its last good snapshot (the broken copy is {n}.corrupt)')
        else:
            out.append(f'{n} is corrupt and has no good snapshot: look at it, or remove it so Tutti starts it again empty')
    return out


def round_services(serving, healthy, start):
    """Starts the dashboard when this machine serves and it does not answer. Returns lines."""
    if serving and not healthy():
        start()
        return ['the dashboard did not answer: started again']
    return []


def dashboard_health(cfg):
    """Check the address the dashboard actually binds, not necessarily loopback."""
    from . import move
    dashboard = cfg.get('dashboard') or {}
    return move.local_health(dashboard.get('port', 4777), host=dashboard.get('bind') or '127.0.0.1') is not None


def dashboard_unit(run=subprocess.run, which=shutil.which, platform=None):
    """Return managed, unmanaged, or unknown; never guess when the user service manager cannot answer."""
    from . import move
    executable = which('systemctl')
    if not executable:
        return 'unknown' if (platform or sys.platform).startswith('linux') else 'unmanaged'
    try:
        r = run([executable, '--user', 'show', move.UNIT, '--property=LoadState'],
                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return 'unknown'
    load = next((line.partition('=')[2] for line in r.stdout.splitlines() if line.startswith('LoadState=')), '')
    if load == 'not-found':
        return 'unmanaged'
    return 'managed' if not r.returncode and load == 'loaded' else 'unknown'


@contextmanager
def start_lock(cfg):
    """Serialize dashboard starts across watchdog processes without locking authentication."""
    from . import auth, config
    folder = os.path.join(config.state_dir(cfg), 'dashboard-watchdog')
    os.makedirs(folder, mode=0o700, exist_ok=True)
    with _START_LOCK:
        fd = os.open(os.path.join(folder, 'start.lock'), os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            auth._lock(fd)
            yield
        finally:
            os.close(fd)


def managed_unit_matches(cfg, run=subprocess.run, read=None):
    """Accept only an unchanged Tutti-generated unit pinned to this exact config, with no drop-ins."""
    from . import install, move
    try:
        r = run(['systemctl', '--user', 'show', move.UNIT, '--property=FragmentPath', '--property=DropInPaths',
                 '--property=NeedDaemonReload', '--property=ActiveState'], capture_output=True, text=True, timeout=5)
        if r.returncode:
            return False
        props = dict(line.split('=', 1) for line in r.stdout.splitlines() if '=' in line)
        expected = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'),
                                'systemd', 'user', move.UNIT)
        if (props.get('FragmentPath') != expected or props.get('DropInPaths') != ''
                or props.get('NeedDaemonReload') != 'no' or props.get('ActiveState') not in ('inactive', 'failed')):
            return False
        if read is None:
            fd = os.open(expected, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, encoding='utf8') as f:
                text = f.read()
        else:
            text = read(expected)
        match = re.search(r'^ExecStart=/bin/sh -c \'"(/[A-Za-z0-9_./-]+)" --config "', text, re.MULTILINE)
        config_path = cfg.get('_path', '')
        if not match or not re.fullmatch(r'/[A-Za-z0-9_./-]+', config_path):
            return False
        return text == install.systemd_unit(match.group(1), config_path)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def managed_start(matches, may_write, run):
    if not matches():
        return [WarningLine('dashboard did not answer; a user service exists but its configuration and health are unverified: '
                            'check systemctl --user status tutti-dashboard and dashboard.bind')]
    if not may_write():
        return [WarningLine('dashboard did not answer; failover lease expired: waiting for ownership before starting')]
    try:
        r = run(['systemctl', '--user', 'start', '--no-block', 'tutti-dashboard.service'],
                capture_output=True, text=True, timeout=5)
        if not r.returncode:
            return ['dashboard did not answer; requested its verified inactive user service to start']
    except (OSError, subprocess.SubprocessError):
        pass
    return [WarningLine('dashboard did not answer; its verified user service could not be started: '
                        'check systemctl --user status tutti-dashboard')]


def pid_warning(cfg, now):
    from . import failover
    try:
        age = (time.time() if now is None else now) - os.path.getmtime(failover.dashboard_pid_file(cfg))
    except OSError:
        age = float('inf')
    if 0 <= age < 60:
        return [WarningLine('dashboard did not answer; a recorded process is alive, allowing up to 60 seconds to start')]
    return [WarningLine('dashboard did not answer or its recorded start time is uncertain; a recorded PID is alive but its '
                        'identity and health are unverified: inspect the dashboard process and service before restarting')]


def ensure_dashboard(cfg, health=None, unit=None, run=subprocess.run, start=None, recorded=None, now=None, matches=None,
                     may_write=None):
    """Avoid competing with an unverified unit; serialize unmanaged starts and recheck health under the lock."""
    from . import failover
    health = health or (lambda: dashboard_health(cfg))
    unit = unit or (lambda: dashboard_unit(run))
    start = start or (lambda: failover.start_dashboard(cfg))
    recorded = recorded or (lambda: failover.recorded_pid(cfg))
    matches = matches or (lambda: managed_unit_matches(cfg, run))
    may_write = may_write or (lambda: failover.may_write(cfg))
    if health():
        return []
    with start_lock(cfg):
        if health():
            return []
        owner = unit()
        if owner == 'managed':
            return managed_start(matches, may_write, run)
        if owner != 'unmanaged':
            return [WarningLine('dashboard did not answer; user service state is unknown: check systemctl --user status tutti-dashboard')]
        if recorded():
            return pid_warning(cfg, now)
        if not may_write():
            return [WarningLine('dashboard did not answer; failover lease expired: waiting for ownership before starting')]
        start()
        return ['the dashboard did not answer: started again']
