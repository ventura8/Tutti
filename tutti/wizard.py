"""The setup wizard's one step engine (docs/SETUP-WIZARD.md): two front-ends, the dashboard's Setup view (tutti/web/wizard.js,
GET and POST /api/wizard) and the terminal's (`tutti setup --tui`, tutti/wizardtui.py), show the same steps from here.

Each step is drawn from what `tutti setup` detects (tutti/setup.py) and the config: a status (done, needs-you, optional,
skipped), rows that say what is there (each with the exact command when only the person can do it: a tool's own sign-in,
a phone's pairing), and fields prefilled with the detected or current value. Applying a step changes only what differs
(idempotent), writes through yamledit/configmerge (comments kept, the config checked, the old file kept as .bak) and
records the step in <state>/wizard.json, so the wizard resumes where it was left. Secrets never pass through here: sign-ins
open in each tool's own window, and wizard.json holds step ids and times only."""
import json
import os
import re
import socket
import subprocess
import time
from datetime import datetime, timezone

from . import config, configmerge, durable, failover, setup, yamledit

API = '/api/wizard'
FILE = 'wizard.json'
DETECT_SECONDS = 300   # the dashboard's detection is reused for this long (ssh, Tailscale and adb take seconds)
PLANS = {'claude': ('pro', 'max5', 'max20'), 'cursor': ('pro', 'pro+', 'ultra'), 'agy': ('free', 'pro', 'ultra'),
         'copilot': ('pro', 'pro+')}
AGENT_TITLES = {'claude': 'Claude Code', 'cursor': 'Cursor', 'agy': 'Antigravity (Gemini and Claude models)',
                'copilot': 'GitHub Copilot (opt-in: it costs money per request)'}
