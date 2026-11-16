"""All household work through Tutti (docs/INTAKE.md): the leases a session takes on a machine or a device for a task, the
intake guard that finds work done outside Tutti (git commits in an autopilot repo, leases, that no Tutti task or run explains),
and the AGENTS.md snippet `tutti connect --repo` inserts. The checks are pure (linked, outside_commits, insert_snippet); the
I/O is in scan_repo, view and the lease file."""
import contextlib
import json
import os
import re
import subprocess
import time

from . import backlog, config, durable

try:
    import fcntl
except ImportError:   # Windows: no flock
    fcntl = None

LEASES = 'session-leases.json'
KINDS = ('machine', 'device')
DEFAULT_MINUTES = 120
WINDOW_HOURS = 24          # how far back the guard looks
CACHE_SECONDS = 300        # git is read at most this often for the dashboard
TASK_RX = re.compile(r'\b(t\d{1,6})\b')
TUTTI_BRANCHES = ('autopilot/', 'tutti/', 'ap-', 'tutti-run-')
TUTTI_REFS = [f'refs/{k}/{b}*' for k in ('heads', 'remotes/*') for b in TUTTI_BRANCHES]
BOT_AUTHORS = ('[bot]', 'dependabot', 'renovate')
MARK_START = '<!-- tutti:all-work-through-tutti -->'
MARK_END = '<!-- /tutti:all-work-through-tutti -->'
SNIPPET = f'''{MARK_START}
## All work goes through Tutti

This repository belongs to a household that runs all its AI work through Tutti. Every session (Claude Code, Codex, Cursor,
Antigravity, a person's own agent) does its work as a Tutti task, so it is scheduled, paced and visible:

- before you change anything: find or add the task (MCP `backlog_add`, or `tutti backlog add <repo> "<title>"`), then claim
  it (`backlog_claim`, or `tutti backlog claim <id> --who <session>`); mention the task id (t123) in every commit;
- a machine or an Android device: lease it for the task (`lease_claim`, or `tutti lease claim device <name> --who <session>
  --task <id>`) and release it when done (`lease_release`);
- tests on another machine: `test_on` (or `tutti test`); usage you read: `usage_report` (or `tutti usage log`);
- a question only the owner can answer: `ask_owner`; Claude-only work (browser, judgement): `claude_task` (or `tutti run
  claude "<task>" --repo <repo>`);
- when done: `backlog_complete` (or `tutti backlog done <id>`).

Commits and leases no Tutti task explains show on the dashboard as "Work outside Tutti" (docs/INTAKE.md in Tutti).
{MARK_END}
'''


class IntakeError(Exception):
    """A lease command cannot be done; the message says why and what to type instead."""


# ---- leases a session takes on a machine or a device


def leases_path(cfg):
    return os.path.join(config.state_dir(cfg), LEASES)


