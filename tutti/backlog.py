"""`tutti backlog`: a queue of bounded tasks per repository, in <state>/backlog.json, that `tutti autopilot` hands to workers
with room. A task: id, title, repo (a name under autopilot.repos), scope (paths it may change), the acceptance check, a size,
where it came from, its status and every attempt. Seeded from sources that are safe to automate: the repo's docs/FEEDBACK.md
open items, TODO/FIXME with an owner tag, and the gaps a repo's `gaps` command prints."""
import contextlib
import json
import os
import re
import subprocess
import time

from . import config, worktree

try:
    import fcntl
except ImportError:   # Windows: no flock; one writer at a time is then the caller's job
    fcntl = None

FILE = 'backlog.json'
SIZES = {'S': 1, 'M': 2, 'L': 3}   # what a task costs in bounded-task units of an account's room
# queued: waits for a worker; running: a run works on it; review: passed its check, a person reviews it; needs-person: parked
# (no progress, or too many attempts); done: accepted; dropped: removed from the queue but kept for the record; candidate: a
# suggestion (a scout's Sentry triage while self-improvement is off) that waits for `tutti backlog accept <id>`
STATUSES = ('candidate', 'queued', 'running', 'review', 'needs-person', 'done', 'dropped')
OPEN = ('queued', 'running')
TODO_RX = r'\b(TODO|FIXME)\(([A-Za-z0-9_.@-]+)\):?\s*(.*)'
TODO_ERE = r'(TODO|FIXME)\([A-Za-z0-9_.@-]+\)'   # the same for git grep -E


class BacklogError(Exception):
    """A backlog command cannot be done; the message says why and what to type instead."""


