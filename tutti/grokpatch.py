"""Grok as a patch author (t206, docs/SCOUTS.md, *Patches*): a scout drafts a change as one unified diff; Tutti applies it
and runs the check on its own machines. The scout never pushes, opens a pull request, builds or tests: its diff is
unverified until Tutti's check passes.

  tutti scout patch <task> [--via <scout>]   sends the task: repo, base commit, scope files with their contents, the check
  tutti scout apply <task>                   applies the answer's diff at that base in a worktree of its own and runs the check

The contract Grok agreed to (ventura8/Tutti#2139): one ```diff block per answer, against the given base, only the scoped
files, at most a few lines of rationale; input up to about 60 KB, a diff up to about 20 KB; a few tasks a day. A failed
check goes back once as a new task with the failing output and the first diff.

Pure: brief, extract, paths, check_scope. I/O: gather (git), ask (the scout's webhook), feed (the backlog), apply (git and
the check)."""
import os
import re
import subprocess
import time

from . import backlog, config, scouts, worktree

MAX_INPUT = 60_000   # bytes of scoped files sent inline
MAX_DIFF = 20_000    # bytes of diff taken from an answer
KIND = 'patch'
DIFF_RX = re.compile(r'```diff[ \t]*\n(.*?)```', re.S)
PATH_RX = re.compile(r'^(?:---|\+\+\+) (?:a/|b/)?(\S+)', re.M)


class PatchError(ValueError):
    pass


# ---- pure

def brief(task, base, repo_url, files, failure=None):
    """The task text the scout gets: the contract, the task, and the scoped files at `base` (`files`: {path: text})."""
    lines = [
        'PATCH TASK from Tutti. Draft ONE unified diff; you never push, open a pull request, build or test.',
        'Reply with exactly ONE fenced ```diff block (git apply format, a/ and b/ paths) against the base commit below,',
        'touching only the scoped files, and at most 5 lines of rationale. No other code blocks. If you could not read a file,',
        'say so instead of guessing its content.',
        '',
        f'Repository: {repo_url}',
        f'Base commit: {base}',
        f'Task {task["id"]}: {task["title"]}',
        f'Scope (the only files you may change): {", ".join(task.get("scope") or [])}',
        'Acceptance check (Tutti runs it): ' + (task.get('check') or "the repository's own check"),
    ]
    for c in task.get('criteria') or []:
        lines.append(f'Done when: {c}')
    for n in (task.get('notes') or [])[-5:]:
        lines.append(f'Note: {str(n)[:500]}')
    if failure:
        lines += ['', 'YOUR FIRST DIFF FAILED Tutti\'s check. Send a corrected diff against the same base.',
                  'First diff:', '```diff', failure['diff'].rstrip(), '```', 'Failing output (last lines):', '```',
                  failure['output'][-4000:].rstrip(), '```']
    for path, text in files.items():
        lines += ['', f'FILE {path} at {base[:12]}:', '```', text.rstrip('\n'), '```']
    return '\n'.join(lines)


def extract(answer):
    """The one diff in an answer, or PatchError saying what is wrong with it."""
    found = DIFF_RX.findall(answer or '')
    if not found:
        raise PatchError('patch: the answer has no ```diff block; ask again with `tutti scout patch <task>`')
    if len(found) > 1:
        raise PatchError(f'patch: the answer has {len(found)} diff blocks, the contract asks for one; ask again')
    diff = found[0]
    if len(diff.encode()) > MAX_DIFF:
        raise PatchError(f'patch: the diff is {len(diff.encode()) // 1000} KB, over {MAX_DIFF // 1000} KB; '
                         'split the task into smaller ones')
    return diff if diff.endswith('\n') else diff + '\n'


def paths(diff):
    """The files a diff touches (both sides, /dev/null left out)."""
    return sorted({p for p in PATH_RX.findall(diff) if p != '/dev/null'})


def in_scope(path, scope):
    return any(path == s or (s.endswith('/') and path.startswith(s)) for s in scope)


