"""Running work that was not started through `tutti quiet` or `tutti ship`, found by itself for the dashboard's Jobs:

- registered jobs: `tutti job register` writes <state>/jobs/<pid>.json (a title, the log); `tutti job done` adds the result;
- scripts the config names (`dashboard.watch_scripts`, e.g. a project's test and ship scripts), by their process, with the
  log their standard output goes to (/proc/<pid>/fd/1) for the tail and the progress;
- agent CLIs (`dashboard.watch_agents`: cursor-agent, agy, copilot) by their process: the program and its folder only, never
  its arguments (a prompt may hold anything);
- an agent session's background task outputs (`dashboard.task_globs`): active or idle from the file's time and the progress
  line of its end; the text itself is never shown (it may hold anything).

A process is matched by the name of its program (argv[0]) or, for an interpreter, of its script (argv[1]); never by
searching command lines, so a watcher never finds itself. Pure over a /proc root and a clock, for tests."""
import glob
import json
import os
import time

from . import state

INTERPRETERS = ('bash', 'sh', 'node', 'python3', 'python')
TASK_RECENT = 6 * 3600     # task outputs older than this are not listed
TASK_ACTIVE = 120          # a task output written in the last 2 minutes is active


def processes(proc='/proc'):
    """[(pid, argv)] of every process whose command line can be read."""
    out = []
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc, name, 'cmdline'), 'rb') as f:
                argv = [a.decode('utf8', 'replace') for a in f.read().split(b'\0') if a]
        except OSError:
            continue
        if argv:
            out.append((int(name), argv))
    return out


def program(argv):
    """The program a command line runs: argv[0]'s name, or for an interpreter the first argument that is not an option."""
    first = os.path.basename(argv[0])
    if first not in INTERPRETERS:
        return first, 1
    for i, a in enumerate(argv[1:], 1):
        if not a.startswith('-'):
            return os.path.basename(a), i + 1
    return first, len(argv)


def link(proc, pid, what):
    try:
        return os.readlink(os.path.join(proc, str(pid), what))
    except OSError:
        return None


def started_at(proc, pid, clock=time.time):
    """When the process started, from its /proc folder's time (close enough for a dashboard)."""
    try:
        return os.stat(os.path.join(proc, str(pid))).st_mtime
    except OSError:
        return clock()


