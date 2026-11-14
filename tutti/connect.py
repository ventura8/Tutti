"""`tutti connect`: wire Tutti into every main agent on this computer (docs/CONNECT.md).

Each agent gets the MCP server (`tutti mcp`) in its own MCP config, and a short instruction (a skill, or a marked block in
its global instructions file) telling it to hand bounded work to Tutti and to honour pacing. Every edit merges: only the
`tutti` entry and the marked block are ever written or removed, the file is backed up first, a second run changes nothing,
and `tutti connect --undo <agent>` takes it out again. The paths were checked against each tool on 2026-11-14 (docs).

The interface `tutti setup` calls: connect_all(cfg_path, home=None) -> [result], status_all(home=None) -> [row].
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

from . import mcpserver

NAME = 'tutti'
BEGIN, END = '<!-- tutti:begin (tutti connect; `tutti connect --undo` removes it) -->', '<!-- tutti:end -->'
TBEGIN, TEND = '# tutti:begin (tutti connect; `tutti connect --undo` removes it)', '# tutti:end'

GUIDE = """Tutti (local orchestrator) is connected as the MCP server `tutti`.
- Hand bounded work to Tutti: one well-scoped change with a clear check (a fix, a test, a small refactor, a translation).
  Call `handoff_advice` first, then `delegate_task` (project + size), then poll `job_status`. Keep design, judgement and
  anything the user is waiting on yourself.
- Honour pacing: when `handoff_advice` or `usage` says an account is in panic, do not send it work; when you are ahead of
  your own pace, offload more.
- Queue work for later with `backlog_add`; run tests on another machine or an Android device with `test_on`; ask a scout
  with `scout_ask`; `away_mode` sets the owner's mode.
- Without MCP, the same in a shell: `tutti run auto "<task>" --repo <dir> --bg`, `tutti wait run <id>`, `tutti usage`."""

SKILL = f"""---
name: tutti
description: Hand bounded coding work to Tutti's cheaper workers and machines, honouring usage pacing. Use when a task is a
  well-scoped change with a clear check, when you are ahead of your pace, or to run tests on another machine or device.
---

