"""Linked worktrees made usable for checks: a working git binary, the main checkout's dependency folders shared by hard
links, and the docker mount that lets git inside a container reach the main repository's .git."""
import glob
import os
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


def share(top, patterns, say=print):
    """Hard-links (cp -al) each folder matching `patterns` from the main checkout into the worktree when it is missing there.
    Hard links cost no space; a later install in the worktree replaces files instead of writing through them."""
    main = main_checkout(top)
    if not main or os.path.realpath(main) == os.path.realpath(top):
        return []
    done = []
    for pat in patterns:
        for src in sorted(glob.glob(os.path.join(main, pat))):
            rel = os.path.relpath(src, main)
            dst = os.path.join(top, rel)
            if os.path.isdir(src) and not os.path.exists(dst) and os.path.isdir(os.path.dirname(dst)):
                subprocess.run(['cp', '-al', src, dst], check=True)
                done.append(rel)
    if done:
        say(f'worktree: shared {", ".join(done)} from {main} (hard links)')
    return done


def docker_mount(top):
    """`-v <common>:<common>` for a linked worktree (git inside the container needs the main .git), else ''."""
    c = common_dir(top)
    return f'-v {c}:{c}' if c else ''
