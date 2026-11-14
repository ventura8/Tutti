"""ship: format -> commit -> checks -> push -> deploy, in the work tree you are standing in, and nowhere else.
Targeted checks by default (only what changed); the full checks only when asked (--full), for example before a release."""
import itertools
import os
import re
import shlex
import subprocess
import sys
import time

from . import config, remote, routing, worktree

_CHECK_NUMBER = itertools.count(1)


class ShipError(Exception):
    """A step refused or failed; the message says which, why, and what state the tree is in."""


def git(top, *args, check=True):
    r = subprocess.run([worktree.git_bin(), '-C', top, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise ShipError(f'git {" ".join(args)} failed in {top}: {r.stderr.strip() or r.stdout.strip()}')
    return r.stdout.strip()


def toplevel(directory):
    r = subprocess.run([worktree.git_bin(), '-C', directory, 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    if r.returncode != 0:
        raise ShipError(f'{directory} is not inside a git work tree: cd into the worktree you want to ship (nothing was changed)')
    return r.stdout.strip()


def guard_tree(cfg, top, track=None, allow_protected=False):
    """Refuses the wrong tree before anything is written: a named track must be the tree we stand in, and a protected branch
    is never committed to by accident."""
    branch = git(top, 'symbolic-ref', '--short', '-q', 'HEAD', check=False) or '(detached)'
    if track:
        want = os.path.join(config.expand(cfg['project']['worktrees']), track)
        if os.path.realpath(want) != os.path.realpath(top):
            raise ShipError(f'refused: --track {track} is {want}, but this directory belongs to {top}. cd into the track\'s '
                            'worktree and run ship again (nothing was changed)')
    if branch in cfg['ship']['protected'] and not allow_protected:
        raise ShipError(f'refused: {top} is on the protected branch "{branch}". Ship from a work branch, or pass '
                        '--allow-protected if you mean it (nothing was changed)')
    return branch


def message(text, trailer):
    return f'{text.strip()}\n\n{trailer.strip()}' if trailer else text.strip()


def pending(top):
    """Files changed or new in the work tree, not yet committed."""
    names = git(top, 'diff', '--name-only', 'HEAD', check=False).split('\n') + \
        git(top, 'ls-files', '--others', '--exclude-standard').split('\n')
    return sorted({n for n in names if n})


def changed_since(top, base):
    if not base:
        return []
    return [n for n in git(top, 'diff', '--name-only', '--diff-filter=d', f'{base}...HEAD').split('\n') if n]


def shell(top, command, env):
    """Runs `command` with bash in `top`. Tutti's variables are exported at the start of the bash line as well as put in
    the process environment: on Windows `bash` is often WSL, which does not inherit the Windows process environment.
    Exported, not prefixed: `K=v echo "$K"` expands $K before the assignment applies, so a check read it empty. The line
    goes through remote.argv, which sends it base64-encoded on Windows (WSL's bash.exe mangled its quotes)."""
    if env:
        exports = '; '.join(f'export {k}={shlex.quote(str(v))}' for k, v in env.items())
        command = f'{exports}; {command}'
    return subprocess.run(remote.argv({'transport': 'local'}, command), cwd=top, env={**os.environ, **env})


def run_check(cfg, top, check, env, track):
    """One check: here, or on a routed machine behind its slot queue. Returns (exit code, the machine or 'local')."""
    if check.get('where', 'local') != 'route':
        return shell(top, check['run'], env).returncode, 'local'
    machines = {n: config.merge(m, {'runner': check.get('runner') or {}}) for n, m in cfg['machines'].items()}
    choice = routing.choose(cfg['routing']['order'], machines, lambda n: remote.probe(machines[n]))
    return remote.run(cfg, machines[choice.machine], track, top, check['run']), choice.machine


def quiet_check(cfg, top, check, env, track):
    """The same, with the output kept in a log; only the verdict line (check.verdict, a regex) reaches the screen."""
    # <time>-<pid>-<n>: two checks started in the same second by one ship each keep their own log, result and command.
    log = os.path.join(config.state_dir(cfg), f'ship-{int(time.time())}-{os.getpid()}-{next(_CHECK_NUMBER)}.log')
    with open(log + '.cmd', 'w', encoding='utf8') as f:   # what ran, for the dashboard
        f.write(check['run'] + '\n')
    with open(log, 'w', encoding='utf8') as f:
        sys.stdout.flush()
        saved = os.dup(1), os.dup(2)
        try:
            os.dup2(f.fileno(), 1)
            os.dup2(f.fileno(), 2)
            rc, where = run_check(cfg, top, check, env, track)
        finally:
            sys.stdout.flush()
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
    with open(log + '.rc', 'w', encoding='utf8') as f:   # the dashboard reads the result beside the log
        f.write(f'{rc}\n')
    with open(log, encoding='utf8', errors='replace') as f:
        lines = f.read().splitlines()
    rx = re.compile(check.get('verdict') or r'$^')
    hits = [line for line in lines if rx.search(line)]
    return rc, where, log, (hits[-1] if hits else '')


def run_checks(cfg, top, checks, env, track, say):
    for check in checks:
        if check.get('quiet'):
            rc, where, log, verdict = quiet_check(cfg, top, check, env, track)
            say(f'check [{where}] rc={rc} {verdict} (log {log})')
        else:
            rc, where = run_check(cfg, top, check, env, track)
            say(f'check [{where}] rc={rc}: {check["run"][:80]}')
        if rc != 0:
            raise ShipError(f'the check `{check["run"][:120]}` failed (exit {rc}). The commit is kept locally and was NOT pushed: '
                            'fix the finding, then run ship again')


def plan(cfg, top, branch, text, full, no_add):
    s = cfg['ship']
    checks = s['full_checks'] if full else s['checks']
    push = s.get('push')
    lines = [f'tree    {top} (branch {branch})',
             f'share   {" ".join(s.get("share", [])) or "(none)"}',
             f'format  {s.get("format") or "(none)"}',
             f'add     {"(skipped: --no-add)" if no_add else " ".join(s["add"])}',
             f'commit  {message(text, s.get("trailer", "")).splitlines()[0]}' + (' (+ trailer)' if s.get('trailer') else ''),
             *[f'check   [{c.get("where", "local")}] {c["run"]}' for c in checks],
             f'push    {push["remote"]} HEAD:{push["branch"]}' if push else 'push    (none)',
             f'deploy  {s.get("deploy") or "(none)"}']
    return '\n'.join(lines)


def ship(cfg, directory, text, full=False, no_add=False, track=None, allow_protected=False, dry_run=False, say=print):
    s = cfg['ship']
    top = toplevel(directory)
    branch = guard_tree(cfg, top, track, allow_protected)
    if dry_run:
        say(plan(cfg, top, branch, text, full, no_add))
        return
    say(f'ship: {top} (branch {branch})')
    worktree.share(top, s.get('share', []), say)
    if s.get('format') and shell(top, s['format'], {'TUTTI_CHANGED': ' '.join(pending(top))}).returncode != 0:
        say('ship: the format step failed; going on, the checks will show why')
    if not no_add:
        paths = [p for p in s['add'] if os.path.exists(os.path.join(top, p))]
        if paths:
            git(top, 'add', '-A', '--', *paths)
    if subprocess.run([worktree.git_bin(), '-C', top, 'diff', '--cached', '--quiet']).returncode != 0:
        git(top, 'commit', '-q', '-m', message(text, s.get('trailer', '')))
    env = {'TUTTI_CHANGED': ' '.join(changed_since(top, s.get('base'))), 'TUTTI_COMMIT': git(top, 'rev-parse', 'HEAD'), 'TUTTI_TREE': top,
           'TUTTI_GIT_MOUNT': worktree.docker_mount(top)}
    run_checks(cfg, top, s['full_checks'] if full else s['checks'], env, track or os.path.basename(top), say)
    push = s.get('push')
    if push:
        git(top, 'push', '-q', push['remote'], f'HEAD:{push["branch"]}')
        say(f'pushed {git(top, "log", "--oneline", "-1")} to {push["remote"]}/{push["branch"]}')
    if s.get('deploy') and shell(top, s['deploy'], env).returncode != 0:
        raise ShipError('the deploy step failed after the push: the commit is on the remote, the deploy is not live. Read its '
                        'output above and run the deploy command again')
