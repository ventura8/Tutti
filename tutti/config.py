"""Finds, loads and checks tutti.config.yaml. Everything personal (hosts, paths, accounts) lives in that file, never in code."""
import copy
import json
import math
import os
import re

from . import files

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
    # watch_scripts: script names whose running processes are jobs; watch_agents: agent CLIs shown by process; task_globs:
    # an agent session's background task outputs (shown by time only)
    'dashboard': {'port': 4777, 'refresh_seconds': 10, 'probe_seconds': 30, 'stall_minutes': 10, 'watch_scripts': [],
                  'watch_agents': [], 'task_globs': []},
    # test.prepare: steps a command needs first ({when: regex, run: cmd}); test.no_coverage: how a run with coverage off looks
    'test': {'prepare': [], 'no_coverage': ''},
    # delegate: `tutti run --repo`; checks default to ship.checks, worktrees go under project.worktrees/<prefix><id>
    # remote_track: the folder on the test machine that routed checks copy the worktree into (one copy, kept in sync by rsync)
    'delegate': {'checks': None, 'prefix': 'tutti-run-', 'remote_track': 'tutti-run'},
    # review: `tutti pr review`; who to ask, how long to wait for them, and which cheap workers review when none answers
    'review': {'reviewers': [], 'timeout_minutes': 20, 'poll_seconds': 30, 'fallback': []},
    # main: `tutti conduct`; the main (conductor) agent, the ones that take over when its quota is out, a command per agent
    # when it conducts (default: its worker command), extra ask-first actions, how often the run checks the readings
    'main': {'agent': 'claude', 'fallback': [], 'commands': {}, 'ask_first': [], 'check_seconds': 30, 'rounds': 12,
             'command_timeout': 3600},
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
    # enabled: false keeps an account out of pacing, panic, routing and the dashboard (an opt-in model such as a premium one);
    # its readings stay in the log, unused
    cfg['disabled_accounts'] = sorted(n for n, a in cfg['accounts'].items() if (a or {}).get('enabled', True) is False)
    cfg['accounts'] = {n: a for n, a in cfg['accounts'].items() if n not in cfg['disabled_accounts']}
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
    c = m.get('containers')
    if c is not None and (not isinstance(c, list) or not all(isinstance(x, str) and x for x in c)):
        errs.append(f'machines.{name}.containers is {c!r}: a list of container name prefixes, e.g. [tutti-, myproj-test-]')
    return errs + _heat_errors(name, m.get('heat'))


def _heat_errors(name, heat):
    if heat is None:
        return []
    if not isinstance(heat, dict):
        return [f'machines.{name}.heat is {heat!r}: write it as {{cap_c: 85, stop_c: 95, hot_slots: 1}}, or remove the key']
    errs = []
    for k in ('cap_c', 'stop_c'):
        v = heat.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v < 150):
            errs.append(f'machines.{name}.heat.{k} is {v!r}: use degrees C, for example 85')
    slots = heat.get('hot_slots', 1)
    if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1:
        errs.append(f'machines.{name}.heat.hot_slots is {slots!r}: use a whole number, 1 or more')
    return errs


def _test_errors(cfg):
    errs = []
    for i, r in enumerate(cfg['test'].get('prepare') or []):
        if not isinstance(r, dict) or not isinstance(r.get('run'), str) or not r.get('run'):
            errs.append(f'test.prepare[{i}] needs run: the command to run first, e.g. python3 scripts/ci/e2e_build.py')
            continue
        errs += _regex_errors(f'test.prepare[{i}].when', r.get('when'))
    if cfg['test'].get('no_coverage'):
        errs += _regex_errors('test.no_coverage', cfg['test']['no_coverage'])
    return errs


def _review_errors(cfg):
    r = cfg['review']
    errs = [f'review.fallback names "{n}", which is not a cli worker under workers:' for n in r.get('fallback') or []
            if (cfg['workers'].get(n) or {}).get('kind') != 'cli']
    errs += [f'review.reviewers has "{n}": use copilot or coderabbit' for n in r.get('reviewers') or []
             if n not in ('copilot', 'coderabbit')]
    v = r.get('timeout_minutes')
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        errs.append(f'review.timeout_minutes is {v!r}: use minutes, 0 or more (0: do not wait, fall back at once)')
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
        errs += _account_source_errors(name, a or {}) + _account_worker_errors(cfg, name, a or {}) + _account_pace_errors(name, a or {})
    return errs + _dashboard_errors(cfg['dashboard']) + files.errors(cfg) + _test_errors(cfg) + _review_errors(cfg) + _main_errors(cfg)


def _main_errors(cfg):
    m = cfg['main']
    if not isinstance(m, dict):
        return [f'main is {m!r}: write it as a mapping, e.g. main: {{agent: cursor, fallback: [agy, local]}}']
    fallback = m.get('fallback')
    if not isinstance(fallback, list):
        return [f'main.fallback is {fallback!r}: a list of workers, e.g. [agy, cursor]']
    errs = [f'main.fallback names "{n}", which is not under workers:' for n in fallback if n not in cfg['workers']]
    commands = m.get('commands') if isinstance(m.get('commands'), dict) else {}
    errs += [f'main.commands.{n}: "{n}" is not under workers:' for n in commands if n not in cfg['workers']]
    errs += [f'main.commands.{n} is {c!r}: a list such as [claude, -p, "{{prompt}}"]' for n, c in commands.items()
             if not isinstance(c, list) or not c or not all(isinstance(x, str) for x in c)]
    for key in ('check_seconds', 'rounds', 'command_timeout'):
        v = m.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            errs.append(f'main.{key} is {v!r}: use a number above 0')
    return errs


def _account_pace_errors(name, a):
    errs = []
    share = a.get('share', 1)
    if not isinstance(share, (int, float)) or not 0 < share <= 1:
        errs.append(f'accounts.{name}.share is {share!r}: use a fraction above 0 and up to 1 (0.5 = half)')
    panic_at = a.get('panic_at', 95)
    if not isinstance(panic_at, (int, float)) or not 0 < panic_at <= 100:
        errs.append(f'accounts.{name}.panic_at is {panic_at!r}: use a percentage above 0 and up to 100 (95 = panic at 95 % used)')
    for key, hint in (('stale_hours', 'hours after which a hand reading is old (0: never remind)'),
                      ('task_pct', 'the points of quota one bounded task costs, e.g. 2')):
        v = a.get(key, 1)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            errs.append(f'accounts.{name}.{key} is {v!r}: use a number, 0 or more: {hint}')
    if 'reading_file' in a and (not isinstance(a['reading_file'], str) or not a['reading_file']):
        errs.append(f'accounts.{name}.reading_file is {a["reading_file"]!r}: give the path of the file an orchestrator writes its % to')
    return errs


def _command_errors(key, spec, needs):
    if not isinstance(spec, dict):
        return [f'{key} is {spec!r}: write it as a mapping with command: [...] and {needs}:']
    errs = []
    cmd = spec.get('command')
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
        errs.append(f'{key}.command is missing: a list such as [agy, -p, /usage, --output-format, json]')
    want = (int, float) if needs == 'allowance' else str
    if isinstance(spec.get(needs), bool) or not isinstance(spec.get(needs), want) or not spec.get(needs):
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
        github = isinstance(a['reader'], dict) and a['reader'].get('format') == 'github-premium'
        errs += _command_errors(f'{key}.reader', a['reader'], 'allowance' if github else 'bucket')
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
