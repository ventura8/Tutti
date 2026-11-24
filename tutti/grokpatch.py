"""Grok as a patch author (t206, docs/SCOUTS.md, *Patches*): a scout drafts a change as one unified diff; Tutti applies it
and runs the check on its own machines. The scout never pushes, opens a pull request, builds or tests: its diff is
unverified until Tutti's check passes.

  tutti scout patch <task> [--via <scout>]   sends the task: repo, base commit, scope files with their contents, the check
  tutti scout apply <task>                   applies the answer's diff at that base in a worktree of its own and runs the check

The contract Grok agreed to (ventura8/Tutti#2139): one ```diff block per answer, against the given base, only the scoped
files, at most a few lines of rationale; input up to about 60 KB, a diff up to about 20 KB; a few tasks a day. A failed
check goes back once as a new task with the failing output and the first diff.

Automatic (the scout round, `auto`): while the scout's account has room, a queued task that fits (file scope, size S or M,
not yet tried) is sent by itself, a kept diff is applied and checked by itself, and a failed check goes back once. A task the
scout fails, or cannot take, is left for the workers as before.

Pure: brief, extract, paths, check_scope, eligible, pick. I/O: gather (git), ask (the scout's webhook), feed (the backlog),
apply (git and the check), auto (the round)."""
import contextlib
import os
import re
import subprocess
import sys
import time

from . import backlog, config, scouts, worktree

MAX_INPUT = 60_000   # bytes of scoped files sent inline
MAX_DIFF = 20_000    # bytes of diff taken from an answer
KIND = 'patch'
DIFF_RX = re.compile(r'```diff[ \t]*\n(.*?)```', re.S)
PATH_RX = re.compile(r'^(?:---|\+\+\+) (?:a/|b/)?(\S+)', re.M)


AUTO_SIZES = ('S', 'M')   # larger tasks do not fit a 20 KB diff drafted from 60 KB of input
AUTO_PER_DAY = 3          # "a few tasks a day" (the contract)
AUTO_OPEN = 1             # patch requests waiting for an answer at once


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


SHA_RX = re.compile(r'[0-9a-f]{40}')
SAFE_PATH = re.compile(r'(?!-)(?!.*(?:^|/)\.\.(?:/|$))[\w./+@-]+')


def safe_path(p):
    """A repo-relative path git can take as a path, never as an option (no leading -, no .., no absolute path)."""
    if not isinstance(p, str) or not SAFE_PATH.fullmatch(p) or p.startswith('/'):
        raise PatchError(f'patch: "{p}" is not a plain path inside the repository; fix the task\'s scope')
    return p


def safe_sha(s):
    if not isinstance(s, str) or not SHA_RX.fullmatch(s):
        raise PatchError(f'patch: "{s}" is not a full commit id; send the task again with `tutti scout patch <task>`')
    return s


def in_scope(path, scope):
    return any(path == s or (s.endswith('/') and path.startswith(s)) for s in scope)


def check_scope(diff, scope):
    """Raises PatchError when the diff touches a file outside the task's scope or touches nothing."""
    touched = paths(diff)
    if not touched:
        raise PatchError('patch: the diff names no file; ask again')
    for p in touched:
        safe_path(p)
    outside = [p for p in touched if not in_scope(p, scope)]
    if outside:
        raise PatchError(f'patch: the diff changes {", ".join(outside)}, outside the scope ({", ".join(scope)}); it was not applied')
    return touched


def eligible(task, now=None):
    """(ok, why) for the automatic round: a queued task with file scope and a small size that no scout has handled yet,
    or whose first diff failed the check and has not gone back once."""
    p = task.get('patch') or {}
    scope = task.get('scope') or []
    if task.get('status') != 'queued':
        return False, 'not queued'
    if p.get('request') or p.get('diff'):
        return False, 'a scout already has it'
    if p.get('error'):
        return False, 'its diff was refused once; the workers take it'
    if p.get('second'):
        return False, 'the scout had its tries'
    if task.get('size', 'S') not in AUTO_SIZES:
        return False, f'size {task.get("size")} is over {AUTO_SIZES[-1]}'
    if not scope or any(s.endswith('/') for s in scope):
        return False, 'its scope is not a list of files'
    if task.get('depends_on') or float(task.get('not_before') or 0) > (now or 0):
        return False, 'blocked or waiting'
    return True, 'fits'


