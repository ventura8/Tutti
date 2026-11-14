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
TRANSPORTS = ('local', 'ssh', 'wsl', 'android')
RUNNERS = ('docker', 'shell', 'native')
# A Termux machine (transport android): one light run at a time, little memory, locks in Termux's own tmp (Android has no /tmp)
ANDROID_DEFAULTS = {'port': 8022, 'slots': 1, 'min_free_gb': 2, 'lock_prefix': '/data/data/com.termux/files/usr/tmp/tutti-slot-'}
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
    'devices': {},
    'brief': {'rules': []},
    'accounts': {},
    'ship': {'protected': ['main', 'master'], 'add': ['.'], 'checks': [], 'full_checks': []},
    # watch_scripts: script names whose running processes are jobs; watch_agents: agent CLIs shown by process; task_globs:
    # an agent session's background task outputs (shown by time only); shapes: a job kind's known phases or counted lines
    # (docs/DASHBOARD.md, *Progress*); typical_minutes: the rough estimate for an agent kind before any run of it finished
    'dashboard': {'port': 4777, 'refresh_seconds': 10, 'probe_seconds': 30, 'stall_minutes': 10, 'watch_scripts': [],
                  'watch_agents': [], 'task_globs': [], 'shapes': {}, 'typical_minutes': {'agent': 20, 'agent task': 15, 'conduct': 30}},
    # test.prepare: steps a command needs first ({when: regex, run: cmd}); test.no_coverage: how a run with coverage off looks
    'test': {'prepare': [], 'no_coverage': ''},
    # delegate: `tutti run --repo`; checks default to ship.checks, worktrees go under project.worktrees/<prefix><id>
    # remote_track: the folder on the test machine that routed checks copy the worktree into (one copy, kept in sync by rsync)
    'delegate': {'checks': None, 'prefix': 'tutti-run-', 'remote_track': 'tutti-run'},
    # review: `tutti pr review`; who to ask, how long to wait for them, and which cheap workers review when none answers
    'review': {'reviewers': [], 'timeout_minutes': 20, 'poll_seconds': 30, 'fallback': []},
    # main: `tutti conduct`; the main (conductor) agent, the ones that take over when its quota is out, a command per agent
    # when it conducts (default: its worker command), extra ask-first actions, how often the run checks the readings
    # autopilot: `tutti autopilot`; accounts whose pace triggers it, the escalation ladder of retries, how many runs at once,
    # when a task is parked for a person, and the repos it may work in ({name: {path, check, where, pr, commit, trailers, gaps,
    # land, base}}); fill_room: free room on the trigger accounts is filled from the repos' sources by itself (tutti/fill.py);
    # auto_review: a result to review is reviewed by review.fallback and landed by its repo's `land` (merge, pr or branch)
    'autopilot': {'enabled': False, 'accounts': [], 'ladder': [], 'max_parallel': 1, 'no_progress': 3, 'max_attempts': 12, 'repos': {},
                  'quiet_hours': [], 'fill_room': True, 'auto_review': True},
    # pacing: the owner's own prompting (tutti/owner.py): the weekly schedule that seeds the learned weekday x hour profile
    # (days, hours of the reserve window, the local window, points per owner hour, the UTC offset; None: this computer's),
    # the mode (auto, active, away, vacation-until-YYYY-MM-DD), the owner hours with no rise after which auto says away,
    # and the accounts the owner prompts on (None: the main agent's)
    'pacing': {'schedule': {'days': ['mon', 'tue', 'wed', 'thu', 'fri'], 'hours': 8, 'window': '01-14', 'rate': 1.0,
                            'utc_offset': None},
               'mode': 'auto', 'away_after_hours': 3, 'accounts': None},
    # scouts: helpers that are not workers (Grok Bot's webhook routine): asked questions, they answer on a GitHub issue
    'scouts': {},
    # telemetry: crash reports and usage statistics (docs/TELEMETRY.md); recorded locally always, sent only with consent.
    # sentry_dsn: prefer $TUTTI_SENTRY_DSN or ~/.config/tutti/telemetry.env (`tutti telemetry setup`); endpoint: your own
    # collector instead of Sentry; environment: Sentry's environment tag
    'telemetry': {'sentry_dsn': '', 'endpoint': '', 'environment': 'production'},
    # self_improve: crashes and insights become autopilot tasks, PRs and (where a project allows) merges and patch releases;
    # home: the project Tutti's own fixes go to (docs/TELEMETRY.md, *Self-improvement*)
    'self_improve': {'enabled': True, 'every_hours': 6, 'max_new_per_day': 3, 'min_count': 3, 'home': ''},
    # autocompact: a worker agent's context is compacted at threshold_pct of its window (tutti/autocompact.py); the
    # dashboard can change the percentage; context_tokens is the window when workers.<w>.context_tokens does not say
    'autocompact': {'enabled': True, 'threshold_pct': 60, 'context_tokens': 200000},
    # user: who this Tutti works for (docs/HOUSEHOLD.md): the name (default: the login's) and the weight of its share of the
    # household's machines. household: the coordinator's port and bind address, how long a task's lease lasts without a
    # heartbeat, when a member with no slot counts as starved, how long before a reset an account's spare room may be shared
    # (accounts.<n>.share_spare), and how long a call to the coordinator may take before the member works alone
    'user': {'name': '', 'weight': 1},
    'household': {'port': 4778, 'bind': '0.0.0.0', 'lease_minutes': 30, 'starve_minutes': 20, 'share_spare_hours': 6,
                  'timeout_seconds': 3},
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


