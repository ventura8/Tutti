"""Secrets never travel in a process environment (backlog t116: the Grok key leaked three times through agents printing
/proc/<pid>/environ, `env` or `ps e`). Tutti reads a secret from its file (~/.config/tutti/grok.env, mode 600) at the point
of use; at start it drops secret-looking variables from its own environment, so nothing it launches inherits them, and a
long-running command (the dashboard) re-executes itself once without them, so its own /proc environ is clean too.

Anything that shows an environment goes through `mask`. Agents: never print a process environment; use `tutti doctor`."""
import os
import re
import sys

from . import config

FILES = ('grok.env',)
SECRET_RX = re.compile(r'(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|DSN|AUTH)', re.I)
KEEP = {'TUTTI_GROK_URL'}           # set by the same file; a webhook URL carries its routine id, treat it as secret too
OWNED = {'TUTTI_GROK_KEY', 'TUTTI_GROK_URL'}   # plus every name a secret file holds; never a run's own TUTTI_BROWSER_TOKEN
MASK = '****'


def is_secret(name):
    return bool(SECRET_RX.search(name)) or name in KEEP


def mask(env):
    """A copy of `env` with every secret-looking value replaced (the names stay, to say what is set)."""
    return {k: (MASK if is_secret(k) and v else v) for k, v in env.items()}


def scrubbed(env=None):
    """A copy of `env` without the secrets Tutti owns (OWNED and the names its secret files hold)."""
    env = os.environ if env is None else env
    return {k: v for k, v in env.items() if not owned(k)}


def owned(name, env=None):
    return name in OWNED or name in file_names(env)


def _secret_folders(env):
    """Folders whose secret files may be read. An explicit `env` without a non-empty `XDG_CONFIG_HOME` reads none
    (never the host `~/.config/tutti`; LESSONS 2026-11-14 / t276)."""
    if env is None:
        return [config.xdg_tutti_dir(os.environ)]
    if env.get('XDG_CONFIG_HOME'):
        return [config.xdg_tutti_dir(env)]
    return []


def file_names(env=None):
    from . import groksetup
    return {k for folder in _secret_folders(env) for f in FILES
            for k in groksetup.read_env_file(os.path.join(folder, f))}


def get(name, env=None):
    """The secret `name`: from its file first (the point of use), else from `env` (a test, or an old shell export).

    An explicit `env` without `XDG_CONFIG_HOME` never reads the host's `~/.config/tutti` (LESSONS 2026-11-14: `env={}`
    used to fall back there and make laptop-only test failures). Pass a temporary `XDG_CONFIG_HOME` when the file should
    be read."""
    from . import groksetup
    source = os.environ if env is None else env
    for folder in _secret_folders(env):
        for f in FILES:
            v = groksetup.read_env_file(os.path.join(folder, f)).get(name, '')
            if v.strip():
                return v
    return source.get(name, '')


def scrub_self(env=None):
    """Drops the owned secrets from this process's environment; returns the names dropped (children inherit none)."""
    env = os.environ if env is None else env
    gone = [k for k in list(env) if owned(k, env)]
    for k in gone:
        del env[k]
    return gone


def reexec_clean(argv=None, execv=os.execv, env=None):
    """For a long-running command: when secrets were in the start environment, run again without them (scrub_self first)."""
    if scrub_self(env):
        argv = argv or [sys.executable, *sys.orig_argv[1:]]   # the same command line, -c or -m included
        execv(argv[0], argv)


def doctor_problems(env=None):
    """`tutti doctor`: the owned secrets this shell still exports (names only, the values masked)."""
    env = os.environ if env is None else env
    names = sorted(k for k in env if owned(k, env) and env[k])
    return [f'{", ".join(names)} exported in this shell (values hidden): remove the export or the `. ~/.config/tutti/grok.env` '
            'line from ~/.profile; Tutti reads ~/.config/tutti/grok.env itself, and an exported key leaks to anything that '
            'prints the environment'] if names else []
