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

from . import autopilot, backlog, config, remote, review, workers, worktree

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
    target = min(free, autopilot.settings(cfg)['max_parallel'] * AHEAD)
    return max(0, target - fresh_units(cfg, tasks))


def pick(data, found, need):
    """The first `need` of [(repo, title, scope, source, check)] whose title the backlog never had (a dropped or done task is
    not queued again), taken round-robin over the repos so one repo never takes all the room."""
    known = {backlog.key_of(t) for t in data['tasks']}
    by_repo = {}
    for item in found:
        if backlog.key_of({'repo': item[0], 'title': item[1]}) not in known:
            by_repo.setdefault(item[0], []).append(item)
    out, queues = [], list(by_repo.values())
    while len(out) < need and any(queues):
        for q in queues:
            if q and len(out) < need:
                out.append(q.pop(0))
    return out


def fill(cfg, data, states, machines, now, run=subprocess.run):
    """Queues new tasks for the free room. Returns {room, queued, added, note} for the dashboard."""
    st = autopilot.read_state(cfg).get('fill') or {}
    free = room(cfg, data['tasks'], states, machines)
    need = want(cfg, data['tasks'], free)
    info = {'at': now, 'room': free, 'queued': fresh_units(cfg, data['tasks']), 'added': 0, 'read_at': st.get('read_at'),
            'note': st.get('note', '')}
    if not fill_on(cfg) or need <= 0 or (st.get('read_at') and now - st['read_at'] < FILL_EVERY and not st.get('added')):
        return info
    found, problems = [], []
    for name, repo in autopilot.settings(cfg)['repos'].items():
        if not os.path.isdir(config.expand(repo['path'])):
            problems.append(f'{name}: no folder {repo["path"]}')
            continue
        found += [(name, t, s, src, repo.get('check') or '') for t, s, src in backlog.sources(name, repo, run, untested=True)]
    for repo_name, title, scope, source, check in pick(data, found, need):
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
                t['publish_note'] = f'the review of {t["id"]} stopped before it finished: it is tried again'
    waiting = [t for t in data['tasks'] if t['status'] == 'review' and t['repo'] in autopilot.settings(cfg)['repos']
               and land_policy(cfg, t) != 'branch' and t.get('branch') and not t.get('landing')]
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
    if body.lstrip('*_# ').lower().startswith('no findings'):
        return True, []
    bullets = [ln.strip()[2:].strip() for ln in body.splitlines() if ln.strip().startswith(('- ', '* '))]
    return False, (bullets or [body[:400] or 'the reviewer gave no verdict'])[:8]


def gated(cfg, task):
    """'' when the attempt's acceptance check held the repo's gates (autopilot.acceptance adds autopilot.repos.<r>.check to
    every check), else why the result may not land."""
    repo = autopilot.settings(cfg)['repos'].get(task['repo']) or {}
    if not (repo.get('check') or '').strip():
        return f'autopilot.repos.{task["repo"]}.check is not set: a result lands only after the repo\'s lint and type gates passed'
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
    fetched = run(wt, 'fetch', '-q', 'origin', base).returncode == 0
    done = run(wt, 'rebase', '-q', 'FETCH_HEAD' if fetched else base)
    if done.returncode == 0:
        return ''
    run(wt, 'rebase', '--abort')
    return f'it does not rebase cleanly onto {base} ({(done.stderr or done.stdout).strip()[:200]})'


def open_pr(task, base, subject, body, gh=subprocess.run):
    """The task's pull request: its draft marked ready, or a new one. (url, '') or (None, why)."""
    wt = task['worktree']
    if task.get('pr'):
        gh(['gh', 'pr', 'ready', task['pr']], cwd=wt, capture_output=True, text=True)
        return task['pr'], ''
    pr = gh(['gh', 'pr', 'create', '--base', base, '--head', task['branch'], '--title', subject, '--body', body], cwd=wt,
           capture_output=True, text=True)
    return (pr.stdout.strip().splitlines()[-1], '') if pr.returncode == 0 and pr.stdout.strip() else \
        (None, f'gh pr create failed: {pr.stderr.strip()[:200]}')


def publish_reviewed(cfg, task, reviewer, run=git, gh=subprocess.run):
    """(status, note) after a clean review: rebased onto its base, pushed, a pull request, merged when land is merge."""
    policy, wt = land_policy(cfg, task), task['worktree']
    base = base_of(cfg, task, run)
    why = rebase(wt, base, run)
    if why:
        return 'needs-person', f'reviewed clean by {reviewer}, but {why}: rebase {task["branch"]} by hand'
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
    if policy != 'merge':
        return 'done', f'reviewed clean by {reviewer}: pull request {url} into {base}, for a person to merge (land: pr)'
    merged = gh(['gh', 'pr', 'merge', url, '--squash', '--delete-branch'], cwd=wt, capture_output=True, text=True)
    if merged.returncode != 0:
        return 'done', f'reviewed clean by {reviewer}: pull request {url} is open; the merge failed ({merged.stderr.strip()[:200]})'
    return 'done', f'reviewed clean by {reviewer} and merged into {base}: {url}'


def land(cfg, task_id, run=git, gh=subprocess.run, run_worker=None, now=None):
    """`tutti autopilot land <id>`: the review of one result and what follows. The worker runs outside the backlog's lock."""
    now = now or time.time()
    task = backlog.find(backlog.load(cfg), task_id)
    if task['status'] != 'review':
        return f'{task_id} is {task["status"]}, not waiting for a review'
    status, note, notes = 'review', '', []
    why = gated(cfg, task)
    if why:
        status, note = 'needs-person', why
    else:
        diff = run(task['worktree'], 'show', '--stat', '--patch', 'HEAD').stdout
        try:
            reviewer, text = ask_reviewer(cfg, diff, run_worker)
        except RuntimeError as e:
            reviewer, text, note = None, '', f'the review could not run: {e}; it is tried again after the next probe'
        if reviewer:
            clean, notes = verdict(text)
            if clean:
                status, note = publish_reviewed(cfg, task, reviewer, run, gh)
            else:
                status, note = 'queued', f'{reviewer} found {len(notes)} problem(s): sent back with them as notes'
    with backlog.locked(cfg) as data:
        t = backlog.find(data, task_id)
        t.pop('landing', None)
        t['publish_note'] = note
        if status == 'queued':
            t['reviews'] = t.get('reviews', 0) + 1
            t['notes'] = [*t.get('notes', []), *notes]
            if t['reviews'] >= MAX_REVIEWS:
                status, t['parked'] = 'needs-person', f'the reviewer sent it back {t["reviews"]} times: {"; ".join(notes)[:300]}'
        elif status == 'needs-person':
            t['parked'] = note
        t['status'] = status
        if task.get('pr'):
            t['pr'] = task['pr']
        t['landed_at'] = now
    return f'{task_id}: {status}: {note}'
