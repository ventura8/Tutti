"""The household's chat: a page where a member asks the household's AI what is running, why a machine is idle, how much
quota is left, and asks it to queue work or pause something. The page is a thin client: it sends the message to the
server, which picks a model (local first, then online, by the user's pick), runs it with the household's facts and the
tools it may call, and returns the answer with any tool calls as cards the user confirms.

The server enforces who may do what (auth.act_for writes the audit line); the page only hides what would be refused.
A viewer asks and creates work for their projects within the admin's limits; changing the household (pausing, settings,
users) is for admins only.

The model's tools are the MCP tools' own schemas, validated before anything runs. Read-only tools run at once and their
answer goes back to the model; the others become a proposal the user confirms.

The chat never depends on the user's own computer: a model on it is not offered, and a command that would run on it is
not passed (CHAT_DROPS)."""
import json
import re
import time
from datetime import datetime, timezone

from . import config, auth, telemetry, welcome

# ---- the rules a message may name

SLASH_TASK = re.compile(r'^\s*/task\s+(?P<project>[\w.-]+)\s*:\s*(?P<title>.+)$', re.I)
SLASH_DELEGATE = re.compile(r'^\s*/delegate\s+(?P<rest>.+)$', re.I)
SLASH = re.compile(r'^\s*/(?P<cmd>[\w-]+)\s*(?P<arg>.*)$', re.I)
ADD = re.compile(r'^(?:add|queue)\s+(?:a\s+)?task\s+(?:in|for)\s+(?P<project>[\w.-]+)\s*:\s*(?P<title>.+)$', re.I)
TESTS = re.compile(r'^(?:add|queue)\s+(?:a\s+)?task\s+(?:in|for)\s+(?P<project>[\w.-]+)\s*:\s*run\s+(?:the\s+)?full\s+test\s+suite', re.I)
DELEGATE = re.compile(r'^(?:hand|delegate)\s+(?:a\s+)?task\s+(?:to|in)\s+(?P<project>[\w.-]+)\s*:\s*(?P<task>.+)$', re.I)
ASK = re.compile(r'^(?:ask|tell)\s+(?P<scout>[\w.-]+)\s*:\s*(?P<question>.+)$', re.I)
CONTROL = re.compile(r'^(?P<action>pause|resume|unpause)\s+(?P<name>[\w.-]+)$', re.I)
SIZE = re.compile(r'\b(?:size|s)\s*[:=]?\s*(?P<size>[xs]?\s*s)\b', re.I)

COMMANDS = {'help', 'status', 'queue', 'running', 'quota', 'machines', 'devices', 'backlog', 'needs'}
WORK_TOOLS = {'backlog_add', 'delegate_task', 'scout_ask'}
HOUSEHOLD_TOOLS = {'control'}
HISTORY_TURNS = 12
DEFAULTS = {'viewer_tasks_per_day': 5, 'viewer_projects': [], 'history_turns': HISTORY_TURNS}


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def find_name(name, known):
    """The known name that matches `name` (case-insensitive, or a unique prefix), or None."""
    if not name:
        return None
    n = str(name).lower()
    for k in known or []:
        if k.lower() == n:
            return k
    hits = [k for k in known or [] if k.lower().startswith(n)]
    return hits[0] if len(hits) == 1 else None


def short_project(rest, known):
    """(project, task) from 'project: task' or 'project task'."""
    if ':' in rest:
        p, t = rest.split(':', 1)
        return p.strip(), t.strip()
    parts = rest.split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return rest.strip(), ''


def parse(text, known):
    """The call a message makes ({tool, args, label}) or None when it is not a command.
    Rules worker, account: [names]}.
    {tool, args, label} or {'error': plain text} when the request is clear but names something unknown."""
    text = text.strip()
    m = SLASH_TASK.match(text)
    if m:
        return work('backlog_add', m['project'], known, lambda p: {'repo': p, 'title': SIZE.sub(' ', m['title']).strip(),
                                                                  **size_of(m['title'])})
    m = SLASH_DELEGATE.match(text)
    if m:
        project, task = short_project(m['rest'], known)
        return work('delegate_task', project, known, lambda p: {'project': p, 'task': SIZE.sub(' ', task).strip(), **size_of(task)})
    m = SLASH.match(text)
    if m and m['cmd'].lower() in COMMANDS:
        return slash_call(m['cmd'].lower(), m['arg'].strip(), known)
    m = ADD.match(text)
    if m:
        return work('backlog_add', m['project'], known, lambda p: {'repo': p, 'title': SIZE.sub(' ', m['title']).strip(),
                                                                  **size_of(m['title'])})
    m = TESTS.match(text)
    if m:
        return work('backlog_add', m['project'], known, lambda p: {'repo': p, 'title': 'Run the full test suite and fix what fails',
                                                                  'size': 'S'})
    m = DELEGATE.match(text)
    if m:
        return work('delegate_task', m['project'], known, lambda p: {'project': p, 'task': SIZE.sub(' ', m['task']).strip(),
                                                                    **size_of(m['task'])})
    m = ASK.match(text)
    if m and find_name(m['scout'], known.get('scouts', [])):
        return {'tool': 'scout_ask', 'args': {'scout': find_name(m['scout'], known['scouts']), 'question': m['question'].strip()}}
    m = CONTROL.match(text)
    if m:
        return control_call(m['action'].lower(), m['name'], known)
    return None


