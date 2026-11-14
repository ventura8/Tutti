"""Finds, loads and checks tutti.config.yaml. Everything personal (hosts, paths, accounts) lives in that file, never in code."""
import copy
import json
import math
import os
import re

PROJECT_NAME = 'tutti.config.yaml'
NAMES = (PROJECT_NAME, 'tutti.config.yml', 'tutti.config.json')
# Under XDG: the canonical name first, then the project-style name as a fallback.
XDG_NAMES = ('config.yaml', PROJECT_NAME)
TRANSPORTS = ('local', 'ssh', 'wsl')
# Ollama serves plain HTTP on localhost or through an ssh tunnel; a host written with its own scheme
# (https://proxy:443) keeps it.
OLLAMA_SCHEME = 'http'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, 'tutti.config.example.yaml')
DEFAULTS = {
    'project': {'worktrees': '.', 'state': '~/.local/state/tutti'},
    'machines': {},
    'runner': {'kind': 'shell', 'memory_gb': 12, 'cpus': 8, 'env': {}, 'args': [], 'prelude': ''},
    'routing': {'order': []},
    'sync': {'exclude': ['/.git', '.env', '.env.*', '*.key', '*.pem'], 'ensure_dirs': []},
    'workers': {},
    'brief': {'rules': []},
    'accounts': {},
    'ship': {'protected': ['main', 'master'], 'add': ['.'], 'checks': [], 'full_checks': []},
    'dashboard': {'port': 4777, 'refresh_seconds': 10, 'probe_seconds': 30, 'stall_minutes': 10},
}
MACHINE_DEFAULTS = {'transport': 'ssh', 'root': 'tutti-wt', 'slots': 2, 'min_free_gb': 6, 'lock_prefix': '/tmp/tutti-slot-',
                    'yield_file': '~/.tutti/yield'}


class ConfigError(Exception):
    """The config file is missing or wrong; the message says which key and what to write instead."""


def ollama_url(host, path):
    """The URL of an Ollama API path on `host` (`name:port`, or a full `scheme://name:port`)."""
    base = host if '://' in host else f'{OLLAMA_SCHEME}://{host}'
    return base.rstrip('/') + path


def xdg_tutti_dir(env=None):
    """`$XDG_CONFIG_HOME/tutti` when set, else `~/.config/tutti`."""
    env = os.environ if env is None else env
    return os.path.join(os.path.expanduser(env.get('XDG_CONFIG_HOME') or '~/.config'), 'tutti')


def xdg_config_path(env=None):
    """The canonical XDG config path to create: `.../tutti/config.yaml`."""
    return os.path.join(xdg_tutti_dir(env), 'config.yaml')


def find(start=None, env=None):
    """$TUTTI_CONFIG, else the nearest tutti.config.* from `start` upwards, else XDG config.yaml, tutti.config.yaml; None if absent."""
    env = os.environ if env is None else env
    if env.get('TUTTI_CONFIG'):
        return env['TUTTI_CONFIG']
    d = os.path.abspath(start or os.getcwd())
    while True:
        for n in NAMES:
            if os.path.isfile(os.path.join(d, n)):
                return os.path.join(d, n)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    base = xdg_tutti_dir(env)
    for n in XDG_NAMES:
        home = os.path.join(base, n)
        if os.path.isfile(home):
            return home
    return None


def parse(text, path=PROJECT_NAME):
    if path.endswith('.json'):
        return json.loads(text) or {}
    try:
        import yaml
    except ImportError as e:
        raise ConfigError('PyYAML is needed to read a YAML config: `pip install pyyaml`, or write tutti.config.json') from e
    return yaml.safe_load(text) or {}


def merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def normalise(raw):
    """Defaults filled in; every machine gets its name, the machine defaults and its own merged runner."""
    if not isinstance(raw, dict):
        raise ConfigError('the config must be a mapping at the top level (machines:, workers:, accounts:, ...)')
    cfg = merge(DEFAULTS, raw)
    for name, m in list(cfg['machines'].items()):
        m = merge(MACHINE_DEFAULTS, m or {})
        if m.get('host') == 'local':
            m['transport'] = 'local'
        if m.get('locks'):
            m['slots'] = len(m['locks'])
        m['name'] = name
        m['runner'] = merge(cfg['runner'], m.get('runner'))
        cfg['machines'][name] = m
    if not cfg['routing']['order']:
        cfg['routing']['order'] = list(cfg['machines'])
    return cfg


def _machine_errors(name, m):
    errs = []
    if m['transport'] not in TRANSPORTS:
        errs.append(f'machines.{name}.transport is "{m["transport"]}": use one of {", ".join(TRANSPORTS)}')
    if m['transport'] != 'local' and not m.get('host'):
        errs.append(f'machines.{name}.host is missing: give its ssh alias, or "local" for this computer')
    if m['transport'] == 'wsl' and not m.get('wsl_helper'):
        errs.append(f'machines.{name}.wsl_helper is missing: the path, inside WSL, of the base64 runner (docs/MACHINES.md)')
    if not isinstance(m['slots'], int) or m['slots'] < 1:
        errs.append(f'machines.{name}.slots is {m["slots"]!r}: use a whole number, 1 or more')
    if m['runner']['kind'] not in ('docker', 'shell'):
        errs.append(f'machines.{name}.runner.kind is "{m["runner"]["kind"]}": use docker or shell')
    if m['runner']['kind'] == 'docker' and not m['runner'].get('image'):
        errs.append(f'machines.{name}.runner.image is missing: a docker runner needs the image to run')
    return errs


