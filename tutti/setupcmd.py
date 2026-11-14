"""`tutti setup`: a guided, mostly automatic first run (docs/INSTALL.md). It detects (tutti/setup.py), asks only what it
cannot detect (one question at a time, a recommended default, `--yes` takes every default), adds what is missing to the
config (tutti/configmerge.py), opens each tool's own sign-in, installs the dashboard as a login service, turns on
self-update, fixes what `tutti doctor` can, and saves what it found and what is left to <state>/setup.json for the
dashboard's Set up card. Running it again only fills what is missing."""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import yaml

from . import config, configmerge, doctor, guided, install, setup

PLANS = {'claude': ('pro', 'max5', 'max20'), 'cursor': ('pro', 'pro+', 'ultra'), 'agy': ('free', 'pro', 'ultra'),
         'copilot': ('pro', 'pro+')}
WINDOWS = {'claude': '7d', 'cursor': 'monthly', 'agy': '7d', 'copilot': 'monthly'}
MAIN_ORDER = ('claude', 'cursor', 'agy', 'copilot')


class Asker:
    """Answers with the recommended default (zero questions, the default: docs/INSTALL.md, *The rule for every setup*);
    with `yes=False` (`tutti setup --ask`) it asks one question at a time instead."""

    def __init__(self, yes=True, read=input, out=sys.stdout):
        self.yes, self.read, self.out = yes, read, out

    def ask(self, question, default):
        if self.yes:
            return default
        try:
            got = self.read(f'{question} [{default}] ').strip()
        except EOFError:
            return default
        return got or default

    def confirm(self, question, default=True):
        return self.ask(question, 'Y' if default else 'n').lower().startswith('y')


def next_monday(now):
    days = (7 - now.weekday()) % 7 or 7
    return (now + timedelta(days=days)).strftime('%Y-%m-%dT00:00Z')


def account(a, asker, now):
    """The account entry of a found agent: its window, an estimated reset, and its plan: the tool's own plan command
    where it has one (the card reads it), else the owner's answer as `subscription` (a label)."""
    window = WINDOWS[a['name']]
    entry = {'window': window, 'resets': next_monday(now) if window == '7d' else now.strftime('%Y-%m-01T00:00Z'),
             'resets_estimated': True}
    spec = setup.AGENTS[a['name']].get('plan')
    if spec:
        return {**entry, 'plan': {**spec, 'every_minutes': 360}}
    choices = PLANS[a['name']]
    return {**entry, 'subscription': asker.ask(f'Your {a["name"]} plan ({", ".join(choices)})?', choices[0])}


class Additions:
    """What to add: {section: {name: value}} and {section.name: why}; a name the config already has is never added."""

    def __init__(self, raw):
        self.raw, self.add, self.why, self.choices = raw, {}, {}, []

    def has(self, section, name):
        return name in (self.raw.get(section) or {})

    def put(self, section, name, value, reason):
        if not self.has(section, name):
            self.add.setdefault(section, {})[name] = value
            self.why[f'{section}.{name}'] = reason


def plan_agents(x, found, asker, now):
    for a in found['agents']:
        x.put('workers', a['name'], setup.AGENTS[a['name']]['worker'], f'found {a["bin"]} on PATH')
        if not x.has('accounts', a['name']):
            entry = account(a, asker, now)
            x.put('accounts', a['name'], entry, 'the reset is a guess until the first reading')
            if 'subscription' in entry:
                x.choices.append(guided.choice(f'accounts.{a["name"]}.subscription', entry['subscription'],
                                               'no tool says the plan; the most common one'))
            x.choices.append(guided.choice(f'accounts.{a["name"]}.resets', entry['resets'], 'a guess until the first usage reading'))
    if found['ollama'] and not any((w or {}).get('kind') == 'ollama' for w in (x.raw.get('workers') or {}).values()):
        x.put('workers', 'local', {'kind': 'ollama', 'host': '127.0.0.1:11434', 'model': found['ollama'][0]}, 'ollama list')


