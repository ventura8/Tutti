"""Secrets never travel in a process environment (backlog t116: the Grok key leaked three times through agents printing
/proc/<pid>/environ, `env` or `ps e`). Tutti reads a secret from its file (~/.config/tutti/grok.env, mode 600) at the point
of use; at start it drops secret-looking variables from its own environment, so nothing it launches inherits them, and a
long-running command (the dashboard) re-executes itself once without them, so its own /proc environ is clean too.

Anything that shows an environment goes through `mask`. Agents: never print a process environment; use `tutti doctor`."""
import os
import re
import sys

from . import config

FILES = ('grok.env', 'home-assistant.env')   # each secret's own file, mode 600
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
    source = os.environ if env is None else env
    return {k: v for k, v in source.items() if not owned(k, env)}


def owned(name, env=None):
    return name in OWNED or name in file_names(env)


def file_names(env=None):
    from . import groksetup
    if env is not None and not env.get('XDG_CONFIG_HOME'):
        return set()
    return {k for f in FILES for k in groksetup.read_env_file(os.path.join(config.xdg_tutti_dir(env), f))}


def get(name, env=None):
    """The secret `name`: from its file first (the point of use), else from `env` (a test, or an old shell export).
    A plain dict without XDG_CONFIG_HOME is a test's whole world; this process's own environment never is: under a service
    manager it has no XDG_CONFIG_HOME and its secrets were scrubbed at start, so the file is the only source (the scout
    stopped after every dashboard restart, 2026-11-22: docs/LESSONS.md)."""
    from . import groksetup
    if env is not None and env is not os.environ and not env.get('XDG_CONFIG_HOME'):
        return env.get(name, '')
    for f in FILES:
        v = groksetup.read_env_file(os.path.join(config.xdg_tutti_dir(env), f)).get(name, '')
        if v.strip():
            return v
    return (os.environ if env is None else env).get(name, '')


def scrub_self(env=None):
    """Drops the owned secrets from this process's environment; returns the names dropped (children inherit none)."""
    source = env
    env = os.environ if env is None else env
    gone = [k for k in list(env) if owned(k, source)]
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
    source = env
    env = os.environ if env is None else env
    names = sorted(k for k in env if owned(k, source) and env[k])
    return [f'{", ".join(names)} exported in this shell (values hidden): remove the export or the `. ~/.config/tutti/grok.env` '
            'line from ~/.profile; Tutti reads ~/.config/tutti/grok.env itself, and an exported key leaks to anything that '
            'prints the environment'] if names else []
