"""The autopilot's two jobs beside dispatching. `autopilot.fill_room` (opt-in, on by default): free room on the trigger
accounts (autopilot.accounts, as the planner gives it) is filled by itself: when fewer fresh tasks are queued than the room
holds, the repos' safe sources (FEEDBACK.md, owner-tagged TODOs, the repo's `gaps` command for lint and coverage findings, and
source files no test names) are read and new bounded tasks queued, at most a few per parallel run. `autopilot.auto_review`
(on by default): a result waiting in "to review" is reviewed by the cheap reviewer (review.fallback) on its commit; a clean
review lands it by autopilot.repos.<repo>.land (merge: a pull request merged at once; pr: a pull request a person merges;
branch: it stays for a person), findings send it back to the queue with the findings as notes. A result lands only when its
attempt passed the repo's `check` (its lint and type gates). Pure parts: want, pick, verdict; the I/O: fill, review_round, land."""
import os
import subprocess
import sys
import tempfile
import time

from . import autopilot, backlog, config, priority, remote, review, workers, worktree

AHEAD = 3            # fresh tasks kept queued per parallel run: enough to never idle, never a flood
FILL_EVERY = 600     # seconds between reads of a repo's sources while the room stays unfilled
MAX_REVIEWS = 3      # a result sent back this many times by the reviewer needs a person
LANDS = ('merge', 'pr', 'branch')


def fill_on(cfg):
    """The dashboard's switch (or `tutti autopilot fill on|off`) when it was used, else autopilot.fill_room."""
    toggle = autopilot.read_state(cfg).get('fill_room')
    return bool(autopilot.settings(cfg).get('fill_room', True) if toggle is None else toggle)


def set_fill(cfg, on):
    with autopilot.state_locked(cfg) as data:
        data['fill_room'] = bool(on)


def room(cfg, tasks, states, machines):
    """Units of room on the trigger accounts now, after the runs going and the attempts the readings do not show yet."""
    used = autopilot.spent_units(tasks, states)
    return sum(max(0, autopilot.step_room(cfg, n, states, used, machines or ())[0]) for n in autopilot.settings(cfg)['accounts'])


def fresh_units(cfg, tasks):
    repos = autopilot.settings(cfg)['repos']
    return sum(autopilot.units(t) for t in tasks if t['status'] == 'queued' and not t['attempts'] and t['repo'] in repos)


def want(cfg, tasks, free):
    """Fresh tasks to add now: the room, capped at AHEAD per parallel run, less the fresh tasks already queued."""
    from . import parallel
    target = min(free, parallel.current(cfg) * AHEAD)
    return max(0, target - fresh_units(cfg, tasks))


def pick(data, found, need, cfg=None):
    """The first `need` of [(repo, title, scope, source, check)] whose title the backlog never had (a dropped or done task is
    not queued again), shared out over the repos by their projects' priority (weighted fair share, tutti/priority.py), so a
    more important project gets more of the room and no repo takes all of it."""
    known = {backlog.key_of(t) for t in data['tasks']}
    by_repo = {}
    for item in found:
        if backlog.key_of({'repo': item[0], 'title': item[1]}) not in known:
            by_repo.setdefault(item[0], []).append(item)
    items = [it for q in by_repo.values() for it in q]
    return priority.by_repo(cfg or {}, items, lambda it: it[0])[:max(0, need)]


def due(cfg, data, states, machines, now):
    """(room, want) when the repos' sources should be read now, else None: the switch on, room to fill, and at most every
    FILL_EVERY seconds while the last read added nothing."""
    st = autopilot.read_state(cfg).get('fill') or {}
    free = room(cfg, data['tasks'], states, machines)
    need = want(cfg, data['tasks'], free)
    if not fill_on(cfg) or need <= 0 or (st.get('read_at') and now - st['read_at'] < FILL_EVERY and not st.get('added')):
        return None
    return free, need