def check_scope(diff, scope):
    """Raises PatchError when the diff touches a file outside the task's scope or touches nothing."""
    touched = paths(diff)
    if not touched:
        raise PatchError('patch: the diff names no file; ask again')
    outside = [p for p in touched if not in_scope(p, scope)]
    if outside:
        raise PatchError(f'patch: the diff changes {", ".join(outside)}, outside the scope ({", ".join(scope)}); it was not applied')
    return touched


# ---- I/O

def _git(path, *args):
    r = subprocess.run([worktree.git_bin(), '-C', path, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise PatchError(f'patch: git {" ".join(args)} failed in {path}: {(r.stderr or r.stdout).strip()[:300]}')
    return r.stdout.strip()


def repo_path(cfg, task):
    repo = (cfg.get('autopilot') or {}).get('repos', {}).get(task['repo'])
    if not repo:
        raise PatchError(f'patch: {task["id"]} is for {task["repo"]}, which is not under autopilot.repos; add it there first')
    return config.expand(repo['path'])


def gather(cfg, task):
    """(base sha, repo url, {path: text}) of the task's scoped files at the repository's HEAD; bounded by MAX_INPUT."""
    scope = task.get('scope') or []
    if not scope or any(s.endswith('/') for s in scope):
        raise PatchError(f'patch: {task["id"]} needs its scope as file paths (not folders) so the scout gets the files: '
                         f'tutti backlog rm {task["id"]}, then add it with --scope <file> for each file')
    path = repo_path(cfg, task)
    base = _git(path, 'rev-parse', 'HEAD')
    url = _git(path, 'remote', 'get-url', 'origin')
    url = re.sub(r'^git@github\.com:', 'https://github.com/', url).removesuffix('.git')
    url = re.sub(r'//[^/@]+@', '//', url)   # never a credential in a URL sent out
    files, size = {}, 0
    for p in scope:
        rc = subprocess.run([worktree.git_bin(), '-C', path, 'show', f'{base}:{p}'], capture_output=True, text=True)
        text = rc.stdout if rc.returncode == 0 else ''   # a new file: empty
        size += len(text.encode())
        if size > MAX_INPUT:
            raise PatchError(f'patch: the scoped files of {task["id"]} pass {MAX_INPUT // 1000} KB; give it fewer files')
        files[p] = text
    return base, url, files


def ask(cfg, tid, via, gh_fn=scouts.gh, post_fn=scouts.post, env=None, failure=None, now=None):
    """Sends task `tid` to scout `via` as a patch task; the backlog task waits for the answer. Returns the scout request."""
    now = now or time.time()
    sc = scouts.settings(cfg, via)
    url, key = scouts.secrets(sc, env)
    with backlog.locked(cfg) as data:
        task = backlog.find(data, tid)
        if task['status'] != 'queued':
            raise PatchError(f'patch: {tid} is {task["status"]}; only a queued task goes to a scout')
        base, repo_url, files = gather(cfg, task)
        text = brief(task, base, repo_url, files, failure)
        with scouts.locked(cfg) as sdata:
            r = scouts.new_request(sdata, sc, text, KIND, skill='drafting', now=now, limit=MAX_INPUT + 8000)
            r.update(patch_for=tid, base=base)
            scouts.send(sc, r, url, key, gh_fn, post_fn, now)
        if r['status'] == 'failed':
            raise PatchError(f'patch: {via} was not reached: {r.get("why")}')
        task['patch'] = {'scout': via, 'request': r['id'], 'base': base, 'url': r.get('url'), 'at': now,
                         'second': bool(failure)}
        task['notes'] = [*task.get('notes', []), f'Sent to {via} as a patch task ({r["id"]}, {r.get("url")}); waits for its diff.']
    return r


def holds(tasks):
    """{task id: why} of queued tasks waiting for a scout's diff: no worker takes them meanwhile."""
    return {t['id']: f'waits for {t["patch"]["scout"]}\'s diff ({t["patch"]["request"]})' for t in tasks
            if t.get('status') == 'queued' and (t.get('patch') or {}).get('request') and not t['patch'].get('diff')}


def answered(sdata):
    """Answered patch requests not fed to the backlog yet (marked fed)."""
    out = []
    for r in sdata['requests']:
        if r.get('kind') == KIND and r.get('status') == 'answered' and not r.get('fed'):
            r['fed'] = True
            out.append({'id': r['id'], 'task': r.get('patch_for'), 'answer': r.get('answer') or ''})
    return out


def feed(cfg, answers):
    """Keeps each answer's diff on its task (state/patches/<task>-<request>.diff) when it is valid; else says why on it."""
    events = []
    if not answers:
        return events
    folder = os.path.join(config.state_dir(cfg), 'patches')
    os.makedirs(folder, exist_ok=True)
    with backlog.locked(cfg) as data:
        for a in answers:
            task = next((t for t in data['tasks'] if t['id'] == a['task']), None)
            if not task or (task.get('patch') or {}).get('request') != a['id']:
                continue
            try:
                diff = extract(a['answer'])
                check_scope(diff, task.get('scope') or [])
            except PatchError as e:
                task['patch'].update(error=str(e), request=None)
                task['notes'] = [*task.get('notes', []), f'{a["id"]}: {e}']
                events.append(f'{task["id"]}: {e}')
                continue
            path = os.path.join(folder, f'{task["id"]}-{a["id"]}.diff')
            with open(path, 'w', encoding='utf8') as f:
                f.write(diff)
            task['patch']['diff'] = path
            task['notes'] = [*task.get('notes', []), f'{a["id"]}: a diff arrived ({len(paths(diff))} file(s)); '
                                                     f'`tutti scout apply {task["id"]}` applies it and runs the check.']
            events.append(f'{task["id"]}: diff from {a["id"]} kept at {path}')
    return events


def apply(cfg, tid, run=subprocess.run):
    """Applies the task's diff at its base in a worktree of its own (branch grok/<task>) and runs the check there.
    Returns (ok, worktree, output). A pass leaves the branch for review; a failure says what to do next."""
    data = backlog.load(cfg)
    task = backlog.find(data, tid)
    p = task.get('patch') or {}
    if not p.get('diff') or not os.path.isfile(p['diff']):
        raise PatchError(f'patch: {tid} has no diff yet: `tutti scout patch {tid}` asks for one, '
                         f'`tutti scout answer {p.get("request")}` waits')
    path = repo_path(cfg, task)
    wt = os.path.join(config.state_dir(cfg), 'patches', f'wt-{tid}')
    if os.path.exists(wt):
        _git(path, 'worktree', 'remove', '--force', wt)
    _git(path, 'worktree', 'add', '-B', f'grok/{tid}', wt, p['base'])
    _git(wt, 'apply', '--check', p['diff'])
    _git(wt, 'apply', p['diff'])
    _git(wt, 'add', '-A')
    _git(wt, '-c', 'user.name=Tutti', '-c', 'user.email=tutti@localhost', 'commit', '-q', '-m',
         f'{task["title"][:60]} ({tid})\n\nDrafted by scout {p["scout"]} ({p["request"]}), applied and checked by Tutti.')
    check = task.get('check') or ((cfg.get('autopilot') or {}).get('repos', {}).get(task['repo']) or {}).get('check') or 'true'
    r = run(['bash', '-lc', check], cwd=wt, capture_output=True, text=True, timeout=3600)
    out = (r.stdout or '') + (r.stderr or '')
    ok = r.returncode == 0
    with backlog.locked(cfg) as data:
        t = backlog.find(data, tid)
        t['patch'].update(applied=time.time(), ok=ok, worktree=wt)
        if ok:
            t.update(status='review', branch=f'grok/{tid}', worktree=wt)
            t['notes'] = [*t.get('notes', []), f'The scout\'s diff passed the check in {wt} (branch grok/{tid}); review and land it.']
        else:
            t['notes'] = [*t.get('notes', []), f'The scout\'s diff failed the check (exit {r.returncode}).'
                          + ('' if p.get('second') else f' `tutti scout patch {tid}` sends it back once with the output.')]
            t['patch']['failure'] = {'diff': open(p['diff'], encoding='utf8').read(), 'output': out[-6000:]}
            t['patch']['request'] = t['patch']['diff'] = None
    return ok, wt, out