def pick(tasks, sent_today, waiting, now=None, per_day=AUTO_PER_DAY, open_max=AUTO_OPEN):
    """The id of the next task to send, or None: at most `per_day` a day and `open_max` waiting; the most urgent
    (lowest P level, then oldest) eligible task first."""
    from . import priority
    if sent_today >= per_day or waiting >= open_max:
        return None
    ok = [t for t in tasks if eligible(t, now)[0]]
    ok.sort(key=lambda t: (priority.task_level(t) if 'priority' in t else 9, t.get('added') or 0))
    return ok[0]['id'] if ok else None


def to_apply(tasks, now=0):
    """Ids of tasks whose diff is kept and not being applied (a start older than 2 h counts as lost)."""
    return [t['id'] for t in tasks if t.get('status') == 'queued' and (t.get('patch') or {}).get('diff')
            and (not t['patch'].get('applying') or now - float(t['patch']['applying']) > 7200)]


# ---- I/O

def _git(path, *args, stdin=None):
    r = subprocess.run([worktree.git_bin(), '-C', path, *args], capture_output=True, text=True, input=stdin)
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
    for p in scope:
        safe_path(p)
    path = repo_path(cfg, task)
    base = safe_sha(_git(path, 'rev-parse', 'HEAD'))
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
    diff_path = patch_file(cfg, p.get('diff'))
    if not diff_path:
        raise PatchError(f'patch: {tid} has no diff yet: `tutti scout patch {tid}` asks for one, '
                         f'`tutti scout answer {p.get("request")}` waits')
    path = repo_path(cfg, task)
    wt = os.path.join(config.state_dir(cfg), 'patches', f'wt-{tid}')
    if os.path.exists(wt):
        _git(path, 'worktree', 'remove', '--force', wt)
    if not re.fullmatch(r't\d+', str(tid)):
        raise PatchError(f'patch: "{tid}" is not a task id like t123')
    try:
        _git(path, 'worktree', 'add', '-B', f'grok/{tid}', '--', wt, safe_sha(p['base']))
        with open(diff_path, encoding='utf-8') as f:
            diff = f.read()
        check_scope(diff, task.get('scope') or [])   # checked again here: the saved file is read, never passed to git as an argument
        # --recount: a model's hunk headers often miscount their lines (Grok said larger diffs risk it; t259's did): git counts
        _git(wt, 'apply', '--recount', '--check', stdin=diff)
        _git(wt, 'apply', '--recount', stdin=diff)
        _git(wt, 'add', '-A')
        _git(wt, '-c', 'user.name=Tutti', '-c', 'user.email=tutti@localhost', 'commit', '-q', '-F', '-',
             stdin=f'{task["title"][:60]} ({tid})\n\nDrafted by scout {p["scout"]} ({p["request"]}), applied and checked by Tutti.')
    except PatchError as e:   # the diff does not apply: say so on the task, free it for the workers (never stuck 'applying')
        with backlog.locked(cfg) as data:
            t = backlog.find(data, tid)
            t['patch'].pop('applying', None)
            t['patch'].update(error=str(e)[:300], request=None, diff=None)
            t['notes'] = [*t.get('notes', []), f'The scout\'s diff could not be applied: {e}']
        raise
    check = task.get('check') or ((cfg.get('autopilot') or {}).get('repos', {}).get(task['repo']) or {}).get('check') or 'true'
    r = run(['bash', '-lc', check], cwd=wt, capture_output=True, text=True, timeout=3600)
    out = (r.stdout or '') + (r.stderr or '')
    ok = r.returncode == 0
    with backlog.locked(cfg) as data:
        t = backlog.find(data, tid)
        t['patch'].pop('applying', None)
        t['patch'].update(applied=time.time(), ok=ok, worktree=wt)
        if ok:
            t.update(status='review', branch=f'grok/{tid}', worktree=wt)
            t['notes'] = [*t.get('notes', []), f'The scout\'s diff passed the check in {wt} (branch grok/{tid}); review and land it.']
        else:
            t['notes'] = [*t.get('notes', []), f'The scout\'s diff failed the check (exit {r.returncode}).'
                          + ('' if p.get('second') else f' `tutti scout patch {tid}` sends it back once with the output.')]
            t['patch']['failure'] = {'diff': diff, 'output': out[-6000:]}
            t['patch']['request'] = t['patch']['diff'] = None
    return ok, wt, out