def machine_of(cfg, name, own):
    """One machine with its defaults (a Termux machine's own), its name and its runner merged over the shared one."""
    android = own.get('transport') == 'android'
    m = merge(merge(MACHINE_DEFAULTS, ANDROID_DEFAULTS) if android else MACHINE_DEFAULTS, own)
    if m.get('host') == 'local':
        m['transport'] = 'local'
    if m.get('locks'):
        m['slots'] = len(m['locks'])
    m['name'] = name
    m['runner'] = merge(cfg['runner'], own.get('runner') or {})
    if android and 'kind' not in (own.get('runner') or {}):
        m['runner']['kind'] = 'native'   # Android has no Docker: a Termux machine runs commands natively, niced
    return m


def normalise(raw):
    """Defaults filled in; every machine gets its name, the machine defaults and its own merged runner."""
    if not isinstance(raw, dict):
        raise ConfigError('the config must be a mapping at the top level (machines:, workers:, accounts:, ...)')
    cfg = merge(DEFAULTS, raw)
    for name, m in list(cfg['machines'].items()):
        cfg['machines'][name] = machine_of(cfg, name, m or {})
    if not cfg['routing']['order']:
        cfg['routing']['order'] = list(cfg['machines'])
    # an Android device always routes last: it is the slowest and the one a person may pick up
    cfg['routing']['order'] = sorted(cfg['routing']['order'], key=lambda n: (cfg['machines'].get(n) or {}).get('transport') == 'android')
    from . import devices  # devices imports this module
    devices.normalise(cfg)
    opt_out(cfg)
    return cfg


def is_on(entry):
    """An account or worker is on unless `enabled: false`; one marked `costs_money: true` (pay per use: API keys, premium
    requests) is off unless `enabled: true`, so nothing that can cost extra money runs by default."""
    entry = entry or {}
    if entry.get('paused'):
        return False   # paused by the owner (dashboard or `tutti pause`): skipped until resumed
    if 'enabled' in entry:
        return entry['enabled'] is not False
    return not (entry.get('costs_money') or entry.get('opt_in'))


