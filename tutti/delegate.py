"""`tutti run <worker> "<task>" --repo <repo>`: a bounded code task handed to a cheap worker end to end. A worktree of the repo
(created, or reused by name), the worker run headless in it, the diff captured, the targeted checks run on the diff, and one
report the orchestrator can review: what changed, the diff's path, every check's verdict and what was not checked.
The worker never commits; the orchestrator applies or discards the diff."""
import contextlib
import json
import os
import re
import subprocess
import sys
import time

from . import config, ship, workers, worktree

try:
    import fcntl
except ImportError:   # Windows: no flock; runs there share no remote copy lock
    fcntl = None

RUNS = 'runs'


@contextlib.contextmanager
def track_lock(cfg, track, say):
    """One delegated run at a time per remote copy: routed checks rsync the worktree into <machine root>/<track>, and two runs
    copying into the same folder would mix their trees. A run with its own --track has its own copy and its own lock."""
    if fcntl is None:
        yield
        return
    path = os.path.join(runs_dir(cfg), f'{track}.lock')
    with open(path, 'w', encoding='utf8') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            say(f'run: another run holds "{track}" (a shared worktree or remote copy); waiting for it (--track <name> picks another)')
            fcntl.flock(f, fcntl.LOCK_EX)
        yield


RUN_ID = re.compile(r'^\d+-\d+(?:-\d+)?$')
TRACK = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')


def valid_id(run_id):
    """A run id is `<time>-<pid>[-<n>]`: anything else (a path, `..`) never reaches a file name."""
    return bool(RUN_ID.match(str(run_id)))


def own_worktree(cfg, path):
    """`path` when it is a run worktree of ours (<project.worktrees>/<delegate.prefix>...), else None: a report that was edited
    to name another folder never gets that folder removed."""
    if not isinstance(path, str):
        return None
    root = os.path.realpath(config.expand(cfg['project']['worktrees']))
    real = os.path.realpath(path)
    if os.path.dirname(real) != root or not os.path.basename(real).startswith(cfg['delegate']['prefix']):
        return None
    return os.path.join(root, os.path.basename(real))


def runs_dir(cfg):
    d = os.path.join(config.state_dir(cfg), RUNS)
    os.makedirs(d, exist_ok=True)
    return d


def new_id(cfg=None, now=None, pid=None):
    """`<unix time>-<pid>`, with `-<n>` when this process already started a run in this second: the dashboard reads the time
    and the pid from a job log's name (state.LOG_NAME)."""
    base = f'{int(now or time.time())}-{pid or os.getpid()}'
    if cfg is None:
        return base
    n, run_id = 1, base
    while os.path.exists(paths(cfg, run_id)['log']):
        n, run_id = n + 1, f'{base}-{n + 1}'
    return run_id


def paths(cfg, run_id):
    base = os.path.join(runs_dir(cfg), run_id)
    # .cmd, .rc and .pid sit beside the log as for quiet runs and ship checks: the dashboard reads them the same way
    return {'log': base + '.log', 'cmd': base + '.log.cmd', 'rc': base + '.log.rc', 'pid': base + '.log.pid', 'dir': base,
            'diff': os.path.join(base, 'diff.patch'), 'report': os.path.join(base, 'report.txt'),
            'json': os.path.join(base, 'report.json'), 'worker_log': os.path.join(base, 'worker.log')}


def tree_for(cfg, repo, run_id, name=None, say=print):
    """The worktree the worker edits: <project.worktrees>/<delegate.prefix><name or id>, created detached at the repo's HEAD,
    or reused when it already is a worktree of the same repository. Returns (path, created)."""
    top = ship.toplevel(repo)
    root = config.expand(cfg['project']['worktrees'])
    wt = os.path.join(root, f'{cfg["delegate"]["prefix"]}{name or run_id}')
    if os.path.exists(os.path.join(wt, '.git')):
        if os.path.realpath(worktree.main_checkout(wt) or wt) != os.path.realpath(worktree.main_checkout(top) or top):
            raise ship.ShipError(f'refused: {wt} exists but belongs to another repository; pick another --track name')
        say(f'run: reusing the worktree {wt}')
        return wt, False
    if os.path.exists(wt):
        raise ship.ShipError(f'refused: {wt} exists and is not a git worktree; move it away or pick another --track name')
    os.makedirs(root, exist_ok=True)
    ship.git(top, 'worktree', 'add', '-q', '--detach', wt, 'HEAD')
    say(f'run: new worktree {wt} at {ship.git(top, "log", "--oneline", "-1")}')
    return wt, True


