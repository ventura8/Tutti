"""Linked worktrees made usable for checks: a working git binary, the main checkout's dependency folders shared by hard
links, and the docker mount that lets git inside a container reach the main repository's .git."""
import glob
import os
import shlex
import shutil
import subprocess

# A wrapper git first on PATH (an agent runtime's) can lack a working https helper; /usr/bin/git is the system's.
SYSTEM_GIT = ('/usr/bin/git', '/bin/git')


def _works(path):
    try:
        return subprocess.run([path, '--version'], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def git_bin(env=None):
    """TUTTI_GIT when set, else the system git, else whatever `git` is on PATH."""
    env = os.environ if env is None else env
    if env.get('TUTTI_GIT'):
        return env['TUTTI_GIT']
    for p in SYSTEM_GIT:
        if os.path.exists(p) and _works(p):
            return p
    return shutil.which('git') or 'git'


def _rev(top, *args):
    r = subprocess.run([git_bin(), '-C', top, 'rev-parse', '--path-format=absolute', *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ''


def common_dir(top):
    """The main repository's .git when `top` is a linked worktree, else ''."""
    if not os.path.isfile(os.path.join(top, '.git')):
        return ''
    return _rev(top, '--git-common-dir')


def main_checkout(top):
    c = common_dir(top)
    return os.path.dirname(c) if c and os.path.basename(c) == '.git' else ''


def _copy(src, dst):
    """cp -al into a temporary name, then rename; a plain copy when hard links cannot cross filesystems. Never leaves a
    partial destination behind."""
    tmp = f'{dst}.tutti-tmp'
    for args in (['cp', '-al'], ['cp', '-a']):
        shutil.rmtree(tmp, ignore_errors=True)
        if subprocess.run([*args, src, tmp], capture_output=True).returncode == 0:
            os.rename(tmp, dst)
            return True
    shutil.rmtree(tmp, ignore_errors=True)
    return False


LOCKS = ('package-lock.json', 'pnpm-lock.yaml', 'yarn.lock')


def lock_differs(main, top, rel):
    """True when the lockfile next to `rel` (a node_modules folder) differs between the main checkout and the worktree: the
    main checkout's packages would be the wrong ones (a branch that changed its dependencies)."""
    base = os.path.dirname(rel)
    for name in LOCKS:
        a, b = os.path.join(main, base, name), os.path.join(top, base, name)
        if os.path.exists(a) or os.path.exists(b):
            try:
                with open(a, 'rb') as fa, open(b, 'rb') as fb:
                    return fa.read() != fb.read()
            except OSError:
                return True
    return False


INSTALLERS = {'package-lock.json': ['npm', 'ci', '--prefer-offline', '--no-audit', '--no-fund'],
              'pnpm-lock.yaml': ['pnpm', 'install', '--frozen-lockfile', '--prefer-offline'],
              'yarn.lock': ['yarn', 'install', '--frozen-lockfile']}
INSTALL_TIMEOUT = 900


def install(top, say=print, run=subprocess.run):
    """The worktree's own install, by its lockfile (npm ci, pnpm or yarn), bounded by INSTALL_TIMEOUT; False when the tool
    is missing, the install failed or timed out, said with the fix."""
    lock = next((n for n in LOCKS if os.path.exists(os.path.join(top, n))), 'package-lock.json')
    argv = INSTALLERS[lock]
    if not shutil.which(argv[0]):
        say(f'worktree: the lockfile differs from the main checkout and {argv[0]} is not installed; run {" ".join(argv[:2])} in {top}')
        return False
    try:
        r = run(argv, cwd=top, capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        say(f'worktree: {" ".join(argv[:2])} took over {INSTALL_TIMEOUT} s in {top} (registry or network?); run it there by hand')
        return False
    if r.returncode != 0:
        say(f'worktree: {" ".join(argv[:2])} failed in {top}: {(r.stderr or r.stdout).strip()[-300:]}; fix the lockfile, then '
            'run it again')
        return False
    say(f'worktree: installed its own dependencies ({" ".join(argv[:2])}): its lockfile differs from the main checkout')
    return True


def share(top, patterns, say=print, run=subprocess.run):
    """Hard-links (cp -al) each folder matching `patterns` from the main checkout into the worktree when it is missing there.
    Hard links cost no space; a later install in the worktree replaces files instead of writing through them."""
    main = main_checkout(top)
    if not main or os.path.realpath(main) == os.path.realpath(top):
        return []
    done, stale = [], []
    for pat in patterns:
        for src in sorted(glob.glob(os.path.join(main, pat))):
            rel = os.path.relpath(src, main)
            dst = os.path.join(top, rel)
            if os.path.isdir(src) and not os.path.exists(dst) and os.path.isdir(os.path.dirname(dst)):
                if os.path.basename(rel) == 'node_modules' and lock_differs(main, top, rel):
                    stale.append(rel)   # the main checkout's packages are the wrong ones for this branch
                elif _copy(src, dst):
                    done.append(rel)
    if done:
        say(f'worktree: shared {", ".join(done)} from {main} (hard links)')
    if 'node_modules' in stale:
        install(top, say, run)
    elif stale:
        say(f'worktree: not shared (their lockfile differs from the main checkout): {", ".join(stale)}; run npm ci there')
    return done


def docker_mount(top):
    """`-v <common>:<common>` for a linked worktree (git inside the container needs the main .git), else ''."""
    c = common_dir(top)
    return f'-v {shlex.quote(f"{c}:{c}")}' if c else ''