def plan_machines(x, found, asker):
    if not x.raw.get('machines'):
        x.put('machines', 'here', {'host': 'local', 'slots': 1}, 'this computer')
    ssh = [m for m in found['machines'] if m['source'] == 'ssh' and not x.has('machines', m['name'])]
    if ssh and asker.confirm(f'Use these machines from ~/.ssh/config for heavy runs: {", ".join(m["name"] for m in ssh)}?'):
        for m in ssh:
            x.put('machines', m['name'], {'host': m['host'], 'slots': 1}, 'from ~/.ssh/config')
            x.choices.append(guided.choice(f'machines.{m["name"]}', 'used for heavy runs, 1 slot', 'a Host in ~/.ssh/config'))
    for d in found['devices']:
        if d['state'] == 'device':
            x.put('devices', (d['model'] or d['serial']).lower().replace(':', '-'), {'serial': d['serial'], 'machine': 'here'}, 'adb')


def plan_projects(x, found, asker):
    known = {(p or {}).get('match') for p in (x.raw.get('projects') or {}).values()}
    new = [r for r in found['repos'] if r['name'] not in known and not x.has('projects', r['name'])]
    if not new:
        return
    names = ', '.join(r['name'] for r in new[:8]) + (f' and {len(new) - 8} more' if len(new) > 8 else '')
    if asker.confirm(f'Show these projects on the dashboard, most recently worked on first: {names}?'):
        for r in new:
            x.put('projects', r['name'], {'match': r['name'], 'priority': r['priority']}, r['path'])
        x.choices.append(guided.choice('projects', f'{len(new)} added', 'git repositories found; the most recently worked on first'))


def plan(found, raw, asker, now=None):
    """(additions {section: {name: value}}, {section.name: why}, top-level keys to add); only what the config lacks."""
    now = now or datetime.now(timezone.utc)
    x = Additions(raw or {})
    plan_agents(x, found, asker, now)
    plan_machines(x, found, asker)
    plan_projects(x, found, asker)
    top = {}
    main = next((n for n in MAIN_ORDER if any(a['name'] == n for a in found['agents'])), None)
    if 'main' not in x.raw and main:
        top['main'] = {'agent': main}
        x.choices.append(guided.choice('main.agent', main, 'the first found of Claude, Cursor, Antigravity, Copilot'))
    if 'self_update' not in x.raw:
        top['self_update'] = {'enabled': True}
    plan.choices = x.choices   # the defaults picked, for the report (read by write_additions)
    return x.add, x.why, top


def apply(text, add, why, top):
    """(new text, [sections that could not be written]): every addition merged, comments kept."""
    stuck = []
    text = text or configmerge.HEADER
    for section, entries in add.items():
        merged = configmerge.add(text, section, entries, {k: why.get(f'{section}.{k}') for k in entries})
        if merged is None:
            stuck.append(section)
        else:
            text = merged
    for key, value in top.items():
        text = configmerge.set_top(text, key, value, 'tutti setup' if key != 'self_update' else 'opt out: enabled: false')
    return text, stuck


def write_config(path, text):
    """Writes the config after checking it loads; the old one is kept as <path>.bak."""
    errs = config.validate(config.normalise(yaml.safe_load(text) or {}))
    if errs:
        raise config.ConfigError('tutti setup made a config that does not check out (please report it):\n  - ' + '\n  - '.join(errs))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + '.bak')
    config.write_text(path, text)


def systemd_missing(platform, run):
    """The reason no systemd user session can start the dashboard here, or None."""
    try:
        ok = run(['systemctl', '--user', 'show-environment'], capture_output=True, text=True, check=False).returncode == 0
    except OSError:
        ok = False
    if ok:
        return None
    if platform == 'wsl':
        return ('WSL without systemd: add "[boot] systemd=true" to /etc/wsl.conf (sudo), run `wsl --shutdown` in Windows, '
                'then `tutti setup` again')
    return 'no systemd user session here: start `tutti dashboard` from your desktop\'s autostart instead'


def write_changed(files):
    """Writes each (path, text) whose file differs; True when any did."""
    changed = False
    for path, text in files:
        old = open(path, encoding='utf8').read() if os.path.exists(path) else None
        if old != text:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf8') as f:
                f.write(text)
            changed = True
    return changed