def capture_diff(wt, diff_path):
    """Everything the worker changed or added against HEAD, as one patch; the index is restored afterwards.
    Returns (changed paths, the --stat text)."""
    ship.git(wt, 'add', '-A')
    try:
        names = [n for n in ship.git(wt, 'diff', '--cached', '--name-only', 'HEAD').split('\n') if n]
        stat = ship.git(wt, 'diff', '--cached', '--stat', 'HEAD')
        patch = subprocess.run([worktree.git_bin(), '-C', wt, 'diff', '--cached', '--binary', 'HEAD'], capture_output=True).stdout
    finally:
        ship.git(wt, 'reset', '-q')
    os.makedirs(os.path.dirname(diff_path), exist_ok=True)
    with open(diff_path, 'wb') as f:
        f.write(patch)
    return names, stat


def denials(worker, lines, log_path):
    """Tool calls the worker's CLI refused (worker.denied, a regex), from its output and its own log ({log} in its command).
    A headless CLI that denies a command still exits 0, so the run must say it."""
    rx = worker.get('denied')
    if not rx:
        return []
    texts = list(lines)
    try:
        with open(log_path, encoding='utf8', errors='replace') as f:
            texts += f.read().splitlines()
    except OSError:
        pass
    pattern = re.compile(rx)
    return list(dict.fromkeys(t.strip()[:200] for t in texts if pattern.search(t)))   # a line in the output and the log counts once


def run_checks(cfg, wt, checks, env, track, say=print, folder=None):
    """Every check, never stopping at the first failure (the report needs them all). [{run, rc, where, verdict, log, coverage}]."""
    out = []
    for check in checks:
        check = {**check, 'quiet': True, 'verdict': check.get('verdict') or r'\[SUCCESS\]|\[FAILURE\]|passed|failed|error'}
        rc, where, log, verdict = ship.quiet_check(cfg, wt, check, env, track, folder)
        note = ship.check_coverage(cfg, check, wt)
        out.append({'run': check['run'], 'rc': rc, 'where': where, 'verdict': verdict, 'log': log, 'coverage': note})
        say(f'run: check [{where}] rc={rc} {verdict}'.rstrip())
    return out


def summary(r):
    """The reviewable report, plain text."""
    lines = [f'run {r["id"]}: {r["worker"]} exit {r["worker_rc"]}; {len(r["changed"])} file(s) changed',
             f'task      {r["task"][:200]}', f'worktree  {r["worktree"]}',
             f'diff      {r["diff"]}' + (' (empty)' if not r['changed'] else '')]
    if r['stat']:
        lines += ['', r['stat']]
    if r['denied']:
        lines += ['', f'DENIED: the worker\'s CLI refused {len(r["denied"])} tool call(s); the task may be incomplete. Allow the commands '
                      'it needs in the worker\'s headless settings (docs/WORKERS.md), then run again:']
        lines += [f'  {d}' for d in r['denied'][:5]]
    if r['checks'] is None:
        lines += ['', 'checks    skipped (--no-checks): nothing was tested']
    elif not r['checks']:
        lines += ['', 'checks    none configured (delegate.checks or ship.checks): nothing was tested']
    for c in r['checks'] or []:
        lines.append(f'check     [{c["where"]}] rc={c["rc"]} {c["verdict"] or ""} (log {c["log"]})'.rstrip())
        if c['coverage']:
            lines.append(f'          {c["coverage"]}')
    lines += ['', 'next      review: git -C ' + r['worktree'] + ' diff (or less ' + r['diff'] + ')',
              f'          apply:  git -C <your tree> apply --index {r["diff"]}',
              f'          discard: tutti runs discard {r["id"]}']
    return '\n'.join(lines)


def verdict(r):
    """0: the worker finished, changed something, nothing was denied and every check passed; 1 otherwise."""
    if r['worker_rc'] != 0 or not r['changed'] or r['denied']:
        return 1
    return 1 if any(c['rc'] != 0 for c in r['checks'] or []) else 0


def write(p, text, mode='w'):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, mode, encoding='utf8') as f:
        f.write(text)


def delegate(cfg, name, task, files, repo, run_id=None, track=None, extra=(), checks=True, discard=False, say=None):
    """The whole hand-off. Returns the report dict (also written to report.json and report.txt). Runs that name the same
    --track share one worktree, so they take turns: the second waits until the first has its report."""
    if track and not TRACK.match(track):
        raise ship.ShipError(f'refused: --track "{track}" must be one folder name (letters, digits, . _ -), nothing was changed')
    lock = track_lock(cfg, f'tree-{track}', say or (lambda m: sys.stderr.write(f'tutti: {m}\n'))) if track else contextlib.nullcontext()
    with lock:
        return _delegate(cfg, name, task, files, repo, run_id, track, extra, checks, discard, say)