def gather(cfg, data, states, machines, now, run=subprocess.run):
    """([(repo, title, scope, source, check)], problems) read from every repo when `due`, else None. Runs the repos' commands,
    so the caller holds no lock (a slow `gaps` command must not block the backlog)."""
    if due(cfg, data, states, machines, now) is None:
        return None
    found, problems = [], []
    for name, repo in autopilot.settings(cfg)['repos'].items():
        if not os.path.isdir(config.expand(repo['path'])):
            problems.append(f'{name}: no folder {repo["path"]}')
            continue
        found += [(name, t, s, src, repo.get('check') or '') for t, s, src in backlog.sources(repo, run, untested=True)]
    return found, problems


def fill(cfg, data, states, machines, now, found=None):
    """Queues the gathered tasks the free room wants (recomputed under the lock). Returns {room, queued, added, note}."""
    st = autopilot.read_state(cfg).get('fill') or {}
    free = room(cfg, data['tasks'], states, machines)
    info = {'at': now, 'room': free, 'queued': fresh_units(cfg, data['tasks']), 'added': 0, 'read_at': st.get('read_at'),
            'note': st.get('note', '')}
    if found is None:
        return info
    items, problems = found
    for repo_name, title, scope, source, check in pick(data, items, want(cfg, data['tasks'], free), cfg):
        backlog.add(data, backlog.new_task(repo_name, title, scope, check, 'S', source))
        info['added'] += 1
    info.update(read_at=now, queued=fresh_units(cfg, data['tasks']))
    info['note'] = (f'queued {info["added"]} new task(s) from the repos\' sources' if info['added'] else
                    'the repos\' sources hold nothing new: add tasks, a `gaps` command, or FEEDBACK.md items') + \
        (f' ({"; ".join(problems)})' if problems else '')
    return info


# ---- results waiting for review


def land_policy(cfg, task):
    return (autopilot.settings(cfg)['repos'].get(task['repo']) or {}).get('land', 'pr')


def review_on(cfg):
    return bool(autopilot.settings(cfg).get('auto_review', True))


def review_round(cfg, data, now, is_alive, spawn=subprocess.Popen):
    """Starts `tutti autopilot land <id>` for the oldest result to review (one at a time: a review runs a worker). A land
    process that died without finishing frees its task for the next round. Returns event lines."""
    events, going = [], None
    for t in data['tasks']:
        lnd = t.get('landing')
        if t['status'] == 'review' and lnd:
            if is_alive(lnd.get('pid')):
                going = t
            else:
                t.pop('landing')
                t['retry_after'] = now + FILL_EVERY
                t['publish_note'] = f'the review of {t["id"]} stopped before it finished: it is tried again in {FILL_EVERY // 60} min'
    waiting = [t for t in data['tasks'] if t['status'] == 'review' and t['repo'] in autopilot.settings(cfg)['repos']
               and land_policy(cfg, t) != 'branch' and t.get('branch') and not t.get('landing') and now >= t.get('retry_after', 0)]
    if going or not waiting or not review_on(cfg):
        return events
    t = waiting[0]
    argv = [sys.executable, remote.BIN, '--config', cfg['_path'], 'autopilot', 'land', t['id']]
    log = os.path.join(config.state_dir(cfg), f'land-{t["id"]}.log')
    with open(log, 'a', encoding='utf8') as f:
        child = spawn(argv, stdout=f, stderr=f, stdin=subprocess.DEVNULL, start_new_session=True)
    t['landing'] = {'pid': child.pid, 'at': now}
    events.append(f'{t["id"]}: the reviewer reads its commit (then {land_policy(cfg, t)})')
    return events


def verdict(text):
    """(clean?, findings) of a review's text: clean when it says `No findings`; else its bullets (at most 8)."""
    body = (text or '').strip()
    bullets = [ln.strip()[2:].strip() for ln in body.splitlines() if ln.strip().startswith(('- ', '* '))]
    if body.lstrip('*_# ').lower().startswith('no findings') and not bullets:   # a hedged "no findings, but - ..." is not clean
        return True, []
    if bullets:
        return False, bullets[:8]
    return False, [body[:400] or 'the reviewer gave no verdict']


def gated(cfg, task):
    """'' when the passing attempt ran the repo's gates as they are today (autopilot.dispatch records them, and acceptance
    runs them with the task's own check), else why the result may not land yet."""
    repo = autopilot.settings(cfg)['repos'].get(task['repo']) or {}
    gates = (repo.get('check') or '').strip()
    if not gates:
        return f'autopilot.repos.{task["repo"]}.check is not set: a result lands only after the repo\'s lint and type gates passed'
    passed = [a for a in task['attempts'] if a.get('ok')]
    if not passed or passed[-1].get('gates') != gates:
        return 'its passing attempt did not run the repo\'s current gates'
    return ''