# ---- landing a diff without a person (owner, 2026-11-22: make Grok as useful as it can be, save the session's tokens)

def allowlist_new(wt, new_files):
    """Adds the diff's new test and doc files to SDIST_ALLOWLIST and MANIFEST.in when the repository has them (Tutti's own
    packaging guard); a repository without them is left alone. Returns the files added."""
    path = os.path.join(wt, 'SDIST_ALLOWLIST')
    if not os.path.isfile(path) or not os.path.isfile(os.path.join(wt, 'MANIFEST.in')):
        return []
    names = open(path, encoding='ascii').read().splitlines()
    root = os.path.realpath(wt)
    add = [f for f in new_files if f not in names and _inside(root, f) and os.path.isfile(os.path.join(root, f))]
    if not add:
        return []
    names = sorted(set(names) | set(add), key=lambda x: x.encode())
    generated = []
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location('build_guard', os.path.join(wt, 'build_guard.py'))
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        generated = list(guard.GENERATED)
    except (OSError, ImportError, AttributeError):
        return []
    with open(path, 'w', encoding='ascii') as f:
        f.write('\n'.join(names) + '\n')
    with open(os.path.join(wt, 'MANIFEST.in'), 'w', encoding='ascii') as f:
        f.write('global-exclude *\n' + ''.join(f'include {p}\n' for p in [*names, *generated]))
    return add


def _inside(root, rel):
    """True when a diff's path stays inside the worktree: a scout's diff is not trusted to name only its own files."""
    full = os.path.realpath(os.path.join(root, rel))
    return not os.path.isabs(rel) and full.startswith(root + os.sep)


def patch_file(cfg, path):
    """The saved diff's path when it lies in <state>/patches (where scout patch saves it), else None: the backlog is a
    file anyone with the state can edit, so its path is never opened blindly."""
    root = os.path.realpath(os.path.join(config.state_dir(cfg), 'patches'))
    full = os.path.realpath(path) if path else ''
    return full if full.startswith(root + os.sep) and os.path.isfile(full) else None


def autoland(cfg, tid, via, run=subprocess.run, apply_fn=None, ask_fn=None):
    """A kept diff applied and checked; a pass pushed as grok/<task> with a pull request for a person to review and merge (never
    merged here); a first failure sent back to the scout once with its output. Returns one event line."""
    ok, wt, out = (apply_fn or apply)(cfg, tid, run)
    if not ok:
        t = backlog.find(backlog.load(cfg), tid)
        if (t.get('patch') or {}).get('second'):
            return f'{tid}: the second diff failed its check too: left for a person (worktree {wt})'
        (ask_fn or ask)(cfg, tid, via, failure=t['patch'].get('failure'))
        return f'{tid}: the diff failed its check: sent back to {via} with the output (second try)'
    saved = patch_file(cfg, (backlog.find(backlog.load(cfg), tid).get('patch') or {}).get('diff'))
    with open(saved, encoding='utf8') if saved else contextlib.nullcontext(None) as f:
        new = [p for p in paths(f.read() if f else '') if p.startswith(('tests/', 'docs/'))]
    if allowlist_new(wt, new):
        _git(wt, 'add', '-A')
        _git(wt, '-c', 'user.name=Tutti', '-c', 'user.email=tutti@localhost', 'commit', '-q', '-m',
             f'chore: package the new files ({tid})')
    _git(wt, 'push', '-q', '-u', 'origin', f'grok/{tid}')
    t = backlog.find(backlog.load(cfg), tid)
    body = (f'Drafted by the {via} scout ({t["patch"].get("request")}) as one diff, applied at its base and checked by Tutti '
            f'(`{t.get("check") or "the repository check"}` passed). Review it and merge it; Tutti never merges a scout\'s diff.')
    r = run(['gh', 'pr', 'create', '--head', f'grok/{tid}', '--title', f'{t["title"][:60]} ({tid}, drafted by {via})',
             '--body', body], cwd=wt, capture_output=True, text=True, timeout=120)
    url = (r.stdout or '').strip().splitlines()[-1] if r.returncode == 0 and r.stdout else ''
    with backlog.locked(cfg) as data:
        task = backlog.find(data, tid)
        task['pr'] = url or task.get('pr')
        task['notes'] = [*task.get('notes', []), f'Landed for review: {url or "push done, the PR could not be opened"}']
    return f'{tid}: the diff passed its check: pull request {url or "not opened (" + (r.stderr or "").strip()[:120] + ")"}'