def _delegate(cfg, name, task, files, repo, run_id, track, extra, checks, discard, say):
    if run_id and not valid_id(run_id):
        raise ship.ShipError(f'refused: run id "{run_id}" is not <time>-<pid>: let tutti choose it (nothing was changed)')
    run_id = run_id or new_id(cfg)
    p = paths(cfg, run_id)
    os.makedirs(p['dir'], exist_ok=True)
    write(p['cmd'], f'run {name}: {task.splitlines()[0][:150]}\n')
    write(p['pid'], f'{os.getpid()}\n')
    log = open(p['log'], 'a', encoding='utf8')   # closed in finally: the log outlives the say() closure
    say = say or (lambda m: (sys.stderr.write(f'tutti: {m}\n'), log.write(m + '\n'), log.flush()))
    try:
        wt, created = tree_for(cfg, repo, run_id, track, say)
        worktree.share(wt, cfg['ship'].get('share', []), say)
        rc, lines = workers.run(cfg, name, task, files, cwd=wt, out=log, extra=extra, log=p['worker_log'])
        changed, stat = capture_diff(wt, p['diff'])
        found = denials(cfg['workers'][name], lines, p['worker_log'])
        r = {'id': run_id, 'worker': name, 'task': task, 'worker_rc': rc, 'worktree': wt, 'created': created, 'diff': p['diff'],
             'changed': changed, 'stat': stat, 'denied': found, 'checks': None, 'report': p['report']}
        if checks and changed:
            env = {'TUTTI_CHANGED': ' '.join(changed), 'TUTTI_TREE': wt, 'TUTTI_GIT_MOUNT': worktree.docker_mount(wt)}
            chosen = cfg['delegate']['checks'] if cfg['delegate']['checks'] is not None else cfg['ship']['checks']
            remote_track = track or cfg['delegate']['remote_track']
            with track_lock(cfg, remote_track, say):
                r['checks'] = run_checks(cfg, wt, chosen, env, remote_track, say, p['dir'])
        elif checks:
            r['checks'] = []
        r['rc'] = verdict(r)
        write(p['report'], summary(r) + '\n')
        write(p['json'], json.dumps(r, indent=2) + '\n')
        if discard and created:
            remove_tree(wt, say)
            r['worktree_removed'] = True
        return r
    finally:
        log.close()


RUN_LOG = 'runs.jsonl'


def log_run(cfg, worker, account, rc, run_id=None, lines=(), now=None):
    """One line per worker run in <state>/runs.jsonl: when, which worker and account it spent, the result, the AI credits it
    printed. The dashboard counts runs per worker today from it."""
    from . import readers
    entry = {'at': (now or time.time()), 'worker': worker, 'account': account, 'rc': rc, 'id': run_id,
             'credits': readers.credits_in(lines) or None}
    with open(os.path.join(config.state_dir(cfg), RUN_LOG), 'a', encoding='utf8') as f:
        f.write(json.dumps(entry) + '\n')
    return entry


def runs_today(cfg, now=None):
    """{worker: number of runs since 00:00 UTC today}."""
    now = now or time.time()
    day = now - now % 86400
    out = {}
    try:
        with open(os.path.join(config.state_dir(cfg), RUN_LOG), encoding='utf8') as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get('at', 0) >= day:
                    out[e.get('worker')] = out.get(e.get('worker'), 0) + 1
    except OSError:
        pass
    return out


def remove_tree(wt, say=print):
    main = worktree.main_checkout(wt)
    if not main:
        raise ship.ShipError(f'{wt} is not a linked worktree: nothing removed')
    ship.git(main, 'worktree', 'remove', '--force', wt)
    say(f'run: removed the worktree {wt}')


def load_report(cfg, run_id):
    if not valid_id(run_id):
        return None
    try:
        with open(paths(cfg, run_id)['json'], encoding='utf8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def listing(cfg):
    rows = []
    for name in sorted(os.listdir(runs_dir(cfg)), reverse=True):
        if not name.endswith('.log'):
            continue
        run_id = name[:-4]
        r = load_report(cfg, run_id)
        if r:
            rows.append(f'{run_id}  {r["worker"]:8} rc {r["rc"]}  {len(r["changed"])} file(s)  {r["task"].splitlines()[0][:70]}')
        else:
            rows.append(f'{run_id}  (running or ended without a report: tutti wait run {run_id})')
    return '\n'.join(rows) or 'no runs yet: tutti run <worker> "<task>" --repo <repo>'
