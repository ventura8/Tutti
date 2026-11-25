"""Tutti's state kept light (docs/FAILOVER.md, *Light and self-healing*): its size and biggest items measured, what is not
needed cut, logs and runs rotated and capped, and a budget `tutti doctor` shows.

The budget (`failover.budget_mb`, default 50 MB) is what the backup main machine copies; `failover.delta_kb` (default 512 KB)
is what one sync round should send. Pure: measure() and plan() take a folder and a clock; prune() deletes what plan() named."""
import os
import re
import shutil
import time

# never needed again once written: screenshots of old checks, stale busy markers of dead processes, old ship and land logs
JUNK_SUFFIXES = ('.png',)
JUNK_PREFIXES = ('land-', 'ship-', 'layout-')   # land and ship logs, the layout check's screenshots
LOG_SUFFIXES = ('.log', '.jsonl')
DEFAULTS = {'budget_mb': 50, 'delta_kb': 512, 'runs_keep': 200, 'runs_days': 14, 'log_mb': 2, 'junk_days': 3}


def opts(cfg):
    return {**DEFAULTS, **{k: v for k, v in ((cfg or {}).get('failover') or {}).items() if k in DEFAULTS}}


def size_of(path):
    if os.path.isfile(path) or os.path.islink(path):
        try:
            return os.lstat(path).st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def measure(folder, top=8):
    """{'total': bytes, 'items': [(name, bytes)] biggest first} of a state folder."""
    items = []
    try:
        names = os.listdir(folder)
    except OSError:
        return {'total': 0, 'items': []}
    for n in names:
        items.append((n, size_of(os.path.join(folder, n))))
    items.sort(key=lambda x: -x[1])
    return {'total': sum(s for _n, s in items), 'items': items[:top]}


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def plan(folder, o, now=None):
    """[(path, why)] to delete, and [(path, keep_bytes)] logs to cut to their cap. Nothing touches backlog, users, auth or config."""
    now = now or time.time()
    drop, cut = [], []
    try:
        names = os.listdir(folder)
    except OSError:
        return drop, cut
    old = now - o['junk_days'] * 86400
    for n in names:
        p = os.path.join(folder, n)
        if os.path.isfile(p) and n.startswith(JUNK_PREFIXES) and n.endswith(('.log', *JUNK_SUFFIXES)) and _mtime(p) < old:
            drop.append((p, 'an old screenshot or ship log'))
        elif os.path.isfile(p) and n.endswith(LOG_SUFFIXES) and size_of(p) > o['log_mb'] * 1024 * 1024:
            cut.append((p, o['log_mb'] * 1024 * 1024))
    drop += _old_runs(os.path.join(folder, 'runs'), o, now)
    return drop, cut


RUN_ID = re.compile(r'(?:tutti-run-)?(\d{6,}-\d+(?:-\d+)?)')


def _run_of(name):
    """The run a file of runs/ belongs to: its folder and its .log, .log.rc, .log.pid ... and lock share one id."""
    m = RUN_ID.match(name)
    return m.group(1) if m else name


def _alive(runs, rid):
    """True while the run's process still runs: its files are never pruned under it."""
    try:
        with open(os.path.join(runs, rid + '.log.pid'), encoding='utf8') as f:
            pid = int(f.read().strip() or 0)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _old_runs(runs, o, now):
    """Runs beyond the newest `runs_keep`, or older than `runs_days`, go with all their files (their index entry in the
    backlog stays). Counted per run, not per file: a run has about eight files, so counting files kept only the newest
    few dozen runs and a report vanished within hours. A run whose process is alive is never pruned."""
    try:
        names = os.listdir(runs)
    except OSError:
        return []
    groups = {}
    for n in names:
        groups.setdefault(_run_of(n), []).append(os.path.join(runs, n))
    newest = sorted(groups, key=lambda r: max(_mtime(p) for p in groups[r]), reverse=True)
    cutoff = now - o['runs_days'] * 86400
    drop = []
    for i, rid in enumerate(newest):
        last = max(_mtime(p) for p in groups[rid])
        if (i >= o['runs_keep'] or last < cutoff) and not _alive(runs, rid):
            drop += [(p, 'an old run') for p in groups[rid]]
    return drop


def cut_log(path, keep):
    """The newest `keep` bytes of a log, from a line start."""
    with open(path, 'rb') as f:
        f.seek(max(0, os.path.getsize(path) - keep))
        tail = f.read()
    nl = tail.find(b'\n')
    tail = tail[nl + 1:] if 0 <= nl < len(tail) - 1 else tail
    # in place, never a rename: a writer that holds the file open for append keeps writing into this same file
    with open(path, 'r+b') as f:
        f.write(tail)
        f.truncate()


def prune(folder, o, now=None):
    """Applies plan(); returns (freed bytes, lines)."""
    drop, cut = plan(folder, o, now)
    freed, lines = 0, []
    for p, why in drop:
        size = size_of(p)
        try:
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        except OSError:
            continue
        freed += size
        lines.append(f'removed {os.path.basename(p)} ({why}, {size // 1024} KB)')
    for p, keep in cut:
        before = size_of(p)
        cut_log(p, keep)
        freed += before - size_of(p)
        lines.append(f'cut {os.path.basename(p)} to its newest {keep // (1024 * 1024)} MB')
    return freed, lines


def mb(n):
    return f'{n / (1024 * 1024):.1f} MB'


def report(folder, o):
    """Lines for `tutti doctor` and `tutti state size`, and the problems (over budget)."""
    m = measure(folder)
    lines = [f'state: {mb(m["total"])} of a {o["budget_mb"]} MB budget in {folder}']
    lines += [f'  {n}: {mb(s)}' for n, s in m['items']]
    problems = []
    if m['total'] > o['budget_mb'] * 1024 * 1024:
        problems.append(f'Tutti\'s state is {mb(m["total"])}, over its {o["budget_mb"]} MB budget (biggest: '
                        f'{", ".join(n for n, _s in m["items"][:3])}): run `tutti state prune` (old runs, screenshots and logs go; '
                        f'the backlog, users and settings stay), or raise failover.budget_mb')
    return lines, problems
