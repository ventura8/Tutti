"""`tutti autopilot`: when an account is behind its pace, Tutti hands bounded backlog tasks to it by itself instead of only
advising. Each tick: collect the finished runs (a pass becomes a branch, and a draft pull request when the repo asks for one;
a failure goes back to the queue and the next attempt continues from the same worktree with the failure in its brief, one
step up the escalation ladder), then dispatch the next tasks while a ladder step has room, a machine has a free slot that is
not hot and fewer than max_parallel runs are going. Nothing is ever merged. A task that fails the same way
`no_progress` times in a row on the strongest step, or reaches `max_attempts`, is parked as "needs a person".
The plan is pure (plan, signature, brief, should_park); the I/O is in tick, dispatch and collect."""
import hashlib
import json
import os
import re
import subprocess
import sys
import time

from . import backlog, config, delegate, remote, routing, worktree

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
    tmp = state_path(cfg) + '.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, state_path(cfg))


def enabled(cfg):
    """The dashboard's toggle (or `tutti autopilot on|off`) when it was used, else autopilot.enabled."""
    toggle = read_state(cfg).get('enabled')
    return bool(settings(cfg)['enabled'] if toggle is None else toggle)


def set_enabled(cfg, on):
    data = read_state(cfg)
    data['enabled'] = bool(on)
    data['toggled_at'] = time.time()
    write_state(cfg, data)


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
    if name in cfg['workers']:
        return {'name': name, 'worker': name, 'kind': cfg['workers'][name]['kind'], 'args': [], 'account': None}
    return None


def units(task):
    return backlog.SIZES.get(task.get('size'), 1)


def step_room(cfg, name, states, running_units):
    """(units of room, why not) for one ladder step now. A trigger account (autopilot.accounts) has room only while it is
    behind its line (or late), as much as its readings say; any other account only while it is not ahead of its pace, one task
    at a time; a worker without an account (a local model) one task at a time. Panic and spent are never used."""
    step = step_of(cfg, name)
    if not step:
        return 0, f'{name}: not usable (unknown, turned off, or it costs money)'
    used = running_units.get(name, 0)
    if not step['account']:
        return (1 - used, f'{name}: busy with a task') if used else (1, '')
    s = states.get(name)
    if not s:
        return 0, f'{name}: no pacing state yet'
    unfit = routing.account_unfit(s)
    if unfit:
        return 0, f'{name}: {unfit}'
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


def assign(steps, queued, rooms, free, out):
    """Each queued task, oldest first, to the first step from its attempt's rung up that has room for its size."""
    for t in queued:
        if free <= 0:
            break
        start = min(len(t['attempts']), len(steps) - 1) if steps else 0
        pick = next((n for n in steps[start:] if rooms[n][0] >= units(t)), None)
        if pick is None:
            out['waiting'][t['id']] = '; '.join(rooms[n][1] or f'{n}: too little room for a {t["size"]} task' for n in steps[start:])
            continue
        rooms[pick] = (rooms[pick][0] - units(t), rooms[pick][1] or f'{pick}: its room is taken by the running tasks')
        out['dispatch'].append((t['id'], pick))
        free -= 1


def plan(cfg, tasks, states, machines, on):
    """What to do now, pure. {state, reason, dispatch: [(task id, step name)], waiting: {task id: why}}."""
    queued = [t for t in tasks if t['status'] == 'queued' and t['repo'] in settings(cfg)['repos']]
    running = [t for t in tasks if t['status'] == 'running']
    out = {'state': 'off', 'reason': 'turned off: the dashboard\'s toggle or `tutti autopilot on` starts it', 'dispatch': [], 'waiting': {}}
    if not on:
        return out
    if not queued:
        busy = f'{len(running)} run(s) going; ' if running else ''
        return {**out, 'state': 'idle' if not running else 'running', 'reason': busy + EMPTY}
    free = settings(cfg)['max_parallel'] - len(running)
    if free <= 0:
        return {**out, 'state': 'running', 'reason': f'{len(running)} run(s) going: max_parallel is {settings(cfg)["max_parallel"]}'}
    slots, why = machine_capacity(cfg, machines)
    if slots is not None:
        free = min(free, slots)
        if free <= 0:
            return {**out, 'state': 'waiting', 'reason': why}
    used = {}
    for t in running:
        used[t.get('step')] = used.get(t.get('step'), 0) + units(t)
    rooms = {n: step_room(cfg, n, states, used) for n in ladder(cfg)}
    assign(ladder(cfg), queued, rooms, free, out)
    if out['dispatch']:
        return {**out, 'state': 'dispatching', 'reason': f'{len(out["dispatch"])} task(s) sent to ' +
                ', '.join(sorted({n for _, n in out['dispatch']}))}
    reasons = '; '.join(r[1] for r in rooms.values() if r[1]) or 'no ladder step (autopilot.ladder or autopilot.accounts)'
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


