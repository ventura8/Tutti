"""`tutti run <worker> "<task>" --repo <repo>`: a bounded code task handed to a cheap worker end to end. A worktree of the repo
(created, or reused by name), the worker run headless in it, the diff captured, the targeted checks run on the diff, and one
report the orchestrator can review: what changed, the diff's path, every check's verdict and what was not checked.
The worker never commits; the orchestrator applies or discards the diff."""
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import yaml

from . import compact, config, remote, ship, workers, worktree

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


def resolve_repo(cfg, repo):
    """`--repo` as a folder, or a name under autopilot.repos: the path whose worktree the run creates. A bare name always
    comes from the config, so the run works from a non-git cwd (MCP `delegate_task`, `tutti run --repo tutti`)."""
    entry = ((cfg.get('autopilot') or {}).get('repos') or {}).get(repo)
    if isinstance(entry, dict) and entry.get('path'):
        return config.expand(entry['path'])
    return repo


def fresh_base(top, say=print, base=None):
    """The commit a new run starts from: the branch's upstream after a fetch when the local checkout is behind it (a stale
    checkout made workers redo old code and write broken tests), else HEAD (ahead or diverged: local commits are kept).
    No remote, no upstream or no network: HEAD, said. `base` (`tutti run --base origin/<branch>`): that ref after a fetch of
    its remote, so a task that names its branch starts there whatever the checkout's own state."""
    if base:
        return named_base(top, base, say)
    if ship.git(top, 'remote', check=False) == '':
        return 'HEAD'
    upstream = ship.git(top, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}', check=False)
    if not upstream or ' ' in upstream:
        return 'HEAD'
    remote = upstream.split('/', 1)[0]
    if subprocess.run([worktree.git_bin(), '-C', top, 'fetch', '-q', remote], capture_output=True, timeout=120).returncode != 0:
        say(f'run: could not fetch {remote}; starting from the local HEAD')
        return 'HEAD'
    behind = subprocess.run([worktree.git_bin(), '-C', top, 'merge-base', '--is-ancestor', 'HEAD', upstream]).returncode == 0
    if behind and ship.git(top, 'rev-parse', 'HEAD') != ship.git(top, 'rev-parse', upstream):
        say(f'run: the local checkout is behind {upstream}; starting from {upstream}')
        return upstream
    return 'HEAD'


def named_base(top, base, say=print):
    """The commit `--base <ref>` names, after fetching its remote when it is a remote ref. A ref that cannot be resolved
    stops the run with what to do: never a silent start from somewhere else."""
    if not re.fullmatch(r'[A-Za-z0-9._/-]{1,200}', base) or base.startswith('-') or '..' in base:
        raise ship.ShipError(f'refused: --base "{base}" is not a branch or ref name (nothing was changed)')
    remote = base.split('/', 1)[0] if '/' in base else ''
    if remote and remote in ship.git(top, 'remote', check=False).split():
        if subprocess.run([worktree.git_bin(), '-C', top, 'fetch', '-q', remote], capture_output=True, timeout=120).returncode:
            say(f'run: could not fetch {remote}; using the local copy of {base}')
    sha = ship.git(top, 'rev-parse', '--verify', '--quiet', f'{base}^{{commit}}', check=False)
    if not sha:
        raise ship.ShipError(f'refused: --base {base} is not a commit in {top}: check the name (`git -C {top} branch -a`) '
                             '(nothing was changed)')
    say(f'run: starting from {base} ({sha[:8]})')
    return sha


def track_name(prefix, name):
    """--track as given: a run id, a track name, or the worktree's full name or path (<prefix><id>), which resolves to the
    same worktree instead of a new empty <prefix><prefix><id>."""
    name = os.path.basename(str(name).rstrip('/')) if name else name
    while name and prefix and name.startswith(prefix) and len(name) > len(prefix):
        name = name[len(prefix):]
    return name


