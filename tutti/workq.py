"""The compute work queue (docs/FLEET.md, *The work queue*): a machine whose model is busy or missing still contributes. At
each dashboard round, after the autopilot, every machine with a free slot at its share (machines.<m>.use_pct, its quiet hours,
someone at its keyboard) takes the next due job of `work_queue.jobs`, by project priority:
- continuous gates and tests on each project's latest commit (heavy ones on the machine with the most cores);
- translations QA, coverage reports, e2e shards: any command a project names.

A failed job is run once more on the same commit (flaky-test hunting): it passes the second time, so a "flaky" backlog
task; it fails again, so a "fix" task. A job's `findings` (a regex with a `file` group, e.g. the coverage gate's FAIL rows)
become backlog tasks, one per file.

Pure: due, order, assign, verdict, tasks_of. The I/O: tick (git, the worktree, the detached run), collect."""
import json
import os
import re
import shlex
import subprocess
import sys
import time

from . import backlog, config, planner, priority, remote, worktree
from . import state as state_mod

STATE = 'work-queue.json'
TAIL = 4000
UNIT = {'h': 3600, 'm': 60, 'd': 86400}


def settings(cfg):
    return cfg.get('work_queue') or {}


def jobs(cfg):
    return [j for j in settings(cfg).get('jobs') or [] if isinstance(j, dict) and j.get('name') and j.get('run')]


def errors(cfg):
    """Every problem of work_queue.jobs, each naming its key and the fix."""
    out, seen = [], set()
    for i, j in enumerate(settings(cfg).get('jobs') or []):
        key = f'work_queue.jobs[{i}]'
        if not isinstance(j, dict) or not j.get('name') or not j.get('run'):
            out.append(f'{key} needs name: and run: (the command, run in the repo\'s worktree)')
            continue
        if j['name'] in seen:
            out.append(f'{key}: the name {j["name"]!r} is used twice: give each job its own')
        seen.add(j['name'])
        where = j.get('where', 'route')
        if where not in ('route', 'here') and where not in cfg['machines']:
            out.append(f'{key}.where is {where!r}: use route, here (this computer, no slot) or a name under machines:')
        if not re.fullmatch(r'commit|\d+[hmd]', str(j.get('every', 'commit'))):
            out.append(f'{key}.every is {j.get("every")!r}: use commit (each new commit) or a period such as 6h, 30m, 1d')
        f = j.get('findings') or {}
        if f and '(?P<file>' not in str(f.get('pattern', '')):
            out.append(f'{key}.findings.pattern needs a (?P<file>...) group: the file each finding names')
    return out


# ---- pure


def period(every):
    every = str(every or 'commit')
    return None if every == 'commit' else int(every[:-1]) * UNIT[every[-1]]


def due(job, last, commit, now):
    """Whether a job should run now: a rerun waits (flaky hunting) first; `every: commit` on a commit it has not run on; a
    period once it has passed since its last start."""
    if not last:
        return True
    if last.get('rerun'):
        return True
    p = period(job.get('every'))
    if p is None:
        return bool(commit) and last.get('commit') != commit
    return now - last.get('at', 0) >= p


def order(cfg, items, st):
    """Due jobs by their project's priority (weighted fair share), then the one that waited longest."""
    items = sorted(items, key=lambda j: (st.get('last', {}).get(j['name']) or {}).get('at', 0))
    return priority.fair_order(cfg, items, lambda j: j.get('project'))


def quiet(cfg, machine, now_dt):
    hours = (cfg['machines'].get(machine) or {}).get('quiet_hours') or (cfg.get('autopilot') or {}).get('quiet_hours')
    return bool(hours) and planner.quiet(now_dt, hours)


def assign(cfg, items, rows, busy, now_dt):
    """[(job, machine or 'here')]: each due job to a machine with a free slot (at its share), not in its quiet hours and not
    already running a queue job; a heavy job to the machine with the most cores first, the rest by routing order."""
    free = {r['name']: r for r in rows if r.get('status') == 'free' and (r.get('free') or 0) > 0 and r['name'] not in busy
            and not quiet(cfg, r['name'], now_dt)}
    route = cfg['routing']['order']
    out = []
    for j in items:
        where = j.get('where', 'route')
        if where == 'here':
            out.append((j, 'here'))
            continue
        names = [where] if where != 'route' else sorted(free, key=lambda n: (-(free[n].get('cores') or 0), route.index(n)
                                                                             if n in route else 99) if j.get('heavy') else
                                                        (route.index(n) if n in route else 99))
        pick = next((n for n in names if n in free), None)
        if pick:
            out.append((j, pick))
            del free[pick]
    return out


