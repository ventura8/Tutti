"""Finds, loads and checks tutti.config.yaml. Everything personal (hosts, paths, accounts) lives in that file, never in code."""
import copy
import json
import math
import os
import re
import sys

from . import files

PROJECT_NAME = 'tutti.config.yaml'
NAMES = (PROJECT_NAME, 'tutti.config.yml', 'tutti.config.json')
# Under XDG: the canonical name first, then the project-style name as a fallback.
XDG_NAMES = ('config.yaml', PROJECT_NAME)
TRANSPORTS = ('local', 'ssh', 'wsl', 'android', 'household')   # household: another member's machine, run by its owner's Tutti
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
    'hosts': {},
    # time.zone: what local time means (owner hours, quiet hours, tariffs, what the CLI prints): auto detects this
    # computer's IANA zone (tutti/tz.py), daylight saving included; an IANA name such as Europe/Bucharest overrides it
    'time': {'zone': 'auto'},
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
    # bind: the address the dashboard listens on (127.0.0.1; another needs sign-in); origins: https names it is reached at
    # through a proxy (a LAN or household address; sign-in required)
    'dashboard': {'port': 4777, 'bind': '127.0.0.1', 'origins': [], 'refresh_seconds': 10, 'probe_seconds': 30, 'stall_minutes': 10,
                  'watch_scripts': [],
                  'watch_agents': [], 'task_globs': [], 'shapes': {}, 'typical_minutes': {'agent': 20, 'agent task': 15, 'conduct': 30}},
    # test.prepare: steps a command needs first ({when: regex, run: cmd}); test.no_coverage: how a run with coverage off looks
    'test': {'prepare': [], 'no_coverage': ''},
    # delegate: `tutti run --repo`; checks default to ship.checks, worktrees go under project.worktrees/<prefix><id>
    # remote_track: the folder on the test machine that routed checks copy the worktree into (one copy, kept in sync by rsync)
    'delegate': {'checks': None, 'prefix': 'tutti-run-', 'remote_track': 'tutti-run'},
    # review: `tutti pr review`; who to ask, how long to wait for them, and which cheap workers review when none answers
    # coderabbit: wait (default; no merge until CodeRabbit reviewed), fallback, skip or skip-when-unavailable; repos.<owner/name>
    'review': {'reviewers': [], 'timeout_minutes': 20, 'poll_seconds': 30, 'fallback': [],
               'coderabbit': 'wait', 'greptile': 'wait', 'repos': {},
               'rounds': {}},   # rounds.<reviewer or local>: how often each is asked per pull request (1; 0: never)
    # main: `tutti conduct`; the main (conductor) agent, the ones that take over when its quota is out, a command per agent
    # when it conducts (default: its worker command), extra ask-first actions, how often the run checks the readings
    # autopilot: `tutti autopilot`; accounts whose pace triggers it, the escalation ladder of retries, how many runs at once,
    # when a task is parked for a person, and the repos it may work in ({name: {path, check, where, pr, commit, trailers, gaps,
    # land, base}}); fill_room: free room on the trigger accounts is filled from the repos' sources by itself (tutti/fill.py);
    # auto_review: a result to review is reviewed by review.fallback and landed by its repo's `land` (merge, pr or branch)
    'autopilot': {'enabled': False, 'accounts': [], 'ladder': [], 'max_parallel': None, 'no_progress': 3, 'max_attempts': 12, 'repos': {},
                  'checkpoint_minutes': 5, 'quiet_hours': [], 'fill_room': True, 'auto_review': True, 'stuck_minutes': 30},
    # depbots: dependency bots' pull requests and vulnerability reports as autopilot tasks (tutti/depbots.py,
    # docs/SECURITY-INTAKE.md): the bots whose pull requests are dependency updates (authors or branch prefixes; match: a
    # regex the title or branch must hold), the scanners whose review comments are security findings, how often to read
    'depbots': {'enabled': True, 'every_minutes': 360, 'group_trivial': True,
                'bots': [{'name': 'dependabot', 'authors': ['dependabot[bot]'], 'branch_prefixes': ['dependabot/']},
                         {'name': 'cursor', 'authors': ['cursor[bot]'], 'branch_prefixes': ['cursor/'],
                          'match': r'\bbump\b|\bupgrade\b|\bdependenc|\bcve-|\bghsa-|vulnerab'}],
                'scanners': [{'name': 'cursor', 'authors': ['cursor[bot]']}, {'name': 'greptile', 'authors': ['greptile-apps[bot]']}]},
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
    # home: the project Tutti's own fixes go to (docs/TELEMETRY.md, *Self-improvement*); auto_accept: a Sentry or scout triage
    # task joins the queue by itself, no `tutti backlog accept` (still the 3-a-day cap and the dedupe)
    'self_improve': {'enabled': True, 'auto_accept': True, 'every_hours': 6, 'max_new_per_day': 3, 'min_count': 3, 'home': ''},
    # welcome: the welcome-back summary for every user (tutti/welcome.py); away_hours: None is pacing.away_after_hours
    'welcome': {'enabled': True, 'away_hours': None, 'ai': True},
    # failover: the backup main machine (tutti/failover.py, docs/FAILOVER.md): backup '' elects one, 'none' turns it off; the
    # main is silent `minutes` before the backup takes over; Tutti returns after `failback_minutes` healthy; the state's budget
    'failover': {'backup': '', 'minutes': 5, 'failback_minutes': 5, 'sync_seconds': 60, 'dir': '~/.tutti-failover', 'reach': {},
                 'addresses': {}, 'budget_mb': 50, 'delta_kb': 512, 'runs_keep': 200, 'runs_days': 14, 'log_mb': 2, 'junk_days': 3},
    # ai_assist: Tutti uses AI, local and online, to make itself better (tutti/aiassist.py, docs/TELEMETRY.md, *AI assist*);
    # off until an admin opts in; local, online and online_code (code to online models) are separate switches
    'ai_assist': {'enabled': False, 'local': True, 'online': False, 'online_code': False, 'auto_tune': True, 'every_hours': 6,
                  'max_proposals': 5, 'local_worker': '', 'scout': '', 'research_every': '7d', 'online_worker': ''},
    # autocompact: a worker agent's context is compacted at threshold_pct of its window (tutti/autocompact.py); the
    # dashboard can change the percentage; context_tokens is the window when workers.<w>.context_tokens does not say
    'autocompact': {'enabled': True, 'threshold_pct': 60, 'context_tokens': 200000},
    # user: who this Tutti works for (docs/HOUSEHOLD.md): the name (default: the login's) and the weight of its share of the
    # household's machines. household: the coordinator's port and bind address, how long a task's lease lasts without a
    # heartbeat, when a member with no slot counts as starved, how long before a reset an account's spare room may be shared
    # (accounts.<n>.share_spare), and how long a call to the coordinator may take before the member works alone
    'user': {'name': '', 'weight': 1},
    # self_update: Tutti installs its newest checksummed release at login and once a day from the dashboard (docs/INSTALL.md);
    # enabled: false is the opt-out
    'self_update': {'enabled': True},
    # vpn: optional (docs/NETWORK.md). prefer: which one `tutti vpn setup` sets up (tailscale or wireguard); Tutti never needs one
    'vpn': {'prefer': 'tailscale', 'wireguard': {'port': 51820, 'subnet': 'auto', 'hub': None, 'endpoint': None}},
    # member_shell: members' runs on this login's shared machines may use a shell runner (default: a container only)
    'household': {'member_shell': False, 'job_timeout_seconds': 3600, 'port': 4778, 'bind': 'auto', 'lease_minutes': 30,
                  'starve_minutes': 20, 'share_spare_hours': 6, 'timeout_seconds': 3},
    # update: `tutti update` and the dashboard's daily round; auto: false turns the round off, packages: apt packages whose
    # upgrade needs the owner's sudo (named with one exact command), programs: CLIs to update besides the workers' own
    'update': {'auto': True, 'every_hours': 24, 'packages': [], 'programs': []},
    # dispatch: keep devices, machines and workers busy (tutti/dispatch.py, docs/DISPATCH.md): idle targets pull pending test
    # shards (projects.<p>.tests) by themselves; a target idle idle_minutes while compatible work waits gets a warning; a
    # run's lease ends after lease_minutes without a heartbeat; a failed shard is retried `retries` times. On by default.
    # A phone call on a device (any SIM slot) pauses its work: looked for every call_poll_seconds during a run, work starts
    # again resume_after_call_seconds after the call; pause_on_owner_use (opt-in): a screen a person turned on holds it too.
    # night mode for Android devices (docs/DISPATCH.md, *Night mode*): 1 % brightness and every volume at 0 while Tutti holds
    # or works a device in these hours; devices: [] means every device
    'night': {'enabled': True, 'hours': '22-08', 'devices': []},
    'dispatch': {'enabled': True, 'idle_minutes': 5, 'lease_minutes': 10, 'retries': 2, 'quiet_hours': [],
                 'call_poll_seconds': 3, 'resume_after_call_seconds': 30, 'pause_on_owner_use': False,
                 'requeue_cooldown_minutes': 10},   # a device whose run never started or lost the front rests this long
    # energy: local models' estimated energy cost (docs/ENERGY.md). country and currency: '' detects them (time zone, then
    # locale); price_per_kwh: None uses the bundled typical tariff (shown as estimated); tariff: time-of-day prices
    # [{hours: '23-07', price: 0.9}] (local hours); prefer_cheap_hours: local-model autopilot runs wait for the cheap ones;
    # include_idle: count the draw while Tutti keeps a model loaded (keep_alive_minutes after its last run); compare_with:
    # the cloud API priced for the same tokens; machines: per machine {base_w, gpu, gpu_tdp_w, cpu_tdp_w, ollama}
    'energy': {'country': '', 'currency': '', 'price_per_kwh': None, 'tariff': [], 'prefer_cheap_hours': False, 'include_idle': True,
               'keep_alive_minutes': 5, 'compare_with': 'claude-sonnet', 'machines': {}},
    # browser: the household's work browser (tutti/browser.py, docs/BROWSER.md), the one Chrome every user's and worker's
    # browser task uses: machine ('' is this one), mode (existing: the owner's own Chrome profile; dedicated: Tutti's own
    # profile; isolated: a sandboxed Chrome as the tutti-browser user), path, profile, user_profile, headless, idle_minutes,
    # port (127.0.0.1), max_tabs, sites (the admin's sites to sign in to), socket, scopes ({task type: [hosts]}), allow_local
    'browser': {'machine': '', 'mode': 'dedicated', 'path': '', 'profile': '', 'user_profile': '', 'headless': True,
                'idle_minutes': 5, 'port': 9333, 'max_tabs': 4, 'sites': [], 'socket': '', 'scopes': {}, 'allow_local': [],
                'file_tasks': [], 'max_file_mb': 50},
    # auth: sign-in with Google for the dashboard (docs/AUTH.md): the OAuth client ID (public; `tutti auth setup` saves it in
    # the state folder instead) and how long a session lasts
    'auth': {'client_id': '', 'session_hours': 72},
    'main': {'agent': 'claude', 'fallback': [], 'commands': {}, 'ask_first': [], 'check_seconds': 30, 'rounds': 12,
             'command_timeout': 3600},
    # fleet: the household's local models as workers (docs/FLEET.md): every machine's Ollama is read and a machine with models
    # but no configured ollama worker gets `<machine>-local` (a tunnel from port_base on); memory_floor_gb: this computer
    # runs no local model with less memory free; enabled: false turns the discovery and the overflow routing off
    # work_queue: compute jobs for machines whose model is busy or missing (docs/FLEET.md, The work queue): each job {name, run,
    # project, repo, where (route, here or a machine), heavy, every (commit or 6h), ref, share, findings {pattern, title, size}}
    'work_queue': {'enabled': True, 'jobs': []},
    'fleet': {'enabled': True, 'port_base': 11450, 'memory_floor_gb': 3},
    # host: where Tutti runs (docs/MOVE.md), written by `tutti move`: the machine that serves the dashboard and keeps the
    # state, this computer's own name (me), and on a client how to reach the host (ssh, config, tutti, state, usage_log),
    # the stable name members use (address) and its port; on the host, jobs: the client whose repositories and sign-ins
    # run the worker jobs through its bridge. Empty: Tutti runs here, as it always did.
    'host': {},
}
# Safe by default: a machine without a `heat` block takes one run at 85 C or more and none at 95 C, the hotter of its CPU
# and GPU (a laptop with no limit ran at load 16/16 and 95 C under a work-queue job, 2026-11-15). Termux machines have
# their own battery-temperature policy.
HEAT_DEFAULT = {'cap_c': 85, 'stop_c': 95, 'hot_slots': 1}
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
    """The one config of this user, whatever the working directory: $TUTTI_CONFIG, else the XDG config.yaml (or its
    tutti.config.yaml), else the nearest tutti.config.* from `start` upwards; None if absent. The XDG file wins over a
    project file so the dashboard, the MCP server, the service and every command read the same file (docs/CONFIG.md)."""
    env = os.environ if env is None else env
    if env.get('TUTTI_CONFIG'):
        return env['TUTTI_CONFIG']
    base = xdg_tutti_dir(env)
    for n in XDG_NAMES:
        home = os.path.join(base, n)
        if os.path.isfile(home):
            return home
    return nearest(start)


