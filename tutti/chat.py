"""The dashboard's Chat (docs/CHAT.md), the pure part: conversations per user, the requests it understands, who may do what,
which model answers, the facts it answers from and the prompts it suggests. I/O (files, models, tools) is in chatrun.py.

The chat runs on the household host, inside the dashboard: a user's own computer can be off. Its answers come from the
dashboard's last snapshot and a model of the household (local models first, then an account with room that the asking user
owns or that its owner shares); never another member's private quota."""
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone

MAX_TEXT = 8000            # characters in one message
MAX_ATTACHMENTS = 3
MAX_ATTACHMENT_BYTES = 32 * 1024
MAX_CONVERSATIONS = 200    # the oldest beyond this are dropped
MAX_MESSAGES = 400         # per conversation; the oldest beyond this are dropped
TITLE_MAX = 80
HISTORY_TURNS = 10         # earlier messages a model sees
DEFAULTS = {'viewer_tasks_per_day': 5, 'viewer_projects': [], 'history_turns': HISTORY_TURNS}
# work a viewer may create, within the limits: backlog tasks, which the autopilot runs under its own pacing and sharing rules.
# A hand-off or a scout question spends an account at once, so it is an admin's (never another member's private quota).
WORK_TOOLS = ('backlog_add',)
HOUSEHOLD_TOOLS = ('control',)                              # changes the household: admins only
HOUSEHOLD = {'sub': 'household', 'email': None}              # the target of a household action (auth.may_act_for: admins only)
DIR_MODE, FILE_MODE = 0o700, 0o600


class ChatError(Exception):
    """A request the chat refuses; the text is shown to the user as it is (plain words, the reason and what to do)."""


def now_iso(clock=time.time):
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat(timespec='seconds')


def new_id(prefix):
    return f'{prefix}{secrets.token_hex(6)}'


# ---- storage: <state>/chat/<user key>.json, one file per user, mode 600 in a 700 folder


def user_key(sub):
    """A user's file name: a hash of the sign-in's subject, so the folder lists no e-mail or id."""
    return hashlib.sha256(str(sub).encode()).hexdigest()[:24]