def ask_reviewer(cfg, diff, run_worker=None):
    """(reviewer, text) of the cheap reviewer's review of `diff` (review.PROMPT, review.fallback), run in an empty folder."""
    name, args = review.pick_fallback(cfg)
    if not name:
        raise RuntimeError(f'no reviewer has room ({args}): review.fallback names the cheap workers that review')
    with tempfile.TemporaryDirectory(prefix='tutti-review-') as d:
        with open(os.path.join(d, 'pr.diff'), 'w', encoding='utf8') as f:
            f.write(diff)
        argv = workers.with_args(workers.command(cfg['workers'][name], review.PROMPT), args)
        run_worker = run_worker or (lambda a, cwd: subprocess.run(a, cwd=cwd, capture_output=True, text=True,
                                                                    stdin=subprocess.DEVNULL, timeout=1800))
        r = run_worker(argv, d)
    if r.returncode != 0 or not (r.stdout or '').strip():
        raise RuntimeError(f'{name} did not finish the review (exit {r.returncode}): {(r.stderr or "")[-300:]}')
    return name, r.stdout.strip()


def git(cwd, *args):
    return subprocess.run([worktree.git_bin(), '-C', cwd, *args], capture_output=True, text=True)


def base_of(cfg, task, run=git):
    """autopilot.repos.<repo>.base, else the branch the repository's own checkout is on (the owner's work branch)."""
    repo = autopilot.settings(cfg)['repos'][task['repo']]
    if repo.get('base'):
        return repo['base']
    out = run(config.expand(repo['path']), 'rev-parse', '--abbrev-ref', 'HEAD')
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() != 'HEAD' else 'main'


def rebase(wt, base, run=git):
    """'' when the branch now sits on the newest base, else why not (a conflicting rebase is aborted)."""
    fetched = run(wt, 'fetch', '-q', 'origin', base)
    if fetched.returncode != 0:
        return f'retry: git fetch origin {base} failed ({fetched.stderr.strip()[:200]})'
    done = run(wt, 'rebase', '-q', 'FETCH_HEAD')
    if done.returncode == 0:
        return ''
    run(wt, 'rebase', '--abort')
    return f'it does not rebase cleanly onto {base} ({(done.stderr or done.stdout).strip()[:200]})'


def open_pr(task, base, subject, body, gh=subprocess.run):
    """The task's pull request: its draft marked ready, or a new one. (url, '') or (None, why)."""
    wt = task['worktree']
    if task.get('pr'):
        ready = gh(['gh', 'pr', 'ready', task['pr']], cwd=wt, capture_output=True, text=True)
        return (task['pr'], '') if ready.returncode == 0 else (None, f'gh pr ready failed: {ready.stderr.strip()[:200]}')
    pr = gh(['gh', 'pr', 'create', '--base', base, '--head', task['branch'], '--title', subject, '--body', body], cwd=wt,
           capture_output=True, text=True)
    lines = (pr.stdout or '').strip().splitlines()
    if pr.returncode != 0 or not lines:
        return None, f'gh pr create failed: {pr.stderr.strip()[:200]}'
    return lines[-1], ''


def merge(task, url, base, reviewer, run=git, gh=subprocess.run):
    """(status, note) of a squash merge. A merge that exits non-zero is checked against the PR's state before it is called
    failed; the remote branch is deleted after (gh cannot delete a branch a worktree holds)."""
    wt = task['worktree']
    merged = gh(['gh', 'pr', 'merge', url, '--squash'], cwd=wt, capture_output=True, text=True)
    if merged.returncode != 0:
        state = gh(['gh', 'pr', 'view', url, '--json', 'state', '--jq', '.state'], cwd=wt, capture_output=True, text=True)
        if (state.stdout or '').strip() != 'MERGED':
            why = merged.stderr.strip()[:200]
            return 'review', f'retry: reviewed clean by {reviewer}: pull request {url} is open; the merge failed ({why})'
    run(wt, 'push', '-q', 'origin', '--delete', task['branch'])
    return 'done', f'reviewed clean by {reviewer} and merged into {base}: {url}'