def opt_out(cfg):
    """Takes the accounts and workers that are off out of the config: out of pacing, panic, routing, review and conduct
    fallbacks and the dashboard (an account's readings stay in the log, unused). An account whose worker is off is off too.
    `off` lists them for the dashboard's note, with whether they cost money."""
    off_workers = sorted(n for n, w in cfg['workers'].items() if not is_on(w))
    off_accounts = sorted(n for n, a in cfg['accounts'].items() if not is_on(a) or (a or {}).get('worker') in off_workers)
    def entry(kind, n, section):
        e = cfg[section][n] or {}
        out = {'name': n, 'kind': kind, 'costs_money': bool(e.get('costs_money'))}
        return {**out, 'paused': e['paused']} if e.get('paused') else out
    cfg['off'] = [entry('worker', n, 'workers') for n in off_workers] + [entry('account', n, 'accounts') for n in off_accounts]
    cfg['disabled_accounts'], cfg['disabled_workers'] = off_accounts, off_workers
    cfg['accounts'] = {n: a for n, a in cfg['accounts'].items() if n not in off_accounts}
    cfg['workers'] = {n: w for n, w in cfg['workers'].items() if n not in off_workers}
    gone = set(off_workers)
    cfg['routing']['workers'] = [w for w in cfg['routing'].get('workers') or [] if w not in gone]
    for section in ('main', 'review'):
        if isinstance(cfg.get(section), dict) and isinstance(cfg[section].get('fallback'), list):
            cfg[section]['fallback'] = [w for w in cfg[section]['fallback'] if w not in gone]
    if isinstance(cfg.get('main'), dict) and isinstance(cfg['main'].get('commands'), dict):
        cfg['main']['commands'] = {n: c for n, c in cfg['main']['commands'].items() if n not in gone}


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
    if m['runner']['kind'] not in RUNNERS:
        errs.append(f'machines.{name}.runner.kind is "{m["runner"]["kind"]}": use docker, shell or native')
    if m['transport'] == 'android':
        errs += _android_errors(name, m)
    if m['runner']['kind'] == 'docker' and not m['runner'].get('image'):
        errs.append(f'machines.{name}.runner.image is missing: a docker runner needs the image to run')
    c = m.get('containers')
    if c is not None and (not isinstance(c, list) or not all(isinstance(x, str) and x for x in c)):
        errs.append(f'machines.{name}.containers is {c!r}: a list of container name prefixes, e.g. [tutti-, myproj-test-]')
    return errs + _heat_errors(name, m.get('heat'))


def _android_errors(name, m):
    from . import devices
    errs = devices.policy_errors(f'machines.{name}', m.get('policy'))
    if m['runner']['kind'] == 'docker':
        errs.append(f'machines.{name}.runner.kind is docker: Android has no Docker; remove the key (a Termux machine runs natively)')
    p = m.get('port')
    if isinstance(p, bool) or not isinstance(p, int) or not 0 < p < 65536:
        errs.append(f'machines.{name}.port is {p!r}: the port of Termux\'s sshd, 8022 unless you changed it')
    return errs


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
    from . import devices
    errs += devices.errors(cfg)
    for name, w in cfg['workers'].items():
        errs += _worker_errors(name, w)
        if (w or {}).get('machine') and w['machine'] not in cfg['machines']:
            errs.append(f'workers.{name}.machine names "{w["machine"]}", which is not under machines:')
    errs += [f'routing.workers names "{n}", which is not under workers:' for n in cfg['routing'].get('workers') or []
             if n not in cfg['workers']]
    for name, a in cfg['accounts'].items():
        errs += _account_source_errors(name, a or {}) + _account_worker_errors(cfg, name, a or {}) + _account_pace_errors(name, a or {})
    errs += _dashboard_errors(cfg['dashboard']) + files.errors(cfg) + _test_errors(cfg) + _review_errors(cfg)
    from . import autocompact, owner, scouts, selfimprove, sentry, users  # they import modules that import this one
    errs += selfimprove.errors(cfg) + sentry.endpoint_errors((cfg.get('telemetry') or {}).get('endpoint'))
    return errs + autocompact.errors(cfg) + _main_errors(cfg) + _projects_errors(cfg) + _autopilot_errors(cfg) + scouts.errors(cfg) + \
        owner.schedule_errors(cfg['pacing']) + users.errors(cfg)


def _autopilot_name_error(cfg, key, n):
    a, w = cfg['accounts'].get(n), cfg['workers'].get(n)
    paid = any(o['name'] == n and o['costs_money'] for o in cfg.get('off') or [])
    if paid or (a or {}).get('costs_money') or (w or {}).get('costs_money'):
        return f'autopilot.{key} names "{n}", which costs money per use: the autopilot never spends money; remove it'
    if a is None and w is None:
        return f'autopilot.{key} names "{n}", which is neither under accounts: nor under workers: (or it is turned off)'
    users = [x for x, acc in cfg['accounts'].items() if (acc or {}).get('worker') == n] if a is None else []
    if users:
        return (f'autopilot.{key} names the worker "{n}", which accounts use: name the account instead ({", ".join(users)}), '
                'so its pace, panic and model apply')
    return None


def _autopilot_name_errors(cfg, key, names):
    return [e for e in (_autopilot_name_error(cfg, key, n) for n in names or []) if e]