def service(platform, home_dir, run=subprocess.run, start=True):
    """('ok'|'todo', detail). Writes the login service and starts it; unchanged files are left alone (idempotent).
    start=False (a dashboard already runs, e.g. setup was started from its Set up card): enabled for the next login only,
    so a second dashboard never fights the first for the port."""
    missing = systemd_missing(platform, run) if platform in ('linux', 'wsl') else None
    if missing:
        return 'todo', missing
    target = platform if platform in ('win32', 'darwin') else 'linux'
    # the `tutti` on PATH (a release, a uv or pipx install, a checkout's link), else where a release install puts it
    files, cmds = install.service_plan(target, home_dir, tutti=shutil.which('tutti.cmd' if target == 'win32' else 'tutti'))
    if not start:
        cmds = [[x for x in argv if x != '--now'] for argv in cmds if argv[0] != 'launchctl']   # launchd loads it at login
    if not write_changed(files):
        return 'ok', f'the dashboard starts at login ({files[0][0]})'
    for argv in cmds:
        r = run(argv, capture_output=True, text=True, check=False)
        # linger (start at boot, before a login) is a bonus: without it the dashboard still starts at login
        if r.returncode != 0 and argv[:2] != ['launchctl', 'unload'] and argv[0] != 'loginctl':
            return 'todo', f'`{" ".join(argv)}` failed ({(r.stderr or r.stdout).strip()[:120]}): run it by hand, then `tutti setup`'
    return 'ok', f'the dashboard now starts at login ({files[0][0]})'


def comes_first(dirs, first, second):
    return first in dirs and second in dirs and dirs.index(first) < dirs.index(second)


def fix_git(problems, path=None, bin_dir=None):
    """('ok'|'todo', detail): when the first git cannot do HTTPS and a working one exists, a `git` wrapper in the user's bin
    (when that folder comes first in PATH) points at the working one."""
    if not problems:
        return 'ok', 'git works over HTTPS'
    msg = problems[0]
    bad = msg.split(' cannot do HTTPS')[0]
    bin_dir = bin_dir or os.path.expanduser('~/.local/bin')
    dirs = (path or os.environ.get('PATH', '')).split(os.pathsep)
    if ' works' not in msg or os.name == 'nt' or not comes_first(dirs, bin_dir, os.path.dirname(bad)):
        return 'todo', msg
    good = msg.rsplit('; ', 1)[1].removesuffix(' works')
    wrapper = os.path.join(bin_dir, 'git')
    with open(wrapper, 'w', encoding='utf8') as f:
        f.write(f'#!/bin/sh\n# tutti setup: {bad} cannot do HTTPS; this runs the working git\nexec "{good}" "$@"\n')
    os.chmod(wrapper, stat.S_IRWXU)   # the owner's own command: read, write and run by them only
    return 'ok', f'git: {wrapper} now runs {good} (the first git in PATH could not do HTTPS)'


def item(id_, title, state, detail, command=None, fix=None):
    return {'id': id_, 'title': title, 'state': state, 'detail': detail, 'command': command, 'fix': fix}


def login_rows(found, logins):
    rows = []
    for a in found['agents']:
        state = {'yes': 'ok', 'no': 'todo'}.get(logins.get(a['name'], a['login']), 'info')
        detail = {'ok': 'signed in', 'todo': 'not signed in: its own sign-in opens in a terminal'}.get(
            state, 'Tutti cannot tell (the tool keeps it to itself): if it asks, sign in with the command')
        command = None if state == 'ok' else ' '.join(a['login_command'])
        rows.append(item(f'login-{a["name"]}', f'{a["name"]} sign-in', state, detail, command))
    if not found['agents']:
        rows.append(item('agents', 'AI agents', 'todo', 'no agent CLI found (claude, cursor-agent, agy, copilot): install one, '
                         'then `tutti setup` again', 'tutti setup'))
    gh = found['gh']
    rows.append(item('gh', 'GitHub sign-in', 'ok' if gh == 'yes' else 'todo',
                     {'yes': 'gh is signed in', 'no': 'gh is not signed in', 'missing': 'gh is missing'}[gh],
                     None if gh == 'yes' else 'gh auth login --web'))
    return rows