def land_in_background(cfg, tids, via, thread=None):
    """autoland for each task in a thread of its own (a check may take minutes); each outcome goes to the log."""
    import sys
    import threading

    def work():
        for tid in tids:
            try:
                line = autoland(cfg, tid, via)
            except (PatchError, OSError, subprocess.SubprocessError, KeyError) as e:
                line = f'{tid}: landing the diff failed ({e}); `tutti scout apply {tid}` tries by hand'
            sys.stderr.write(f'tutti: scouts: {line}\n')
    t = (thread or threading.Thread)(target=work, daemon=True, name='tutti-patch-land')
    t.start()
    return t



def auto(cfg, sc, acc, gh_fn=scouts.gh, post_fn=scouts.post, env=None, now=None, spawn=subprocess.Popen):
    """The scout round's patch step (docs/SCOUTS.md, *Patches*): sends the next fitting task while the scout's account has
    room, and starts `tutti scout apply` for every kept diff (detached: the check can take long). Returns event lines.
    Off when the scout's skills lack `drafting`."""
    now = now or time.time()
    if 'drafting' not in (sc.get('skills') or []):
        return []
    events = []
    data = backlog.load(cfg)
    # with autoland on (the default), scouts.tick already applies, checks and opens the PR for each kept diff: never twice
    for tid in ([] if sc.get('patch_autoland', True) else to_apply(data['tasks'], now)):
        if not re.fullmatch(r't\d+', str(tid)):   # the id names a log file: never a path from the backlog
            events.append(f'patch: skipped a task id that is not like t123 ({tid!r})')
            continue
        tid = f't{int(str(tid)[1:])}'
        with backlog.locked(cfg) as d:
            t = backlog.find(d, tid)
            t['patch']['applying'] = now
        log = os.path.join(config.state_dir(cfg), 'patches', f'apply-{tid}.log')
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, 'a', encoding='utf8') as f:
            spawn([sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bin', 'tutti'),
                   'scout', 'apply', tid], stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        events.append(f'{tid}: the diff is being applied and checked ({log})')
    ok, why = scouts.allowance(acc)
    if not ok:
        return events + [f'patch: nothing sent to {sc["name"]}: {why}']
    with scouts.locked(cfg) as sdata:
        mine = [r for r in sdata['requests'] if r['scout'] == sc['name'] and r.get('kind') == KIND and r['status'] != 'failed']
        today = sum(1 for r in mine if now - float(r.get('added') or 0) < 86400)
        waiting = sum(1 for r in mine if r['status'] in ('queued', 'waiting'))
    tid = pick(data['tasks'], today, waiting, now)
    if not tid:
        return events
    task = backlog.find(data, tid)
    try:
        r = ask(cfg, tid, sc['name'], gh_fn, post_fn, env, failure=(task.get('patch') or {}).get('failure'), now=now)
    except (PatchError, scouts.ScoutError) as e:   # never stops the round; the next one tries the next task
        with backlog.locked(cfg) as d:
            t = backlog.find(d, tid)
            t['patch'] = {**(t.get('patch') or {}), 'error': str(e)[:300], 'request': None}
            t['notes'] = [*t.get('notes', []), f'The scout could not take it: {e}']
        return events + [f'{tid}: not sent to {sc["name"]}: {e}']
    return events + [f'{tid}: sent to {sc["name"]} as a patch task ({r["id"]})']