def path_of(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


@contextlib.contextmanager
def locked(cfg):
    """The backlog, read and written back under one lock: the dashboard's autopilot and a `tutti backlog` command never mix."""
    p = path_of(cfg)
    with open(p + '.lock', 'w', encoding='utf8') as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        data = load(cfg)
        yield data
        save(cfg, data)


def save(cfg, data):
    """Writes the backlog at once (inside `locked`, e.g. right after a run is started, so a crash later in the round never
    leaves a started run marked queued). A temp name of this process: two writers never share one."""
    p = path_of(cfg)
    tmp = f'{p}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, p)


def load(cfg):
    try:
        with open(path_of(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault('next', 1)
    data.setdefault('tasks', [])
    return data


def key_of(t):
    return (t['repo'], re.sub(r'\s+', ' ', t['title'].strip().lower()))


def new_task(repo, title, scope=(), check='', size='S', source='orchestrator', notes=()):
    if size not in SIZES:
        raise BacklogError(f'size "{size}": use one of {", ".join(SIZES)} (small, medium, large)')
    if not title.strip():
        raise BacklogError('a task needs a title that says what to change')
    return {'repo': repo, 'title': title.strip()[:300], 'scope': [s for s in scope if s], 'check': check, 'size': size,
            'source': source, 'status': 'queued', 'attempts': [], 'notes': list(notes), 'added': time.time()}


def add(data, task):
    """Adds the task unless an open or reviewed one with the same repo and title exists; returns (task, added?)."""
    for t in data['tasks']:
        if key_of(t) == key_of(task) and t['status'] not in ('done', 'dropped'):
            return t, False
    task = {**task, 'id': f't{data["next"]}'}
    data['next'] += 1
    data['tasks'].append(task)
    return task, True


def find(data, task_id):
    for t in data['tasks']:
        if t['id'] == task_id:
            return t
    raise BacklogError(f'no task "{task_id}" in the backlog: `tutti backlog list --all` shows every id')


def queued(data, repos=None):
    """The tasks waiting for a worker, oldest first (a retry keeps its place)."""
    return [t for t in data['tasks'] if t['status'] == 'queued' and (repos is None or t['repo'] in repos)]


def listing(data, show_all=False):
    rows = []
    for t in data['tasks']:
        if not show_all and t['status'] in ('done', 'dropped'):
            continue
        tries = len(t['attempts'])
        rows.append(f'{t["id"]:5} {t["status"]:12} {t["repo"]:10} {t["size"]} {tries} attempt(s)  {t["title"][:90]}')
        if t.get('parked'):
            rows.append(f'      needs a person: {t["parked"]}')
    return '\n'.join(rows) or 'the backlog is empty: tutti backlog add <repo> "<title>" --check "<command>", or tutti backlog seed <repo>'


# ---- sources that are safe to automate


def feedback_items(text):
    """(title, scope) of every open item of a FEEDBACK.md: table rows whose last cell starts with `Open`, and the dated
    bullet notes under the table (they have no status until a change closes them)."""
    out = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip('|').split('|')] if line.lstrip().startswith('|') else []
        if len(cells) >= 4 and cells[0].isdigit() and cells[-1].lower().startswith('open'):
            out.append(f'FEEDBACK #{cells[0]}: {cells[3]} Expected: {cells[-2]}')
        elif re.match(r'^- \d{4}-\d{2}-\d{2}\b', line.strip()) and not re.search(r'\bFixed in\b', line):   # a closed note
            out.append('FEEDBACK: ' + line.strip()[2:])
    return out


def todo_items(lines):
    """(title, path) of every `TODO(owner):` or `FIXME(owner):` in `git grep -n` output lines (<path>:<line>:<text>)."""
    out = []
    for line in lines:
        parts = line.split(':', 2)
        m = re.search(TODO_RX, parts[2]) if len(parts) == 3 else None
        if m:
            out.append((f'{m.group(1)}({m.group(2)}) in {parts[0]}:{parts[1]}: {m.group(3).strip()[:200]}', parts[0]))
    return out


def gap_of(line):
    """(title, path or None) of one gap line: `<path>[:<line>[:<col>]]: <what>`, or plain text."""
    parts = [p.strip() for p in line.split(':')]
    path = parts[0]
    rest = parts[1:]
    while rest and rest[0].isdigit():
        rest = rest[1:]
    what = ':'.join(rest).strip()
    if what and PATH_RX.fullmatch(path) and '.' in os.path.basename(path):
        return f'{path}: {what}', path
    return line, None


PATH_RX = re.compile(r'[\w./-]+')


def gap_items(lines):
    """(title, path or None) of a repo's `gaps` command output: one gap per line, `<path>: <what is missing>` or plain text."""
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        out.append(gap_of(line))
    return out


SOURCE_EXT = ('.py', '.ts', '.tsx', '.js', '.mjs', '.sh')
NOT_UNITS = ('__init__', 'index', 'main', 'conftest', 'setup')


def is_test(path):
    parts = path.lower().split('/')
    name = parts[-1]
    return any(p in ('test', 'tests', '__tests__', 'e2e', 'spec') for p in parts[:-1]) or name.startswith('test_') or \
        '.test.' in name or '.spec.' in name or name.endswith('_test.py')


def untested_items(files):
    """(title, path) of every tracked source file (`git ls-files` lines) whose name no test file mentions: a missing test is
    a bounded task with an obvious check. Config files, type declarations, package entry points and the tests are skipped."""
    tests = ' '.join(os.path.basename(f).lower() for f in files if is_test(f))
    out = []
    for f in files:
        base = os.path.basename(f)
        stem, ext = os.path.splitext(base)
        if ext not in SOURCE_EXT or is_test(f) or stem in NOT_UNITS or '.config' in base or base.endswith('.d.ts') \
                or stem.lower() in tests:
            continue
        out.append((f'Add unit tests for {f}: no test file names it; cover its behaviour, a good case and a bad one', f))
    return out


def sources(name, repo, run=subprocess.run, untested=False):
    """(title, scope, source) of every task the repo's safe sources name: FEEDBACK.md's open items, owner-tagged TODO/FIXME,
    the repo's `gaps` command (lint findings, coverage gaps: one per line), and with `untested` the source files without a
    test (autopilot.fill_room)."""
    path = config.expand(repo['path'])
    found = []
    fb = os.path.join(path, repo.get('feedback', 'docs/FEEDBACK.md'))
    if os.path.isfile(fb):
        with open(fb, encoding='utf8') as f:
            found += [(t, [], 'feedback') for t in feedback_items(f.read())]
    grep = run([worktree.git_bin(), '-C', path, 'grep', '-nE', TODO_ERE],
               capture_output=True, text=True)
    found += [(t, [p], 'todo') for t, p in todo_items(grep.stdout.splitlines())]
    if repo.get('gaps'):
        res = run(['bash', '-c', repo['gaps']], cwd=path, capture_output=True, text=True)
        found += [(t, [p] if p else [], 'gaps') for t, p in gap_items(res.stdout.splitlines())]
    if untested:
        files = run([worktree.git_bin(), '-C', path, 'ls-files'], capture_output=True, text=True)
        found += [(t, [p], 'untested') for t, p in untested_items(files.stdout.splitlines())]
    return found


def seed(data, name, repo, run=subprocess.run):
    """Adds the tasks the repo's safe sources name. Returns the added tasks."""
    check = repo.get('check') or ''
    added = []
    for title, scope, source in sources(name, repo, run):
        task, new = add(data, new_task(name, title, scope, check, 'S', source))
        if new:
            added.append(task)
    return added