def verdict(rc, last):
    """('pass'|'flaky'|'fail'|'retry', rerun?) of a finished run: a first failure is run again on the same commit."""
    if rc == 0:
        return ('flaky' if last.get('rerun') else 'pass'), False
    return ('fail', False) if last.get('rerun') else ('retry', True)


def tasks_of(job, outcome, commit, tail):
    """[(title, scope, size)] the run leaves in the backlog: its findings, a flaky or a failing job."""
    out = []
    f = job.get('findings') or {}
    if f.get('pattern'):
        rx = re.compile(f['pattern'], re.M)
        for m in rx.finditer(tail):
            title = (f.get('title') or 'Raise the coverage of {file}').format(**m.groupdict())
            out.append((title, [m.group('file')], f.get('size') or 'S'))
    last = next((ln.strip() for ln in reversed(tail.splitlines()) if ln.strip()), '')[:160]
    if outcome == 'flaky':
        out.append((f'Flaky: the work-queue job {job["name"]} failed, then passed on {commit[:7]}: find the unstable test', [], 'S'))
    elif outcome == 'fail':
        out.append((f'The work-queue job {job["name"]} fails on {commit[:7]}: {last}', [], 'S'))
    return out


# ---- I/O


def path(cfg):
    return os.path.join(config.state_dir(cfg), STATE)