def found_rows(found, stuck):
    machines = [item(f'machine-{m["name"]}', f'Machine {m["name"]}', 'info',
                     f'found on {m["source"]}: give it ssh access from here (an ~/.ssh/config Host), then `tutti setup` again',
                     f'ssh-copy-id {m["host"]}') for m in found['machines'] if m['source'] != 'ssh']
    devices = [item(f'device-{d["serial"]}', f'Android {d["model"] or d["serial"]}', 'todo',
                    f'adb says "{d["state"]}": allow USB debugging on the phone', 'adb devices')
               for d in found['devices'] if d['state'] != 'device']
    configs = [item(f'config-{s}', f'Config: {s}', 'todo', f'{s}: is written inline ({{...}}): make it a block so setup can add to it',
                    'tutti config') for s in stuck]
    return machines + devices + configs


def report_items(found, logins, svc, upd, git, stuck):
    """The Set up card's rows: what setup found and what is left, each with its command or one-click fix."""
    rows = login_rows(found, logins)
    if not found['git']:
        rows.append(item('git', 'git', 'todo', 'git is missing: Tutti needs it for worktrees',
                         'sudo apt install git  # or brew install git'))
    rows.extend([
        item('git-https', 'git over HTTPS', git[0], git[1], 'tutti doctor' if git[0] != 'ok' else None),
        item('service', 'Dashboard at login', svc[0], svc[1], None, 'service' if svc[0] != 'ok' else None),
        item('update', 'Automatic updates', upd[0], upd[1], 'tutti self-update', None),
        item('gpus', 'GPUs', 'info', ', '.join(g['name'] for g in found['gpus']) or 'none found'),
        item('models', 'Local models', 'info', ', '.join(found['ollama'] or []) or 'no ollama models'),
    ])
    return rows + found_rows(found, stuck)


def save_report(cfg, rows, path, steps=()):
    out = {'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'config': path, 'items': rows, 'steps': list(steps),
           'left': sum(1 for r in rows if r['state'] == 'todo'), 'install_lines': {p: install.install_line(p) for p in ('linux', 'win32')}}
    target = os.path.join(config.state_dir(cfg), 'setup.json')
    with open(target, 'w', encoding='utf8') as f:
        json.dump(out, f, indent=2)
    return out


