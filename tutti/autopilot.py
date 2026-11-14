"""`tutti autopilot`: when an account is behind its pace, Tutti hands bounded backlog tasks to it by itself instead of only
advising. Each tick: collect the finished runs (a pass becomes a branch, and a draft pull request when the repo asks for one;
a failure goes back to the queue and the next attempt continues from the same worktree with the failure in its brief, one
step up the escalation ladder), then dispatch the next tasks while a ladder step has room, a machine has a free slot that is
not hot and fewer than max_parallel runs are going. Nothing is ever merged. A task that fails the same way
`no_progress` times in a row on the strongest step, or reaches `max_attempts`, is parked as "needs a person".
The plan is pure (plan, signature, brief, should_park); the I/O is in tick, dispatch and collect."""
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import backlog, compact, config, delegate, planner, remote, routing, telemetry, worktree

try:
    import fcntl
except ImportError:   # Windows: no flock
    fcntl = None

STATE = 'autopilot.json'
TRACK_PREFIX = 'ap-'
TAIL_CHARS = 3000         # the failing output a retry's brief carries
HISTORY_LINES = 8         # earlier failures listed in a brief
EMPTY = 'backlog empty — add tasks'


def settings(cfg):
    return cfg['autopilot']


def state_path(cfg):
    return os.path.join(config.state_dir(cfg), STATE)