def size_of(text):
    m = SIZE.search(text)
    return {'size': m[1].upper()} if m else {}


def work(tool, project, known, args):
    name = find_name(project, known.get('projects', []))
    if not name:
        listed = ', '.join(known.get('projects', [])) or 'none yet (an admin adds them under autopilot.repos)'
        return {'error': f'I don\'t know a project called "{project}". The projects I can queue work for: {listed}.'}
    return {'tool': tool, 'args': args(name)}


def control_call(action, name, known):
    for kind in ('machine', 'device', 'worker', 'account'):
        found = find_name(name, known.get(kind, []))
        if found:
            return {'tool': 'control', 'args': {'action': 'resume' if action == 'unpause' else action, 'kind': kind, 'name': found}}
    return {'error': f'Nothing called "{name}" can be paused here. Machines, devices, workers and accounts are listed on the '
                     'Dashboard tab, under Devices and controls.'}


def known_names(cfg, snap):
    """The names a request may use, from the config and the dashboard's snapshot."""
    repos = list(((cfg.get('autopilot') or {}).get('repos') or {}).keys())
    scouts = list((cfg.get('scouts') or {}).keys()) if isinstance(cfg.get('scouts'), dict) else []
    return {'projects': repos, 'scouts': [s for s in scouts if s not in ('review_trial', 'spend_rest')],
            'machine': list(cfg.get('machines') or {}), 'device': list(cfg.get('devices') or {}),
            'worker': list(cfg.get('workers') or {}), 'account': list(cfg.get('accounts') or {})}


# ---- who may do what


def settings(cfg):
    return {**DEFAULTS, **(cfg.get('chat') or {})}


def errors(cfg):
    """Config errors of `chat:`, each naming the key and the fix."""
    c = cfg.get('chat')
    if c is None:
        return []
    if not isinstance(c, dict):
        return ['chat must be a mapping (viewer_tasks_per_day, viewer_projects, history_turns)']
    errs = []
    v = c.get('viewer_tasks_per_day', DEFAULTS['viewer_tasks_per_day'])
    if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 1000:
        errs.append(f'chat.viewer_tasks_per_day is {v!r}: a whole number from 0 (viewers only ask) to 1000')
    p = c.get('viewer_projects', [])
    if not isinstance(p, list) or not all(isinstance(x, str) for x in p):
        errs.append('chat.viewer_projects must be a list of project names under autopilot.repos (empty: every project)')
    h = c.get('history_turns', HISTORY_TURNS)
    if not isinstance(h, int) or isinstance(h, bool) or not 0 <= h <= 50:
        errs.append(f'chat.history_turns is {h!r}: a whole number from 0 to 50')
    return errs


def project_of(call):
    a = call['args']
    return a.get('repo') or a.get('project')


def refusal(user, call, cfg, done_today):
    """None when `user` may make this call, else the reason in plain words. Viewers ask, and create work for their projects
    within the admin's limits; changing the household (pausing, settings, users) is for admins only. The server enforces
    this (and auth.act_for writes the audit line); the page only hides what would be refused."""
    role = (user or {}).get('role')
    if role not in ('viewer', 'admin'):
        return 'Your sign-in waits for an admin\'s approval, so the chat cannot act for you yet.'
    if role == 'admin':
        return None
    if call['tool'] in HOUSEHOLD_TOOLS:
        return ('Pausing or resuming machines, devices, workers and accounts changes the whole household, so only an admin can '
                'do it. Ask an admin, or ask me why something is busy.')
    if call['tool'] in ('delegate_task', 'scout_ask'):
        return ('Handing work straight to a worker or asking a scout spends an account at once, so only an admin can do it. '
                'Queue it as a task instead ("add task in <project>: …"): the autopilot runs it when there is room.')
    if call['tool'] not in WORK_TOOLS:
        return 'Only an admin can do that from the chat.'
    s = settings(cfg)
    allowed = s['viewer_projects']
    if project_of(call) and allowed and project_of(call) not in allowed:
        return (f'You can create work for {", ".join(allowed)} only (an admin sets this in Settings, Chat). Ask an admin to add '
                f'{project_of(call)}.')
    if done_today >= s['viewer_tasks_per_day']:
        return (f'You have created {done_today} pieces of work today, the limit an admin set for viewers '
                f'({s["viewer_tasks_per_day"]} a day). It resets at midnight; an admin can raise it in Settings, Chat.')
    return None


def describe(call):
    """A short label of what the call does, for the card and the audit log (no secrets: titles and names only)."""
    a, t = call['args'], call['tool']
    if t == 'backlog_add':
        return f'Queue a task in {a["repo"]}: {a["title"]}'
    if t == 'delegate_task':
        return f'Hand off to a worker in {a["project"]}: {a["task"]}'
    if t == 'scout_ask':
        return f'Ask {a["scout"]}: {a["question"]}'
    if t == 'control':
        return f'{a["action"].capitalize()} {a["kind"]} {a["name"]}'
    return t