def _worker_errors(name, w):
    kind = (w or {}).get('kind')
    if kind == 'cli':
        return [] if isinstance(w.get('command'), list) and w['command'] else [
            f'workers.{name}.command is missing: a list such as [cursor-agent, -p, "{{prompt}}"]']
    if kind == 'ollama':
        return [f'workers.{name}.{k} is missing' for k in ('host', 'model') if not w.get(k)]
    return [f'workers.{name}.kind is {kind!r}: use cli or ollama']


def validate(cfg):
    """Every problem at once, each naming its key and the fix; an empty list means the config is usable."""
    errs = []
    for name, m in cfg['machines'].items():
        errs += _machine_errors(name, m)
    errs += [f'routing.order names "{n}", which is not under machines:' for n in cfg['routing']['order'] if n not in cfg['machines']]
    for name, w in cfg['workers'].items():
        errs += _worker_errors(name, w)
        if (w or {}).get('machine') and w['machine'] not in cfg['machines']:
            errs.append(f'workers.{name}.machine names "{w["machine"]}", which is not under machines:')
    errs += [f'routing.workers names "{n}", which is not under workers:' for n in cfg['routing'].get('workers') or []
             if n not in cfg['workers']]
    for name, a in cfg['accounts'].items():
        errs += _account_source_errors(name, a or {})
        errs += _account_worker_errors(cfg, name, a or {})
        share = (a or {}).get('share', 1)
        if not isinstance(share, (int, float)) or not 0 < share <= 1:
            errs.append(f'accounts.{name}.share is {share!r}: use a fraction above 0 and up to 1 (0.5 = half)')
        panic_at = (a or {}).get('panic_at', 95)
        if not isinstance(panic_at, (int, float)) or not 0 < panic_at <= 100:
            errs.append(f'accounts.{name}.panic_at is {panic_at!r}: use a percentage above 0 and up to 100 (95 = panic at 95 % used)')
    return errs + _dashboard_errors(cfg['dashboard'])


def _command_errors(key, spec, needs):
    if not isinstance(spec, dict):
        return [f'{key} is {spec!r}: write it as a mapping with command: [...] and {needs}:']
    errs = []
    cmd = spec.get('command')
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
        errs.append(f'{key}.command is missing: a list such as [agy, -p, /usage, --output-format, json]')
    if not isinstance(spec.get(needs), str) or not spec.get(needs):
        errs.append(f'{key}.{needs} is missing (docs/USAGE-PACING.md: Automatic readings)')
    every = spec.get('every_minutes', 1)
    if isinstance(every, bool) or not isinstance(every, (int, float)) or not math.isfinite(every) or every <= 0:
        errs.append(f'{key}.every_minutes is {every!r}: use a number of minutes above 0')
    return errs


def _regex_errors(key, value):
    try:
        re.compile(value)
    except (re.error, TypeError) as e:
        return [f'{key} is {value!r}, not a regular expression ({e}): write a pattern such as "cursor-agent"']
    return []


def _account_source_errors(name, a):
    """usage_url, reader, plan and match: where readings come from without the owner's input."""
    key = f'accounts.{name}'
    errs = []
    url = a.get('usage_url')
    if url is not None and (not isinstance(url, str) or not url.startswith('https://')):
        errs.append(f'{key}.usage_url is {url!r}: give the https:// address of the provider\'s usage page')
    if 'reader' in a:
        errs += _command_errors(f'{key}.reader', a['reader'], 'bucket')
    if 'plan' in a:
        errs += _command_errors(f'{key}.plan', a['plan'], 'field')
    return errs + _match_errors(key, a.get('match'))


def _account_worker_errors(cfg, name, a):
    errs = []
    if a.get('worker') and a['worker'] not in cfg['workers']:
        errs.append(f'accounts.{name}.worker names "{a["worker"]}", which is not under workers:')
    args = a.get('args', [])
    if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        errs.append(f'accounts.{name}.args is {args!r}: a list of extra arguments for its worker, e.g. [--model, gemini-3.8-flash-high]')
    return errs


def _match_errors(key, m):
    if m is None:
        return []
    if not isinstance(m, dict) or not m.get('command'):
        return [f'{key}.match needs command: a pattern for the job command lines of this account (e.g. "\\bagy\\b")']
    errs = _regex_errors(f'{key}.match.command', m['command'])
    return errs + (_regex_errors(f'{key}.match.model', m['model']) if 'model' in m else [])


def _dashboard_errors(d):
    if not isinstance(d, dict):
        return [f'dashboard is {d!r}: write it as a mapping, e.g. dashboard: {{port: 4777}}, or remove the key']
    errs = []
    port = d.get('port')
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        errs.append(f'dashboard.port is {port!r}: use a port number such as 4777')
    for key in ('refresh_seconds', 'probe_seconds', 'stall_minutes'):
        v = d.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            errs.append(f'dashboard.{key} is {v!r}: use a number above 0')
    return errs


def load(path=None, start=None, env=None):
    env = os.environ if env is None else env
    path = path or find(start, env)
    if not path:
        dest = xdg_config_path(env)
        raise ConfigError(
            f'no config found here, in a parent folder or at {dest}: '
            f'copy with `cp {EXAMPLE} {dest}` and edit it, or set TUTTI_CONFIG to its path')
    with open(path, encoding='utf8') as f:
        cfg = normalise(parse(f.read(), path))
    errs = validate(cfg)
    if errs:
        raise ConfigError(f'{path} has {len(errs)} problem(s):\n  - ' + '\n  - '.join(errs))
    cfg['_path'] = os.path.abspath(path)
    return cfg


def expand(p):
    return os.path.abspath(os.path.expanduser(p))


def state_dir(cfg):
    d = expand(cfg['project']['state'])
    os.makedirs(d, exist_ok=True)
    return d