def _autopilot_repo_errors(n, r):
    if not isinstance(r, dict) or not r.get('path'):
        return [f'autopilot.repos.{n} needs path: the repository\'s folder (and check: its acceptance check)']
    errs = []
    if r.get('where', 'local') not in ('local', 'route'):
        errs.append(f'autopilot.repos.{n}.where is {r["where"]!r}: use local (the check runs here) or route (a routed machine)')
    if r.get('pr', False) not in (False, 'draft'):
        errs.append(f'autopilot.repos.{n}.pr is {r["pr"]!r}: use false (a branch to review) or draft (also a draft pull request)')
    if not isinstance(r.get('built', []), list):
        errs.append(f'autopilot.repos.{n}.built is {r["built"]!r}: a list of folder names whose files are built, never tested')
    if not isinstance(r.get('untested', True), bool):
        errs.append(f'autopilot.repos.{n}.untested is {r["untested"]!r}: true (fill_room queues tests for files no test names) or false')
    if r.get('land', 'pr') not in ('merge', 'pr', 'branch'):
        errs.append(f'autopilot.repos.{n}.land is {r["land"]!r}: use merge (a reviewed result is merged), pr (a pull request a '
                    'person merges) or branch (it waits for a person)')
    return errs


QUIET = re.compile(r'([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d')


def quiet_hours_errors(hours):
    """autopilot.quiet_hours: UTC spans when the autopilot must not run, e.g. ['22:00-06:00'] (the planner gives them nothing)."""
    if hours is None:
        return []
    if not isinstance(hours, list) or not all(isinstance(h, str) and QUIET.fullmatch(h) for h in hours):
        return [f'autopilot.quiet_hours is {hours!r}: a list of UTC spans HH:MM-HH:MM, e.g. ["22:00-06:00"] (past midnight wraps)']
    return []


def _autopilot_errors(cfg):
    ap = cfg['autopilot']
    if not isinstance(ap, dict):
        return [f'autopilot is {ap!r}: write it as a mapping, e.g. autopilot: {{enabled: false, accounts: [...], repos: {{...}}}}']
    errs = _autopilot_name_errors(cfg, 'accounts', ap.get('accounts')) + _autopilot_name_errors(cfg, 'ladder', ap.get('ladder'))
    errs += quiet_hours_errors(ap.get('quiet_hours'))
    errs += [f'autopilot.{k} is {ap[k]!r}: use true or false' for k in ('fill_room', 'auto_review') if not isinstance(ap.get(k), bool)]
    for key in ('max_parallel', 'no_progress', 'max_attempts'):
        v = ap.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            errs.append(f'autopilot.{key} is {v!r}: use a whole number of 1 or more')
    repos = ap.get('repos') or {}
    if not isinstance(repos, dict):
        return errs + [f'autopilot.repos is {repos!r}: write {{<name>: {{path: <folder>, check: <command>}}}}']
    for n, r in repos.items():
        errs += _autopilot_repo_errors(n, r)
    return errs


def _projects_errors(cfg):
    ps = cfg.get('projects') or {}
    if not isinstance(ps, dict):
        return [f'projects is {ps!r}: write it as a mapping, e.g. projects: {{my-app: {{match: my-app}}}}']
    errs = []
    for name, p in ps.items():
        p = p or {}
        if not isinstance(p, dict):
            errs.append(f'projects.{name} is {p!r}: a mapping with match:, machines:, containers:, windows: or repo:')
            continue
        if 'match' in p:
            errs += _regex_errors(f'projects.{name}.match', p['match'])
        errs += [f'projects.{name}.machines names "{m}", which is not under machines:' for m in p.get('machines') or []
                 if m not in cfg['machines']]
        win = p.get('windows') or {}
        errs += [f'projects.{name}.windows names "{m}", which is not a wsl machine under machines:' for m in win
                 if (cfg['machines'].get(m) or {}).get('transport') != 'wsl']
        if 'repo' in p and not (isinstance(p['repo'], str) and re.fullmatch(r'[\w.-]+/[\w.-]+', p['repo'])):
            errs.append(f'projects.{name}.repo is {p["repo"]!r}: the GitHub repository as owner/name, e.g. octo/my-app')
    return errs


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
    for key in ('check_seconds', 'command_timeout'):
        v = m.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            errs.append(f'main.{key} is {v!r}: use a number above 0')
    rounds = m.get('rounds')
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1:
        errs.append(f'main.rounds is {rounds!r}: use a whole number, 1 or more (a local model\'s answers before it is stopped)')
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
                      ('task_pct', 'the points of quota one bounded task costs, e.g. 2'),
                      ('reserve', 'points of the quota the planner leaves for work outside Tutti (default 0)'),
                      ('window_points', 'the points of its limit one whole window costs, e.g. 10')):
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
    errs = _limits_errors(cfg, name, a.get('limits'))
    if a.get('worker') and a['worker'] not in cfg['workers']:
        errs.append(f'accounts.{name}.worker names "{a["worker"]}", which is not under workers:')
    args = a.get('args', [])
    if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        errs.append(f'accounts.{name}.args is {args!r}: a list of extra arguments for its worker, e.g. [--model, gemini-3.8-flash-high]')
    return errs