def parent(proc, pid):
    """The parent pid from /proc/<pid>/stat (the field after the command name, which may hold spaces), or 0."""
    try:
        with open(os.path.join(proc, str(pid), 'stat'), encoding='utf8', errors='replace') as f:
            return int(f.read().rsplit(')', 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def outermost(proc, pids):
    """The matched pids that have no matched ancestor: a script that runs itself again (directly or through a wrapper such as
    `timeout`) is one job, not two."""
    keep = []
    for pid in pids:
        p, depth = parent(proc, pid), 0
        while p > 1 and p not in pids and depth < 8:
            p, depth = parent(proc, p), depth + 1
        if p not in pids:
            keep.append(pid)
    return keep


def script_jobs(cfg, now, stall, proc='/proc', procs=None):
    """The configured scripts and agent CLIs that are running, one job per outermost process."""
    names = set(cfg['dashboard'].get('watch_scripts') or [])
    agents = set(cfg['dashboard'].get('watch_agents') or [])
    found = {}
    for pid, argv in procs if procs is not None else processes(proc):
        if os.path.basename(argv[0]) == 'timeout':
            continue
        name, rest = program(argv)
        if name in names:
            found[pid] = lambda pid=pid, name=name, args=argv[rest:]: script_job(proc, pid, name, args, now, stall)
        elif name in agents and os.path.basename(argv[0]) == name:
            found[pid] = lambda pid=pid, name=name: agent_job(proc, pid, name, now, cfg)
    return [found[pid]() for pid in outermost(proc, set(found))]


def script_job(proc, pid, name, args, now, stall):
    log = link(proc, pid, 'fd/1')
    log = log if log and os.path.isfile(log) else None
    updated = state.mtime(log) if log else None
    lines = state.tail(log) if log else []
    return {'id': f'proc:{pid}', 'kind': 'script', 'title': ' '.join([name, *args])[:200], 'pid': pid, 'log': log,
            'started': state.iso(started_at(proc, pid)), 'updated': state.iso(updated),
            'idle_seconds': round(now - updated) if updated else None, 'rc': None,
            'status': state.status_of(True, updated, None, now, stall), 'tail': lines[-12:],
            'progress': state.progress(log, lines) if log else None}


def started_by_tutti(cfg, cwd):
    """True when an agent works in a folder Tutti made: a run worktree or a fallback review's empty folder."""
    if not cwd:
        return False
    root = os.path.realpath(os.path.expanduser(cfg['project']['worktrees']))
    base = os.path.basename(cwd)
    return (os.path.dirname(cwd) == root and base.startswith(cfg['delegate']['prefix'])) or base.startswith('tutti-review-')


def agent_job(proc, pid, name, now, cfg=None):
    cwd = link(proc, pid, 'cwd')
    outside = cfg is not None and not started_by_tutti(cfg, cwd)
    title = f'{name} in {cwd or "?"}' + (' (started outside Tutti: use tutti run auto)' if outside else '')
    return {'id': f'agent:{pid}', 'kind': 'agent', 'title': title, 'pid': pid, 'log': None, 'outside': outside,
            'started': state.iso(started_at(proc, pid)), 'updated': None, 'idle_seconds': None, 'rc': None,
            'status': 'running', 'tail': [], 'progress': None, 'age_seconds': round(now - started_at(proc, pid))}


def registered_jobs(folder, now, stall, is_alive):
    """Jobs written by `tutti job register`: running while their pid lives; then done or failed by `tutti job done`, or ended."""
    out = []
    for path in glob.glob(os.path.join(folder, '*.json')):
        try:
            with open(path, encoding='utf8') as f:
                info = json.load(f)
        except (OSError, ValueError):
            continue
        pid, log = info.get('pid'), info.get('log')
        running = info.get('rc') is None and bool(pid) and is_alive(pid)
        if not running and now - (state.mtime(path) or 0) > TASK_RECENT:
            continue
        updated = state.mtime(log) if log else None
        lines = state.tail(log) if log else []
        status = state.status_of(running, updated, info.get('rc'), now, stall)
        out.append({'id': f'job:{pid}', 'kind': 'job', 'title': info.get('title') or f'job {pid}', 'pid': pid, 'log': log,
                    'started': state.iso(info.get('started')), 'updated': state.iso(updated),
                    'idle_seconds': round(now - updated) if updated else None, 'rc': info.get('rc'), 'status': status,
                    'tail': lines[-12:], 'progress': state.progress(log, lines) if log else None})
    return out


def task_jobs(cfg, now):
    """An agent session's background task outputs: active (written in the last two minutes) or idle; no text is shown."""
    out = []
    for pattern in cfg['dashboard'].get('task_globs') or []:
        for path in glob.glob(os.path.expanduser(pattern)):
            updated = state.mtime(path)
            if not updated or now - updated > TASK_RECENT:
                continue
            active = now - updated < TASK_ACTIVE
            out.append({'id': f'task:{os.path.basename(path)}', 'kind': 'agent task', 'title': f'background task {os.path.basename(path)}',
                        'pid': None, 'log': None, 'started': None, 'updated': state.iso(updated), 'idle_seconds': round(now - updated),
                        'rc': None, 'status': 'running' if active else 'ended', 'tail': [],
                        'progress': state.progress(path, state.tail(path, 5)) if active else None})
    return out


def register(folder, pid, title, log=None, proc='/proc', clock=time.time):
    """`tutti job register`: the record; the log defaults to where the process's standard output goes, when that is a file."""
    if not log:
        out = link(proc, pid, 'fd/1')
        log = out if out and os.path.isfile(out) else None
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f'{int(pid)}.json')
    with open(path, 'w', encoding='utf8') as f:
        json.dump({'pid': int(pid), 'title': title, 'log': os.path.abspath(log) if log else None, 'started': clock(), 'rc': None}, f)
    return path


def finish(folder, pid, rc):
    """`tutti job done`: the result beside the record. False when no record exists."""
    path = os.path.join(folder, f'{int(pid)}.json')
    try:
        with open(path, encoding='utf8') as f:
            info = json.load(f)
    except (OSError, ValueError):
        return False
    info['rc'] = int(rc)
    with open(path, 'w', encoding='utf8') as f:
        json.dump(info, f)
    return True