class Store:
    """Every conversation of every user on this host. One lock for the store; each user's file is written whole (a temp file,
    then a rename), so a crash never leaves half a file. `tutti move` carries it with the state folder."""

    def __init__(self, folder, clock=time.time):
        self.folder, self.clock, self.lock = os.path.join(folder, 'chat'), clock, threading.RLock()

    def path(self, sub):
        return os.path.join(self.folder, user_key(sub) + '.json')

    def load(self, sub):
        try:
            with open(self.path(sub), encoding='utf8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or not isinstance(data.get('conversations', []), list):
            data = {}   # a damaged file: start again rather than fail every request of this user
        data.setdefault('conversations', [])
        return data

    def save(self, sub, data):
        os.makedirs(self.folder, mode=DIR_MODE, exist_ok=True)
        os.chmod(self.folder, DIR_MODE)
        data['conversations'] = sorted(data['conversations'], key=lambda c: c.get('updated') or '', reverse=True)[:MAX_CONVERSATIONS]
        fd, tmp = tempfile.mkstemp(dir=self.folder, prefix='.chat-')
        with os.fdopen(fd, 'w', encoding='utf8') as f:
            json.dump(data, f)
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, self.path(sub))

    def change(self, sub, fn):
        """fn(data) under the lock, then saved; returns what fn returns."""
        with self.lock:
            data = self.load(sub)
            out = fn(data)
            self.save(sub, data)
            return out

    def conversation(self, data, cid):
        for c in data['conversations']:
            if c['id'] == cid:
                return c
        raise ChatError('That conversation is gone (deleted here or on another device). Pick another one or start a new chat.')

    def listing(self, sub, query=''):
        """[{id, title, updated, preview, count}], newest first; a query keeps those whose title or messages hold it."""
        q = (query or '').strip().lower()
        out = []
        for c in self.load(sub)['conversations']:
            texts = [c.get('title') or ''] + [m.get('text') or '' for m in c['messages']]
            if q and not any(q in t.lower() for t in texts):
                continue
            last = c['messages'][-1]['text'] if c['messages'] else ''
            hit = next((m['text'] for m in c['messages'] if q and q in (m.get('text') or '').lower()), '') if q else ''
            out.append({'id': c['id'], 'title': c.get('title') or 'New chat', 'updated': c.get('updated'),
                        'preview': (hit or last)[:120], 'count': len(c['messages'])})
        return sorted(out, key=lambda c: c['updated'] or '', reverse=True)

    def create(self, sub, title='', model='auto', messages=None):
        conv = {'id': new_id('c'), 'title': clean_title(title), 'created': now_iso(self.clock), 'updated': now_iso(self.clock),
                'model': model, 'messages': list(messages or [])}
        self.change(sub, lambda d: d['conversations'].insert(0, conv))
        return conv

    def rename(self, sub, cid, title):
        title = clean_title(title)
        if not title:
            raise ChatError('A name needs at least one letter or digit.')

        def go(d):
            self.conversation(d, cid)['title'] = title
        self.change(sub, go)

    def delete(self, sub, cid):
        def go(d):
            self.conversation(d, cid)
            d['conversations'] = [c for c in d['conversations'] if c['id'] != cid]
        self.change(sub, go)

    def clear(self, sub):
        """Every conversation of this user, gone: the file itself is removed, and the images beside it."""
        with self.lock:
            try:
                os.remove(self.path(sub))
            except FileNotFoundError:
                pass
            shutil.rmtree(self.blobs(sub), ignore_errors=True)

    def blobs(self, sub):
        return os.path.join(self.folder, user_key(sub) + '.files')

    def put_blob(self, sub, data):
        """Keeps an image (base64) in its own file beside the user's chats, so the conversations stay small; its ref."""
        ref = hashlib.sha256(data.encode()).hexdigest()[:24]
        folder = self.blobs(sub)
        os.makedirs(folder, mode=DIR_MODE, exist_ok=True)
        path = os.path.join(folder, ref)
        if not os.path.exists(path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
            with os.fdopen(fd, 'w', encoding='ascii') as f:
                f.write(data)
        return ref

    def blob(self, sub, ref):
        if not re.fullmatch(r'[0-9a-f]{24}', str(ref or '')):
            return None
        try:
            with open(os.path.join(self.blobs(sub), ref), encoding='ascii') as f:
                return f.read()
        except OSError:
            return None

    def get(self, sub, cid):
        return self.conversation(self.load(sub), cid)

    def put_message(self, sub, cid, msg):
        """Adds or replaces (same id) a message, and names a new conversation after its first message."""
        def go(d):
            c = self.conversation(d, cid)
            for i, m in enumerate(c['messages']):
                if m['id'] == msg['id']:
                    c['messages'][i] = msg
                    break
            else:
                c['messages'].append(msg)
            c['messages'] = c['messages'][-MAX_MESSAGES:]
            if not c.get('title') and msg['role'] == 'user':
                c['title'] = clean_title(msg['text']) or 'New chat'
            c['updated'] = now_iso(self.clock)
        self.change(sub, go)

    def branch(self, sub, cid, mid):
        """A new conversation with the messages up to `mid` (its model, effort and mode too): another way from there."""
        conv = self.get(sub, cid)
        ids = [m['id'] for m in conv['messages']]
        if mid not in ids:
            raise ChatError('That message is gone: reload the chat.')
        kept = [{**m, 'id': new_id('m'), 'cards': []} if m['role'] == 'assistant' else {**m, 'id': new_id('m')}
                for m in conv['messages'][:ids.index(mid) + 1]]
        new = self.create(sub, f'Branch of {conv.get("title") or "a chat"}', conv.get('model') or 'auto', kept)

        def go(d):
            c = self.conversation(d, new['id'])
            c.update({k: conv[k] for k in ('effort', 'mode') if conv.get(k)})
            return c
        return self.change(sub, go)

    def empty(self, sub, cid):
        """/clear: the conversation's messages, its summary and totals, gone; the conversation and its settings stay."""
        def go(d):
            c = self.conversation(d, cid)
            c.update(messages=[], summary=None, usage=None)
        self.change(sub, go)

    def update_message(self, sub, cid, msg):
        """Replaces a message that is still there; one dropped meanwhile (an edit, a delete) stays dropped. False if gone."""
        def go(d):
            c = self.conversation(d, cid)
            for i, m in enumerate(c['messages']):
                if m['id'] == msg['id']:
                    c['messages'][i] = msg
                    c['updated'] = now_iso(self.clock)
                    return True
            return False
        try:
            return self.change(sub, go)
        except ChatError:
            return False

    def truncate_after(self, sub, cid, mid, keep=True):
        """Drops the messages after `mid` (and `mid` itself unless keep): an edit or a retry starts again from there."""
        def go(d):
            c = self.conversation(d, cid)
            ids = [m['id'] for m in c['messages']]
            if mid not in ids:
                raise ChatError('That message is gone: reload the chat.')
            c['messages'] = c['messages'][:ids.index(mid) + (1 if keep else 0)]
        self.change(sub, go)

    def interrupted(self, sub, live_ids):
        """Answers left `streaming` by a restart (no live job writes them): marked stopped, so the page offers Retry."""
        def go(d):
            for c in d['conversations']:
                for m in c['messages']:
                    if m.get('status') == 'streaming' and m['id'] not in live_ids:
                        m['status'] = 'stopped'
                        m['note'] = 'This answer was cut off when Tutti restarted. Retry to get a full one.'
        with self.lock:
            data = self.load(sub)
            if any(m.get('status') == 'streaming' and m['id'] not in live_ids for c in data['conversations'] for m in c['messages']):
                go(data)
                self.save(sub, data)
            return data


def clean_title(text):
    text = re.sub(r'\s+', ' ', str(text or '')).strip()
    return text[:TITLE_MAX - 1] + '…' if len(text) > TITLE_MAX else text


def check_message(text, attachments):
    """(text, attachments) cleaned, or ChatError in plain words. Attachments are text or log snippets only."""
    text = str(text or '').strip()
    attachments = attachments or []
    if not isinstance(attachments, list) or not all(isinstance(a, dict) for a in attachments):
        raise ChatError('Attachments must be files: attach text or log files with the paper clip.')
    if not text and not attachments:
        raise ChatError('Type a message first.')
    if len(text) > MAX_TEXT:
        raise ChatError(f'That message is {len(text):,} characters; the limit is {MAX_TEXT:,}. Attach long logs as a file instead.')
    if not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS:
        raise ChatError(f'Attach at most {MAX_ATTACHMENTS} files to one message.')
    out = []
    for a in attachments:
        if a.get('type') or a.get('data'):   # a pasted screenshot or an image file
            from . import chatctl
            try:
                out.append(chatctl.check_image(a))
            except chatctl.ControlError as e:
                raise ChatError(str(e)) from e
            continue
        name, body = str(a.get('name') or 'snippet.txt')[:80], a.get('text')
        if not isinstance(body, str) or '\x00' in body:
            raise ChatError(f'"{name}" is not a text file. Attach text or log snippets only (.txt, .log, .json, .md, code).')
        size = len(body.encode())
        if size > MAX_ATTACHMENT_BYTES:
            raise ChatError(f'"{name}" is {size // 1024} KB; the limit is {MAX_ATTACHMENT_BYTES // 1024} KB. Attach the part that '
                            'matters (the end of a log, the failing test).')
        out.append({'name': name, 'size': size, 'text': body})
    return text, out


# ---- what the chat understands as a request for work (the rest goes to a model)

ADD = re.compile(r'^(?:please\s+)?(?:add|create|queue|make)\s+(?:a\s+)?(?:new\s+)?(?:backlog\s+)?task\s+(?:in|for|to|on)\s+'
                 r'(?P<project>[\w.-]+)\s*[:,–-]?\s*(?P<title>.+)$', re.I | re.S)
TESTS = re.compile(r'^(?:please\s+)?(?:queue|run|start)\s+(?:the\s+)?(?:full\s+)?tests?(?:\s+suite)?\s+(?:for|in|on|of)\s+'
                   r'(?P<project>[\w.-]+)\s*[.!]?$', re.I)
DELEGATE = re.compile(r'^(?:please\s+)?(?:delegate|hand\s+off|handoff)\s+(?:in|for|to|on)\s+(?P<project>[\w.-]+)\s*[:,–-]?\s*'
                      r'(?P<task>.+)$', re.I | re.S)
ASK = re.compile(r'^(?:please\s+)?ask\s+(?:the\s+)?(?:scout\s+)?(?P<scout>[\w.-]+)(?:\s+scout)?\s*[:,–-]?\s*(?P<question>.+)$', re.I | re.S)
CONTROL = re.compile(r'^(?:please\s+)?(?P<action>pause|resume|unpause)\s+(?:the\s+)?(?P<name>[\w.-]+)\s*[.!]?$', re.I)
SIZE = re.compile(r'\s*\[size\s+(XS|S|M|L)\]\s*', re.I)
# short commands (owner, 2026-11-17): /task <project> <title>; /delegate [<project>] <what>; delegate: [<project>:] <what>
SLASH_TASK = re.compile(r'^/task\s+(?P<project>[\w.-]+)\s*[:,–-]?\s*(?P<title>.+)$', re.I | re.S)
SLASH_DELEGATE = re.compile(r'^(?:/delegate\s+|delegate\s*:\s*)(?P<rest>.+)$', re.I | re.S)
# the other short commands: /tasks [all], /status [<run id>], /usage, /hosts, /devices, /pause <name>, /resume <name>,
# /ask <scout> <question>, /tests <project>, /help
SLASH = re.compile(r'^/(?P<cmd>[a-z]+)\b\s*(?P<arg>.*)$', re.I | re.S)
COMMANDS = {
    'task': '/task <project> <what>: queue a task in a project',
    'delegate': '/delegate [<project>] <what>: hand work to the best agent now (also "delegate: ...")',
    'tasks': '/tasks [all]: the task queue',
    'status': '/status [<run id>]: what is running, or one run',
    'usage': '/usage: every account against its pace',
    'hosts': '/hosts: the computers and their state',
    'devices': '/devices: phones, tablets and TVs',
    'pause': '/pause <host, device, agent or account>',
    'resume': '/resume <host, device, agent or account>',
    'ask': '/ask <scout> <question>: ask a scout such as grok',
    'tests': '/tests <project>: queue the full test suite',
    'help': '/help: these commands',
}


def find_name(name, names):
    """The configured name `name` stands for, matched without case; None when none."""
    low = name.lower()
    for n in names:
        if n.lower() == low:
            return n
    return None


def slash_call(cmd, arg, known):
    """The tool call of a short command other than /task and /delegate, or {'answer': text} for /help."""
    if cmd == 'help':
        return {'answer': 'Commands: ' + '; '.join(COMMANDS.values()) + '. Plain words work too.'}
    simple = {'usage': 'usage', 'hosts': 'machines', 'devices': 'devices'}
    if cmd in simple:
        return {'tool': simple[cmd], 'args': {}}
    if cmd == 'tasks':
        return {'tool': 'backlog_list', 'args': {'all': arg.lower() == 'all'}}
    if cmd == 'status':
        return {'tool': 'job_status', 'args': {'id': arg}}
    if cmd in ('pause', 'resume') and arg:
        return control_call(cmd, arg, known)
    if cmd == 'ask' and ' ' in arg:
        scout, question = arg.split(' ', 1)
        name = find_name(scout, known.get('scouts', []))
        return ({'tool': 'scout_ask', 'args': {'scout': name, 'question': question.strip()}} if name
                else {'error': f'There is no scout called {scout}. Try /ask grok <question>.'})
    if cmd == 'tests' and arg:
        return work('backlog_add', arg, known, lambda p: {'repo': p, 'title': 'Run the full test suite and fix what fails',
                                                          'size': 'S'})
    return {'error': f'{COMMANDS[cmd]} needs more: {COMMANDS[cmd].split(":")[0]}'}


def short_project(rest, known):
    """(project, what) of a short command: a leading project name ('roradio: fix ...' or 'roradio fix ...') when it is one,
    else the household's first project (its top priority in the config's order)."""
    head, _, tail = rest.strip().partition(' ')
    name = find_name(head.rstrip(':,'), known.get('projects', []))
    if name and tail.strip():
        return name, tail.strip()
    projects = known.get('projects') or ['']
    return projects[0], rest.strip()


def parse(text, known):
    """The tool call a message asks for, or None. `known` = {projects, scouts, machine, device, worker, account: [names]}.
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
        if line.lstrip().startswith('```'):
            fenced = not fenced
        m = None if fenced else ACTION.match(line)
        if m:
            try:
                calls.append(json.loads(m[1]))
                continue
            except ValueError:
                pass
        keep.append(line)
    return '\n'.join(keep).rstrip(), calls


# ---- which model answers


def is_owner(user, owner_sub):
    """The owner of this host's config (and so of its accounts): sign-in off, or the first admin."""
    return user.get('sub') == 'local' or (owner_sub is not None and user.get('sub') == owner_sub)


def _paused(snap):
    return {f'{p.get("kind")}:{p.get("name")}' for p in (snap.get('controls') or {}).get('paused') or [] if isinstance(p, dict)}


def machine_out(name, snap, own_machine=None):
    """Why a machine cannot run a chat model now (paused, offline, away or asleep, or the asking user's own computer), or None."""
    if not name:
        return None
    if f'machine:{name}' in _paused(snap):
        return f'{name} is paused'
    m = next((x for x in (snap.get('machines') or {}).get('items') or [] if x.get('name') == name), {})
    pres = ((snap.get('presence') or {}).get('items') or {}).get(f'machine:{name}') or m.get('presence') or {}
    if pres.get('state') in ('away', 'asleep', 'off'):
        return f'{name} is {pres["state"]}'
    if m.get('status') == 'offline' or m.get('paused'):
        return f'{name} is {"offline" if m.get("status") == "offline" else "lent out"}'
    if own_machine and name == own_machine:
        return f'{name} is your own computer, and the chat never depends on it'
    return None


PICKS = {'main': 'Auto: the main agent', 'auto': 'Auto: local first, then online', 'local': 'Local models only',
         'online': 'Online agents only'}
TOOL_PARAMS_B = 7.0   # a local model this large or larger is given the tools; a smaller one only talks


def params_b(model):
    """A model's size in billions of parameters from its name (`qwen3.8:27b-q8_0` is 27), or None when the name does not say."""
    m = re.search(r'(\d+(?:\.\d+)?)\s*b\b', str(model or '').lower().replace(':', ' ').replace('-', ' '))
    return float(m[1]) if m else None


def local_capable(model, sizes=None):
    """Whether a local model may call tools: its size from the fleet's reading (Ollama's own), else from its name; a model
    whose size nobody knows only talks."""
    size = (sizes or {}).get(model) or params_b(model)
    return size is not None and size >= TOOL_PARAMS_B


def fleet_sizes(cfg):
    """{model name: billions of parameters} from the fleet's last reading of every machine's Ollama (docs/FLEET.md)."""
    from . import fleet  # it imports config, which imports this module's errors()
    machines = (fleet.read_state(cfg).get('machines') or {}) if cfg.get('project') else {}
    return {m.get('name'): m.get('params_b') for e in machines.values() if isinstance(e, dict)
            for m in e.get('models') or [] if isinstance(m, dict) and m.get('params_b')}


def capability(cand):
    return cand.get('params') or 0.0


def online_model(cfg, name, w, rows, accounts, paused, owner, configured, account_args):
    """Choose a configured account whose quota can safely be bound to this worker's command."""
    from . import routing
    linked = [n for n, worker in configured.items() if worker == name]
    options = []
    for acc_name in linked or [name]:
        conf = (cfg.get('accounts') or {}).get(acc_name)
        acc = accounts.get(acc_name)
        why = cloud_out(name, acc_name, acc or {}, conf or {}, w, paused, rows.get(name, {}), owner)
        if not why and linked and conf is None:
            why = f'{acc_name}: its configured account is paused or disabled'
        if not why and linked and acc is None:
            why = f'{acc_name}: no pacing state yet; refresh usage before using this account'
        args = account_args.get(acc_name) or []
        if not why and len(linked) > 1 and (not args or any(account_args.get(n) == args for n in linked if n != acc_name)):
            why = f'{acc_name}: configure distinct account args so Chat can select this account on {name}'
        missing = routing.missing_limits(cfg, acc_name, acc or {})
        if not why and missing:
            why = f'{acc_name}: missing linked quota state ({", ".join(missing)}); resume it or refresh its reading'
        if not why and acc and routing.quota_closed(acc):
            why = f'{acc_name}: a linked quota is in panic or spent'
        options.append({'kind': 'cli', 'worker': name, 'account': acc_name, 'why': why, 'tools': True,
                        'label': f'{name} ({acc_name} account)'})
    return next((candidate for candidate in options if not candidate['why']), options[0])


def all_models(cfg, snap, user, owner_sub=None, own_machine=None):
    """Every model of the household with why it cannot answer now (None when it can): ([local], [online]). Local models
    are sorted by capability (the largest first, as `qwen3.8:27b` on the PC), each marked whether it gets the tools."""
    paused = _paused(snap)
    workers = cfg.get('workers') or {}
    rows = {w.get('name'): w for w in (snap.get('workers') or {}).get('items') or []}
    local, online, sizes = [], [], fleet_sizes(cfg)
    for name, w in workers.items():
        if (w or {}).get('kind') != 'ollama':
            continue
        why = f'{name} is paused' if f'worker:{name}' in paused else machine_out(w.get('machine'), snap, own_machine)
        if why is None and rows.get(name, {}).get('ready') is False:
            why = f'{name}\'s model server does not answer'
        where = w.get('machine') or 'the host'
        local.append({'kind': 'ollama', 'worker': name, 'model': w.get('model'), 'machine': where, 'why': why,
                      'params': sizes.get(w.get('model')) or params_b(w.get('model')), 'tools': local_capable(w.get('model'), sizes),
                      'label': f'{w.get("model")} on {where}'})
    local.sort(key=lambda c: -capability(c))
    accounts = {a.get('name'): a for a in (snap.get('usage') or {}).get('accounts') or []}
    configured = cfg.get('_account_workers') or {n: (a or {}).get('worker') or n for n, a in (cfg.get('accounts') or {}).items()}
    account_args = cfg.get('_account_args') or {n: (a or {}).get('args') or [] for n, a in (cfg.get('accounts') or {}).items()}
    for name, w in workers.items():
        if (w or {}).get('kind') != 'cli':
            continue
        online.append(online_model(cfg, name, w, rows, accounts, paused, is_owner(user, owner_sub), configured, account_args))
    return local, online


def candidates(cfg, snap, user, owner_sub=None, own_machine=None, pick='auto'):
    """The models that may answer this user, in order, and why the others may not: ([{kind, worker, label, ...}], [why]).

    `pick`: auto (the household's local models first, by capability, then online accounts with room), local, online, or one
    worker's name. Local models run on machines that are here, not paused, not lent out and never the user's own; online
    accounts are the owner's own or shared (`accounts.<n>.share_spare`): another member's private quota is never spent."""
    local, online = all_models(cfg, snap, user, owner_sub, own_machine)
    pool = {'local': local, 'online': online}.get(pick, local + online)
    if pick == 'main':   # the main agent now (tutti/conduct.py: auto, a hand-off or the owner's switch), then the rest as auto
        from . import conduct
        try:
            main = conduct.current(cfg)
        except (KeyError, TypeError):   # a config without main agents: the same order as auto
            main = ''
        pool = [c for c in online + local if c['worker'] == main] + [c for c in local + online if c['worker'] != main]
    if pick not in PICKS:
        pool = [c for c in local + online if c['worker'] == pick] or [{'worker': pick, 'why': f'{pick} is not a model here'}]
    out, before = [], []
    for c in pool:   # each model knows why the ones ranked before it could not answer: the page says why it fell back
        if c['why']:
            before.append(c['why'])
        else:
            out.append({**c, 'before': list(before)})
    return out, before


def choices(cfg, snap, user, owner_sub=None, own_machine=None):
    """The picker's options: the three modes, then every model with whether it can answer now."""
    local, online = all_models(cfg, snap, user, owner_sub, own_machine)
    out = [{'id': k, 'label': v, 'group': 'mode', 'available': True} for k, v in PICKS.items()]
    for c in local + online:
        out.append({'id': c['worker'], 'label': c['label'], 'group': 'local' if c['kind'] == 'ollama' else 'online',
                    'available': not c['why'], 'why': c['why'], 'tools': c['tools']})
    return out


def valid_pick(pick, cfg):
    return pick in PICKS or pick in (cfg.get('workers') or {})


def cloud_out(name, acc_name, acc, conf, w, paused, row, owner):
    if w.get('costs_money') or conf.get('costs_money') or w.get('enabled') is False or conf.get('enabled') is False:
        return f'{name} is off or costs money per use'
    if f'worker:{name}' in paused or f'account:{acc_name}' in paused:
        return f'{name} is paused'
    if not owner and not conf.get('share_spare'):
        return f'{acc_name} is the owner\'s private quota'
    if acc.get('panic') or acc.get('verdict') in ('spent', 'late'):
        return f'{acc_name} has no room ({acc.get("verdict") or "panic"})'
    if row.get('ready') is False:
        return f'{name} is not installed on the host'
    return None


# ---- the facts it answers from


def _ago(seconds):
    if seconds is None:
        return ''
    m = int(seconds // 60)
    return f'{int(seconds)} s' if m < 1 else f'{m} min' if m < 90 else f'{m // 60} h {m % 60} min'


def running(snap):
    return [j for j in (snap.get('jobs') or {}).get('items') or [] if j.get('status') in ('running', 'stalled')]


def job_line(j):
    where = j.get('machine') or j.get('worker') or 'the host'
    took = _ago((j.get('progress') or {}).get('elapsed_seconds') or (j.get('facts') or {}).get('seconds'))
    return f'{j.get("title") or j.get("id")} ({j.get("status")} on {where}{", " + took if took else ""})'


def account_line(a):
    used = a.get('used')
    left = f'{100 - used:.0f} % left' if isinstance(used, (int, float)) else 'no reading'
    reset = (a.get('reset') or '')[:16].replace('T', ' ')
    return f'{a["name"]}: {left}, {a.get("verdict") or "unknown"}{", resets " + reset + " UTC" if reset else ""}'


def machine_line(m, snap):
    pres = (((snap.get('presence') or {}).get('items') or {}).get(f'machine:{m["name"]}') or m.get('presence') or {})
    busy = m.get('busy')
    state = m.get('status') or 'unknown'
    detail = f'{busy} busy of {m.get("slots")} slots' if isinstance(busy, int) else (m.get('reason') or '')
    return f'{m["name"]}: {state}{" (" + detail + ")" if detail else ""}{"; " + pres["label"] if pres.get("label") else ""}'


def facts(snap):
    """The dashboard's state as short lines a model can answer from (no secrets: names, counts, percentages, reasons)."""
    run = running(snap)
    lines = [f'Now: {now_iso()}', f'Running jobs: {len(run)}'] + [f'- {job_line(j)}' for j in run[:12]]
    q = snap.get('queue') or {}
    items = [i for g in q.get('groups') or [] for i in g.get('items') or []]
    lines += [f'Queued: {len(items)}'] + [f'- {i.get("id")} {i.get("project")}: {i.get("title", "")[:90]} (waits: '
                                          f'{(i.get("why") or "")[:140]})' for i in items[:8]]
    lines += ['Accounts:'] + [f'- {account_line(a)}' for a in (snap.get('usage') or {}).get('accounts') or []]
    nxt = (snap.get('summary') or {}).get('next_worker') or {}
    if nxt.get('reason'):
        lines.append(f'Next worker: {nxt.get("worker") or "none"}; {nxt["reason"][:300]}')
    lines += ['Machines:'] + [f'- {machine_line(m, snap)}' for m in (snap.get('machines') or {}).get('items') or []]
    lines += ['Devices:'] + [f'- {d.get("name")}: {d.get("status")}{" (" + d["reason"][:120] + ")" if d.get("reason") else ""}'
                             for d in (snap.get('devices') or {}).get('items') or []]
    paused = (snap.get('controls') or {}).get('paused') or []
    if paused:
        lines.append('Paused: ' + ', '.join(f'{p.get("kind")} {p.get("name")}' for p in paused if isinstance(p, dict)))
    ap = snap.get('autopilot') or {}
    if ap:
        lines.append(f'Autopilot: {"on" if ap.get("enabled") else "off"}{"; " + str(ap.get("last") or "")[:200] if ap.get("last") else ""}')
    return '\n'.join(lines)


IDLE = re.compile(r'why\s+is\s+(?:the\s+)?(?P<name>[\w.-]+)\s+(?:idle|free|doing nothing|not busy)', re.I)
QUOTA = re.compile(r'\bquota\b|how much (?:quota|usage|room) ', re.I)
RUN = re.compile(r'what.?s\s+running|what is running|running now|busy|status', re.I)
QUEUE = re.compile(r'queue|waiting|backlog', re.I)


def builtin_answer(text, snap):
    """A plain Markdown answer from the state alone, for when no model has room (and the base every model gets)."""
    m = IDLE.search(text)
    if m:
        return idle_answer(m['name'], snap)
    if QUOTA.search(text):
        rows = [f'- **{account_line(a).split(":", 1)[0]}**:{account_line(a).split(":", 1)[1]}'
                for a in (snap.get('usage') or {}).get('accounts') or []]
        return '**Quota now**\n\n' + ('\n'.join(rows) or 'No account is configured yet.')
    if QUEUE.search(text):
        items = [i for g in (snap.get('queue') or {}).get('groups') or [] for i in g.get('items') or []]
        rows = [f'- `{i.get("id")}` {i.get("project")}: {i.get("title", "")[:80]}. Waits: {(i.get("why") or "its turn")[:120]}'
                for i in items[:8]]
        return f'**{len(items)} waiting**\n\n' + ('\n'.join(rows) or 'Nothing is waiting.')
    if RUN.search(text):
        run = running(snap)
        rows = [f'- {job_line(j)}' for j in run[:10]]
        more = f'\n\n…and {len(run) - 10} more on the Dashboard tab.' if len(run) > 10 else ''
        return f'**{len(run)} running**\n\n' + ('\n'.join(rows) or 'Nothing is running right now.') + more
    return None


def idle_answer(name, snap):
    items = (snap.get('machines') or {}).get('items') or []
    m = next((x for x in items if x.get('name', '').lower() == name.lower()), None)
    if m is None:
        dev = next((d for d in (snap.get('devices') or {}).get('items') or [] if d.get('name', '').lower() == name.lower()), None)
        if dev:
            return f'**{dev["name"]}** is {dev.get("status")}: {dev.get("reason") or "no reason recorded"}.'
        names = ', '.join(x['name'] for x in items) or 'none'
        return f'I don\'t know a machine called "{name}". The machines: {names}.'
    reasons = [machine_line(m, snap)]
    if m.get('paused'):
        reasons.append('Its yield flag is up: it is lent out, so Tutti sends it nothing.')
    if f'machine:{m["name"]}' in _paused(snap):
        reasons.append('It is paused on the dashboard (Devices and controls); Resume it there, or ask an admin.')
    nxt = (snap.get('summary') or {}).get('next_worker') or {}
    if nxt.get('reason') and not nxt.get('worker'):
        reasons.append(f'No account has room for new work: {nxt["reason"][:240]}')
    waits = [i for g in (snap.get('queue') or {}).get('groups') or [] for i in g.get('items') or []]
    if not waits:
        reasons.append('Nothing is waiting in the queue, so there is nothing to give it.')
    return f'**Why {m["name"]} is idle**\n\n' + '\n'.join(f'- {r}' for r in reasons)


def suggestions(snap, user, known, cfg=None):
    """Up to four prompts for an empty chat, from the state now; a viewer gets only what it may do."""
    out = ['What did I miss?']   # the welcome-back summary (tutti/welcome.py)
    for m in (snap.get('machines') or {}).get('items') or []:
        if m.get('status') in ('free', 'offline') or m.get('busy') == 0:
            out.append(f'Why is the {m["name"]} idle?')
            break
    out.append('What\'s running?')
    accounts = (snap.get('usage') or {}).get('accounts') or []
    out.append('How much quota is left?' if accounts else 'What is waiting in the queue?')
    projects = known.get('projects') or []
    if projects:
        allowed = settings(cfg or {})['viewer_projects'] if (user or {}).get('role') != 'admin' else []
        p = next((x for x in projects if not allowed or x in allowed), None)
        if p:
            out.append(f'Queue tests for {p}')
    return out[:4]


SYSTEM = """You are Tutti's household assistant, on the household's always-on host. You answer questions about the household's
AI work (jobs, quota, machines, devices, the queue) from the FACTS below, briefly and in plain words, with Markdown (short
lists, `code`). Never invent jobs, numbers or names that are not in the FACTS; say what is unknown.
You can act through tools: {tools}. Read-only tools answer at once. A tool that changes something is only proposed: the user
confirms it on a card, so say what you propose and why. Call a tool only with names that appear here.
Never say you queued, started, paused or changed anything unless a tool call in this answer did it: to do it, call the
tool; one call per task.
{protocol}
Projects: {projects}. Scouts: {scouts}. Machines: {machines}. Devices: {devices}.
The user is {who} ({role}).

FACTS
{facts}"""
TEXT_PROTOCOL = ('To call a tool, write one line on its own: ACTION {"tool": "<name>", "args": {...}} (JSON, the arguments of '
                 'its schema below). Schemas: ')



def system_prompt(snap, user, known, native_tools=True):
    """The system prompt; a model without native tool calls (a CLI agent) gets the ACTION line protocol and the schemas."""
    tools = model_tools()
    protocol = '' if native_tools else TEXT_PROTOCOL + json.dumps({n: t['schema'] for n, t in tools.items()})
    def names(k):
        return ', '.join(known.get(k) or []) or 'none'
    return SYSTEM.format(tools=', '.join(tools), protocol=protocol, projects=names('projects'), scouts=names('scouts'),
                         machines=names('machine'), devices=names('device'), who=user.get('email') or 'the owner',
                         role=user.get('role'), facts=facts(snap))


def model_messages(system, history, turns=HISTORY_TURNS, vision=False, summary=None, load=None):
    """The model's messages: the system prompt, the summary of what was compacted, then the last `turns` messages after it
    (text attachments inlined and fenced; images given to a model with `vision: true`, named for the others)."""
    out = [{'role': 'system', 'content': system}]
    ids = [m['id'] for m in history]
    if summary and summary.get('upto') in ids:
        history = history[ids.index(summary['upto']) + 1:]
        out.append({'role': 'system', 'content': 'Summary of the earlier conversation:\n' + summary.get('text', '')})
    for m in history[-turns:] if turns else []:
        if m.get('status') == 'failed' or not (m.get('text') or m.get('attachments')):
            continue
        body, images = m.get('text') or '', []
        for a in m.get('attachments') or []:
            if a.get('data') or a.get('ref'):
                data = a.get('data') or (load(a['ref']) if load else None)
                if data:
                    images.append(data)
                body += f'\n\n[Image attached: {a["name"]}{"" if vision else " (this model cannot see images)"}]'
            else:
                body += f'\n\nAttached {a["name"]}:\n```\n{a.get("text", "")[-12000:]}\n```'
        msg = {'role': 'assistant' if m['role'] == 'assistant' else 'user', 'content': body}
        if images and vision:
            msg['images'] = images
        out.append(msg)
    return out