def _limits_errors(cfg, name, limits):
    if limits is None:
        return []
    if not isinstance(limits, list) or not all(isinstance(x, str) for x in limits):
        return [f'accounts.{name}.limits is {limits!r}: a list of other accounts that also bound this one, e.g. [agy-claude-weekly]']
    return [f'accounts.{name}.limits names "{n}", which is not under accounts: (or it is turned off)' if n != name
            else f'accounts.{name}.limits names the account itself: name its other window, e.g. {name}-weekly'
            for n in limits if n == name or n not in cfg['accounts']]


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
    return errs + _shape_errors(d.get('shapes')) + _typical_errors(d.get('typical_minutes'))


def _positive(v):
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) and v > 0


def _typical_errors(t):
    """dashboard.typical_minutes: {job kind: minutes}; 0 or null turns the rough estimate off for that kind."""
    if not isinstance(t, dict):
        return [f'dashboard.typical_minutes is {t!r}: write it as a mapping such as {{agent: 20}}, or remove the key']
    return [f'dashboard.typical_minutes.{k} is {v!r}: use minutes above 0, or 0 / null to turn it off'
            for k, v in t.items() if v not in (None, 0) and not _positive(v)]


def _shape_errors(shapes):
    """dashboard.shapes: {name: {match or kind, phases: [{name, when}] or count + total / total_command, machine}}; a shape with
    neither phases nor count only names a kind of job (and its machine) for the estimates from history."""
    if not isinstance(shapes, dict):
        return [f'dashboard.shapes is {shapes!r}: write it as a mapping of names to shapes, or remove the key']
    return [e for name, s in shapes.items() for e in _one_shape_errors(f'dashboard.shapes.{name}', s)]


def _one_shape_errors(where, s):
    if not isinstance(s, dict) or not (s.get('match') or s.get('kind')):
        return [f'{where}: give it match (a regex on the job title) or kind (quiet, script, run, ...)']
    errs = []
    if s.get('count') and not (s.get('total') or s.get('total_command')):
        errs.append(f'{where}.count: give total (a number) or total_command (prints the number)')
    if 'total' in s and not _positive(s['total']):
        errs.append(f'{where}.total is {s["total"]!r}: use a number above 0, or total_command')
    if 'phases' in s and not isinstance(s['phases'], list):
        errs.append(f'{where}.phases is {s["phases"]!r}: write a list such as [{{name: build}}, {{name: test, when: "^test"}}]')
    errs += _regex_errors(f'{where}.match', s['match']) if s.get('match') else []
    errs += _regex_errors(f'{where}.count', s['count']) if s.get('count') else []
    phases = s.get('phases') or []
    for i, p in enumerate(phases if isinstance(phases, list) else []):
        if not isinstance(p, dict) or not p.get('name'):
            errs.append(f'{where}.phases[{i}]: write {{name: ..., when: <regex>}} (the first phase may leave out when)')
        elif p.get('when'):
            errs += _regex_errors(f'{where}.phases[{i}].when', p['when'])
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
        raw = parse(f.read(), path)
    from . import controls  # the owner's switches from the dashboard, kept in the state folder
    state = ((raw or {}).get('project') or {}).get('state') if isinstance(raw, dict) else None
    raw = controls.apply(raw, controls.read(state or DEFAULTS['project']['state']))
    cfg = normalise(raw)
    cfg['raw_entries'] = {k: copy.deepcopy((raw or {}).get(k) or {}) for k in ('workers', 'accounts', 'machines')}
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