def read_report(cfg):
    try:
        with open(os.path.join(config.state_dir(cfg), 'setup.json'), encoding='utf8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def human_steps(rows, found):
    """What only the person can do, from the report's rows left to do: sign-ins, a sudo line, a tap on a phone."""
    kinds = {'login-': 'sign-in', 'gh': 'sign-in', 'git': 'sudo', 'device-': 'phone', 'service': 'sudo'}
    steps = []
    for r in rows:
        kind = next((k for p, k in kinds.items() if r['id'] == p or (p.endswith('-') and r['id'].startswith(p))), None)
        if r['state'] == 'todo' and kind and (r['command'] or kind == 'phone'):
            steps.append(guided.step(kind, r['title'], r['command'], r['detail']))
    return steps


def sign_ins(steps, runner, out):
    """Opens each sign-in left, one after another, in the tool's own window (Tutti never sees a credential)."""
    for s in steps:
        if s['kind'] == 'sign-in':
            print(f'\n{s["title"]}: opening its own sign-in ({s["command"]}); finish it there.', file=out)
            argv = ['gh', 'auth', 'login', '--web', '--git-protocol', 'https'] if s['command'].startswith('gh ') else s['command'].split()
            runner(argv, check=False)


def summary(found, out):
    names = ', '.join(f'{a["name"]} ({"signed in" if a["login"] == "yes" else a["login"]})' for a in found['agents']) or 'none'
    print(f'found: agents {names}; gh {found["gh"]}; GPUs {", ".join(g["name"] for g in found["gpus"]) or "none"}; '
          f'models {len(found["ollama"] or [])}; machines {len(found["machines"])}; Android {len(found["devices"])}; '
          f'repos {len(found["repos"])}', file=out)


def write_additions(path, text, found, asker, out):
    """[sections that could not be written]; the config gets what it lacks."""
    add, why, top = plan(found, yaml.safe_load(text) or {}, asker)
    choices = plan.choices
    if not (add or top):
        print(f'config: {path} already has everything setup can fill', file=out)
        return [], choices
    new, stuck = apply(text, add, why, top)
    write_config(path, new)
    added = [f'{len(e)} {s}' for s, e in add.items()] + list(top)
    print(f'config: added {", ".join(added)} to {path} (your comments kept; the old file is {path}.bak)', file=out)
    return stuck, choices


def updates_row(cfg):
    if (cfg.get('self_update') or {}).get('enabled', True):
        return 'ok', 'on: Tutti updates itself from checksummed releases (self_update.enabled: false turns it off)'
    return 'info', 'off (self_update.enabled: false)'


def connect_rows(path, steps=None):
    """Every installed agent gets Tutti's MCP server (`tutti connect --all`, docs/CONNECT.md): one row each. The restarts and
    refreshes it needs join `steps`, the one list at the end."""
    from . import connect  # connect imports config only; imported here so setup stays light
    rows, results = [], connect.connect_all(path)
    if steps is not None:
        steps.extend(connect.human_steps(results))
    for r in results:
        if r['status'] in ('not-installed', 'cannot'):
            continue
        ok = r['status'] in ('connected', 'needs-restart')
        note = r.get('note') or ('connected: ask it to hand work to Tutti' if ok else 'not connected')
        rows.append(item(f'connect-{r["agent"]}', f'{r["agent"]} connected to Tutti', 'ok' if ok else 'todo', note,
                         None if ok else f'tutti connect {r["agent"]}'))
    return rows


def finish(cfg, rows, choices, steps, path, out):
    """Saves the report (the defaults picked earlier stay listed with their Change link) and prints the one list."""
    previous = [r for r in (read_report(cfg) or {}).get('items', []) if r['state'] == 'default']
    new = guided.choice_rows(choices)
    keep = [r for r in previous if r['id'] not in {n['id'] for n in new}]
    report = save_report(cfg, rows + new + keep, path, steps)
    print(f'setup: done; {len(new + keep)} default(s) picked for you (the dashboard\'s Set up card: Change)', file=out)
    print(guided.render(steps), file=out)
    return report


def run(a, probe=None, asker=None, platform=None, home_dir=None, runner=subprocess.run, out=sys.stdout):
    """The whole first run; every step skips what is already done."""
    path = a.config or config.find() or config.xdg_config_path()
    text = open(path, encoding='utf8').read() if os.path.exists(path) else ''
    probe = probe or setup.Probe()
    asker = asker or Asker(yes=not getattr(a, 'ask', False), out=out)
    found = setup.detect(probe, me=socket.gethostname())
    summary(found, out)
    first = read_report(config.normalise(yaml.safe_load(text) or {})) is None if text else True
    stuck, choices = write_additions(path, text, found, asker, out)
    cfg = config.load(path)
    svc = ('todo', 'not installed (--no-service): `tutti setup` again to start the dashboard at login')
    if a.service:
        svc = service(platform or install.platform_name(), home_dir or install.home(), runner, start=not a.from_dashboard)
    rows = report_items(found, {}, svc, updates_row(cfg), fix_git(doctor.check_git()), stuck)
    restarts = []
    rows += connect_rows(path, restarts) if a.connect else []
    steps = human_steps(rows, found) + restarts
    finish(cfg, rows, choices, steps, path, out)
    interactive = sys.stdin.isatty() and not a.from_dashboard
    if interactive and steps:
        sign_ins(steps, runner, out)
    if interactive and first and svc[0] == 'ok':
        import webbrowser  # the first run opens the dashboard: install it as an app from the browser's menu
        webbrowser.open(f'http://127.0.0.1:{cfg["dashboard"]["port"]}/')
    if a.join:
        from . import cli  # cli imports this module
        return cli.main(['--config', path, 'household', 'join', a.join])
    return 0