NWO = re.compile(r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')
HOURS = re.compile(r'^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$')
# (id, title, household-wide: an admin's step)
STEPS = (
    ('welcome', 'Welcome', False),
    ('main', 'Main and backup machines', True),
    ('machines', 'Machines, shares and quiet hours', True),
    ('accounts', 'Accounts and agents', True),
    ('github', 'GitHub and repositories', True),
    ('devices', 'Android devices', True),
    ('integrations', 'Integrations', True),
    ('household', 'Household', True),
    ('review', 'Review and finish', False),
)
TITLES = {s[0]: s[1] for s in STEPS}
# Integrations another change plugs in with register() (feat/t181: Linear, Slack, Discord); until then each is listed as coming.
INTEGRATIONS = {
    'linear': {'title': 'Linear', 'what': 'issues become backlog tasks, and their state follows the work'},
    'slack': {'title': 'Slack', 'what': 'questions and finished work reach you in a channel'},
    'discord': {'title': 'Discord', 'what': 'questions and finished work reach you in a channel'},
}
_PLUGINS = {}


class WizardError(RuntimeError):
    """A step could not be applied; the message names the field, why, and what to do."""


def register(name, title, state_fn, apply_fn=None, what=''):
    """Plugs an integration into the Integrations step: state_fn(cfg) -> (state 'ok'|'todo'|'info', detail, command or
    None); apply_fn(cfg, value) runs when the person turns it on (value True) in the wizard. Secrets stay in the
    integration's own sign-in or secret store, never in the wizard."""
    _PLUGINS[name] = {'title': title, 'state': state_fn, 'apply': apply_fn, 'what': what}


# ---- progress (<state>/wizard.json: step ids and times, nothing else)


def progress(cfg):
    try:
        with open(os.path.join(config.state_dir(cfg), FILE), encoding='utf8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def mark(cfg, sid, how='done'):
    """Records a step as done or skipped (a later apply of the step clears the skip)."""
    p = progress(cfg)
    done, skipped = list(p.get('done') or []), list(p.get('skipped') or [])
    done, skipped = [s for s in done if s != sid], [s for s in skipped if s != sid]
    (done if how == 'done' else skipped).append(sid)
    p.update(done=done, skipped=skipped, at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    if sid == 'review' and how == 'done':
        p['finished'] = p['at']
    durable.dump_json(os.path.join(config.state_dir(cfg), FILE), p)
    return p


# ---- detection


def remote_repos(probe):
    """[{name, repo}] of the person's own GitHub repositories (`gh repo list`), [] when gh is missing or signed out."""
    code, out = probe.run(['gh', 'repo', 'list', '--json', 'name,nameWithOwner', '--limit', '100'])
    try:
        rows = json.loads(out) if code == 0 else []
    except ValueError:
        return []
    return [{'name': r['name'], 'repo': r['nameWithOwner']} for r in rows
            if isinstance(r, dict) and NWO.match(r.get('nameWithOwner') or '')]


def detect(probe=None):
    """What the wizard shows that the config does not hold: setup.detect plus the person's GitHub repositories."""
    probe = probe or setup.Probe()
    found = setup.detect(probe, me=socket.gethostname())
    found['remote_repos'] = remote_repos(probe) if found['gh'] == 'yes' else []
    found['home'] = probe.home
    return found


def cached(holder, force=False, detect_fn=None, clock=time.time):
    """The dashboard's detection, kept on `holder` for DETECT_SECONDS (a test's collector sets `wizard_detect`)."""
    fn = detect_fn or getattr(holder, 'wizard_detect', None) or detect
    hit = getattr(holder, '_wizard_found', None)
    if force or not hit or clock() - hit[0] > DETECT_SECONDS:
        hit = (clock(), fn())
        holder._wizard_found = hit
    return hit[1]


# ---- the steps' views (pure: config, detection, progress)


def row(title, state, detail, command=None, runnable=False):
    """A line of a step: state ok | todo | info; `command` the exact line only the person can run (Copy, or Run in the TUI
    when `runnable`: a tool's own sign-in)."""
    return {'title': title, 'state': state, 'detail': detail, 'command': command, 'runnable': runnable}


def field(key, label, kind, value, options=None, **extra):
    """An input: kind choice | multi | bool | number | text; options [{value, label, note}]."""
    return {'key': key, 'label': label, 'kind': kind, 'value': value, 'options': options or [], **extra}


def opt(value, label, note=''):
    return {'value': value, 'label': label, 'note': note}


def machine_power(m):
    bits = [f'{m.get("slots", 1)} slot{"s" if m.get("slots", 1) != 1 else ""}', m.get('transport') or 'ssh']
    if m.get('use_pct') is not None and m.get('use_pct') != 100:
        bits.append(f'{m["use_pct"]} % share')
    return ', '.join(bits)


def view_welcome(cfg, found, p):
    agents = ', '.join(a['name'] for a in found['agents']) or 'none yet'
    rows = [row('What Tutti does', 'info', 'Tutti runs your coding agents on your machines and accounts, paces their usage, keeps '
                'a backup main machine ready, and shows everything here. Each step below is prefilled with what it found; '
                'change only what is wrong. You can leave and come back: the wizard resumes where you were.'),
            row('Found on this computer', 'info', f'agents: {agents}; machines: {len(cfg["machines"])} in the config, '
                f'{len(found["machines"])} discovered; Android devices: {len(found["devices"])}; repositories: {len(found["repos"])}'),
            row('What Tutti never does', 'info', 'it never sees a password or token: each sign-in opens in the tool\'s own window, '
                'and nothing secret is written to the config or shown here')]
    return {'status': 'done' if 'welcome' in (p.get('done') or []) else 'needs-you', 'rows': rows, 'fields': []}


def candidates(cfg, found):
    """[{name, label, note, in_config}] for the main and backup pickers: the config's machines, then ssh hosts not in it."""
    out = []
    o = failover.opts(cfg)
    for n, m in cfg['machines'].items():
        if m.get('transport') in ('android', 'household'):
            continue
        role = 'main' if n == failover.main_of(cfg) else 'backup' if n == o['backup'] else 'machine'
        out.append({'name': n, 'label': n, 'note': f'{role}; {machine_power(m)}', 'in_config': True})
    for d in found['machines']:
        if d['name'] in cfg['machines']:
            continue
        where = {'ssh': 'in ~/.ssh/config, added to the config when picked', 'tailscale': 'on Tailscale',
                 'lan': 'on the LAN'}[d['source']]
        state = '' if d.get('online', True) else ', offline'
        out.append({'name': d['name'], 'label': d['name'], 'note': f'{where}{state}{"" if d["source"] == "ssh" else "; needs ssh first"}',
                    'in_config': False, 'source': d['source'], 'host': d['host']})
    return out


def view_main(cfg, found, p):
    o, cands = failover.opts(cfg), candidates(cfg, found)
    main = failover.main_of(cfg)
    main = main if main in cfg['machines'] else next((n for n, m in cfg['machines'].items() if m.get('host') == 'local'), main)
    pickable = [c for c in cands if c['in_config'] or c.get('source') == 'ssh']
    backup = o['backup'] or 'auto'
    rows = [row('Main machine', 'ok', f'{main}: runs the dashboard, the coordinator, the autopilot and the device leases'),
            row('Backup main machine', 'ok' if o['backup'] not in ('', 'none') else 'info',
                {'': 'Tutti elects the fittest machine (auto)', 'none': 'off: no failover'}.get(o['backup'], o['backup'])
                + '; it holds a warm copy and takes over when the main is silent (docs/FAILOVER.md)')]
    rows += [row(f'{c["name"]} needs ssh', 'todo', f'found {c["note"]}: give it key-based ssh from here, then check again',
                 f'ssh-copy-id {c["host"]}') for c in cands if not c['in_config'] and c.get('source') != 'ssh']
    fields = [field('main', 'Main machine', 'choice', main, [opt(c['name'], c['label'], c['note']) for c in pickable]),
              field('backup', 'Backup main machine', 'choice', backup,
                    [opt('auto', 'Auto', 'Tutti picks the fittest machine')] + [opt(c['name'], c['label'], c['note']) for c in pickable]
                    + [opt('none', 'None', 'no failover')]),
              field('install', 'Install the failover service on both machines now', 'bool', False,
                    note='copies Tutti, its config and state to the backup over ssh and starts its round as a user service')]
    return {'status': 'done' if 'main' in (p.get('done') or []) else 'needs-you', 'rows': rows, 'fields': fields}


def view_machines(cfg, found, p):
    rows, fields = [], []
    for n, m in cfg['machines'].items():
        if m.get('transport') == 'household':
            continue
        hours = m.get('quiet_hours') or []
        rows.append(row(n, 'ok', f'{machine_power(m)}; quiet hours {", ".join(hours) or "none"}'))
        fields.append(field(f'machines.{n}.use_pct', f'{n}: share for Tutti (%)', 'number', int(m.get('use_pct', 100)), min=0, max=100))
        fields.append(field(f'machines.{n}.quiet_hours', f'{n}: quiet hours (HH:MM-HH:MM, comma-separated)', 'text', ', '.join(hours),
                            placeholder='22:00-07:00'))
    new = [d for d in found['machines'] if d['source'] == 'ssh' and d['name'] not in cfg['machines']]
    if new:
        fields.append(field('add', 'Add these ssh hosts as machines', 'multi', [],
                            [opt(d['name'], d['name'], d['host']) for d in new]))
    status = 'done' if 'machines' in (p.get('done') or []) else 'optional'
    return {'status': status, 'rows': rows, 'fields': fields}


def account_row(a, cfg):
    title = AGENT_TITLES.get(a['name'], a['name'])
    if a['login'] == 'yes':
        return row(title, 'ok', 'signed in' + (f'; plan {a["plan"]}' if a.get('plan') else ''))
    state = 'todo' if a['login'] == 'no' else 'info'
    why = 'not signed in: its own sign-in opens in a terminal' if a['login'] == 'no' else \
        'Tutti cannot tell (the tool keeps its login to itself): sign in if it asks'
    return row(title, state, why, ' '.join(a['login_command']), runnable=True)


def view_accounts(cfg, found, p):
    rows = [account_row(a, cfg) for a in found['agents']]
    if not found['agents']:
        rows.append(row('AI agents', 'todo', 'no agent found (claude, cursor-agent, agy, copilot): install one, then check again'))
    models = found.get('ollama') or []
    rows.append(row('Local models', 'ok' if models else 'info', ', '.join(models) or 'no ollama models on this computer'))
    accts, workers = cfg.get('accounts') or {}, cfg.get('workers') or {}
    fields = []
    for a in found['agents']:
        if a['name'] == 'copilot':
            fields.append(field('copilot', 'Use GitHub Copilot as a worker (it costs money per request)', 'bool', 'copilot' in workers))
            continue
        if setup.AGENTS[a['name']].get('plan'):
            continue   # the tool says its plan itself
        cur = (accts.get(a['name']) or {}).get('subscription') or PLANS[a['name']][0]
        fields.append(field(f'plan.{a["name"]}', f'{AGENT_TITLES[a["name"]]}: your plan', 'choice', cur,
                            [opt(x, x) for x in PLANS[a['name']]]))
    if models:
        has = any((w or {}).get('kind') == 'ollama' for w in workers.values())
        fields.append(field('local', f'Use the local model {models[0]} as a worker', 'bool', has))
    left = any(r['state'] == 'todo' for r in rows)
    status = 'needs-you' if left else 'done' if 'accounts' in (p.get('done') or []) else 'optional'
    return {'status': status, 'rows': rows, 'fields': fields}


def view_github(cfg, found, p):
    from . import github, githubcmd
    app = os.path.exists(github.token_path(cfg, githubcmd.OWNER))
    gh = found['gh']
    rows = [row('GitHub CLI', 'ok' if gh == 'yes' else 'todo', {'yes': 'signed in', 'no': 'not signed in',
                                                                  'missing': 'gh is not installed'}[gh],
                None if gh == 'yes' else 'gh auth login --web --git-protocol https', runnable=gh == 'no'),
            row('Tutti\'s GitHub App', 'ok' if app else 'info', 'connected' if app else
                'optional: pull requests opened as the App, not as you (docs/GITHUB.md)', None if app else 'tutti github connect', True)]
    known = {(x or {}).get('match') for x in (cfg.get('projects') or {}).values()} | set(cfg.get('projects') or {})
    local = [r for r in found['repos'] if r['name'] not in known]
    here = {r['name'] for r in found['repos']}
    remote = [r for r in found.get('remote_repos') or [] if r['name'] not in here]
    fields = []
    if local:
        fields.append(field('projects', 'Manage these repositories (found under your project folders, most recent first)', 'multi',
                            [r['name'] for r in local], [opt(r['name'], r['name'], r['path']) for r in local]))
    if remote:
        fields.append(field('clone', 'Clone these GitHub repositories into ~/Projects and manage them', 'multi', [],
                            [opt(r['repo'], r['repo']) for r in remote]))
    status = 'needs-you' if gh != 'yes' else 'done' if 'github' in (p.get('done') or []) or not local else 'optional'
    return {'status': status, 'rows': rows, 'fields': fields}


def view_devices(cfg, found, p):
    rows = []
    for d in found['devices']:
        ok = d['state'] == 'device'
        rows.append(row(f'Android {d["model"] or d["serial"]}', 'ok' if ok else 'todo',
                        'ready over adb' if ok else f'adb says "{d["state"]}": allow USB debugging on the device',
                        None if ok else 'adb devices'))
    configured = cfg.get('devices') or {}
    rows += [row(f'{n} (in the config)', 'ok', (d or {}).get('serial', '')) for n, d in configured.items()]
    rows.append(row('Pair a phone or tablet over Wi-Fi', 'info', 'on the device: Developer options, Wireless debugging, Pair with QR '
                    'code; the Devices card on the dashboard shows the QR, the terminal prints it', 'tutti devices pair', True))
    left = any(r['state'] == 'todo' for r in rows)
    status = 'needs-you' if left else 'done' if configured or 'devices' in (p.get('done') or []) else 'optional'
    return {'status': status, 'rows': rows, 'fields': [], 'links': [{'href': '#dashboard', 'label': 'Devices card'}]}


def view_integrations(cfg, found, p):
    rows, fields = [], []
    for name, spec in INTEGRATIONS.items():
        plug = _PLUGINS.get(name)
        if not plug:
            rows.append(row(spec['title'], 'info', f'coming soon: {spec["what"]}'))
            continue
        state, detail, command = plug['state'](cfg)
        rows.append(row(plug['title'], state, detail, command, runnable=bool(command)))
        if plug['apply']:
            fields.append(field(f'integration.{name}', f'Turn on {plug["title"]}', 'bool', state == 'ok'))
    for name, plug in _PLUGINS.items():
        if name not in INTEGRATIONS:
            state, detail, command = plug['state'](cfg)
            rows.append(row(plug['title'], state, detail, command, runnable=bool(command)))
            if plug['apply']:
                fields.append(field(f'integration.{name}', f'Turn on {plug["title"]}', 'bool', state == 'ok'))
    status = 'done' if 'integrations' in (p.get('done') or []) else 'optional'
    return {'status': status, 'rows': rows, 'fields': fields}


def view_household(cfg, found, p):
    rows = [row('Invite someone', 'info', 'the Users tab makes an invite link (viewer or admin) with sign-in on (docs/AUTH.md); the '
                'person opens it and runs the one install line it shows', None),
            row('Or a join code', 'info', 'for a member on your LAN or VPN without the dashboard\'s sign-in (docs/HOUSEHOLD.md)',
                'tutti household code', True)]
    status = 'done' if 'household' in (p.get('done') or []) else 'optional'
    return {'status': status, 'rows': rows, 'fields': [], 'links': [{'href': '#users', 'label': 'Users tab'}]}


VIEWS = {'welcome': view_welcome, 'main': view_main, 'machines': view_machines, 'accounts': view_accounts, 'github': view_github,
         'devices': view_devices, 'integrations': view_integrations, 'household': view_household}


def view_review(steps, p):
    rows = [row(s['title'], {'done': 'ok', 'needs-you': 'todo'}.get(s['status'], 'info'), s['status']) for s in steps]
    return {'status': 'done' if p.get('finished') else 'needs-you', 'rows': rows, 'fields': []}


def view(cfg, found, admin=True):
    """The whole wizard: {first_run, admin, current, steps: [{id, title, admin, locked, status, rows, fields, links}]}. A
    viewer sees the household-wide steps locked and without their details."""
    p = progress(cfg)
    steps = []
    for sid, title, household in STEPS[:-1]:
        locked = household and not admin
        s = {'id': sid, 'title': title, 'admin': household, 'locked': locked, 'links': []}
        s.update(VIEWS[sid](cfg, found, p) if not locked else {'status': 'locked', 'rows': [], 'fields': []})
        if sid in (p.get('skipped') or []) and s['status'] != 'done':
            s['status'] = 'skipped'
        steps.append(s)
    steps.append({'id': 'review', 'title': TITLES['review'], 'admin': False, 'locked': False, 'links': [], **view_review(steps, p)})
    current = next((s['id'] for s in steps if s['status'] == 'needs-you' and not s['locked']), 'review')
    return {'first_run': admin and not p, 'admin': admin, 'current': current, 'finished': p.get('finished'), 'steps': steps}


# ---- applying a step (writes only what differs)


def edit_config(cfg, edits):
    """Each (path, value) changed in the config file in place; the result checked and the old file kept as .bak."""
    from . import setupcmd
    path = cfg['_path']
    with open(path, encoding='utf8') as f:
        old = f.read()
    text = old
    for keys, value in edits:
        try:
            text = yamledit.edit(text, keys, value)
        except yamledit.EditError as e:
            raise WizardError(f'{".".join(keys)} could not be written ({e}): edit it in {path}, then check again') from e
    if text != old:
        try:
            setupcmd.write_config(path, text)
        except config.ConfigError as e:
            raise WizardError(f'{e}: nothing was changed') from e
    return text != old


def add_entries(cfg, section, entries, why=''):
    from . import setupcmd
    path = cfg['_path']
    with open(path, encoding='utf8') as f:
        text = f.read()
    entries = {k: v for k, v in entries.items() if k not in (cfg.get(section) or {})}
    if not entries:
        return False
    merged = configmerge.add(text, section, entries, {k: why for k in entries})
    if merged is None:
        raise WizardError(f'{section}: is written inline ({{...}}) in {path}: make it a block, then apply this step again')
    try:
        setupcmd.write_config(path, merged)
    except config.ConfigError as e:
        raise WizardError(f'{e}: nothing was changed') from e
    return True


def reload(cfg):
    return config.load(cfg['_path'])


def new_machine(found, name):
    d = next((x for x in found['machines'] if x['name'] == name), None)
    if not d or d['source'] != 'ssh':
        raise WizardError(f'"{name}" is not a machine in the config nor a Host in ~/.ssh/config: give it ssh access first '
                          f'(ssh-copy-id), then pick it')
    return {'host': d['host'], 'slots': 1, 'heat': dict(config.HEAT_DEFAULT)}


def apply_main(cfg, found, data, run, installer):
    main, backup = str(data.get('main') or ''), str(data.get('backup') or 'auto')
    if not main:
        raise WizardError('main: pick the machine that runs Tutti')
    if main == backup:
        raise WizardError(f'backup: {main} is already the main machine; pick another one, auto or none')
    adds = {n: new_machine(found, n) for n in (main, backup) if n not in ('auto', 'none') and n not in cfg['machines']}
    said = []
    if adds:
        add_entries(cfg, 'machines', adds, 'tutti setup wizard')
        cfg = reload(cfg)
        said.append(f'added {", ".join(adds)} to machines')
    if edit_config(cfg, [(['failover', 'main'], main)]):
        cfg = reload(cfg)
        said.append(f'main machine: {main}')
    want = '' if backup == 'auto' else backup
    if failover.opts(cfg)['backup'] != want:
        from . import failovercmd
        try:
            failovercmd.set_backup(cfg, backup, yes=bool(data.get('yes')), run=run)
        except failover.FailoverError as e:
            raise WizardError(f'backup: {e}') from e
        cfg = reload(cfg)
        said.append(f'backup main machine: {backup}')
    if data.get('install') and backup != 'none':
        said.append(installer(cfg, run))
    here = failover.me(cfg)
    if main != here and cfg['machines'].get(main, {}).get('host') != 'local':
        said.append(f'Tutti still runs on {here}: move it to {main} with `tutti move {main}` (docs/MOVE.md)')
    return '; '.join(said) or 'nothing to change'


def install_failover(cfg, run):
    """The failover service on the backup and here (tutti failover setup)."""
    from . import failovercmd
    try:
        backup, why = failovercmd.chosen_backup(cfg)
        if not backup:
            raise WizardError(f'install: no backup main machine ({why}): pick one')
        lines = []
        failovercmd.setup_backup(cfg, backup, run, say_fn=lines.append)
        failovercmd.install_local_unit(cfg, run)
    except (failover.FailoverError, subprocess.SubprocessError, OSError) as e:
        raise WizardError(f'install: the failover service could not be set up ({e}): run `tutti failover setup` to see each step') from e
    return f'failover service installed on {backup} and here'


def quiet_hours(name, text):
    spans = [s.strip() for s in str(text or '').split(',') if s.strip()]
    bad = [s for s in spans if not HOURS.match(s)]
    if bad:
        raise WizardError(f'machines.{name}.quiet_hours: {", ".join(bad)} is not HH:MM-HH:MM (e.g. 22:00-07:00); fix it and apply again')
    return spans


def apply_machines(cfg, found, data, run, installer):
    edits = []
    for n, m in cfg['machines'].items():
        pct = data.get(f'machines.{n}.use_pct')
        if pct is not None:
            if not isinstance(pct, int) or isinstance(pct, bool) or not 0 <= pct <= 100:
                raise WizardError(f'machines.{n}.use_pct: {pct!r} is not a whole number from 0 to 100; fix it and apply again')
            if pct != m.get('use_pct', 100):
                edits.append((['machines', n, 'use_pct'], pct))
        if f'machines.{n}.quiet_hours' in data:
            hours = quiet_hours(n, data[f'machines.{n}.quiet_hours'])
            if hours != (m.get('quiet_hours') or []):
                edits.append((['machines', n, 'quiet_hours'], hours or yamledit.DELETE))
    adds = {n: new_machine(found, n) for n in data.get('add') or [] if n not in cfg['machines']}
    changed = edit_config(cfg, edits)
    if adds:
        add_entries(reload(cfg) if changed else cfg, 'machines', adds, 'tutti setup wizard')
    said = [f'{len(edits)} setting(s) changed'] if edits else []
    return '; '.join(said + ([f'added {", ".join(adds)}'] if adds else [])) or 'nothing to change'


class _Plan:
    def __init__(self, plan):
        self.plan = plan

    def ask(self, _q, _default):
        return self.plan


def apply_accounts(cfg, found, data, run, installer):
    from . import setupcmd
    said, now = [], datetime.now(timezone.utc)
    for a in found['agents']:
        name = a['name']
        if name == 'copilot' and not data.get('copilot'):
            continue
        plan = data.get(f'plan.{name}')
        if plan is not None and plan not in PLANS[name]:
            raise WizardError(f'plan.{name}: "{plan}" is not one of {", ".join(PLANS[name])}')
        if add_entries(cfg, 'workers', {name: setup.AGENTS[name]['worker']}, 'tutti setup wizard'):
            cfg = reload(cfg)
            said.append(f'worker {name}')
        if name not in (cfg.get('accounts') or {}):
            add_entries(cfg, 'accounts', {name: setupcmd.account(a, _Plan(plan or PLANS[name][0]), now)}, 'tutti setup wizard')
            cfg = reload(cfg)
            said.append(f'account {name}')
        elif plan and (cfg['accounts'][name] or {}).get('subscription') != plan and not setup.AGENTS[name].get('plan'):
            edit_config(cfg, [(['accounts', name, 'subscription'], plan)])
            cfg = reload(cfg)
            said.append(f'{name} plan {plan}')
    models = found.get('ollama') or []
    has = any((w or {}).get('kind') == 'ollama' for w in (cfg.get('workers') or {}).values())
    if data.get('local') and models and not has:
        add_entries(cfg, 'workers', {'local': {'kind': 'ollama', 'host': '127.0.0.1:11434', 'model': models[0]}}, 'ollama list')
        said.append(f'local model {models[0]}')
    return ', '.join(said) and 'added ' + ', '.join(said) or 'nothing to change'


def apply_github(cfg, found, data, run, installer):
    local = {r['name']: r for r in found['repos']}
    remote = {r['repo']: r for r in found.get('remote_repos') or []}
    pick = [n for n in data.get('projects') or [] if n in local]
    clones = [r for r in data.get('clone') or [] if r in remote]
    bad = [r for r in data.get('clone') or [] if r not in remote]
    if bad:
        raise WizardError(f'clone: {", ".join(map(str, bad))} is not one of your GitHub repositories (`gh repo list`); check again')
    said = []
    home = found.get('home') or os.path.expanduser('~')
    for nwo in clones:
        name = remote[nwo]['name']
        dest = os.path.join(home, 'Projects', name)
        if not os.path.exists(dest):
            r = run(['gh', 'repo', 'clone', nwo, dest], capture_output=True, text=True, check=False, timeout=600)
            if r.returncode != 0:
                raise WizardError(f'clone: `gh repo clone {nwo}` failed ({(r.stderr or "").strip()[:160]}): run it in a terminal')
            said.append(f'cloned {nwo}')
        pick.append(name)
    entries = {}
    for i, n in enumerate(dict.fromkeys(pick)):
        entries[n] = {'match': n, 'priority': local[n]['priority'] if n in local else len(local) + i}
    if add_entries(cfg, 'projects', entries, 'tutti setup wizard'):
        said.append(f'{len(entries)} project(s) managed')
    return ', '.join(said) or 'nothing to change'


def apply_integrations(cfg, found, data, run, installer):
    said = []
    for name, plug in _PLUGINS.items():
        if data.get(f'integration.{name}') and plug['apply']:
            said.append(str(plug['apply'](cfg, True) or f'{plug["title"]} on'))
    return ', '.join(said) or 'nothing to change'


APPLY = {'main': apply_main, 'machines': apply_machines, 'accounts': apply_accounts, 'github': apply_github,
         'integrations': apply_integrations}


def apply(cfg, found, sid, data, run=subprocess.run, installer=install_failover):
    """(message) after applying step `sid` with `data` ({field key: value}; {"skip": true} skips it). Raises WizardError."""
    if sid not in TITLES:
        raise WizardError(f'step "{sid}" is not one of {", ".join(TITLES)}')
    if not isinstance(data, dict):
        raise WizardError('send the step\'s fields as a JSON object')
    if data.get('skip'):
        if sid in ('welcome', 'review'):
            raise WizardError(f'{TITLES[sid]} cannot be skipped')
        mark(cfg, sid, 'skipped')
        return f'{TITLES[sid]}: skipped (come back to it at any time)'
    msg = APPLY[sid](cfg, found, data, run, installer) if sid in APPLY else 'done'
    mark(cfg, sid)
    return f'{TITLES[sid]}: {msg}'


# ---- the dashboard's side (GET and POST /api/wizard)


def is_admin(collector, headers):
    """With sign-in on, the signed-in user's role (collector.who, docs/AUTH.md); a role check plugged in as
    collector.authorize; without either, this computer's owner, an admin."""
    who = getattr(collector, 'who', None)
    if callable(who):
        w = who(headers)
        if w or getattr(collector, 'signed', False):
            return bool(w) and w.get('role') == 'admin'
    check = getattr(collector, 'authorize', None)
    return not callable(check) or check(headers, 'admin') is None


def get(collector, headers, force=False):
    try:
        found = cached(collector, force)
    except (OSError, subprocess.SubprocessError) as e:
        return 500, {'error': f'detection failed ({e}): run `tutti setup --tui` in a terminal to see it'}
    return 200, view(collector.cfg, found, is_admin(collector, headers))


def post(collector, headers, body, run=subprocess.run, installer=install_failover):
    """POST /api/wizard {"step": id, "data": {...}} (an admin's: every step writes the household's config) or
    {"action": "detect"} (detect again now)."""
    if not is_admin(collector, headers):
        return 403, {'error': 'only an admin can change the setup: ask the household\'s admin (the Users tab lists them)'}
    try:
        req = json.loads(body or b'null')
    except ValueError:
        req = None
    if not isinstance(req, dict):
        return 400, {'error': 'send {"step": "<id>", "data": {...}} or {"action": "detect"}'}
    if req.get('action') == 'detect':
        return get(collector, headers, force=True)
    try:
        msg = apply(collector.cfg, cached(collector), str(req.get('step') or ''), req.get('data') or {}, run, installer)
    except WizardError as e:
        return 409, {'error': str(e)}
    if callable(getattr(collector, 'reload', None)):
        collector.reload()
    code, answer = get(collector, headers)
    return code, {**answer, 'message': msg}