def should_park(cfg, task):
    """'' to retry, else why the task needs a person: the same failure `no_progress` times in a row on the strongest step, or
    max_attempts reached."""
    s = settings(cfg)
    tries = task['attempts']
    if len(tries) >= s['max_attempts']:
        return f'{len(tries)} attempts without a pass (autopilot.max_attempts is {s["max_attempts"]})'
    n = s['no_progress']
    last = tries[-n:]
    steps = ladder(cfg)
    if steps and len(last) == n and all(not a['ok'] and a['step'] == steps[-1] for a in last) \
            and len({a['signature'] for a in last}) == 1:
        return f'no progress: the strongest step ({steps[-1]}) failed the same way {n} times in a row: {last[-1]["why"][:300]}'
    return ''


def brief(task, repo):
    """The worker's task: what to change, where, the acceptance check, and for a retry what went wrong so far."""
    lines = [task['title'], '']
    if task['scope']:
        lines.append('Change only these paths (and tests or docs that belong to them): ' + ', '.join(task['scope']))
    check = task.get('check') or repo.get('check')
    if check:
        lines.append(f'Acceptance: this command must pass from the repository root: {check}')
    lines.append('Keep the change small and bounded; if the task is not safe to do as described, change nothing and say why.')
    tries = task['attempts']
    if tries:
        last = tries[-1]
        lines += ['', f'This is attempt {len(tries) + 1}. The worktree already holds the earlier attempts\' changes: continue from '
                      'them, do not start over.', f'The last attempt ({last["step"]}) failed: {last["why"][:400]}']
        if last.get('stat'):
            lines += ['Its diff:', last['stat'][-1200:]]
        if last.get('tail'):
            lines += ['The end of the failing output:', last['tail'][-TAIL_CHARS:]]
        if len(tries) > 1:
            lines += ['Earlier failures:'] + [f'- attempt {i + 1} ({a["step"]}): {a["why"][:160]}'
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
    check = task.get('check') or repo.get('check') or ''
    argv = [sys.executable, remote.BIN, '--config', cfg['_path'], 'run', step['worker'], brief(task, repo)]
    if step['kind'] == 'ollama':
        argv += [s for s in task['scope'] if os.path.isfile(os.path.join(repo_path(cfg, task), s))]
    argv += ['--repo', repo_path(cfg, task), '--track', TRACK_PREFIX + task['id'], '--id', run_id,
             '--remote-track', cfg['delegate']['remote_track']]
    if check:
        argv += ['--check', check, '--check-where', repo.get('where', 'local')]
    argv += [f'--extra={x}' for x in step['args']]
    with open(p['log'], 'a', encoding='utf8') as log:
        child = spawn(argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    with open(p['pid'], 'w', encoding='utf8') as f:
        f.write(f'{child.pid}\n')
    task.update(status='running', run=run_id, step=step_name, worker=step['worker'], started=now)
    return run_id


def tail_of(path, chars=TAIL_CHARS):
    try:
        with open(path, encoding='utf8', errors='replace') as f:
            return f.read()[-chars:]
    except OSError:
        return ''


def attempt_of(cfg, task, report, now):
    tails = {c['log']: tail_of(c['log']) for c in (report or {}).get('checks') or []}
    sig, why = signature(report, tails)
    failing = [tails[c['log']] for c in (report or {}).get('checks') or [] if c['rc'] != 0]
    if report is None or report.get('worker_rc') or not failing:
        failing = [tail_of(delegate.paths(cfg, task['run'])['log'])]
    ok = bool(report) and report['rc'] == 0
    return {'run': task['run'], 'step': task.get('step'), 'worker': task.get('worker'), 'at': now, 'ok': ok,
            'signature': sig, 'why': why, 'stat': (report or {}).get('stat', ''), 'diff': (report or {}).get('diff'),
            'tail': '' if report and report['rc'] == 0 else failing[-1][-TAIL_CHARS:]}


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
    run(wt, 'checkout', '-q', '-B', branch)
    run(wt, 'add', '-A')
    subject = repo.get('commit', 'chore(autopilot): {title}').replace('{title}', task['title'])[:100]
    msg = f'{subject}\n\nBacklog task {task["id"]}, attempt {len(task["attempts"])}, run {report["id"]} by {task.get("step")}.\n'
    for line in repo.get('trailers') or []:
        msg += f'\n{line}'
    done = run(wt, 'commit', '-q', '-m', msg)
    if done.returncode != 0:
        task['publish_note'] = f'the commit was refused ({(done.stderr or done.stdout).strip()[:200]}): review the diff {report["diff"]}'
        return
    if repo.get('pr') != 'draft':
        task['publish_note'] = f'committed on {branch} in {wt}: review it, then merge or `tutti backlog retry {task["id"]} --note ...`'
        return
    pushed = run(wt, 'push', '-q', '-u', 'origin', branch)
    if pushed.returncode != 0:
        task['publish_note'] = f'committed on {branch}; the push failed ({pushed.stderr.strip()[:200]})'
        return
    body = f'Backlog task {task["id"]} ({task["source"]}), done by {task.get("step")} in {len(task["attempts"])} attempt(s).\n\n' \
           f'Check: `{task.get("check") or repo.get("check")}` passed. Opened by `tutti autopilot`; never merged by it.'
    pr = gh(['gh', 'pr', 'create', '--draft', '--head', branch, '--title', subject, '--body', body], cwd=wt, capture_output=True, text=True)
    task['pr'] = pr.stdout.strip() if pr.returncode == 0 else None
    failed = f'committed on {branch}; gh pr create failed: {pr.stderr.strip()[:200]}'
    task['publish_note'] = f'draft pull request {task["pr"]}' if task['pr'] else failed


def finished(cfg, task, is_alive):
    """(ended?, report or None) of a running task's run."""
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
        if a['ok']:
            t['status'] = 'review'
            publish_fn(cfg, t, report)
            events.append(f'{t["id"]} passed on {a["step"]}: {t.get("publish_note", "")}')
            continue
        park = should_park(cfg, t)
        t['status'], t['parked'] = ('needs-person', park) if park else ('queued', None)
        events.append(f'{t["id"]} failed on {a["step"]} ({a["why"][:120]})' + (f': parked: {park}' if park else ': queued again'))
    return events


def tick(cfg, states, machines, now=None, is_alive=None, spawn=subprocess.Popen, publish_fn=publish):
    """One round: collect, plan, dispatch. `states`: {account: pacing.account_state}; `machines`: state.machines_state items."""
    from . import state as state_mod
    now = now or time.time()
    is_alive = is_alive or state_mod.alive
    on = enabled(cfg)
    with backlog.locked(cfg) as data:
        events = collect(cfg, data, now, is_alive, publish_fn)
        p = plan(cfg, data['tasks'], states, machines, on)
        for task_id, step in p['dispatch']:
            t = backlog.find(data, task_id)
            run_id = dispatch(cfg, t, step, now, spawn)
            events.append(f'{task_id} sent to {step} (run {run_id}, attempt {len(t["attempts"]) + 1})')
    st = read_state(cfg)
    st['last'] = {'at': now, 'state': p['state'], 'reason': p['reason'], 'waiting': p['waiting']}
    st['events'] = ([{'at': now, 'text': e} for e in events] + st.get('events', []))[:50]
    write_state(cfg, st)
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
                               'parked', 'diff', 'worktree')}
    v['attempt_count'] = len(t['attempts'])
    v['history'] = [{'step': a['step'], 'ok': a['ok'], 'why': a['why'][:200], 'signature': a['signature'], 'run': a['run']}
                    for a in t['attempts']]
    if t['status'] == 'running':
        v['elapsed'] = now - (t.get('started') or now)
        v['last_line'] = last_line(delegate.paths(cfg, t['run'])['log'])
        v['credits'] = credits_of(cfg, t['run'])
    return v