def read(cfg):
    try:
        with open(path(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def write(cfg, data):
    tmp = f'{path(cfg)}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path(cfg))


def repo_of(cfg, job):
    """(the repository folder, the autopilot repo name its tasks go to)."""
    repos = (cfg.get('autopilot') or {}).get('repos') or {}
    if job.get('repo') in repos:
        return config.expand(repos[job['repo']]['path']), job['repo']
    folder = config.expand(job.get('repo') or '.')
    name = next((n for n, r in repos.items() if os.path.realpath(config.expand(r['path'])) == os.path.realpath(folder)), None)
    return folder, job.get('backlog') or name


def head(folder, ref='HEAD', run=subprocess.run):
    r = run(['git', '-C', folder, 'rev-parse', ref], capture_output=True, text=True, timeout=30)
    return r.stdout.strip() if r.returncode == 0 else ''


def tree(cfg, job, folder, commit, run=subprocess.run):
    """A detached worktree of the repo at `commit` for this job (made once, moved to each new commit), its dependency folders
    shared from the main checkout (job.share: ship.share's patterns)."""
    wt = os.path.join(config.expand(cfg['project']['worktrees']), 'workq-' + re.sub(r'[^\w.-]', '-', job['name']))
    if not os.path.isdir(wt):
        run(['git', '-C', folder, 'worktree', 'add', '--force', '--detach', wt, commit], capture_output=True, timeout=300)
    run(['git', '-C', wt, 'checkout', '-q', '--force', '--detach', commit], capture_output=True, timeout=300)
    if job.get('share'):
        worktree.share(wt, job['share'], say=lambda m: None)
    return wt


def argv_of(cfg, job, where, wt, rc_path):
    """The detached run: `tutti test --on <machine>` (its slot queue, its runner) or, `here`, the command in the worktree."""
    if where == 'here':
        inner = f'cd {shlex.quote(wt)} && {job["run"]}'
    else:
        tutti = ' '.join(shlex.quote(x) for x in [sys.executable, remote.BIN, '--config', cfg['_path'], 'test', '--on', where,
                                                    '--dir', wt, 'workq-' + re.sub(r'[^\w.-]', '-', job['name'])])
        inner = f'{tutti} {shlex.quote(job["run"])}'
    return ['bash', '-c', f'{inner}; echo $? > {shlex.quote(rc_path)}']


def tail_of(p):
    try:
        with open(p, 'rb') as f:
            f.seek(max(0, os.path.getsize(p) - TAIL * 4))
            return f.read().decode('utf8', 'replace')[-TAIL * 4:]
    except OSError:
        return ''


def collect(cfg, st, now, is_alive):
    """Finished runs: their verdict, a rerun when a first failure needs one, the backlog tasks they leave. Returns events."""
    events, adds = [], []
    for name, lease in list((st.get('leases') or {}).items()):
        if is_alive(lease['pid']):
            continue
        try:
            with open(lease['rc'], encoding='utf8') as f:
                rc = int(f.read().strip() or 1)
        except (OSError, ValueError):
            rc = 1
        job = next((j for j in jobs(cfg) if j['name'] == name), {'name': name})
        last = (st.setdefault('last', {}).get(name)) or {}
        outcome, rerun = verdict(rc, last)
        text = tail_of(lease['log'])
        st['last'][name] = {'commit': lease['commit'], 'at': lease['started'], 'ended': now, 'rc': rc, 'outcome': outcome,
                            'machine': lease['machine'], 'rerun': rerun, 'log': lease['log']}
        events.append(f'{name} on {lease["machine"]}: {outcome} ({lease["commit"][:7]})')
        if lease.get('backlog'):
            adds += [(lease['backlog'], t, s, z, job.get('run')) for t, s, z in tasks_of(job, outcome, lease['commit'], text)]
        del st['leases'][name]
    if adds:
        with backlog.locked(cfg) as data:
            for repo, title, scope, size, _check in adds:
                _, new = backlog.add(data, backlog.new_task(repo, title, scope, '', size, 'work-queue'))
                if new:
                    events.append(f'backlog: {title[:100]}')
    return events


def tick(cfg, rows, now=None, spawn=subprocess.Popen, run=subprocess.run, is_alive=None):
    """One round: collect the finished runs, then start the due jobs on the free machines. Returns the events."""
    from datetime import datetime, timezone
    if settings(cfg).get('enabled') is False or not jobs(cfg):
        return []
    now = now or time.time()
    is_alive = is_alive or state_mod.alive
    st = read(cfg)
    st.setdefault('leases', {})
    events = collect(cfg, st, now, is_alive)
    busy = {lease['machine'] for lease in st['leases'].values()}
    waiting = []
    for j in jobs(cfg):
        if j['name'] in st['leases']:
            continue
        folder, repo = repo_of(cfg, j)
        commit = head(folder, j.get('ref', 'HEAD'), run) if os.path.isdir(folder) else ''
        if commit and due(j, (st.get('last') or {}).get(j['name']), commit, now):
            waiting.append({**j, '_folder': folder, '_repo': repo, '_commit': commit})
    for j, where in assign(cfg, order(cfg, waiting, st), rows, busy, datetime.fromtimestamp(now, timezone.utc)):
        folder = os.path.join(config.state_dir(cfg), 'work-queue')
        os.makedirs(folder, exist_ok=True)
        base = os.path.join(folder, re.sub(r'[^\w.-]', '-', j['name']))
        wt = tree(cfg, j, j['_folder'], j['_commit'], run)
        rc_path = base + '.rc'
        if os.path.exists(rc_path):
            os.remove(rc_path)
        with open(base + '.log', 'w', encoding='utf8') as log:
            p = spawn(argv_of(cfg, j, where, wt, rc_path), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                      start_new_session=True)
        st['leases'][j['name']] = {'pid': p.pid, 'machine': where, 'commit': j['_commit'], 'started': now, 'log': base + '.log',
                                   'rc': rc_path, 'backlog': j['_repo']}
        events.append(f'{j["name"]} started on {where} ({j["_commit"][:7]})')
    st['events'] = ([{'at': now, 'text': e} for e in events] + (st.get('events') or []))[:50]
    write(cfg, st)
    return events


def view(cfg):
    st = read(cfg)
    return {'jobs': [{'name': j['name'], 'project': j.get('project'), 'where': j.get('where', 'route'),
                      'running': (st.get('leases') or {}).get(j['name']), 'last': (st.get('last') or {}).get(j['name'])}
                     for j in jobs(cfg)], 'events': (st.get('events') or [])[:10]}


def describe(v):
    if not v['jobs']:
        return 'work queue: no jobs (work_queue.jobs in the config; docs/FLEET.md, The work queue)'
    lines = []
    for j in v['jobs']:
        r, last = j['running'], j['last'] or {}
        now = f'running on {r["machine"]} ({r["commit"][:7]})' if r else \
            (f'last {last.get("outcome")} on {last.get("machine")} ({(last.get("commit") or "")[:7]})' if last else 'not run yet')
        lines.append(f'{j["name"]:24} {j.get("project") or "-":24} {now}')
    return '\n'.join(lines + [f'  {e["text"]}' for e in v['events'][:5]])
