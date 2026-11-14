"""`tutti doctor`: checks of the machine Tutti runs on, one line per problem with the fix."""
import os
import shutil
import subprocess

# what the dynamic loader prints when a program's libraries do not match (a git bundled with another tool, say)
LOADER_ERRORS = ('error while loading shared libraries', 'symbol lookup error', 'version `', 'cannot open shared object', 'dlopen')


def loader_error(text):
    """True when the output of a program says it could not load its libraries."""
    return any(m in (text or '') for m in LOADER_ERRORS)


def git_candidates(path):
    """Every `git` on a PATH string, in order, without duplicates."""
    seen = []
    for d in (path or '').split(os.pathsep):
        g = os.path.join(d, 'git')
        if d and os.path.isfile(g) and os.access(g, os.X_OK) and g not in seen:
            seen.append(g)
    return seen


def git_https_problem(git, ok, output):
    """The message for a git whose HTTPS helper does not run, or None when it does."""
    if ok or not loader_error(output):
        return None
    first = (output or '').strip().splitlines()[0][:160]
    return (f'{git} cannot do HTTPS ("{first}"), so `git clone https://...` fails: put a working git first in PATH '
            '(for example /usr/bin) or remove the tool whose bundled git comes first')


def probe_https(git):
    """Run the git's HTTPS helper without arguments: it prints usage when sound, a loader error when not. Returns (ok, output)."""
    try:
        exec_path = subprocess.run([git, '--exec-path'], capture_output=True, text=True, timeout=20, check=False).stdout.strip()
        helper = os.path.join(exec_path, 'git-remote-https')
        if not os.path.isfile(helper):
            return False, f'{helper}: no such file (error while loading shared libraries: the HTTPS helper is missing)'
        r = subprocess.run([helper], capture_output=True, text=True, timeout=20, check=False)
        return not loader_error(r.stderr), r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return False, f'error while loading shared libraries: {e}'


def check_git(path=None, probe=probe_https):
    """Problems with the git that comes first in PATH; names a working one further on when there is one."""
    first = shutil.which('git', path=path)
    if not first:
        return ['no git in PATH: install git and put it in PATH']
    ok, out = probe(first)
    problem = git_https_problem(first, ok, out)
    if not problem:
        return []
    for other in git_candidates(path or os.environ.get('PATH', '')) + ['/usr/bin/git']:
        if other != first and os.path.isfile(other) and probe(other)[0]:
            return [f'{problem}; {other} works']
    return [problem]