def nearest(start=None):
    """The nearest tutti.config.* from `start` (the working directory) upwards; None if there is none."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        for n in NAMES:
            if os.path.isfile(os.path.join(d, n)):
                return os.path.join(d, n)
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def write_text(path, text):
    """Writes the config atomically (a temp file beside it, then a rename) into the real file: a symlinked config stays a
    symlink, its target gets the new text and keeps its mode. Returns the real path written."""
    real = os.path.realpath(path)
    mode = os.stat(real).st_mode & 0o777 if os.path.exists(real) else 0o644
    os.makedirs(os.path.dirname(real), exist_ok=True)
    tmp = f'{real}.{os.getpid()}.tmp'
    try:
        with open(tmp, 'w', encoding='utf8') as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)   # a failed write leaves no temp file behind
    return real


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


def physical(cfg, name):
    """The machine whose card a name counts on: a dedicated pseudo-machine (machines.<m>.dedicated, a desktop session for UI
    runs) on the same computer as a regular machine (the same host; `local` is this computer) is that machine, so it gets no
    card of its own and its power and energy are never counted twice."""
    m = cfg['machines'].get(name)
    if not m or not m.get('dedicated') or not m.get('host'):
        return name
    return next((n for n, o in cfg['machines'].items() if n != name and not o.get('dedicated') and o.get('host') == m.get('host')),
                name)


def machine_of(cfg, name, own):
    """One machine with its defaults (a Termux machine's own), its name and its runner merged over the shared one."""
    android = own.get('transport') == 'android'
    m = merge(merge(MACHINE_DEFAULTS, ANDROID_DEFAULTS) if android else MACHINE_DEFAULTS, own)
    if m.get('host') == 'local':
        m['transport'] = 'local'
    m['slots_from'] = 'config' if 'slots' in own else 'default'
    if m.get('locks'):
        m['slots'], m['slots_from'] = len(m['locks']), 'locks'
    m['name'] = name
    if not android and not own.get('heat'):
        m['heat'] = dict(HEAT_DEFAULT)
    m['runner'] = merge(cfg['runner'], own.get('runner') or {})
    if android and 'kind' not in (own.get('runner') or {}):
        m['runner']['kind'] = 'native'   # Android has no Docker: a Termux machine runs commands natively, niced
    return m


def normalise(raw):
    """Defaults filled in; every machine gets its name, the machine defaults and its own merged runner."""
    if not isinstance(raw, dict):
        raise ConfigError('the config must be a mapping at the top level (hosts:, workers:, accounts:, ...)')
    raw = dict(raw)
    if 'hosts' in raw and 'machines' not in raw:
        raw['machines'] = raw['hosts']
    elif 'machines' in raw and 'hosts' not in raw:
        raw['hosts'] = raw['machines']
    cfg = merge(DEFAULTS, raw)
    cfg['_ai_assist_file'] = dict(raw.get('ai_assist') or {}) if isinstance(raw.get('ai_assist'), dict) else {}   # as written
    for name, m in list(cfg['machines'].items()):
        cfg['machines'][name] = machine_of(cfg, name, m or {})
        if cfg['machines'][name].get('via'):   # probed through that client's bridge (tutti/bridge.py)
            cfg['machines'][name]['_state'] = expand(cfg['project']['state'])
    cfg['hosts'] = cfg['machines']
    if not cfg['routing']['order']:
        cfg['routing']['order'] = list(cfg['machines'])
    # an Android device always routes last: it is the slowest and the one a person may pick up
    cfg['routing']['order'] = sorted(cfg['routing']['order'], key=lambda n: (cfg['machines'].get(n) or {}).get('transport') == 'android')
    from . import devices  # devices imports this module
    devices.normalise(cfg)
    opt_out(cfg)
    lenient(cfg)
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
    if m['transport'] not in ('local', 'household') and not m.get('host'):
        errs.append(f'machines.{name}.host is missing: give its ssh alias, or "local" for this computer')
    errs += _ssh_word_errors(name, m)
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


def _ssh_word_errors(name, m):
    """host and user are one word of the ssh command line: never an option (-oProxyCommand=...) or two words."""
    bad = [k for k in ('host', 'user') if isinstance(m.get(k), str) and (m[k].startswith('-') or any(c.isspace() for c in m[k]))]
    return [f'machines.{name}.{k} is {m[k]!r}: a host name, address or ssh alias (no leading "-", no spaces)' for k in bad]


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
    errs += [f'review.reviewers has "{n}": use copilot, coderabbit, greptile or a scout named under scouts: (e.g. grok)'
             for n in r.get('reviewers') or [] if n not in ('copilot', 'coderabbit', 'greptile') and n not in (cfg.get('scouts') or {})]
    pols = ('wait', 'fallback', 'skip', 'skip-when-unavailable')
    bots = ('coderabbit', 'greptile')
    where = [(f'review.{b}', r.get(b)) for b in bots] + [(f'review.repos.{n}.{b}', o.get(b)) for n, o in (r.get('repos') or {}).items()
                                                         if isinstance(o, dict) for b in bots]
    errs += [f'{k} is {v!r}: use {", ".join(pols)}' for k, v in where if v is not None and v not in pols]
    errs += [f'review.repos.{n} is {o!r}: use {{coderabbit: <policy>}}' for n, o in (r.get('repos') or {}).items()
             if not isinstance(o, dict)]
    rounds = r.get('rounds') or {}
    pairs = rounds.items() if isinstance(rounds, dict) else [('', rounds)]
    errs += [f'review.rounds.{n} is {v!r}: use a whole number, 0 or more (0: that reviewer is not called)'
             for n, v in pairs if isinstance(v, bool) or not isinstance(v, int) or v < 0]
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
    errs += [f'machines.{n}.via names "{m["via"]}", which is not under machines: (the client this machine is reached through)'
             for n, m in cfg['machines'].items() if m.get('via') and m['via'] not in cfg['machines']]
    from . import move
    errs += move.errors(cfg)
    fb = (cfg.get('failover') or {}).get('backup') or ''
    if fb not in ('', 'none') and fb not in cfg['machines']:
        errs.append(f'failover.backup names "{fb}", which is not under machines: name one of them, auto (\'\') or none')
    elif fb not in ('', 'none'):
        from . import failover
        if fb not in failover.candidates(cfg):
            errs.append(f'failover.backup is "{fb}", which cannot be the backup main machine (the main itself, a phone or a '
                        f'member\'s machine): name one of {", ".join(failover.candidates(cfg)) or "(none)"}, auto (\'\') or none')
    from . import devices, share, vpn, workq
    errs += workq.errors(cfg)
    for name, m in cfg['machines'].items():
        errs += share.limit_errors(name, m.get('limit'))
    errs += devices.errors(cfg)
    from . import devclass
    errs += devclass.errors(cfg)
    errs += vpn.errors(cfg)
    for name, w in cfg['workers'].items():
        errs += _worker_errors(name, w)
        if (w or {}).get('machine') and w['machine'] not in cfg['machines']:
            errs.append(f'workers.{name}.machine names "{w["machine"]}", which is not under machines:')
    errs += [f'routing.workers names "{n}", which is not under workers:' for n in cfg['routing'].get('workers') or []
             if n not in cfg['workers']]
    for name, a in cfg['accounts'].items():
        errs += _account_source_errors(name, a or {}) + _account_worker_errors(cfg, name, a or {}) + _account_pace_errors(name, a or {})
    errs += _dashboard_errors(cfg['dashboard']) + _auth_errors(cfg.get('auth'))
    errs += files.errors(cfg) + _test_errors(cfg) + _review_errors(cfg)
    # they import modules that import this one
    from . import autocompact, chat, depbots, dispatch, energy, owner, presence, scouts, selfimprove, sentry, tiers, tz, users
    errs += dispatch.errors(cfg) + tiers.errors(cfg) + tz.errors(cfg) + presence.errors(cfg)
    from . import aiassist, needs, selfheal, welcome
    errs += selfheal.errors(cfg)
    errs += needs.errors(cfg) + welcome.errors(cfg) + aiassist.errors(cfg) + selfimprove.errors(cfg)
    errs += sentry.endpoint_errors((cfg.get('telemetry') or {}).get('endpoint'))
    return errs + autocompact.errors(cfg) + _main_errors(cfg) + _projects_errors(cfg) + _autopilot_errors(cfg) + scouts.errors(cfg) + \
        depbots.errors(cfg) + \
        owner.schedule_errors(cfg['pacing']) + users.errors(cfg) + _update_errors(cfg['update']) + energy.errors(cfg) + \
        chat.errors(cfg)


PACKAGE_NAME = re.compile(r'^[a-z0-9][a-z0-9+.-]*$')
PROGRAM_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


def _update_errors(u):
    if not isinstance(u, dict):
        return [f'update is {u!r}: write it as a mapping, e.g. update: {{auto: true, packages: [cursor]}}, or remove the key']
    errs = [] if isinstance(u.get('auto'), bool) else [f'update.auto is {u.get("auto")!r}: use true or false']
    hours = u.get('every_hours')
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or hours < 1:
        errs.append(f'update.every_hours is {hours!r}: use a number of hours, 1 or more (24: once a day)')
    for key, rule in (('packages', PACKAGE_NAME), ('programs', PROGRAM_NAME)):
        items = u.get(key)
        if not isinstance(items, list):
            errs.append(f'update.{key} is {items!r}: use a list of names, e.g. [cursor]')
            continue
        errs += [f'update.{key} names {n!r}: use a plain {key[:-1]} name such as cursor' for n in items
                 if not isinstance(n, str) or not rule.match(n)]
    return errs


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
    if not isinstance(r.get('household', False), bool):
        errs.append(f'autopilot.repos.{n}.household is {r["household"]!r}: true (its backlog is shared with the household, '
                    'docs/HOUSEHOLD.md) or false')
    if not isinstance(r.get('untested', True), bool):
        errs.append(f'autopilot.repos.{n}.untested is {r["untested"]!r}: true (fill_room queues tests for files no test names) or false')
    if r.get('land', 'pr') not in ('merge', 'pr', 'branch'):
        errs.append(f'autopilot.repos.{n}.land is {r["land"]!r}: use merge (a reviewed result is merged), pr (a pull request a '
                    'person merges) or branch (it waits for a person)')
    return errs


QUIET = re.compile(r'([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d')


def quiet_hours_errors(hours):
    """autopilot.quiet_hours: local-time spans (time.zone) when the autopilot must not run, e.g. ['22:00-06:00'] (the
    planner gives them nothing)."""
    if hours is None:
        return []
    if not isinstance(hours, list) or not all(isinstance(h, str) and QUIET.fullmatch(h) for h in hours):
        return [f'autopilot.quiet_hours is {hours!r}: a list of local-time spans HH:MM-HH:MM, e.g. ["22:00-06:00"] (past midnight wraps)']
    return []


def _autopilot_errors(cfg):
    ap = cfg['autopilot']
    if not isinstance(ap, dict):
        return [f'autopilot is {ap!r}: write it as a mapping, e.g. autopilot: {{enabled: false, accounts: [...], repos: {{...}}}}']
    errs = _autopilot_name_errors(cfg, 'accounts', ap.get('accounts')) + _autopilot_name_errors(cfg, 'ladder', ap.get('ladder'))
    errs += quiet_hours_errors(ap.get('quiet_hours'))
    errs += [f'autopilot.{k} is {ap[k]!r}: use true or false' for k in ('fill_room', 'auto_review') if not isinstance(ap.get(k), bool)]
    from . import parallel
    errs += parallel.errors(ap.get('parallel'))
    for key in ('max_parallel', 'no_progress', 'max_attempts'):
        v = ap.get(key)
        if key == 'max_parallel' and v is None:   # optional: the limit is adaptive (tutti/parallel.py); a number caps it
            continue
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
        errs += _project_errors(cfg, name, p or {})
    return errs


def _project_errors(cfg, name, p):
    if not isinstance(p, dict):
        return [f'projects.{name} is {p!r}: a mapping with match:, machines:, containers:, windows:, repo:, priority:, '
                'commit: or tests:']
    errs = []
    if 'match' in p:
        errs += _regex_errors(f'projects.{name}.match', p['match'])
    errs += [f'projects.{name}.machines names "{m}", which is not under machines:' for m in p.get('machines') or []
             if m not in cfg['machines']]
    win = p.get('windows') or {}
    errs += [f'projects.{name}.windows names "{m}", which is not a wsl machine under machines:' for m in win
             if (cfg['machines'].get(m) or {}).get('transport') != 'wsl']
    pr = p.get('priority', 0)
    if isinstance(pr, bool) or not isinstance(pr, int) or pr < 0:
        errs.append(f'projects.{name}.priority is {pr!r}: a whole number, 0 for the most important project, 1 for the next')
    if 'repo' in p and not (isinstance(p['repo'], str) and re.fullmatch(r'[\w.-]+/[\w.-]+', p['repo'])):
        errs.append(f'projects.{name}.repo is {p["repo"]!r}: the GitHub repository as owner/name, e.g. octo/my-app')
    return errs


def target_errors(key, v):
    """A target_pct: a percentage above 0 and up to 100, or absent (100)."""
    if v is None or (not isinstance(v, bool) and isinstance(v, (int, float)) and 0 < v <= 100):
        return []
    return [f'{key} is {v!r}: the % of the quota to use by its spend-by time, above 0 and up to 100 (default 100)']


def _main_errors(cfg):
    m = cfg['main']
    if not isinstance(m, dict):
        return [f'main is {m!r}: write it as a mapping, e.g. main: {{agent: cursor, fallback: [agy, local]}}']
    fallback = m.get('fallback')
    if not isinstance(fallback, list):
        return [f'main.fallback is {fallback!r}: a list of workers, e.g. [agy, cursor]']
    errs = [f'main.fallback names "{n}", which is not under workers:' for n in fallback if n not in cfg['workers']]
    errs += target_errors('main.target_pct', m.get('target_pct'))
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
    errs += target_errors(f'accounts.{name}.target_pct', a.get('target_pct'))
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


READER_FORMATS = (None, 'github-premium', 'cursor-api')
EXTRA_ACCOUNT_KEYS = {'credits', 'note', 'name'}   # read by the code, not in the Settings tab's schema


def reader_problems(name, a):
    """Why this version cannot read accounts.<name>.reader (an unknown format, a missing command or field), or []."""
    key, r = f'accounts.{name}.reader', a['reader']
    fmt = r.get('format') if isinstance(r, dict) else None
    if fmt not in READER_FORMATS:
        return [f'{key}.format is {fmt!r}, which this Tutti ({_version()}) cannot read (it knows '
                f'{", ".join(f for f in READER_FORMATS if f)} or a command); a newer Tutti may']
    if fmt == 'cursor-api':   # Cursor's account call (tutti/cursorusage.py): no command
        from . import cursorusage
        if r.get('field') not in cursorusage.FIELDS:
            return [f'{key}.field is {r.get("field")!r}: one of {", ".join(cursorusage.FIELDS)} '
                    '(autoPercentUsed is "Cursor Models", apiPercentUsed "Other Models")']
        return []
    return _command_errors(key, r, 'allowance' if fmt == 'github-premium' else 'bucket')


def _version():
    from . import __version__
    return __version__


def lenient(cfg):
    """Warnings, never errors, for what a config written for another Tutti version may hold: an account key this version
    does not know is ignored, and a reader it cannot read is skipped (removed from the account), so the rest of the config
    and the dashboard still start. Kept in cfg['_warnings']."""
    from . import settings_schema  # it imports this module
    known = {k.split('.')[2] for k in settings_schema.FIELDS if k.startswith('accounts.*.')} | EXTRA_ACCOUNT_KEYS
    warns = []
    for name, a in (cfg.get('accounts') or {}).items():
        if not isinstance(a, dict):
            continue
        warns += [f'accounts.{name}.{k} is not a key Tutti {_version()} knows: ignored (a newer Tutti may read it)'
                  for k in a if k not in known]
        if 'reader' in a:
            problems = reader_problems(name, a)
            if problems:
                warns += [f'{p}: this reader is skipped; the account still works with readings by hand' for p in problems]
                a.pop('reader')
    from . import shards  # a shard entry this version cannot use is skipped with a warning
    warns += shards.lenient(cfg)
    cfg['_warnings'] = warns
    return warns


def _account_source_errors(name, a):
    """usage_url, reader, plan and match: where readings come from without the owner's input."""
    key = f'accounts.{name}'
    errs = []
    url = a.get('usage_url')
    if url is not None and (not isinstance(url, str) or not url.startswith('https://')):
        errs.append(f'{key}.usage_url is {url!r}: give the https:// address of the provider\'s usage page')
    if 'plan' in a:   # a reader this version cannot read is skipped with a warning (lenient), never an error
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
    bind = d.get('bind')
    if not isinstance(bind, str) or not bind:
        errs.append(f'dashboard.bind is {bind!r}: use an address such as 127.0.0.1 (another one needs `tutti auth setup`)')
    origins = d.get('origins')
    if not isinstance(origins, list) or not all(isinstance(o, str) and _origin_ok(o.rstrip('/'))
                                                for o in origins):
        errs.append(f'dashboard.origins is {origins!r}: list https origins such as https://tutti.example '
                    '(no path; plain http only on a loopback name)')
    return errs + _shape_errors(d.get('shapes')) + _typical_errors(d.get('typical_minutes'))


def _origin_ok(origin):
    """An https origin, or plain http only on a loopback name: a session cookie never crosses a network in clear text."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(origin)
        port_ok = parts.port is None or parts.port > 0   # a bad port raises ValueError
    except ValueError:
        return False
    if not port_ok or not parts.netloc or parts.path or parts.query or parts.fragment or parts.username:
        return False
    return parts.scheme == 'https' or (parts.scheme == 'http' and parts.hostname in ('127.0.0.1', 'localhost', '::1'))


def _auth_errors(a):
    if not isinstance(a, dict):
        return [f'auth is {a!r}: write it as a mapping, e.g. auth: {{session_hours: 72}}, or remove the key']
    errs = []
    cid = a.get('client_id') or ''
    if cid and not re.fullmatch(r'\d{4,}-[a-z0-9]{8,}\.apps\.googleusercontent\.com', str(cid)):
        errs.append('auth.client_id is not a Google OAuth client ID (…apps.googleusercontent.com): copy it from the console, '
                    'or run `tutti auth setup`')
    if not _positive(a.get('session_hours')):
        errs.append(f'auth.session_hours is {a.get("session_hours")!r}: use hours above 0, e.g. 72')
    return errs


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
    from . import users  # the household's machines this member shares or uses (docs/HOUSEHOLD.md), kept in user.json
    raw = users.apply(raw, users.read(state or DEFAULTS['project']['state']))
    cfg = normalise(raw)
    cfg['raw_entries'] = {k: copy.deepcopy((raw or {}).get(k) or {}) for k in ('workers', 'accounts', 'machines', 'hosts')}
    errs = validate(cfg)
    if errs:
        raise ConfigError(f'{path} has {len(errs)} problem(s):\n  - ' + '\n  - '.join(errs))
    for w in cfg['_warnings']:
        sys.stderr.write(f'tutti: warning: {path}: {w}\n')
    cfg['_path'] = os.path.abspath(path)
    from . import fleet  # the discovered local model workers (<state>/fleet.json), added after validation: built valid
    fleet.apply(cfg)
    from . import tz
    tz.use(cfg)
    return cfg


# A project with no priority comes after every numbered one (projects.<name>.priority: 0 is the most important).
UNRANKED_PRIORITY = 100


def project_priority(cfg, name):
    """projects.<name>.priority (0 is the most important); UNRANKED_PRIORITY for a project without one or not in the config.
    The one helper every scheduler uses: the autopilot's dispatch and fill_room, and idle dispatch."""
    p = ((cfg.get('projects') or {}).get(name) or {}) if name is not None else {}
    v = p.get('priority') if isinstance(p, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else UNRANKED_PRIORITY


def project_of_repo(cfg, repo):
    """The project an autopilot repo belongs to: the one whose self_improve.autopilot_repo names it, else the one whose
    `match` finds the repo's name or folder; None when no project claims it."""
    projects = cfg.get('projects') or {}
    for name, p in projects.items():
        if ((p or {}).get('self_improve') or {}).get('autopilot_repo') == repo:
            return name
    path = (((cfg.get('autopilot') or {}).get('repos') or {}).get(repo) or {}).get('path') or ''
    for name, p in projects.items():
        rx = (p or {}).get('match')
        if isinstance(rx, str) and rx and any(re.search(rx, x, re.I) for x in (repo, path) if x):
            return name
    return None


def repo_priority(cfg, repo):
    return project_priority(cfg, project_of_repo(cfg, repo))


def expand(p):
    return os.path.abspath(os.path.expanduser(p))


def state_dir(cfg):
    d = expand(cfg['project']['state'])
    os.makedirs(d, exist_ok=True)
    return d


def state_dir_of_machine(machine):
    """The state folder a household machine's entry was made under (`tutti household machine use` records it)."""
    return expand(machine.get('household_state') or DEFAULTS['project']['state'])