def read_leases(cfg):
    try:
        with open(leases_path(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return data.get('leases') if isinstance(data.get('leases'), dict) else {}


@contextlib.contextmanager
def leases_locked(cfg):
    p = leases_path(cfg)
    with open(p + '.lock', 'w', encoding='utf8') as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        data = read_leases(cfg)
        yield data
        durable.dump_json(p, {'leases': data}, indent=1)


def lease_key(kind, name):
    return f'{kind}:{name}'


def known(cfg, kind, name):
    section = cfg.get('machines' if kind == 'machine' else 'devices') or {}
    return name in section


def claim(data, kind, name, who, task, minutes, now):
    """Records the lease; a live lease of another session refuses it. Returns the lease."""
    if kind not in KINDS:
        raise IntakeError(f'lease kind "{kind}": use one of {", ".join(KINDS)}')
    if not who:
        raise IntakeError('a lease needs --who <session>: the session that uses it (e.g. claude-laptop-map)')
    if minutes < 1:
        raise IntakeError(f'--minutes {minutes}: a lease lasts at least 1 minute (the default is {DEFAULT_MINUTES})')
    key = lease_key(kind, name)
    held = data.get(key)
    if held and held['who'] != who and held['until'] > now:
        raise IntakeError(f'{kind} {name} is leased by {held["who"]} until {time.strftime("%H:%M", time.localtime(held["until"]))}'
                          f'{" for " + held["task"] if held.get("task") else ""}: wait, or ask them to release it')
    data[key] = {'kind': kind, 'name': name, 'who': who, 'task': task or '', 'at': now, 'until': now + minutes * 60}
    return data[key]


def release(data, kind, name, who):
    key = lease_key(kind, name)
    held = data.get(key)
    if not held:
        raise IntakeError(f'{kind} {name} has no lease: `tutti lease list` shows them')
    if held['who'] != who:
        raise IntakeError(f'{kind} {name} is leased by {held["who"]}, not {who}: only that session releases it')
    return data.pop(key)


def lease_lines(data, now):
    rows = []
    for v in sorted(data.values(), key=lambda x: x['at']):
        state = 'expired' if v['until'] <= now else f'until {time.strftime("%H:%M", time.localtime(v["until"]))}'
        rows.append(f'{v["kind"]:8} {v["name"]:16} {v["who"]:24} {v.get("task") or "(no task)":8} {state}')
    return '\n'.join(rows) or 'no leases: tutti lease claim device|machine <name> --who <session> --task <id>'


# ---- the intake guard: work outside Tutti


def windows(tasks, repo, now):
    """[(start, end)] of the tasks of `repo` a session or a run worked on: claimed or running until done, else to now."""
    out = []
    for t in tasks:
        if t.get('repo') != repo:
            continue
        start = t.get('claimed_at') or t.get('started')
        if start:
            out.append((float(start), float(t.get('closed_at') or (now if t['status'] in ('claimed', 'running') else start + 3600))))
    return out


def linked(commit, ids, spans, lease_spans):
    """Why a commit belongs to Tutti, or '' when nothing explains it."""
    found = [x for x in TASK_RX.findall(commit['text']) if x in ids]
    if found:
        return f'mentions {found[0]}'
    if 'Backlog task ' in commit['text']:
        return 'an autopilot commit'
    if any(branch_name(r).startswith(TUTTI_BRANCHES) for r in commit['refs']):
        return 'on a Tutti branch'
    if any(a <= commit['at'] <= b for a, b in spans + lease_spans):
        return 'during a claimed task'
    return ''


def branch_name(ref):
    r = ref.strip().removeprefix('HEAD -> ')
    for p in ('refs/heads/', 'refs/remotes/', 'origin/'):
        r = r.removeprefix(p)
    return r


def is_bot(commit):
    return any(b in commit['author'].lower() for b in BOT_AUTHORS)


def outside_commits(commits, tasks, repo, leases, now):
    ids = {t['id'] for t in tasks}
    spans = windows(tasks, repo, now)
    lease_spans = [(v['at'], v['until']) for v in leases.values() if v.get('task') in ids]
    return [c for c in commits if not is_bot(c) and not linked(c, ids, spans, lease_spans)]


def outside_leases(leases, tasks, now):
    """Live leases with no task, or a task that is not open: device or machine use Tutti cannot explain."""
    open_ids = {t['id'] for t in tasks if t['status'] in ('claimed', 'running', 'queued', 'review')}
    out = []
    for v in leases.values():
        if v['until'] <= now:
            continue
        if not v.get('task'):
            out.append({**v, 'why': 'leased for no task'})
        elif v['task'] not in open_ids:
            out.append({**v, 'why': f'its task {v["task"]} is not open'})
    return out


def parse_log(text):
    """Commits from `git log --source --format=%H%x1f%ct%x1f%an%x1f%S%x1f%B%x1e` (%S: the branch it was reached by)."""
    out = []
    for rec in text.split('\x1e'):
        parts = rec.strip('\n').split('\x1f')
        if len(parts) < 5 or not parts[0].strip():
            continue
        sha, at, author, refs, body = parts[:5]
        out.append({'sha': sha.strip()[:12], 'at': float(at or 0), 'author': author, 'refs': [r for r in refs.split(', ') if r.strip()],
                    'text': body, 'subject': body.strip().split('\n', 1)[0][:120]})
    return out


def scan_repo(path, hours=WINDOW_HOURS, run=subprocess.run):
    try:
        # Tutti's own branches left out: a hand commit reached through one of them never passes for Tutti's
        p = run(['git', '-C', path, 'log', *[f'--exclude={x}' for x in TUTTI_REFS], '--all', '--source', f'--since={hours} hours ago',
                 '--format=%H%x1f%ct%x1f%an%x1f%S%x1f%B%x1e'],
                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_log(p.stdout) if p.returncode == 0 else None


def report(cfg, now=None, run=subprocess.run):
    """{'commits': [...], 'leases': [...], 'unreadable': [...]}: what no Tutti task explains."""
    now = time.time() if now is None else now
    tasks = backlog.load(cfg)['tasks']
    leases = read_leases(cfg)
    out = {'commits': [], 'leases': outside_leases(leases, tasks, now), 'unreadable': [], 'at': now, 'hours': WINDOW_HOURS}
    for name, repo in ((cfg.get('autopilot') or {}).get('repos') or {}).items():
        commits = scan_repo(config.expand(repo['path']), run=run)
        if commits is None:
            out['unreadable'].append(name)
            continue
        out['commits'] += [{**c, 'repo': name} for c in outside_commits(commits, tasks, name, leases, now)]
    out['commits'].sort(key=lambda c: -c['at'])
    return out


_CACHE = {}


def view(cfg, now=None, run=subprocess.run):
    """The dashboard's "Work outside Tutti" card, read from git at most every CACHE_SECONDS."""
    now = time.time() if now is None else now
    key = cfg.get('_path')
    hit = _CACHE.get(key)
    if hit and now - hit['at'] < CACHE_SECONDS:
        return hit
    _CACHE[key] = public(report(cfg, now, run))
    return _CACHE[key]


def public(r):
    """What the dashboard shows: no commit bodies."""
    return {**r, 'commits': [{k: v for k, v in c.items() if k != 'text'} for c in r['commits']]}


def refresh_in_background(cfg, every=CACHE_SECONDS):
    """The dashboard's loop: git is read off the request path, so a slow repo never stalls a snapshot."""
    import threading

    def loop():
        while True:
            _CACHE.pop(cfg.get('_path'), None)
            view(cfg)
            time.sleep(every)
    threading.Thread(target=loop, name='tutti-intake', daemon=True).start()


def cached(cfg):
    """The last view, or an empty one while the first read runs."""
    return _CACHE.get(cfg.get('_path')) or {'commits': [], 'leases': [], 'unreadable': [], 'at': 0, 'hours': WINDOW_HOURS}


def lines(r):
    rows = [f'{c["repo"]:12} {c["sha"]} {c["author"][:20]:20} {c["subject"][:80]}' for c in r['commits']]
    rows += [f'{v["kind"]} {v["name"]}: {v["who"]}, {v["why"]}' for v in r['leases']]
    rows += [f'{n}: git could not be read (is autopilot.repos.{n}.path right?)' for n in r['unreadable']]
    return '\n'.join(rows) or f'no work outside Tutti in the last {r["hours"]} h: every commit and lease has its task'


# ---- the AGENTS.md snippet


def insert_snippet(text):
    """`text` with the snippet in it (replacing an older copy), and whether it changed."""
    if MARK_START in text and MARK_END in text:
        head, _, rest = text.partition(MARK_START)
        tail = rest.partition(MARK_END)[2].lstrip('\n')
        new = head + SNIPPET + ('\n' + tail if tail else '')
    else:
        new = text.rstrip('\n') + ('\n\n' if text.strip() else '') + SNIPPET
    return new, new != text


def target_file(repo):
    """AGENTS.md when the repo has one (CLAUDE.md usually imports it), else CLAUDE.md, else a new AGENTS.md."""
    for name in ('AGENTS.md', 'CLAUDE.md'):
        p = os.path.join(repo, name)
        if os.path.exists(p):
            return p
    return os.path.join(repo, 'AGENTS.md')


def connect_repo(repo):
    if not os.path.isdir(os.path.join(repo, '.git')) and not os.path.isfile(os.path.join(repo, '.git')):
        raise IntakeError(f'{repo} is not a git repository: give the folder of a household repo (tutti connect --repo <path>)')
    p = target_file(repo)
    old = ''
    if os.path.exists(p):
        with open(p, encoding='utf8') as f:
            old = f.read()
    new, changed = insert_snippet(old)
    if changed:
        with open(p, 'w', encoding='utf8') as f:
            f.write(new)
    return p, changed