def read_state(cfg):
    try:
        with open(state_path(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def write_state(cfg, data):
    tmp = f'{state_path(cfg)}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, state_path(cfg))


@contextlib.contextmanager
def state_locked(cfg):
    """The state file read and written back under one lock: the dashboard's toggle and a round never overwrite each other."""
    with open(state_path(cfg) + '.lock', 'w', encoding='utf8') as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        data = read_state(cfg)
        yield data
        write_state(cfg, data)


def enabled(cfg):
    """The dashboard's toggle (or `tutti autopilot on|off`) when it was used, else autopilot.enabled."""
    toggle = read_state(cfg).get('enabled')
    return bool(settings(cfg)['enabled'] if toggle is None else toggle)


def set_enabled(cfg, on):
    with state_locked(cfg) as data:
        data['enabled'] = bool(on)
        data['toggled_at'] = time.time()


def ladder(cfg):
    """The escalation ladder: autopilot.ladder, else autopilot.accounts. Attempt n starts at step n (the last step repeats)."""
    s = settings(cfg)
    return list(s.get('ladder') or s['accounts'])


def costs_money(cfg, name):
    a = cfg['accounts'].get(name) or {}
    w = cfg['workers'].get(a.get('worker') or name) or {}
    return bool(a.get('costs_money') or w.get('costs_money'))


def step_of(cfg, name):
    """A ladder step as {name, worker, kind, args, account}, or None when it is not usable at all (unknown, off, costs money)."""
    if costs_money(cfg, name):
        return None
    if name in cfg['accounts']:
        a = cfg['accounts'][name]
        w = cfg['workers'].get(a.get('worker'))
        if not w:
            return None
        return {'name': name, 'worker': a['worker'], 'kind': w['kind'], 'args': list(a.get('args') or []), 'account': name}
    if name in cfg['workers'] and not any((a or {}).get('worker') == name for a in cfg['accounts'].values()):
        return {'name': name, 'worker': name, 'kind': cfg['workers'][name]['kind'], 'args': [], 'account': None}
    return None


def has_files(task):
    """A local model edits only the files it is given: the task's scope must name files (with an extension), not folders."""
    return bool(task['scope']) and all('.' in os.path.basename(s.rstrip('/')) and not s.endswith('/') for s in task['scope'])


def units(task):
    return backlog.SIZES.get(task.get('size'), 1)


def local_room(cfg, name, step, used, machines):
    """A worker without an account (a local model): one task at a time, none while its machine is paused or offline."""
    home = cfg['workers'][step['worker']].get('machine')
    m = next((x for x in machines if x.get('name') == home), None)
    if m and m.get('status') in ('paused', 'offline'):
        return 0, f'{name}: its machine {home} is {m["status"]} ({m.get("reason")})'   # a yielding PC is its owner's
    return (0, f'{name}: busy with a task') if used else (1, '')


def step_room(cfg, name, states, running_units, machines=()):
    """(units of room, why not) for one ladder step now. A trigger account (autopilot.accounts) has room only while it is
    behind its line (or late), as much as its readings say; any other account only while it is not ahead of its pace, one task
    at a time; a worker without an account (a local model) one task at a time. Panic and spent are never used."""
    step = step_of(cfg, name)
    if not step:
        return 0, f'{name}: not usable (unknown, turned off, or it costs money)'
    used = running_units.get(name, 0)
    if not step['account']:
        return local_room(cfg, name, step, used, machines)
    s = states.get(name)
    if not s:
        return 0, f'{name}: no pacing state yet'
    unfit = routing.account_unfit(s)
    if unfit:
        return 0, f'{name}: {unfit}'
    p = s.get('quota_plan')
    if (name in settings(cfg)['accounts'] or name in cfg.get('_spare_accounts', ())) and p:
        # the planner's room (tutti/planner.py): a window's share of its weekly limit, or the room left after the reserve and
        # the background spend; spent before it expires
        room = p['tasks'] - used
        return (room, '') if room > 0 else (0, f'{name}: {p["why"]}')
    if name in settings(cfg)['accounts']:
        if s.get('verdict') not in ('behind', 'late'):
            return 0, f'{name}: at its even-pace line ({s.get("verdict")})'
        room = (s.get('room_tasks') or 0) - used
        return (room, '') if room > 0 else (0, f'{name}: its room is taken by the running tasks')
    if s.get('verdict') == 'ahead':
        return 0, f'{name}: ahead of its pace'
    return (1 - used, '') if not used else (0, f'{name}: busy with a task')


def machine_capacity(cfg, machines):
    """(free slots on fit machines, why none) — None when no machine is configured (the checks run here)."""
    if not cfg['machines']:
        return None, ''
    if not machines:
        return 0, 'machines not probed yet'
    free = sum(m.get('free') or 0 for m in machines if m.get('status') == 'free')
    if free:
        return free, ''
    return 0, 'no machine has a free slot that is not hot: ' + '; '.join(f'{m["name"]}: {m.get("reason")}' for m in machines)


def candidates(cfg, t):
    """The steps a task may go to now, weakest first. A fresh task only to a trigger account (autopilot.accounts): the
    autopilot spends what would be lost, it does not start new work elsewhere. A retry from one step above its last failed
    attempt (the same step after a reviewer's retry of a pass); the last step repeats. A local model only with file scope."""
    steps = ladder(cfg)
    if (settings(cfg)['repos'].get(t['repo']) or {}).get('spare'):
        # another member's project, adopted for spare room (accounts.<n>.share_spare): only the accounts whose room would
        # otherwise be lost at their reset (docs/HOUSEHOLD.md)
        return list(cfg.get('_spare_accounts', ()))
    if not t['attempts']:
        trig = settings(cfg)['accounts']
        return [n for n in steps if n in trig] + [n for n in trig if n not in steps]
    last = t['attempts'][-1]
    at = steps.index(last['step']) if last['step'] in steps else -1
    start = max(min(at + (0 if last['ok'] else 1), len(steps) - 1), 0)
    # the steps above first; then the ones below, strongest first, so a retry never waits while only a weaker step has room
    # (agy's accounts sat idle behind a paced cursor and a yielding PC, 2026-11-14)
    out = steps[start:] + steps[:start][::-1]
    return [n for n in out if (step_of(cfg, n) or {}).get('kind') != 'ollama' or has_files(t)]


def assign(cfg, queued, rooms, free, out):
    """Each queued task, oldest first, to the first of its candidate steps that has room for its size."""
    for t in queued:
        if free <= 0:
            break
        steps = candidates(cfg, t)
        pick = next((n for n in steps if rooms.get(n, (0, ''))[0] >= units(t)), None)
        if pick is None:
            out['waiting'][t['id']] = '; '.join(rooms.get(n, (0, f'{n}: not on the ladder'))[1] or f'{n}: too little room for a '
                                                f'{t["size"]} task' for n in steps) or 'no step it may go to'
            continue
        rooms[pick] = (rooms[pick][0] - units(t), rooms[pick][1] or f'{pick}: its room is taken by the running tasks')
        out['dispatch'].append((t['id'], pick))
        free -= 1


def spent_units(tasks, states):
    """{step: units} the readings do not show yet: the runs going, and every attempt started on an account after its newest
    reading (a finished run's cost stays counted until a new reading includes it)."""
    used = {}
    for t in tasks:
        if t['status'] == 'running':
            used[t.get('step')] = used.get(t.get('step'), 0) + units(t)
        for a in t['attempts']:
            read = (states.get(a['step']) or {}).get('reading_at')
            if a.get('started') and read and a['started'] > _ts(read):
                used[a['step']] = used.get(a['step'], 0) + units(t)
    return used


def _ts(iso):
    from . import pacing
    return pacing.parse_time(iso).timestamp()


def room_reason(name, room, queued):
    """Every step named in the stopped line: why it takes nothing, or, with room, why no queued task fits it."""
    units_free, why = room
    if why or units_free <= 0:
        return why or f'{name}: no room'
    big = max((units(t) for t in queued), default=0)
    return f'{name}: room for {units_free}, but ' + (f'the queued tasks need {big}' if big > units_free else
                                                      'no queued task may go there (a fresh task goes only to autopilot.accounts)')


def waiting_of(tasks, unchecked, hold):
    """Why each task that cannot go now waits: no acceptance check, or held by the household (tutti/sharing.py)."""
    out = {t['id']: f'no acceptance check: give it one (tutti backlog rm {t["id"]}, then add it with --check) or set '
                    f'autopilot.repos.{t["repo"]}.check' for t in unchecked}
    queued = {t['id'] for t in tasks if t['status'] == 'queued'}
    out.update({i: why for i, why in hold.items() if i in queued})
    return out


def capacity_now(cfg, running, machines, now):
    """(how many may start now, None) or (0, {state, reason}) when nothing may: quiet hours, max_parallel, no machine slot."""
    hours = cfg['autopilot'].get('quiet_hours')
    if now is not None and planner.quiet(now, hours):
        return 0, {'state': 'waiting', 'reason': f'quiet hours ({", ".join(hours)} UTC, autopilot.quiet_hours): nothing new starts'}
    free = settings(cfg)['max_parallel'] - len(running)
    if free <= 0:
        return 0, {'state': 'running', 'reason': f'{len(running)} run(s) going: max_parallel is {settings(cfg)["max_parallel"]}'}
    slots, why = machine_capacity(cfg, machines)
    if slots is not None:
        free = min(free, slots)
        if free <= 0:
            return 0, {'state': 'waiting', 'reason': why}
    return free, None


def plan(cfg, tasks, states, machines, on, now=None, hold=None):
    """What to do now, pure. {state, reason, dispatch: [(task id, step name)], waiting: {task id: why}}. `now` (a datetime):
    nothing new starts in `autopilot.quiet_hours`; finished runs are still collected. `hold`: {task id: why} of shared
    tasks this member may not start now (tutti/sharing.py: leased by another member, or the coordinator unreachable)."""
    hold = hold or {}
    queued = [t for t in tasks if t['status'] == 'queued' and t['repo'] in settings(cfg)['repos'] and t['id'] not in hold]
    unchecked = [t for t in queued if not (t.get('check') or settings(cfg)['repos'][t['repo']].get('check'))]
    queued = [t for t in queued if t not in unchecked]
    running = [t for t in tasks if t['status'] == 'running']
    out = {'state': 'off', 'reason': 'turned off: the dashboard\'s toggle or `tutti autopilot on` starts it', 'dispatch': [],
           'waiting': waiting_of(tasks, unchecked, hold)}
    if not on:
        return out
    if not queued:
        busy = f'{len(running)} run(s) going; ' if running else ''
        return {**out, 'state': 'idle' if not running else 'running', 'reason': busy + EMPTY}
    free, stop = capacity_now(cfg, running, machines, now)
    if stop:
        return {**out, **stop}
    used = spent_units(tasks, states)
    names = list(dict.fromkeys([*ladder(cfg), *settings(cfg)['accounts'], *cfg.get('_spare_accounts', ())]))
    rooms = {n: step_room(cfg, n, states, used, machines or ()) for n in names}
    assign(cfg, queued, rooms, free, out)
    if out['dispatch']:
        return {**out, 'state': 'dispatching', 'reason': f'{len(out["dispatch"])} task(s) sent to ' +
                ', '.join(sorted({n for _, n in out['dispatch']}))}
    reasons = '; '.join(room_reason(n, r, queued) for n, r in rooms.items()) or 'no ladder step (autopilot.ladder or autopilot.accounts)'
    return {**out, 'state': 'stopped', 'reason': f'no worker takes a task now: {reasons}'}


# ---- failures: a signature, the brief of the next attempt, and when to give up


def normalise_tail(text):
    """The lines that say how it failed, without what changes between runs (numbers, times, temp names)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][-8:]
    return '\n'.join(re.sub(r'0x[0-9a-f]+|\d+', '#', ln) for ln in lines)


def signature(report, tails):
    """(a short hash, the words) of how an attempt failed: the same failure gives the same signature every time."""
    if report is None:
        words = 'the run ended without a report'
    elif report.get('worker_rc'):
        words = f'the worker exited {report["worker_rc"]}'
    elif report.get('denied'):
        words = 'the worker\'s CLI refused a command: ' + re.sub(r'\d+', '#', report['denied'][0][:120])
    elif not report.get('changed'):
        words = 'the worker changed nothing'
    else:
        failing = [c for c in report.get('checks') or [] if c['rc'] != 0]
        words = '; '.join(f'check `{c["run"][:80]}` failed: {normalise_tail(tails.get(c["log"], ""))}' for c in failing) or 'unknown'
    return hashlib.sha1(words.encode()).hexdigest()[:12], words


def park_reason(cfg, task):
    """('', '') to retry, else (the cause, `max_attempts` or `no_progress`, and why the task needs a person): the same failure
    `no_progress` times in a row on the strongest step, or max_attempts reached."""
    s = settings(cfg)
    tries = task['attempts']
    if len(tries) >= s['max_attempts']:
        return 'max_attempts', f'{len(tries)} attempts without a pass (autopilot.max_attempts is {s["max_attempts"]})'
    n = s['no_progress']
    last = tries[-n:]
    steps = ladder(cfg)
    if steps and len(last) == n and all(not a['ok'] and a['step'] == steps[-1] for a in last) \
            and len({a['signature'] for a in last}) == 1:
        return 'no_progress', (f'no progress: the strongest step ({steps[-1]}) failed the same way {n} times in a row: '
                               f'{last[-1]["why"][:300]}')
    return '', ''


def should_park(cfg, task):
    """'' to retry, else why the task needs a person (park_reason's words)."""
    return park_reason(cfg, task)[1]


def acceptance(task, repo):
    """The command a result must pass: the task's own check AND the repository's `check` (its lint and type gates), so a
    task's new tests passing never hides a gate it broke. Either alone when the other is missing; '' when neither is set."""
    own, gates = (task.get('check') or '').strip(), (repo.get('check') or '').strip()
    if own and gates and own != gates:
        return f'( {own} ) && ( {gates} )'
    return own or gates


def first_line(why, chars):
    """The first line of a failure's reason: the rest repeats the failing output, which the brief gives once, compacted."""
    return (why or '').strip().split('\n', 1)[0][:chars]


def brief(task, repo):
    """The worker's task: what to change, where, the acceptance check, and for a retry what went wrong so far."""
    lines = [task['title'], '']
    if task['scope']:
        lines.append('Change only these paths (and tests or docs that belong to them): ' + ', '.join(task['scope']))
    check = acceptance(task, repo)
    if check:
        lines.append(f'Acceptance: this command must pass from the repository root: {check}')
    lines.append('Keep the change small and bounded; if the task is not safe to do as described, change nothing and say why.')
    tries = task['attempts']
    if tries:
        last = tries[-1]
        lines += ['', f'This is attempt {len(tries) + 1}. The worktree already holds the earlier attempts\' changes: continue from '
                      'them, do not start over.', f'The last attempt ({last["step"]}) failed: {first_line(last["why"], 400)}']
        if last.get('stat'):
            lines += ['Its diff:', compact.compact(last['stat'], 1200)]
        if last.get('tail'):
            # compacted: colour codes, Tutti's echo and repeated lines out, error lines kept (tutti/compact.py)
            lines += ['The end of the failing output:', compact.compact(last['tail'], TAIL_CHARS)]
        if len(tries) > 1:
            lines += ['Earlier failures:'] + [f'- attempt {i + 1} ({a["step"]}): {first_line(a["why"], 160)}'
                                              for i, a in enumerate(tries[:-1])][-HISTORY_LINES:]
    if task.get('notes'):
        lines += ['', 'The reviewer\'s notes (follow them):'] + [f'- {n}' for n in task['notes']]
    return '\n'.join(lines)


# ---- I/O: dispatch, collect, tick


def repo_path(cfg, task):
    return config.expand(settings(cfg)['repos'][task['repo']]['path'])


def dispatch(cfg, task, step_name, now, spawn=subprocess.Popen):
    """Starts `tutti run <worker> <brief> --repo <repo> --track ap-<task>` in the background; marks the task running."""
    step = step_of(cfg, step_name)
    repo = settings(cfg)['repos'][task['repo']]
    run_id = delegate.new_id(cfg)
    p = delegate.paths(cfg, run_id)
    with open(p['log'], 'a', encoding='utf8') as log:   # the id is taken from here on: the next dispatch gets another
        log.write(f'autopilot: task {task["id"]} attempt {len(task["attempts"]) + 1} on {step_name}\n')
    check = acceptance(task, repo)
    argv = [sys.executable, remote.BIN, '--config', cfg['_path'], 'run', step['worker'], brief(task, repo)]
    if step['kind'] == 'ollama':
        argv += [s for s in task['scope'] if not os.path.isdir(os.path.join(repo_path(cfg, task), s))]
    argv += ['--repo', repo_path(cfg, task), '--track', TRACK_PREFIX + task['id'], '--id', run_id,
             '--remote-track', cfg['delegate']['remote_track']]
    if check:
        argv += ['--check', check, '--check-where', repo.get('where', 'local')]
    argv += [f'--extra={x}' for x in step['args']]
    argv += ['--account', step['account']] if step['account'] else []
    with open(p['log'], 'a', encoding='utf8') as log:
        # TUTTI_BACKGROUND: its runs on a household's shared machines are background demand (docs/HOUSEHOLD.md)
        child = spawn(argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
                      env={**os.environ, 'TUTTI_BACKGROUND': '1'})
    with open(p['pid'], 'w', encoding='utf8') as f:
        f.write(f'{child.pid}\n')
    # the repo's gates this attempt runs (acceptance holds them): a result lands only when they are today's (tutti/fill.py)
    task.update(status='running', run=run_id, step=step_name, worker=step['worker'], started=now, gates=(repo.get('check') or '').strip())
    return run_id


def listed(folder, wanted):
    """The entry of `folder` whose name is `wanted`, as a path built from the folder's own listing (never from a name read
    in a file), or None."""
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    return next((os.path.join(folder, n) for n in names if n == wanted), None)


def run_log(cfg, run_id):
    """The log of a run named in the backlog, found in the runs folder, or None."""
    return listed(delegate.runs_dir(cfg), f'{run_id}.log') if isinstance(run_id, str) else None


def check_tails(cfg, run_id, checks):
    """{a check's log as the report names it: the end of that log}, each log found in the run's own folder by its name."""
    folder = listed(delegate.runs_dir(cfg), run_id) if isinstance(run_id, str) else None
    out = {}
    for c in checks:
        name = os.path.basename(c['log']) if isinstance(c.get('log'), str) else ''
        out[c.get('log')] = tail_of(listed(folder, name)) if folder and name else ''
    return out


def tail_of(path, chars=TAIL_CHARS):
    if not path:
        return ''
    try:
        with open(path, encoding='utf8', errors='replace') as f:
            return f.read()[-chars:]
    except OSError:
        return ''


def attempt_of(cfg, task, report, now):
    tails = check_tails(cfg, task['run'], (report or {}).get('checks') or [])
    sig, why = signature(report, tails)
    failing = [tails[c['log']] for c in (report or {}).get('checks') or [] if c['rc'] != 0]
    if report is None or report.get('worker_rc') or not failing:
        failing = [tail_of(run_log(cfg, task['run']))]
    ok = bool(report) and report['rc'] == 0
    if ok:
        sig, why = '', 'passed'
    return {'run': task['run'], 'step': task.get('step'), 'worker': task.get('worker'), 'at': now, 'ok': ok, 'started': task.get('started'),
            'signature': sig, 'why': why, 'stat': (report or {}).get('stat', ''), 'diff': (report or {}).get('diff'),
            'tail': '' if report and report['rc'] == 0 else failing[-1][-TAIL_CHARS:], 'gates': task.get('gates') if ok else None}


def git(cwd, *args):
    return subprocess.run([worktree.git_bin(), '-C', cwd, *args], capture_output=True, text=True)


def slug(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:40] or 'task'


def publish(cfg, task, report, run=git, gh=subprocess.run):
    """A passing attempt becomes the branch autopilot/<id>-<slug> in its worktree, committed by Tutti (never by the worker),
    and a draft pull request when autopilot.repos.<repo>.pr is `draft`. Never merged. Returns notes for the dashboard."""
    repo = settings(cfg)['repos'][task['repo']]
    wt = report['worktree']
    branch = f'autopilot/{task["id"]}-{slug(task["title"])}'
    task.update(branch=branch, worktree=wt, diff=report['diff'])
    for step in (('checkout', '-q', '-B', branch), ('add', '-A')):
        done = run(wt, *step)
        if done.returncode != 0:
            task['publish_note'] = (f'git {step[0]} failed in {wt} ({(done.stderr or done.stdout).strip()[:200]}): nothing was '
                                    f'committed; review the diff {report["diff"]}')
            return
    subject = repo.get('commit', 'chore(autopilot): {title}').replace('{title}', task['title'])
    subject = subject if len(subject) <= 100 else subject[:99].rsplit(' ', 1)[0] + '…'   # a subject line of at most 100
    msg = f'{subject}\n\nBacklog task {task["id"]}, attempt {len(task["attempts"])}, run {report["id"]} by {task.get("step")}.\n'
    for line in repo.get('trailers') or []:
        msg += f'\n{line}'
    done = run(wt, 'commit', '-q', '-m', msg)
    if done.returncode != 0:
        task['publish_note'] = f'the commit was refused ({(done.stderr or done.stdout).strip()[:200]}): review the diff {report["diff"]}'
        return
    open_pr(cfg, task, repo, wt, branch, subject, run, gh)


def open_pr(cfg, task, repo, wt, branch, subject, run=git, gh=subprocess.run):
    """The committed branch pushed and a pull request opened when the repo or self-improvement asks for one."""
    from . import selfimprove
    mode, base, why = selfimprove.pr_plan(cfg, task, repo)
    if not mode:
        task['publish_note'] = f'committed on {branch} in {wt}{why}: review it, then merge or `tutti backlog retry {task["id"]} --note ...`'
        return
    pushed = run(wt, 'push', '-q', '-u', 'origin', branch)
    if pushed.returncode != 0:
        task['publish_note'] = f'committed on {branch}; the push failed ({pushed.stderr.strip()[:200]})'
        return
    body = f'Backlog task {task["id"]} ({task["source"]}), done by {task.get("step")} in {len(task["attempts"])} attempt(s).\n\n' \
           f'Check: `{task.get("check") or repo.get("check")}` passed. Opened by `tutti autopilot`' + \
           ('; self-improvement merges it only once CI, Sonar and the AI reviews are green.' if task.get('pr_mode')
            else '; never merged by it.')
    args = ['gh', 'pr', 'create', *(['--draft'] if mode == 'draft' else []), *(['--base', base] if base else []), '--head', branch,
            '--title', subject, '--body', body]
    pr = gh(args, cwd=wt, capture_output=True, text=True)
    if pr.returncode == 0:
        task['pr'] = pr.stdout.strip()
        task['publish_note'] = ('draft ' if mode == 'draft' else '') + f'pull request {task["pr"]}'
    elif task.get('pr'):   # a retry pushes onto the open pull request
        task['publish_note'] = f'pushed onto the open pull request {task["pr"]}'
    else:
        task['pr'] = None
        task['publish_note'] = f'committed on {branch}; gh pr create failed: {pr.stderr.strip()[:200]}'


def finished(cfg, task, is_alive):
    """(ended?, report or None) of a running task's run."""
    if not delegate.valid_id(task.get('run')):
        return True, None   # a hand-edited backlog: the attempt counts as ended without a report
    report = delegate.load_report(cfg, task['run'])
    if report:
        return True, report
    try:
        with open(delegate.paths(cfg, task['run'])['pid'], encoding='utf8') as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        pid = None
    return (not pid or not is_alive(pid)), None


def collect(cfg, data, now, is_alive, publish_fn=publish):
    """Every running task whose run ended: an attempt recorded; a pass published for review, a failure queued again (from the
    same worktree, one step up) or parked. Returns event lines."""
    events = []
    for t in [t for t in data['tasks'] if t['status'] == 'running']:
        ended, report = finished(cfg, t, is_alive)
        if not ended:
            continue
        a = attempt_of(cfg, t, report, now)
        t['attempts'].append(a)
        record_attempt(cfg, t, a, now)
        if a['ok']:
            t['status'] = 'review'
            publish_fn(cfg, t, report)
            events.append(f'{t["id"]} passed on {a["step"]}: {t.get("publish_note", "")}')
            continue
        park = after_failure(cfg, t)
        events.append(f'{t["id"]} failed on {a["step"]} ({a["why"][:120]})' + (f': parked: {park}' if park else ': queued again'))
    return events


def after_failure(cfg, t):
    """A failed task is parked or queued again (one step up the ladder: an escalation). Returns why it was parked, or ''."""
    cause, park = park_reason(cfg, t)
    t['status'], t['parked'] = ('needs-person', park) if park else ('queued', None)
    n = len(t['attempts'])
    if park:
        telemetry.record('feature', 'autopilot.park', cause=cause, attempts=n, source=t.get('source'))
    elif step_index(cfg, n) > step_index(cfg, n - 1):
        telemetry.record('feature', 'autopilot.escalate', attempt=n + 1, source=t.get('source'))
    return park


def step_index(cfg, attempt):
    """The ladder step attempt n (0-based) starts at: step n, the last one repeating."""
    return min(attempt, max(len(ladder(cfg)) - 1, 0))


def record_attempt(cfg, t, a, now):
    """One autopilot attempt for telemetry: the step's worker kind, the outcome, how long; never the task or its failure."""
    step = step_of(cfg, a['step']) if a.get('step') in ladder(cfg) else {}
    telemetry.record('feature', 'autopilot.attempt', outcome='passed' if a['ok'] else 'failed', attempt=len(t['attempts']),
                     worker_kind=telemetry.tool_of(a.get('worker')), account=telemetry.tool_of(step.get('account')),
                     step_kind=step.get('kind'), source=t.get('source'), size=t.get('size'),
                     minutes=round((now - float(a.get('started') or now)) / 60, 2))


def tick(cfg, states, machines, now=None, is_alive=None, spawn=subprocess.Popen, publish_fn=publish, beat=None):
    """One round: collect, plan, dispatch. `states`: {account: pacing.account_state}; `machines`: state.machines_state items;
    `beat`: this member's heartbeat for the household coordinator (shared projects only; docs/HOUSEHOLD.md)."""
    from . import state as state_mod
    now = now or time.time()
    is_alive = is_alive or state_mod.alive
    on = enabled(cfg)
    from . import fill  # fill imports this module
    filled = None
    found = fill.gather(cfg, backlog.load(cfg), states, machines, now) if on else None   # the repos' commands: never under the lock
    from . import sharing  # sharing imports backlog and coordinator; a household of one makes no call
    cfg['_spare_accounts'] = sharing.spare_accounts(cfg, states, now)
    if beat is not None:
        beat = {**beat, 'spare': bool(cfg['_spare_accounts'])}
    shared = sharing.sync(cfg, backlog.load(cfg), beat, now=now)   # the coordinator's call: never under the lock either
    events_spare = sharing.adopt_offers(cfg, shared, now)
    with backlog.locked(cfg) as data:
        events = collect(cfg, data, now, is_alive, publish_fn)
        hold, mirrored = sharing.under_lock(shared, data, now)
        hold.update(sharing.spare_hold(cfg, data))
        events += mirrored + events_spare
        started = []
        if on:
            filled = fill.fill(cfg, data, states, machines, now, found)
            events += [f'fill_room: {filled["note"]}'] if filled['added'] else []
            events += fill.review_round(cfg, data, now, is_alive, spawn)
        p = plan(cfg, data['tasks'], states, machines, on, datetime.fromtimestamp(now, timezone.utc), hold)
        telemetry.record('feature', 'autopilot.plan', state=p['state'], dispatch=len(p['dispatch']), waiting=len(p['waiting']),
                         collected=len(events))
        backlog.save(cfg, data)   # what was collected is kept even if a start below fails
        for task_id, step in p['dispatch']:
            t = backlog.find(data, task_id)
            try:
                run_id = dispatch(cfg, t, step, now, spawn)
            except OSError as e:
                events.append(f'{task_id} could not be started on {step} ({e}): it stays queued')
                continue
            backlog.save(cfg, data)
            events.append(f'{task_id} sent to {step} (run {run_id}, attempt {len(t["attempts"]) + 1})')
            if t['repo'] in shared['keys']:
                started.append(task_id)
    if started:   # shared tasks just started: their leases at full length now, not at the next round
        sharing.renew(cfg, backlog.load(cfg), started)
    with state_locked(cfg) as st:
        st['last'] = {'at': now, 'state': p['state'], 'reason': p['reason'], 'waiting': p['waiting']}
        st['fill'] = filled or st.get('fill')
        st['events'] = ([{'at': now, 'text': e} for e in events] + st.get('events', []))[:50]
        st['household'] = {'error': shared['error'], 'shared': sorted(shared['keys']), 'held': len(hold)} if shared['keys'] else None
    return p, events


# ---- what the dashboard and `tutti autopilot` show


def credits_of(cfg, run_id):
    try:
        with open(os.path.join(config.state_dir(cfg), delegate.RUN_LOG), encoding='utf8') as f:
            for line in f:
                if f'"{run_id}"' in line:
                    return json.loads(line).get('credits')
    except (OSError, ValueError):
        pass
    return None


def last_line(path):
    lines = [ln for ln in tail_of(path, 2000).splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ''


def task_view(cfg, t, now):
    v = {k: t.get(k) for k in ('id', 'repo', 'title', 'status', 'size', 'source', 'step', 'run', 'branch', 'pr', 'publish_note',
                               'parked', 'diff', 'worktree', 'landing')}
    v['attempt_count'] = len(t['attempts'])
    v['history'] = [{'step': a['step'], 'ok': a['ok'], 'why': a['why'][:200], 'signature': a['signature'], 'run': a['run']}
                    for a in t['attempts']]
    if t['status'] == 'running':
        v['elapsed'] = now - (t.get('started') or now)
        v['last_line'] = last_line(run_log(cfg, t['run']))
        v['credits'] = credits_of(cfg, t['run'])
    return v


def view(cfg, now=None):
    """The Autopilot panel: on or off, the last decision, the queue, the runs going, the results to review, what is parked."""
    now = now or time.time()
    data = backlog.load(cfg)
    st = read_state(cfg)
    tasks = [task_view(cfg, t, now) for t in data['tasks'] if t['status'] not in ('done', 'dropped')]
    s = settings(cfg)
    from . import fill
    return {'enabled': enabled(cfg), 'fill_room': fill.fill_on(cfg), 'fill': st.get('fill'), 'auto_review': fill.review_on(cfg),
            'configured': bool(s['repos']), 'accounts': s['accounts'], 'ladder': ladder(cfg),
            'max_parallel': s['max_parallel'], 'last': st.get('last'), 'events': st.get('events', [])[:10],
            'queue': [t for t in tasks if t['status'] == 'queued'], 'running': [t for t in tasks if t['status'] == 'running'],
            'review': [t for t in tasks if t['status'] == 'review'], 'parked': [t for t in tasks if t['status'] == 'needs-person']}


def fill_line(v):
    fl = v.get('fill') or {}
    if not (fl and v.get('fill_room')):
        return f'fill_room {"on" if v.get("fill_room") else "off"}'
    return f'fill_room on: room {fl["room"]}, {fl["queued"]} fresh queued; {fl["note"]}'.rstrip('; ')


def task_lines(title, t):
    extra = t.get('publish_note') or t.get('parked') or (f'run {t["run"]} on {t["step"]}' if t['status'] == 'running' else '')
    return [f'{title:15} {t["id"]:5} {t["attempt_count"]} attempt(s)  {t["title"][:70]}'] + ([f'{"":21}{extra}'] if extra else [])


def describe(v):
    last = v.get('last') or {}
    lines = [f'autopilot: {"on" if v["enabled"] else "off"}; {last.get("state", "not run yet")}: {last.get("reason", "")}'.rstrip(': '),
             fill_line(v),
             f'ladder: {" -> ".join(v["ladder"]) or "(none)"}; max_parallel {v["max_parallel"]}']
    for title, items in (('running', v['running']), ('to review', v['review']), ('needs a person', v['parked']), ('queued', v['queue'])):
        for t in items:
            lines += task_lines(title, t)
    return '\n'.join(lines)