# ---- the tools a model may call: the MCP tools' own schemas, validated before anything runs


def _control_tool():
    return {'name': 'control', 'description': 'Pause or resume a machine, Android device, worker or account (admins only). A '
            'paused item takes no new work; what runs finishes.',
            'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['action', 'kind', 'name'],
                            'properties': {'action': {'type': 'string', 'enum': ['pause', 'resume']},
                                           'kind': {'type': 'string', 'enum': ['machine', 'device', 'worker', 'account']},
                                           'name': {'type': 'string', 'description': 'its name in the config'}}}}


CHAT_DROPS = ('check',)   # arguments the chat never passes: a viewer or a steered model would run a command on this host


def model_tools():
    """{name: {schema, description, mutating}}: what a model may call. Read-only ones run at once and their answer goes back
    to the model; the others become a proposal the user confirms."""
    from . import mcpserver  # it imports config, which imports this module's errors()
    keep = ('usage', 'machines', 'devices', 'job_status', 'backlog_list', 'backlog_add', 'delegate_task', 'test_on', 'scout_ask')
    out = {}
    for t in [x for x in mcpserver.TOOLS if x['name'] in keep] + [_control_tool()]:
        schema = t['inputSchema']
        if t['name'] == 'backlog_add':   # no `check` from the chat: it is a shell command the autopilot runs (audit 2026-11, H9)
            props = {k: v for k, v in schema['properties'].items() if k not in CHAT_DROPS}
            schema = {**schema, 'properties': props, 'required': [r for r in schema.get('required', []) if r in props]}
        out[t['name']] = {'schema': schema, 'description': t['description'],
                          'mutating': t['name'] == 'control' or not t.get('annotations', {}).get('readOnlyHint', False)}
    return out


def ollama_tools():
    """The tools in Ollama's (and OpenAI's) function-calling shape."""
    return [{'type': 'function', 'function': {'name': n, 'description': t['description'], 'parameters': t['schema']}}
            for n, t in model_tools().items()]


def _type_ok(value, spec):
    t = spec.get('type')
    ok = {'string': isinstance(value, str), 'boolean': isinstance(value, bool),
          'array': isinstance(value, list) and all(isinstance(x, str) for x in value)}.get(t, True)
    return ok and (not spec.get('enum') or value in spec['enum'])


NAMED = {'repo': 'projects', 'project': 'projects', 'scout': 'scouts', 'machine': 'machine'}


def validate(call, known):
    """The call cleaned ({tool, args}) or an error in plain words: a tool that exists, its schema (required, types, enums, no
    other keys) and names that exist here (a project, a scout, a machine, a device or a paused item)."""
    tools = model_tools()
    if not isinstance(call, dict) or call.get('tool') not in tools:
        return f'there is no tool "{(call or {}).get("tool") if isinstance(call, dict) else call}"; the tools are {", ".join(tools)}'
    args = call.get('args') if isinstance(call.get('args'), dict) else {}
    schema = tools[call['tool']]['schema']
    props = schema.get('properties') or {}
    extra = sorted(set(args) - set(props))
    missing = [k for k in schema.get('required', []) if args.get(k) in (None, '', [])]
    if extra or missing:
        return f'{call["tool"]} {"has no " + ", ".join(extra) if extra else "needs " + ", ".join(missing)}'
    for k, v in args.items():
        if not _type_ok(v, props[k]):
            return f'{call["tool"]}.{k} is {v!r}: {props[k].get("enum") or props[k].get("type")}'
    for k, group in NAMED.items():
        if args.get(k) and not find_name(args[k], known.get(group, [])):
            return f'there is no {k} "{args[k]}" here; known: {", ".join(known.get(group, [])) or "none"}'
    if call['tool'] == 'control' and not find_name(args['name'], known.get(args['kind'], [])):
        return f'there is no {args["kind"]} "{args["name"]}" here'
    if args.get('device') and args['device'] != 'any' and not find_name(args['device'], known.get('device', [])):
        return f'there is no device "{args["device"]}" here'
    return {'tool': call['tool'], 'args': cleaned(call['tool'], args, known)}


def cleaned(tool, args, known):
    """The arguments with names as the config spells them, and sizes the backlog takes."""
    fixed = {k: (find_name(v, known.get(NAMED[k], [])) if k in NAMED and isinstance(v, str) else v) for k, v in args.items()}
    if tool == 'control':
        fixed['name'] = find_name(args['name'], known.get(args['kind'], []))
    if tool == 'backlog_add' and fixed.get('size') == 'XS':   # the backlog's smallest size is S (`tutti backlog add --size`)
        fixed['size'] = 'S'
    return fixed


ACTION = re.compile(r'^\s*ACTION\s*:?\s*(\{.*\})\s*$')


def split_actions(text):
    """(text, [calls]): the `ACTION {"tool": ..., "args": {...}}` lines a model without native tool calls writes."""
    keep, calls, fenced = [], [], False
    for line in (text or '').split('\n'):
        if line.lstrip().startswith('