def tree_for(cfg, repo, run_id, name=None, say=print, base=None):
    """The worktree the worker edits: <project.worktrees>/<delegate.prefix><name or id>, created detached at the repo's HEAD,
    or reused when it already is a worktree of the same repository. Returns (path, created)."""
    top = ship.toplevel(resolve_repo(cfg, repo))
    root = config.expand(cfg['project']['worktrees'])
    name = track_name(cfg['delegate']['prefix'], name)
    wt = os.path.join(root, f'{cfg["delegate"]["prefix"]}{name or run_id}')
    if os.path.exists(os.path.join(wt, '.git')):
        if os.path.realpath(worktree.main_checkout(wt) or wt) != os.path.realpath(worktree.main_checkout(top) or top):
            raise ship.ShipError(f'refused: {wt} exists but belongs to another repository; pick another --track name')
        say(f'run: reusing the worktree {wt}')
        return wt, False
    if os.path.exists(wt):
        raise ship.ShipError(f'refused: {wt} exists and is not a git worktree; move it away or pick another --track name')
    os.makedirs(root, exist_ok=True)
    base = fresh_base(top, say, base)
    ship.git(top, 'worktree', 'add', '-q', '--detach', wt, base)
    say(f'run: new worktree {wt} at {ship.git(wt, "log", "--oneline", "-1")}')
    return wt, True


def capture_diff(wt, diff_path):
    """Everything the worker changed or added against HEAD, as one patch; the index is restored afterwards.
    Returns (changed paths, the --stat text)."""
    ship.git(wt, 'add', '-A')
    try:
        names = ship.names(wt, 'diff', '--cached', '--name-only', 'HEAD')
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


READ_DENIED = re.compile(r'read_?file', re.I)


def read_denied(found):
    """True when a refused tool call (from denials()) was a file read, the one refusal that is safe to grant and retry."""
    return any(READ_DENIED.search(t) for t in found)


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


def mark_base_broken(checks, base_results):
    """Pure: a check that failed on the run and failed the same way on the base is `base_broken` (the change is not to blame).
    `base_results` maps a check's index to the rc it had on the base; a failed check with no entry there is left unmarked."""
    for i, c in enumerate(checks):
        if c['rc'] != 0 and base_results.get(i, 0) != 0:
            c['base_broken'] = True
            c['base_rc'] = base_results[i]
    return checks