def view(cfg, now=None):
    """The Autopilot panel: on or off, the last decision, the queue, the runs going, the results to review, what is parked."""
    now = now or time.time()
    data = backlog.load(cfg)
    st = read_state(cfg)
    tasks = [task_view(cfg, t, now) for t in data['tasks'] if t['status'] not in ('done', 'dropped')]
    s = settings(cfg)
    return {'enabled': enabled(cfg), 'configured': bool(s['repos']), 'accounts': s['accounts'], 'ladder': ladder(cfg),
            'max_parallel': s['max_parallel'], 'last': st.get('last'), 'events': st.get('events', [])[:10],
            'queue': [t for t in tasks if t['status'] == 'queued'], 'running': [t for t in tasks if t['status'] == 'running'],
            'review': [t for t in tasks if t['status'] == 'review'], 'parked': [t for t in tasks if t['status'] == 'needs-person']}


def describe(v):
    last = v.get('last') or {}
    lines = [f'autopilot: {"on" if v["enabled"] else "off"}; {last.get("state", "not run yet")}: {last.get("reason", "")}'.rstrip(': '),
             f'ladder: {" -> ".join(v["ladder"]) or "(none)"}; max_parallel {v["max_parallel"]}']
    for title, items in (('running', v['running']), ('to review', v['review']), ('needs a person', v['parked']), ('queued', v['queue'])):
        for t in items:
            extra = t.get('publish_note') or t.get('parked') or (f'run {t["run"]} on {t["step"]}' if t['status'] == 'running' else '')
            lines.append(f'{title:15} {t["id"]:5} {t["attempt_count"]} attempt(s)  {t["title"][:70]}')
            lines += [f'{"":21}{extra}'] if extra else []
    return '\n'.join(lines)