{GUIDE}
"""


def home_of(home=None):
    return home or os.path.expanduser('~')


AGENTS = {
    'claude-code': {
        'label': 'Claude Code (Code tab in Claude Desktop, and the CLI)', 'bins': ['claude'], 'dirs': ['.claude'],
        'mcp': ('json', '.claude.json', 'mcpServers', 'plain'), 'cli': 'claude',
        'guide': ('skill', '.claude/skills/tutti/SKILL.md'), 'procs': ['claude'],
        'restart': 'Start a new Claude Code session (in the desktop app: a new session in the Code tab).',
        'session': 'A session started before Tutti was added does not see it yet: start a new session (in Claude Desktop, '
                   'a new session in the Code tab); new sessions have it.'},
    'claude-desktop': {
        'label': 'Claude Desktop (chat)', 'bins': [], 'dirs': ['.config/Claude'],
        'mcp': ('json', '.config/Claude/claude_desktop_config.json', 'mcpServers', 'plain'),
        'procs': ['claude-desktop', 'Claude'], 'rewrites': True,   # the app rewrites its file and may drop Tutti's entry
        'restart': 'Only the chat part of Claude Desktop needs this (the Code tab is the Claude Code row): quit Claude Desktop '
                   'fully (its tray or menu bar icon, Quit; closing the window is not enough) and open it again; Tutti\'s tools '
                   'then appear under the chat\'s tools menu.'},
    'codex': {
        'label': 'OpenAI Codex CLI', 'bins': ['codex'], 'dirs': ['.codex'],
        'mcp': ('toml', '.codex/config.toml', 'mcp_servers', 'plain'),
        'guide': ('block', '.codex/AGENTS.md'), 'procs': ['codex'],
        'restart': 'Start a new Codex session.'},
    'chatgpt': {
        'label': 'ChatGPT app (desktop and web)', 'bins': [], 'dirs': [], 'procs': [],
        'cannot': 'ChatGPT\'s app cannot connect: its connectors only reach servers on the internet, and Tutti is never '
                  'exposed to the internet. Use Codex instead (the same OpenAI account): `tutti connect codex`.'},
    'antigravity': {
        'label': 'Antigravity (IDE and agy)', 'bins': ['agy', 'antigravity'], 'dirs': ['.gemini/antigravity', '.gemini/antigravity-cli'],
        'mcp': ('json', '.gemini/config/mcp_config.json', 'mcpServers', 'agy'),
        'guide': ('block', '.gemini/GEMINI.md'), 'procs': ['agy', 'antigravity', 'language_server'],
        'restart': 'Start a new agy session; in the Antigravity IDE refresh the MCP servers (Agent panel, MCP, Refresh).'},
    'cursor': {
        'label': 'Cursor (IDE and cursor-agent)', 'bins': ['cursor-agent', 'cursor'], 'dirs': ['.cursor'],
        'mcp': ('json', '.cursor/mcp.json', 'mcpServers', 'plain'),
        'guide': ('skill', '.cursor/skills/tutti/SKILL.md'), 'procs': ['cursor', 'cursor-agent'],
        'restart': 'Restart Cursor and switch `tutti` on once in Settings, MCP; cursor-agent picks it up in a new session.'},
    'copilot-cli': {
        'label': 'GitHub Copilot CLI', 'bins': ['copilot'], 'dirs': ['.copilot'],
        'mcp': ('json', '.copilot/mcp-config.json', 'mcpServers', 'copilot'),
        'guide': ('block', '.copilot/copilot-instructions.md'), 'procs': ['copilot'],
        'restart': 'Start a new Copilot CLI session.'},
    'vscode': {
        'label': 'VS Code (Copilot agent mode)', 'bins': ['code'], 'dirs': ['.config/Code/User'],
        'mcp': ('json', '.config/Code/User/mcp.json', 'servers', 'vscode'), 'procs': ['code'],
        'restart': 'In VS Code run "MCP: List Servers", pick tutti and Start (a one-time trust prompt).'},
    'gemini': {
        'label': 'Gemini CLI', 'bins': ['gemini'], 'dirs': [],
        'mcp': ('json', '.gemini/settings.json', 'mcpServers', 'plain'),
        'guide': ('block', '.gemini/GEMINI.md'), 'procs': ['gemini'],
        'restart': 'Start a new Gemini CLI session.'},
}


class ConnectError(RuntimeError):
    pass


def launcher():
    """The absolute `tutti` a desktop app can start (its PATH is not the shell's)."""
    return shutil.which(NAME) or mcpserver.BIN


def entry(style, cfg_path, command=None):
    base = {'command': command or launcher(), 'args': ['--config', cfg_path, 'mcp']}
    if style == 'agy':
        return {**base, 'disabled': False}
    if style == 'copilot':
        return {'type': 'local', **base, 'tools': ['*']}
    if style == 'vscode':
        return {'type': 'stdio', **base}
    return base


def installed(name, home=None, which=shutil.which):
    a = AGENTS[name]
    return any(which(b) for b in a['bins']) or any(os.path.isdir(os.path.join(home_of(home), d)) for d in a['dirs'])


# ---- files: read, back up, write


def _read(path):
    if not os.path.exists(path):
        return ''
    with open(path, encoding='utf8') as f:
        return f.read()


def backup(path, agent, home=None):
    """A copy of `path` under ~/.config/tutti/connect-backups/<agent>/ before the first change of this run."""
    if not os.path.exists(path):
        return None
    d = os.path.join(home_of(home), '.config', 'tutti', 'connect-backups', agent)
    os.makedirs(d, exist_ok=True)
    dest = os.path.join(d, f'{time.strftime("%Y%m%dT%H%M%S")}-{os.path.basename(path)}')
    shutil.copy2(path, dest)
    return dest


def _write(path, text, agent, home, changed):
    if _read(path) == text:
        return
    changed.append({'file': path, 'backup': backup(path, agent, home)})
    path = os.path.realpath(path)   # a symlinked settings file stays a symlink: its target gets the text
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tutti-tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        f.write(text)
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def load_json(path):
    text = _read(path)
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ConnectError(f'{path} is not valid JSON ({e}): fix it or move it away, then run `tutti connect` again; '
                           'nothing was changed') from e
    if not isinstance(data, dict):
        raise ConnectError(f'{path} is not a JSON object: fix it, then run `tutti connect` again; nothing was changed')
    return data


# ---- the MCP entry


def servers_of(data, path, key):
    servers = data.get(key)
    servers = {} if servers is None else servers
    if not isinstance(servers, dict):
        raise ConnectError(f'"{key}" in {path} is not a JSON object: fix it, then run `tutti connect` again; nothing was changed')
    return servers


def json_entry(path, key):
    return servers_of(load_json(path), path, key).get(NAME)


def set_json(path, key, value, agent, home, changed):
    data = load_json(path)
    servers = dict(servers_of(data, path, key))
    if value is None:
        if NAME not in servers:
            return
        servers.pop(NAME)
    elif servers.get(NAME) == value:
        return
    else:
        servers[NAME] = value
    data[key] = servers
    _write(path, json.dumps(data, indent=2) + '\n', agent, home, changed)


def toml_block(value):
    def q(s):
        return json.dumps(s)
    args = ', '.join(q(x) for x in value['args'])
    return f'{TBEGIN}\n[mcp_servers.{NAME}]\ncommand = {q(value["command"])}\nargs = [{args}]\n{TEND}\n'


def strip_block(text, begin, end):
    i = text.find(begin)
    if i < 0:
        return text
    j = text.find(end, i)
    j = len(text) if j < 0 else j + len(end)
    before, after = text[:i].rstrip('\n'), text[j:].lstrip('\n')
    return before + ('\n\n' if before and after else '\n' if before else '') + after


def toml():
    try:
        import tomllib
    except ImportError as e:   # Python 3.10: every other agent works; Codex's config.toml needs 3.11
        raise ConnectError('Codex\'s config.toml needs Python 3.11 or newer to read safely: upgrade Python, '
                           'or add [mcp_servers.tutti] by hand (docs/CONNECT.md)') from e
    return tomllib


def toml_entry(path):
    text = _read(path)
    tomllib = toml()
    try:
        return (tomllib.loads(text).get('mcp_servers') or {}).get(NAME)
    except tomllib.TOMLDecodeError as e:
        raise ConnectError(f'{path} is not valid TOML ({e}): fix it, then run `tutti connect` again; nothing was changed') from e


def set_toml(path, value, agent, home, changed):
    text = _read(path)
    toml_entry(path)   # refuses a broken file before any change
    base = strip_block(text, TBEGIN, TEND)
    if value is not None:
        if (toml().loads(base).get('mcp_servers') or {}).get(NAME):
            return   # the user's own [mcp_servers.tutti]: theirs, left as it is
        base = (base.rstrip('\n') + '\n\n' if base.strip() else '') + toml_block(value)
    _write(path, base, agent, home, changed)


# ---- the instructions


def set_guide(guide, on, agent, home, changed):
    kind, rel = guide
    path = os.path.join(home_of(home), rel)
    if kind == 'skill':
        if on:
            _write(path, SKILL, agent, home, changed)
        elif os.path.exists(path):
            changed.append({'file': path, 'backup': backup(path, agent, home)})
            os.remove(path)
            if not os.listdir(os.path.dirname(path)):
                os.rmdir(os.path.dirname(path))
        return
    text = strip_block(_read(path), BEGIN, END)
    if on:
        text = (text.rstrip('\n') + '\n\n' if text.strip() else '') + f'{BEGIN}\n{GUIDE}\n{END}\n'
    if text or os.path.exists(path):
        _write(path, text, agent, home, changed)


def guide_present(guide, home=None):
    kind, rel = guide
    text = _read(os.path.join(home_of(home), rel))
    return bool(text) if kind == 'skill' else BEGIN in text


def sharing_guide(name, home):
    """Other connected agents that read the same instructions file (Antigravity and Gemini CLI share ~/.gemini/GEMINI.md)."""
    g = AGENTS[name].get('guide')
    return [n for n, a in AGENTS.items() if n != name and a.get('guide') == g and current_entry(n, home)]


# ---- connect, undo, status


def mcp_path(name, home=None):
    return os.path.join(home_of(home), AGENTS[name]['mcp'][1])


def current_entry(name, home=None):
    kind, _rel, key, _style = AGENTS[name]['mcp']
    path = mcp_path(name, home)
    return toml_entry(path) if kind == 'toml' else json_entry(path, key)


def use_cli(name, home, which):
    """Claude Code keeps ~/.claude.json open while it runs: its own `claude mcp` command writes it safely."""
    return AGENTS[name].get('cli') and home is None and which(AGENTS[name]['cli'])


def cli_set(name, value, run):
    exe = AGENTS[name]['cli']
    run([exe, 'mcp', 'remove', '--scope', 'user', NAME], capture_output=True, text=True, check=False)
    if value is not None:
        p = run([exe, 'mcp', 'add-json', '--scope', 'user', NAME, json.dumps(value)], capture_output=True, text=True, check=False)
        if p.returncode != 0:
            raise ConnectError(f'`{exe} mcp add-json --scope user tutti ...` failed: {(p.stderr or p.stdout).strip()[:300]}')


def apply(name, cfg_path, on, home=None, which=shutil.which, run=subprocess.run, command=None):
    """Connect (on) or undo (off) one agent. {agent, status, changed: [{file, backup}], note}."""
    a = AGENTS[name]
    if 'cannot' in a:
        return {'agent': name, 'status': 'cannot', 'changed': [], 'note': a['cannot']}
    if on and not installed(name, home, which):
        return {'agent': name, 'status': 'not-installed', 'changed': [], 'note': f'{a["label"]} is not installed here'}
    kind, rel, key, style = a['mcp']
    path, changed = os.path.join(home_of(home), rel), []
    value = entry(style, cfg_path, command) if on else None
    if use_cli(name, home, which):
        if current_entry(name, home) != value:
            saved = backup(path, name, home)
            cli_set(name, value, run)
            changed.append({'file': path, 'backup': saved, 'via': f'{a["cli"]} mcp'})
    elif kind == 'toml':
        set_toml(path, value, name, home, changed)
    else:
        set_json(path, key, value, name, home, changed)
    if a.get('guide') and (on or not sharing_guide(name, home)):
        set_guide(a['guide'], on, name, home, changed)
    if changed:
        mark(name, on, home)
        _health.clear()   # the next status asks `claude mcp list` again
    if on and a.get('session'):   # Claude Code: new sessions have it at once; only older ones need a new session
        return {'agent': name, 'status': 'connected', 'changed': changed, 'note': '', 'soft': a['session'] if changed else ''}
    status = ('needs-restart' if changed else 'connected') if on else 'removed'
    return {'agent': name, 'status': status, 'changed': changed, 'note': a.get('restart', '') if on and changed else ''}


def resolve(names):
    if not names or names == ['all']:
        return list(AGENTS)
    bad = [n for n in names if n not in AGENTS]
    if bad:
        raise ConnectError(f'no agent {", ".join(bad)}: the agents are {", ".join(AGENTS)}')
    return names


def connect_all(cfg_path, home=None, names=None, **kw):
    """`tutti setup` calls this: every installed agent connected; agents not installed are skipped. A list of results."""
    out = []
    for n in resolve(names):
        try:
            out.append(apply(n, cfg_path, True, home, **kw))
        except ConnectError as e:
            out.append({'agent': n, 'status': 'error', 'changed': [], 'note': str(e)})
    return out


def marks_path(home=None):
    return os.path.join(home_of(home), '.config', 'tutti', 'connect.json')


def mark(name, on, home=None):
    """When each agent was last connected: a process of it older than that has not loaded Tutti yet."""
    path = marks_path(home)
    try:
        data = json.loads(_read(path) or '{}')
    except ValueError:
        data = {}
    if on:
        data[name] = time.time()
    else:
        data.pop(name, None)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf8') as f:
        json.dump(data, f)


def started(names, proc='/proc'):
    """Start times (epoch seconds) of running processes whose name is one of `names`."""
    try:
        boot = next(float(x.split()[1]) for x in _read(os.path.join(proc, 'stat')).splitlines() if x.startswith('btime'))
        tick = os.sysconf('SC_CLK_TCK')
    except (StopIteration, ValueError, OSError):
        return []
    out = []
    for pid in (d for d in os.listdir(proc) if d.isdigit()):
        try:
            stat = _read(os.path.join(proc, pid, 'stat'))
        except OSError:
            continue
        comm, rest = stat[stat.find('(') + 1:stat.rfind(')')], stat[stat.rfind(')') + 2:].split()
        if comm in names and len(rest) > 19:
            out.append(boot + int(rest[19]) / tick)
    return out


def stale(name, home=None, started_fn=started):
    """True when the agent runs a process started before Tutti was connected to it (it needs a restart)."""
    try:
        at = json.loads(_read(marks_path(home)) or '{}').get(name)
    except ValueError:
        return False
    return bool(at) and any(t < at for t in started_fn(AGENTS[name].get('procs') or []))


def parse_mcp_list(text):
    """Tutti's line in `claude mcp list`: ('connected', ''), ('failed', what it says), or ('absent', '')."""
    for line in (text or '').splitlines():
        if line.strip().startswith(f'{NAME}:'):
            tail = line.rsplit(' - ', 1)[-1].strip()
            low = tail.lower()   # "Connected" only as a whole word: "Disconnected" or "Failed to connect" are not
            ok = re.search(r'\bconnected\b', low) and not any(w in low for w in ('fail', 'not ', 'error', 'needs'))
            return ('connected', '') if ok else ('failed', tail)
    return 'absent', ''


HEALTH_TTL = 60   # `claude mcp list` starts every server to check it: asked at most once a minute
_health = {}


def claude_health(run=subprocess.run, clock=time.time, timeout=60):
    """What `claude mcp list` says of Tutti's server (parse_mcp_list), or None when it could not be asked."""
    if _health and clock() - _health['at'] < HEALTH_TTL:
        return _health['value']
    try:
        p = run([AGENTS['claude-code']['cli'], 'mcp', 'list'], capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        p = None   # cached too: a hung `claude mcp list` is not asked again on every poll
    value = parse_mcp_list(p.stdout) if p is not None and p.returncode == 0 else None
    _health.update(at=clock(), value=value)
    return value


def cli_status(row, name, health, home, started_fn):
    """The row from the agent's own health check (`claude mcp list`): registered and healthy is connected; a session
    started before it was added gets a soft note, not another status."""
    a, (state, said) = AGENTS[name], health
    if state == 'absent':
        return {**row, 'status': 'not-connected', 'note': f'Tutti is not registered in {a["label"]}: run `tutti connect {name}`'}
    if state == 'failed':
        return {**row, 'status': 'error', 'note': f'Tutti is registered, but `{a["cli"]} mcp list` says "{said}": run '
                                                  f'`tutti connect --test {name}` to see why its server does not start'}
    soft = a['session'] if stale(name, home, started_fn) else ''
    if a.get('guide') and not guide_present(a['guide'], home):
        soft = 'The instructions are missing: run `tutti connect` again. ' + soft
    return {**row, 'status': 'connected', 'note': '', 'soft': soft.strip(), 'checked': f'{a["cli"]} mcp list'}


def status(name, home=None, which=shutil.which, started_fn=started, ask=None):
    """One agent's row. `ask`: the agent's own health check (Claude Code: `claude mcp list`), used when it answers."""
    a = AGENTS[name]
    row = {'agent': name, 'label': a['label']}
    if 'cannot' in a:
        return {**row, 'status': 'cannot', 'note': a['cannot']}
    if not installed(name, home, which):
        return {**row, 'status': 'not-installed', 'note': f'{a["label"]} was not found on this computer'}
    if ask is None and use_cli(name, home, which):
        ask = claude_health
    health = ask() if ask and a.get('cli') else None
    if health:
        return cli_status(row, name, health, home, started_fn)
    try:
        e = current_entry(name, home)
    except ConnectError as err:
        return {**row, 'status': 'error', 'note': str(err)}
    if not e:
        return {**row, 'status': 'not-connected', 'note': f'run `tutti connect {name}`'}
    if a.get('guide') and not guide_present(a['guide'], home):
        return {**row, 'status': 'connected', 'note': 'the MCP server is in; the instructions are missing: run `tutti connect` again'}
    if stale(name, home, started_fn):
        if a.get('session'):
            return {**row, 'status': 'connected', 'note': '', 'soft': a['session'], 'entry': e}
        return {**row, 'status': 'needs-restart', 'note': a.get('restart', ''), 'entry': e}
    return {**row, 'status': 'connected', 'note': a.get('restart', ''), 'entry': e}


HEAL_EVERY = 600
_healed = {}


def marked(name, home=None):
    try:
        return bool(json.loads(_read(marks_path(home)) or '{}').get(name))
    except ValueError:
        return False


def heal(row, home=None, which=shutil.which, cfg_path=None):
    """An app that rewrites its own settings file (Claude Desktop) and dropped Tutti's entry after `tutti connect` put it
    there: the entry goes back (the file is backed up first) and the row says so and what the person has to do."""
    name = row['agent']
    if row['status'] != 'not-connected' or not AGENTS[name].get('rewrites') or not marked(name, home):
        return row
    if time.time() - _healed.get(name, 0) < HEAL_EVERY:
        return row   # at most once in HEAL_EVERY: an app in the middle of writing its file is not fought over
    _healed[name] = time.time()
    from . import config
    cfg_path = cfg_path or config.find()
    if not cfg_path:
        return row
    try:
        r = apply(name, cfg_path, True, home, which)
    except ConnectError as e:
        return {**row, 'status': 'error', 'note': f'{AGENTS[name]["label"]} dropped Tutti from its settings and it could not be '
                                                  f'added back: {e}'}
    saved = next((c['backup'] for c in r['changed'] if c.get('backup')), None)
    return {**row, 'status': 'needs-restart', 'healed': True,
            'note': f'{AGENTS[name]["label"]} rewrote its settings file and dropped Tutti; Tutti added it back'
                    + (f' (the old file is kept as {saved})' if saved else '') + '. ' + AGENTS[name]['restart']}


def status_all(home=None, which=shutil.which, started_fn=started, ask=None, cfg_path=None):
    return [heal(status(n, home, which, started_fn, ask), home, which, cfg_path) for n in AGENTS]


# ---- the round trip


def roundtrip(e, timeout=30, popen=subprocess.Popen):
    """Start the agent's configured server exactly as the agent would, initialize, list the tools. (ok, text)."""
    if not e or not e.get('command'):
        return False, 'not connected: run `tutti connect` first'
    msgs = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': mcpserver.PROTOCOL, 'capabilities': {},
                        'clientInfo': {'name': 'tutti-connect-test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
    try:
        p = popen([e['command'], *e.get('args', [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = p.communicate(''.join(json.dumps(m) + '\n' for m in msgs), timeout=timeout)
    except subprocess.TimeoutExpired as x:
        p.kill()
        p.communicate()
        return False, f'the server `{e["command"]}` did not answer within {timeout} s ({x}): run it in a terminal to see why'
    except OSError as x:
        return False, f'the server `{e["command"]}` did not answer ({x}): run `{e["command"]} {" ".join(e.get("args", []))}` in a terminal'
    for line in out.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get('id') == 2 and 'result' in m:
            names = [t['name'] for t in m['result'].get('tools', [])]
            return True, f'{len(names)} tools: {", ".join(names)}'
    return False, f'no tool list came back: {(err or out).strip()[-300:] or "no output"}'


def test(name, home=None, **kw):
    try:
        return roundtrip(current_entry(name, home), **kw)
    except ConnectError as e:
        return False, str(e)


# ---- the command


def print_result(r, out):
    out.write(f'{AGENTS[r["agent"]]["label"]}: {r["status"]}\n')
    for c in r['changed']:
        out.write(f'  changed {c["file"]}' + (f' (backup {c["backup"]})' if c.get('backup') else '') + '\n')
    if r.get('note'):
        out.write(f'  {r["note"]}\n')
    if r.get('soft'):
        out.write(f'  note: {r["soft"]}\n')


def human_steps(results):
    """The restarts and refreshes only the person can do, as guided steps for setup's one list at the end (docs/INSTALL.md)."""
    from . import guided
    return [guided.step('other', f'Restart {AGENTS.get(r["agent"], {}).get("label", r["agent"])}', None, r['note'])
            for r in results if r['status'] == 'needs-restart' and r.get('note')]


def status_line(r):
    t = r.get('test')
    line = f'{r["label"]}: {r["status"]}' + (f' (test: {"ok" if t["ok"] else "failed"}, {t["text"]})' if t else '')
    line += f'\n  {r["note"]}' if r.get('note') and r['status'] != 'connected' else ''
    return line + (f'\n  note: {r["soft"]}' if r.get('soft') else '') + '\n'


def cmd(cfg, a, out=None):
    out = out or sys.stdout
    names = resolve(['all'] if a.all or not a.agent else [a.agent])
    if a.status or a.test:
        rows = status_all(cfg_path=cfg['_path']) if a.agent is None else [heal(status(n), cfg_path=cfg['_path']) for n in names]
        for r in rows:
            if a.test and r['status'] in ('connected', 'needs-restart'):
                ok, text = test(r['agent'])
                r['test'] = {'ok': ok, 'text': text}
        out.write(json.dumps(rows, indent=2) + '\n' if a.json else ''.join(status_line(r) for r in rows))
        return 0
    results = [apply(n, cfg['_path'], False) for n in names] if a.undo else connect_all(cfg['_path'], names=names)
    if a.json:
        out.write(json.dumps(results, indent=2) + '\n')
    else:
        for r in results:
            print_result(r, out)
        if not a.undo:
            from . import guided
            out.write(guided.render(human_steps(results)) + '\n')
    return 1 if any(r['status'] == 'error' for r in results) else 0