def publish_reviewed(cfg, task, base, reviewer, run=git, gh=subprocess.run):
    """(status, note) after a clean review of the rebased branch: pushed, a pull request, merged when land is merge."""
    wt = task['worktree']
    pushed = run(wt, 'push', '-q', '--force-with-lease', '-u', 'origin', task['branch'])
    if pushed.returncode != 0:
        return 'review', f'reviewed clean by {reviewer}; the push failed ({pushed.stderr.strip()[:200]}): it is tried again'
    subject = run(wt, 'log', '-1', '--format=%s').stdout.strip() or task['title'][:100]
    body = (f'Backlog task {task["id"]} ({task["source"]}), done by {task.get("step")} in {len(task["attempts"])} attempt(s).\n\n'
            f'The repo\'s gates passed in the acceptance check; reviewed by {reviewer}: no findings. Opened by `tutti autopilot`.')
    url, why = open_pr(task, base, subject, body, gh)
    if not url:
        return 'review', f'reviewed clean by {reviewer}; {why}'
    task['pr'] = url
    if land_policy(cfg, task) != 'merge':
        return 'done', f'reviewed clean by {reviewer}: pull request {url} into {base}, for a person to merge (land: pr)'
    return merge(task, url, base, reviewer, run, gh)


def decide(cfg, task, run=git, gh=subprocess.run, run_worker=None):
    """(status, note, notes) of one result: its gates, its rebase onto the base, the review of the whole branch, the landing."""
    why = gated(cfg, task)
    if why:
        return 'queued', f'{why}: it runs again with them', [why]
    base = base_of(cfg, task, run)
    why = rebase(task['worktree'], base, run)
    if why:
        return ('review', why, []) if why.startswith('retry') else ('needs-person', f'{why}: rebase {task["branch"]} by hand', [])
    diff = run(task['worktree'], 'diff', '--stat', '--patch', 'FETCH_HEAD...HEAD')
    if diff.returncode != 0 or not diff.stdout.strip():
        return 'needs-person', f'the branch has no diff against {base} to review ({diff.stderr.strip()[:200]})', []
    try:
        reviewer, text = ask_reviewer(cfg, diff.stdout, run_worker)
    except RuntimeError as e:
        return 'review', f'retry: the review could not run: {e}', []
    clean, notes = verdict(text)
    if not clean:
        return 'queued', f'{reviewer} found {len(notes)} problem(s): sent back with them as notes', notes
    status, note = publish_reviewed(cfg, task, base, reviewer, run, gh)
    return status, note, []


def record(t, status, note, notes, now):
    """The outcome written on the task (under the backlog's lock)."""
    t.pop('landing', None)
    t['publish_note'] = note
    if status == 'review':
        t['retry_after'] = now + FILL_EVERY
    if status == 'queued':
        t['reviews'] = t.get('reviews', 0) + 1
        t['notes'] = [*t.get('notes', []), *notes]
        if t['reviews'] >= MAX_REVIEWS:
            status, t['parked'] = 'needs-person', f'sent back {t["reviews"]} times: {"; ".join(notes)[:300]}'
    elif status == 'needs-person':
        t['parked'] = note
    t['status'], t['landed_at'] = status, now


def land(cfg, task_id, run=git, gh=subprocess.run, run_worker=None, now=None):
    """`tutti autopilot land <id>`: the review of one result and what follows. The worker runs outside the backlog's lock."""
    now = now or time.time()
    task = backlog.find(backlog.load(cfg), task_id)
    if task['status'] != 'review':
        return f'{task_id} is {task["status"]}, not waiting for a review'
    status, note, notes = decide(cfg, task, run, gh, run_worker)
    with backlog.locked(cfg) as data:
        t = backlog.find(data, task_id)
        if t['status'] != 'review':   # changed by hand (or another land) meanwhile: theirs stands
            return f'{task_id} became {t["status"]} during the review; left as it is ({note})'
        if task.get('pr'):
            t['pr'] = task['pr']
        record(t, status, note, notes, now)
    return f'{task_id}: {status}: {note}'