def base_checks(cfg, wt, chosen, results, env, track, say=print, folder=None):
    """The checks of `results` that failed, run once more on the run's base commit (HEAD of the worktree: the worker's edits
    are uncommitted) in a temporary clean detached worktree with the shared dependency folders, removed afterwards. Marks the
    ones that fail there too `base_broken`. A base that cannot be prepared marks nothing (the log says so)."""
    failed = [i for i, c in enumerate(results) if c['rc'] != 0]
    if not failed or not cfg['delegate'].get('base_check', True):
        return results
    tmp = tempfile.mkdtemp(prefix='tutti-base-')
    added = False
    try:
        base = ship.git(wt, 'rev-parse', 'HEAD').strip()
        ship.git(wt, 'worktree', 'add', '-q', '--detach', tmp, base)
        added = True
        worktree.share(tmp, cfg['ship'].get('share', []), say)
        say(f'run: {len(failed)} check(s) failed; running them on the base {base[:10]} to see whether the change is to blame')
        base_env = {**env, 'TUTTI_TREE': tmp, 'TUTTI_GIT_MOUNT': worktree.docker_mount(tmp)}
        on_base = run_checks(cfg, tmp, [chosen[i] for i in failed], base_env, track, say, folder)
        mark_base_broken(results, {i: b['rc'] for i, b in zip(failed, on_base, strict=True)})
    except (ship.ShipError, OSError, subprocess.SubprocessError) as e:
        say(f'run: could not check the base commit ({e}); the failed checks are not compared with it (delegate.base_check: false '
            'turns this off)')
    finally:
        if added:
            subprocess.run([worktree.git_bin(), '-C', wt, 'worktree', 'remove', '--force', tmp], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return results


def default_checks(cfg):
    """The config's own checks and their key: delegate.checks, else ship.checks."""
    if cfg['delegate']['checks'] is not None:
        return cfg['delegate']['checks'], 'delegate.checks'
    return cfg['ship']['checks'], 'ship.checks'


def repo_config_checks(cfg, top):
    """(checks, path) from a tutti.config.* inside the target repository (not this config itself), else None."""
    local = next((os.path.join(top, n) for n in config.NAMES if os.path.isfile(os.path.join(top, n))), None)
    if not local or os.path.abspath(local) == os.path.abspath(cfg.get('_path') or ''):
        return None
    try:
        theirs = config.load(local)
    except (config.ConfigError, ValueError, yaml.YAMLError) as e:
        return [], f'none: {local} is not a valid config ({e}); fix it to run its checks'
    return default_checks(theirs)[0], local


def checks_for(cfg, repo):
    """(checks, where they come from) for a run in `repo`. A repository's checks belong to it, so the config's default
    checks never run on another project's code: the project's own `projects.<name>.checks`, else a tutti.config.* inside
    the target repository, else the default (delegate.checks or ship.checks) only when the repository is the one they are
    for (`delegate.project`, or no delegate.project at all); otherwise none, and the report says how to give it some."""
    from . import projects
    found = projects.identify(os.path.abspath(repo)) or {}
    name = projects.rename(cfg, found) if found else None
    own = (projects.configured(cfg).get(name) or {}).get('checks') if name else None
    if own is not None:
        return own, f'projects.{name}.checks'
    theirs = repo_config_checks(cfg, found.get('folder') or os.path.abspath(repo))
    if theirs:
        return theirs
    home = cfg['delegate'].get('project')
    if home and name != home:
        return [], (f'none for {name or repo}: the default checks are for {home} (delegate.project); give this repository its own '
                    f'with projects.{name or "<name>"}.checks in the config')
    return default_checks(cfg)


def checks_note(chosen, source):
    """What the report says when no check ran: why none was chosen."""
    if chosen or source.startswith('none'):
        return source
    return f'none configured ({source})'


FEEDBACK_LINES = 40


def _tail(path, n=FEEDBACK_LINES):
    try:
        with open(path, encoding='utf8', errors='replace') as f:
            return f.read().splitlines()[-n:]
    except (OSError, TypeError):
        return []


def fix_task(task, failed, round_no, rounds, tail=_tail):
    """The task for a fix round: the original task, then each failing check's command and the end of its output, so the
    same worker fixes what the check found in the worktree it already changed (self-care: a small check error should not
    lose a good diff)."""
    parts = [task, '', f'--- Fix round {round_no} of {rounds} ---',
             'Your change is in this worktree, but these checks fail on it. Fix only what they report; keep the rest of your '
             'change, do not revert it, and do not weaken or skip a check.']
    for c in failed:
        lines = tail(c.get('log')) or [c.get('verdict') or '']
        parts += ['', f'$ {c["run"]}  (exit {c["rc"]})', *lines]
    return '\n'.join(parts)


NOT_CODE = re.compile(r'timed? ?out|timeout|unreachable|not reached|no machine|connection (?:refused|timed out)', re.I)
NOT_CODE_RC = (124, 255)   # `timeout`'s exit and ssh's own failure: the check did not judge the code


def code_failure(c):
    """True when a failed check judged the code. A check that timed out, or that never ran because its machine was not
    reached (where 'none', ssh's 255, a verdict saying so), says nothing about the change: no fix round for it."""
    if c['rc'] == 0:
        return False
    return not (c.get('where') == 'none' or c['rc'] in NOT_CODE_RC or NOT_CODE.search(c.get('verdict') or ''))


def fix_rounds(cfg, name, task, files, repo, wt, log, extra, p, check_list, chosen, env, remote_track, r, say,
               work=None, check=None):
    """Up to delegate.fix_rounds times: a failing check's errors go back to the same worker in the same worktree, then the
    checks run again; the run is reported FAILED only when the last round still fails (a local model checks itself). A check
    that also fails on the base (`base_broken`, marked before the rounds) is not sent back: the change is not to blame, so
    when every failing check is base-broken no round runs.
    The diff before round 1 is kept as <diff>.round0 (and moved on after each good round): a round whose worker fails, or
    leaves nothing, never loses it. A check that failed for a reason other than the code (timeout, machine unreachable) is
    not sent back."""
    work, check = work or run_worker, check or run_checks
    rounds = int(cfg['delegate'].get('fix_rounds') or 0) if cfg['workers'][name]['kind'] != 'ollama' else 0
    r['fix_rounds'] = 0
    on_base = {i: c['base_rc'] for i, c in enumerate(r['checks']) if c.get('base_broken')}
    good = p['diff'] + '.round0'
    for n in range(1, rounds + 1):
        failed = [c for c in r['checks'] if c['rc'] != 0 and not c.get('base_broken')]
        if not failed:
            return
        failed = [c for c in failed if code_failure(c)]
        if not failed:
            say(f'run: fix round {n}: the failing check(s) timed out or could not reach their machine, not a code failure; '
                'no fix round (run the checks again)')
            return
        say(f'run: fix round {n} of {rounds}: {len(failed)} check(s) failed; their errors go back to {name}')
        if os.path.isfile(p['diff']):
            shutil.copyfile(p['diff'], good)
        rc, _ = work(cfg, name, fix_task(task, failed, n, rounds), files, wt, log, extra, p['worker_log'], check_list)
        r['fix_rounds'] = n
        changed, stat = diff_after_fix(cfg, repo, wt, p['diff'], True, say)   # the diff file matches the worktree again
        if not changed:
            if os.path.isfile(good):
                shutil.copyfile(good, p['diff'])
            say(f'run: fix round {n}: {name} exited {rc} and left no change; the last good diff and checks stand')
            return
        r['changed'], r['stat'] = changed, stat
        if rc != 0:
            say(f'run: fix round {n}: {name} exited {rc}; the diff is the worktree\'s, the last checks stand')
            return
        env['TUTTI_CHANGED'] = ship.join_changed(r['changed'])
        r['checks'] = mark_base_broken(check(cfg, wt, chosen, env, remote_track, say, p['dir']), on_base)


def outcome(r):
    """The report's first line: PASSED, or FAILED and why, so a run that changed nothing never reads as a success."""
    if r.get('error'):
        return f'FAILED: the run stopped before its checks: {r["error"]}'
    if r['worker_rc'] != 0:
        return f'FAILED: the worker exited {r["worker_rc"]}'
    if r['denied']:
        return 'FAILED: the worker\'s CLI refused tool calls (below)'
    if not r['changed']:
        return 'FAILED: the worker changed nothing (exit 0 is not a fix); run it again or give it to another worker'
    bad = [c for c in r['checks'] or [] if c['rc'] != 0]
    broken = [c for c in bad if c.get('base_broken')]
    if bad and len(broken) == len(bad):
        return f'FAILED: {len(bad)} check(s) fail on the base too (not this change): fix the base branch'
    if bad:
        return f'FAILED: {len(bad)} check(s) failed' + (f' ({len(broken)} of them fail on the base too)' if broken else '')
    return 'PASSED' if r['checks'] else 'CHANGED, NOT TESTED: no check ran; review the diff before using it'


def last_words(lines, worker_log=None, n=8):
    """The end of what the worker said (its final message): why a run that changed nothing stopped, e.g. "the worktree
    is on the wrong commit" or "the file is already covered". From its last output lines, else its log."""
    text = [x.rstrip() for x in (lines or []) if x and x.strip()]
    if not text and worker_log:
        try:
            with open(worker_log, encoding='utf8', errors='replace') as f:
                text = [x.rstrip() for x in f.read().splitlines()[-200:] if x.strip()]
        except OSError:
            text = []
    text = [x for x in text if not x.startswith(('tutti:', 'run:', 'tokens used')) and not x.replace(',', '').isdigit()]
    return [x[:300] for x in text[-n:]]


def summary(r):
    """The reviewable report, plain text."""
    lines = [f'result    {outcome(r)}', f'run {r["id"]}: {r["worker"]} exit {r["worker_rc"]}; {len(r["changed"])} file(s) changed',
             f'task      {r["task"][:200]}', f'worktree  {r["worktree"]}',
             f'diff      {r["diff"]}' + (' (empty)' if not r['changed'] else '')]
    if r['stat']:
        lines += ['', r['stat']]
    if not r['changed'] and r.get('worker_said'):   # why it stopped, in its own words: the reason a person needs
        lines += ['', 'worker said (its last lines):', *[f'  {x}' for x in r['worker_said']]]
    if r['denied']:
        lines += ['', f'DENIED: the worker\'s CLI refused {len(r["denied"])} tool call(s); the task may be incomplete. Allow the commands '
                      'it needs in the worker\'s headless settings (docs/WORKERS.md), then run again:']
        lines += [f'  {d}' for d in r['denied'][:5]]
    if r['checks'] is None:
        lines += ['', 'checks    skipped (--no-checks): nothing was tested']
    elif not r['checks']:
        lines += ['', f'checks    {r.get("checks_from") or "none configured (delegate.checks or ship.checks)"}: nothing was tested']
    if r.get('fix_rounds'):
        lines.append(f'fixes     {r["fix_rounds"]} round(s): the failing checks\' errors went back to the worker')
    for c in r['checks'] or []:
        lines.append(f'check     [{c["where"]}] rc={c["rc"]} {c["verdict"] or ""} (log {c["log"]})'.rstrip())
        if c.get('base_broken'):
            lines.append('          base_broken: it fails on the base commit too; the worker\'s diff is not blamed')
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


def delegate(cfg, name, task, files, repo, run_id=None, track=None, extra=(), checks=True, discard=False, say=None, check_list=None,
             remote_track=None, base=None):
    """The whole hand-off. Returns the report dict (also written to report.json and report.txt). Runs that name the same
    --track share one worktree, so they take turns: the second waits until the first has its report. `check_list` replaces the
    configured checks (`tutti run --check`: the target repo's own check); `remote_track` the folder routed checks copy into."""
    if track and not remote.valid_track(track):
        raise ship.ShipError(f'refused: --track "{track}" must be one folder name (letters, digits, . _ -), nothing was changed')
    track = track_name(cfg['delegate']['prefix'], track) or None   # the full worktree name is the same track: lock, job, copy
    if checks:
        effective_track = remote_track or track or cfg['delegate']['remote_track']
        if not remote.valid_remote_track(effective_track):
            raise ship.ShipError('refused: --remote-track or delegate.remote_track must be one folder name of 1 to 64 letters, '
                                 'digits, dots, underscores or dashes, starting with a letter or digit and not repo.git or '
                                 'tutti-slot.sh; choose another and retry')
    lock = track_lock(cfg, f'tree-{track}', say or (lambda m: sys.stderr.write(f'tutti: {m}\n'))) if track else contextlib.nullcontext()
    with lock:
        return _delegate(cfg, name, task, files, repo, run_id, track, extra, checks, discard, say, check_list, remote_track, base)


def run_worker(cfg, name, task, files, wt, log, extra, worker_log, check_list, reads=()):
    """(exit code, last lines) of the worker in the worktree. A local model (kind ollama) works through `tutti agent`'s loop on
    the given files with the first check as its own check."""
    if cfg['workers'][name]['kind'] != 'ollama':
        return run_compacting(cfg, name, task, files, wt, log, extra, worker_log, reads=reads)
    from . import fleet, local
    from . import workers as workers_mod
    check = (check_list or [{}])[0].get('run') or 'true'
    w = cfg['workers'][name]
    # the model the scheduler chose (`--extra=--model --extra=<name>`, tutti/autopilot.py), else the one that suits the task's
    # first line; its files in steps the model can hold at once. A failed step ends the run: later steps never build on it
    chosen = dict(workers_mod.pairs(list(extra or ()))).get('--model')
    model = next((m for m in w.get('models') or [] if m and m['name'] == chosen), None) or \
        fleet.model_for(w, {'title': task.splitlines()[0] if task else '', 'scope': list(files), 'size': 'S'}) or {}
    worker = {**w, 'model': chosen or model.get('name') or w['model'], '_name': name}
    parts = fleet.steps(files, fleet.files_per_step(model))
    ok = False
    with contextlib.redirect_stdout(log):
        for i, part in enumerate(parts, 1):
            step_task = task if len(parts) == 1 else f'{task}\n\n(Step {i} of {len(parts)}: change only these files now.)'
            ok = local.agent(cfg, worker, step_task, check, part, base=wt)
            if not ok:
                print(f'step {i} of {len(parts)} failed: the run stops here', flush=True)
                break
    return (0 if ok else 1), []


def run_worker_retrying(cfg, name, task, files, wt, log, extra, worker_log, check_list, say):
    """run_worker; when the CLI refused a file read, once more in the same worktree with Tutti's runs folder (logs and diffs of
    runs, nothing else) readable: the brief's own paths are not always the ones the grant found. A refused command is never
    retried wider; it stays a DENIED result."""
    rc, lines = run_worker(cfg, name, task, files, wt, log, extra, worker_log, check_list)
    found = denials(cfg['workers'][name], lines, worker_log)
    if not read_denied(found):
        return rc, lines
    say(f'run: the worker\'s CLI was refused a file read ({found[0][:100]}); running it once more with {runs_dir(cfg)} readable')
    write(worker_log, '')   # the retry's own log, so a refusal that is gone is not reported again
    return run_worker(cfg, name, task, files, wt, log, extra, worker_log, check_list, reads=[runs_dir(cfg)])


def run_compacting(cfg, name, task, files, wt, log, extra, worker_log, run=workers.run, reads=()):
    """The worker's run; when it is stopped at the autocompact threshold, started again in the same worktree with a handoff
    note (the changes, its last output) instead of its grown context, at most autocompact.RESTARTS times."""
    from . import autocompact, conduct  # conduct imports this module
    rc, lines = run(cfg, name, task, files, cwd=wt, out=log, extra=extra, log=worker_log,
              **({'reads': reads} if reads else {}))
    for n in range(autocompact.RESTARTS):
        if rc != workers.COMPACT_RC:
            break
        why = f'its context reached autocompact.threshold_pct (restart {n + 1} of at most {autocompact.RESTARTS})'
        last = compact.compact('\n'.join(lines), 2000).splitlines()
        note = conduct.handoff_note(task, name, name, why, '', conduct.tree_changes(wt), last)
        log.write(f'tutti: run: compacted: {why}\n')
        rc, lines = run(cfg, name, autocompact.restart_task(task, note), files, cwd=wt, out=log, extra=extra, log=worker_log,
              **({'reads': reads} if reads else {}))
    return rc, lines


def fix_for(cfg, repo):
    """`autopilot.repos.<r>.fix` of the repo at `repo`: formatters and generators run before the check, or ''."""
    here = os.path.realpath(repo)
    for r in ((cfg.get('autopilot') or {}).get('repos') or {}).values():
        if isinstance(r, dict) and r.get('path') and os.path.realpath(config.expand(r['path'])) == here:
            return str(r.get('fix') or '')
    return ''


def apply_fix(cfg, repo, wt, changed, say):
    """Runs the repo's fix step over the change (prettier --write, regenerated docs) so a style-only failure does not fail
    the run; a failing fix is said and the check then reports what is still wrong. True when it ran."""
    fix = fix_for(cfg, repo)
    changed = [n for n in changed or [] if os.path.exists(os.path.join(wt, n))]   # a deleted file would fail the formatter
    if not fix or not changed:
        return False
    r = ship.shell(wt, ship.expand_changed(fix, changed), {'TUTTI_CHANGED': ship.join_changed(changed), 'TUTTI_TREE': wt})
    say('run: fix step done (autopilot.repos.<repo>.fix)' if r.returncode == 0 else
        f'run: the fix step exited {r.returncode} (autopilot.repos.<repo>.fix); the check says what is left')
    return True


def diff_after_fix(cfg, repo, wt, diff, checks, say):
    """(changed, stat) of the run, taken again after the repo's fix step when it ran."""
    changed, stat = capture_diff(wt, diff)
    if checks and apply_fix(cfg, repo, wt, changed, say):
        changed, stat = capture_diff(wt, diff)
    return changed, stat


def chosen_checks(cfg, repo, check_list):
    """(checks, where they come from): `tutti run --check` (the autopilot's task check) beats the target repo's own."""
    if check_list is not None:
        return check_list, '--check'
    return checks_for(cfg, repo)


def accounts_of_run(cfg, name, extra=()):
    """The accounts a run of worker `name` spends: the account of that name, else those naming the worker; of several (agy's
    Claude and Gemini buckets) the ones whose `args` the run was given (`extra`), else all of them."""
    if name in cfg['accounts']:
        return [name]
    found = [n for n, a in cfg['accounts'].items() if (a or {}).get('worker') == name]
    given = [n for n in found if (cfg['accounts'][n] or {}).get('args') and all(x in extra for x in cfg['accounts'][n]['args'])]
    return given or found


def failure_report(cfg, run_id, name, task, error, say=print):
    """The report of a run that stopped on an error before it had its own (a worktree that would not build, a worker that
    could not start): rc 1 and the reason, so `tutti runs show` and the dashboard never find a failed run with no report."""
    p = paths(cfg, run_id)
    r = {'id': run_id, 'worker': name, 'task': task, 'worker_rc': 1, 'worktree': (read_meta(cfg, run_id) or {}).get('worktree') or '(none)',
         'created': False, 'diff': p['diff'], 'changed': [], 'stat': '', 'denied': [], 'checks': None, 'report': p['report'],
         'error': error, 'rc': 1}
    try:
        write(p['report'], summary(r) + f'\n\nlog       {p["log"]}\n')
        write(p['json'], json.dumps(r, indent=2) + '\n')
    except OSError as e:
        say(f'run: could not write the failure report for {run_id} ({e}); the reason is in {p["log"]}')
    return r


def _delegate(cfg, name, task, files, repo, run_id, track, extra, checks, discard, say, check_list=None, remote_track=None, base=None):
    if run_id and not valid_id(run_id):
        raise ship.ShipError(f'refused: run id "{run_id}" is not <time>-<pid>: let tutti choose it (nothing was changed)')
    run_id = run_id or new_id(cfg)
    try:
        return _delegate_run(cfg, name, task, files, repo, run_id, track, extra, checks, discard, say, check_list, remote_track, base)
    except Exception as e:   # the report is the record of a failed run: it keeps the reason, then the error goes on
        if not os.path.exists(paths(cfg, run_id)['json']):
            failure_report(cfg, run_id, name, task, f'{type(e).__name__}: {e}', say or print)
        raise


def _delegate_run(cfg, name, task, files, repo, run_id, track, extra, checks, discard, say, check_list=None, remote_track=None, base=None):
    p = paths(cfg, run_id)
    os.makedirs(p['dir'], exist_ok=True)
    write(p['cmd'], f'run {name}: {task.splitlines()[0][:150]}\n')
    write(p['pid'], f'{os.getpid()}\n')
    # what a dashboard "pause, stop now" of this worker, or of the account it spends, stops
    write(p['log'] + '.target', ' '.join([f'worker:{name}', *[f'account:{n}' for n in accounts_of_run(cfg, name, extra)]]) + '\n')
    log = open(p['log'], 'a', encoding='utf8')   # closed in finally: the log outlives the say() closure
    say = say or (lambda m: (sys.stderr.write(f'tutti: {m}\n'), log.write(m + '\n'), log.flush()))
    try:
        repo = resolve_repo(cfg, repo)
        wt, created = tree_for(cfg, repo, run_id, track, say, base)
        # how a "pause, stop now" hands this run over (tutti/handover.py): the same task, worktree and checks
        write(p['log'] + '.job', json.dumps({'kind': 'run', 'worker': name, 'task': task, 'repo': os.path.abspath(repo),
                                             'track': track or run_id, 'run_id': run_id, 'files': list(files or []),
                                             'checks': [c for c in check_list or [] if c.get('run')]}) + '\n')
        # what the run works on, for the dashboard's projects while it runs
        write(os.path.join(p['dir'], 'meta.json'), json.dumps({'repo': os.path.abspath(repo), 'worktree': wt}) + '\n')
        worktree.share(wt, cfg['ship'].get('share', []), say)
        rc, lines = run_worker_retrying(cfg, name, task, files, wt, log, extra, p['worker_log'], check_list, say)
        changed, stat = diff_after_fix(cfg, repo, wt, p['diff'], checks, say)
        found = denials(cfg['workers'][name], lines, p['worker_log'])
        r = {'id': run_id, 'worker': name, 'task': task, 'worker_rc': rc, 'worktree': wt, 'created': created, 'diff': p['diff'],
             'changed': changed, 'stat': stat, 'denied': found, 'checks': None, 'report': p['report']}
        r['worker_said'] = last_words(lines, p['worker_log'])
        chosen, source = chosen_checks(cfg, repo, check_list)
        from . import remote
        identity = os.environ.get('TUTTI_RUN_IDENTITY')
        if remote.valid_run_identity(identity):
            r['run_identity'] = identity
        r['checks_from'] = checks_note(chosen, source)
        if checks and changed and chosen:
            env = {'TUTTI_CHANGED': ship.join_changed(changed), 'TUTTI_TREE': wt, 'TUTTI_GIT_MOUNT': worktree.docker_mount(wt)}
            # Default: unique per-run remote copy (host-wide repo.git stays shared). Explicit --remote-track or --track
            # keeps a named folder (and its lock) so intentional sharing still works (t403, with t402 local bypass).
            isolated = remote_track is None and track is None
            remote_track = remote_track or track or remote.copy_name(cfg['delegate']['remote_track'], run_id)
            say(f'run: checking the change ({len(chosen)} check(s))')   # the dashboard's phase marker
            # Only routed checks touch a remote copy. Local checks run in this run's own worktree (t402).
            lock = track_lock(cfg, remote_track, say) if any(c.get('where', 'local') == 'route' for c in chosen) \
                else contextlib.nullcontext()
            with lock:
                r['checks'] = run_checks(cfg, wt, chosen, env, remote_track, say, p['dir'])
                base_checks(cfg, wt, chosen, r['checks'], env, remote_track, say, p['dir'])
                fix_rounds(cfg, name, task, files, repo, wt, log, extra, p, check_list, chosen, env, remote_track, r, say)
            if isolated:
                used = {c['where'] for c in r['checks'] if c.get('where') not in (None, 'local', 'none')}
                for machine_name in sorted(used):
                    machine = cfg['machines'].get(machine_name)
                    if not machine:
                        continue
                    try:
                        remote.cleanup_copy(cfg, machine, remote_track)
                    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                        say(f'run: could not remove remote copy "{remote_track}" on {machine_name} ({e}); '
                            'remove it by hand if it stays (cleanup does not prove remote writers stopped)')
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
    """{worker: number of runs since local midnight today (time.zone, daylight saving included)}."""
    from . import tz
    now = now or time.time()
    day = tz.to_utc(tz.local_now(now).replace(hour=0, minute=0, second=0, microsecond=0), tz.zone()).timestamp()
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


def read_meta(cfg, run_id):
    """What a run works on, written when it starts: {repo, worktree}; {} when not (yet) written."""
    try:
        with open(os.path.join(paths(cfg, run_id)['dir'], 'meta.json'), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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
