"""`tutti mcp`: Tutti as an MCP server (docs/CONNECT.md), so any MCP-capable main agent drives Tutti natively.

Stdio (newline-delimited JSON-RPC 2.0, what every desktop app and CLI launches) and, for a client that needs it, a local
HTTP endpoint on 127.0.0.1 (POST /mcp, one JSON-RPC message per request, a bearer token from a mode-600 file). Every tool
runs the `tutti` command itself, so the MCP answers are exactly what the terminal prints. Read-only tools are open to every
role; a tool that changes something needs the `admin` role (`mcp.role` in the config, `$TUTTI_ROLE`; the roles are those
of the dashboard's sign-in: pending, viewer, admin).
"""
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler

from . import __version__, config, httpguard, netpolicy

PROTOCOL = '2025-06-18'
ROLES = ('pending', 'viewer', 'admin')   # the same order as the dashboard sign-in's (a higher index may do more)
MUTATING_ROLE = 'admin'
BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'bin', 'tutti')
TIMEOUT = 120          # seconds a read or a quick change may take; long work runs detached and is polled with job_status
HTTP_PORT = 4779
# tool arguments that land as positional words of a `tutti` command: one that starts with "-" would be read as an option
POSITIONAL = ('repo', 'project', 'track', 'scout', 'mode', 'machine', 'device', 'skill', 'id', 'who', 'kind', 'name', 'task',
              'account', 'percent', 'agent', 'section', 'state')   # names: free text goes after `--`
MAX_BODY = 1 << 20

INSTRUCTIONS = ('Tutti runs bounded coding tasks on cheaper workers (cursor-agent, Antigravity, local models) and shared '
                'machines, paced against each account\'s usage. Hand off a task when it is bounded (one change with a clear '
                'check: a fix, a test, a refactor of a few files) and you are ahead of your pace; keep judgement, design '
                'and anything the user is waiting on. Call handoff_advice first, then delegate_task, then poll job_status. All '
                'household work goes through Tutti: claim the backlog task you work on (backlog_add, backlog_claim), lease a machine '
                'or device for it (lease_claim), name the task id in commits, ask the owner with ask_owner, schedule Claude-only '
                'work with claude_task, and close it with backlog_complete. So your work survives your quota: name your account in '
                'backlog_claim, keep task_handoff current after every step, and report usage_report with state spent when you hit '
                'your limit; Tutti then continues the task with another main agent, and backlog_claim takes it back later.')


def _s(desc, **extra):
    return {'type': 'string', 'description': desc, **extra}


def tool(name, desc, props=None, required=(), mutating=False):
    schema = {'type': 'object', 'properties': props or {}, 'additionalProperties': False}
    if required:
        schema['required'] = list(required)
    return {'name': name, 'description': desc, 'inputSchema': schema,
            'annotations': {'readOnlyHint': not mutating, 'destructiveHint': False, 'openWorldHint': mutating}}


SIZES = ['XS', 'S', 'M', 'L']
TOOLS = [
    tool('handoff_advice', 'What should I hand off now? Every account against its pace (ahead, on pace, behind, panic) and '
         'which worker has room. Call it before delegate_task and whenever you wonder whether to offload work.'),
    tool('delegate_task', 'Hand a bounded coding task to a cheap worker (`tutti run auto --repo <project> --bg`): its own '
         'worktree, the worker headless, the checks, a report. Use it for one well-scoped change with a clear check; keep '
         'design and judgement yourself. Returns a run id for job_status.',
         {'task': _s('the task, self-contained: what to change, where, and how to check it'),
          'project': _s('the repository folder (absolute path) or a repo name under autopilot.repos'),
          'size': _s('rough size: XS (minutes), S (under an hour), M (a few hours), L (a day)', enum=SIZES),
          'track': _s('optional: reuse the worktree of this track instead of a new one')},
         required=('task', 'project'), mutating=True),
    tool('job_status', 'The status and log of a delegated run (an id from delegate_task) or a test_on job; with no id, the list '
         'of delegated runs.', {'id': _s('the run or job id; empty for the list')}),
    tool('backlog_list', 'The autopilot\'s backlog of bounded tasks (queued, running, in review).',
         {'all': {'type': 'boolean', 'description': 'also done and dropped tasks'}}),
    tool('backlog_add', 'Queue a bounded task for the autopilot to run later when an account has room (instead of now).',
         {'repo': _s('a repo name under autopilot.repos'), 'title': _s('the task, one line, self-contained'),
          'size': _s('XS, S, M or L', enum=SIZES), 'scope': {'type': 'array', 'items': {'type': 'string'},
                                                            'description': 'paths the task may change'},
          'check': _s('the acceptance check command (default: the repo\'s check)')},
         required=('repo', 'title'), mutating=True),
    tool('main_agent', 'The main agent: with no agent, who it is, the order and who has room now; with an agent, switch to it '
         '(auto: the first with room, the default; a running conductor hands over to a named one).',
         {'agent': _s('optional: auto or a worker name')}, mutating=True),
    tool('usage', 'Usage and pacing of every account: used percent, the target for now, the reset time and the verdict.'),
    tool('machines', 'The machines Tutti can use: free slots, load, memory, the yield flag.'),
    tool('devices', 'The Android devices for end-to-end tests: connection, battery, charging, heat, lock.'),
    tool('test_on', 'Run a test command over a track\'s worktree on a machine (routed, behind its slot queue) or on an Android '
         'device. Starts detached; poll job_status with the returned id.',
         {'track': _s('the track (worktree name) to test'), 'command': _s('the command to run there'),
          'machine': _s('optional: this machine instead of routing'), 'device': _s('optional: an Android device, or "any"')},
         required=('track', 'command'), mutating=True),
    tool('scout_ask', 'Ask a scout (a research helper such as Grok Bot, not a coding worker) a question; the answer arrives on a '
         'GitHub issue. Returns the request id.',
         {'scout': _s('the scout name (see `tutti scout list`)'), 'question': _s('the question or research task'),
          'skill': _s('optional hint', enum=['research', 'web-browse', 'triage', 'monitoring', 'drafting', 'browser-repro'])},
         required=('scout', 'question'), mutating=True),
    tool('pending_questions', 'The questions only the user can answer (a parked task\'s next step, a worker\'s question), '
         'batched: ask the user them together when it suits, then call answer_question for each. The rest of the work goes on.'),
    tool('answer_question', 'Write the user\'s answer to one question back to Tutti; the task resumes (or is dropped).',
         {'id': _s('the question id (q1, q2, ...)'), 'choice': _s('one of its options, or the user\'s own words')},
         required=('id',), mutating=True),
    tool('welcome_summary', 'Welcome back: what happened while the user was away, newest first by project priority: what '
         'shipped, what runs now, what failed or was parked and why, usage by each reset, and a numbered list of what only '
         'the user can do with the exact step. Show it to the user when they return or ask what they missed.'),
    tool('away_mode', 'Set the owner\'s mode for pacing: active, away (the rest of today), auto, or vacation until a date. While '
         'away, Tutti fills the owner\'s room with backlog work. With no mode it only shows the status (still needs admin).',
         {'mode': _s('active, away, auto or vacation-until-YYYY-MM-DD; empty for the status')}, mutating=True),
    # all household work through Tutti (docs/INTAKE.md): a session's own work as tasks, leases, usage and questions
    tool('backlog_claim', 'Claim a backlog task you (this session) will do yourself, so the household sees who works on it and the '
         'autopilot leaves it alone. Mention its id (t123) in every commit. Name the account you spend: if you run out of '
         'tokens, Tutti continues the task with another main agent and you take it back later with this same call (its answer '
         'is the handoff note to continue from).',
         {'id': _s('the task id (t123)'), 'who': _s('this session\'s name, e.g. claude-laptop-map'),
          'account': _s('the account this session spends (see usage), e.g. claude')}, required=('id', 'who'),
         mutating=True),
    tool('task_handoff', 'Keep a claimed task\'s living handoff note current (after every step): done, next, blockers, branch, '
         'worktree, verify (how to check it). If you run out of tokens, the agent that takes over starts from it. With no '
         'text: show the note.',
         {'id': _s('the task id'), 'who': _s('this session\'s name'),
          'section': _s('which part', enum=['done', 'next', 'blockers', 'branch', 'worktree', 'verify', 'show']),
          'text': _s('what to write (empty for show)')}, required=('id', 'who', 'section'), mutating=True),
    tool('session_heartbeat', 'Tell Tutti this session is alive (every Tutti call that names you counts too), the account it '
         'spends and, when you know it, the path of your transcript or log (Tutti reads its end for a usage-limit message).',
         {'who': _s('this session\'s name'), 'account': _s('the account it spends'),
          'transcript': _s('optional: a transcript or log file Tutti may read')}, required=('who',), mutating=True),
    tool('backlog_update', 'Record progress on a task: a note the dashboard and the next worker see.',
         {'id': _s('the task id'), 'note': _s('what was done or what is next')}, required=('id', 'note'), mutating=True),
    tool('backlog_release', 'Give a claimed task back to the queue (the autopilot may then run it).',
         {'id': _s('the task id'), 'who': _s('the session that claimed it')}, required=('id', 'who'), mutating=True),
    tool('backlog_complete', 'Mark a task done (merged or otherwise finished).', {'id': _s('the task id')}, required=('id',),
         mutating=True),
    tool('lease_claim', 'Lease a machine or an Android device for a task before using it, so no one else takes it and the use '
         'is linked to the task.',
         {'kind': _s('machine or device', enum=['machine', 'device']), 'name': _s('its name (see machines / devices)'),
          'who': _s('this session\'s name'), 'task': _s('the backlog task id it is for'),
          'minutes': {'type': 'integer', 'description': 'how long (default 120)'}},
         required=('kind', 'name', 'who', 'task'), mutating=True),
    tool('lease_release', 'Release a machine or device lease when done with it.',
         {'kind': _s('machine or device', enum=['machine', 'device']), 'name': _s('its name'), 'who': _s('the session that leased it')},
         required=('kind', 'name', 'who'), mutating=True),
    tool('leases', 'The machine and device leases sessions hold, with their tasks.'),
    tool('usage_report', 'Report a usage reading you saw for an account (its usage page or /usage): the pacing uses it. When '
         'you hit your own limit (or are about to), report it with state spent or panic and your name: Tutti then continues '
         'your claimed tasks with another main agent.',
         {'account': _s('the account name (see usage)'), 'percent': _s('used percent of the window, e.g. 42'),
          'state': _s('optional: spent (the limit is reached) or panic', enum=['spent', 'panic']),
          'who': _s('optional: this session\'s name (with state)')},
         required=('account', 'percent'), mutating=True),
    tool('ask_owner', 'Ask the owner a question on one of your tasks; it joins the needs queue (dashboard, chat, welcome back) '
         'and the answer comes back on the task (pending_questions / the task\'s notes). Keep working on other things meanwhile.',
         {'task': _s('the task id'), 'question': _s('the question, self-contained'),
          'options': {'type': 'array', 'items': {'type': 'string'}, 'description': 'answers to pick from; the first is the default'},
          'why': _s('why only the owner can answer')}, required=('task', 'question'), mutating=True),
    tool('claude_task', 'Schedule Claude-only work (the browser, judgement, visual checks) as a Tutti task: a backlog item pinned '
         'to the claude worker, run headless (`claude -p`) in its own worktree when the claude account has room, then the '
         'repo\'s review and release policy. Returns the task id (and the run id when it started).',
         {'repo': _s('a repo name under autopilot.repos'), 'task': _s('the task, self-contained'),
          'size': _s('S, M or L', enum=['S', 'M', 'L'])}, required=('repo', 'task'), mutating=True),
    tool('outside_work', 'Work outside Tutti: commits in the household\'s repos and leases of the last 24 h that no Tutti task '
         'explains. Link yours by claiming a task and naming its id in the commit.'),
]
BY_NAME = {t['name']: t for t in TOOLS}


def is_mutating(name, args):
    if name == 'away_mode':
        return bool((args or {}).get('mode'))
    if name == 'main_agent':
        return bool((args or {}).get('agent'))
    return not BY_NAME[name]['annotations']['readOnlyHint']


def role_of(cfg, env=None, default='admin'):
    """The caller's role: $TUTTI_ROLE, else mcp.role in the config, else `default`: admin over stdio and on loopback (the
    person who owns this computer), viewer for `--http` on a LAN or VPN address, where whoever holds the token is not
    necessarily the owner (set mcp.role: admin to allow changes there)."""
    env = os.environ if env is None else env
    r = env.get('TUTTI_ROLE') or ((cfg or {}).get('mcp') or {}).get('role') or default
    return r if r in ROLES else 'pending'


def refusal(name, role):
    return (f'{name} changes something and needs the {MUTATING_ROLE} role; this Tutti gives you "{role}". Ask an admin to '
            'raise your role on the dashboard (Settings, Users), or set mcp.role: admin in your own tutti.config.yaml.')


def scopes(value):
    """A list of paths; a lone string (a common slip for an array field) is one path, never its characters."""
    if isinstance(value, str):
        return [value]
    return [str(p) for p in value or []]


def argv_of(name, a):
    """The `tutti` arguments of a tool call (a list), or None for a tool handled here."""
    size = a.get('size')
    table = {
        'usage': lambda: ['usage'],
        'welcome_summary': lambda: ['welcome', 'show', '--keep-unread'],
        'pending_questions': lambda: ['needs', 'list'],
        'answer_question': lambda: ['needs', 'answer', a['id']] + ([a['choice']] if a.get('choice') else []),
        'machines': lambda: ['machines'],
        'devices': lambda: ['devices'],
        'backlog_list': lambda: ['backlog', 'list'] + (['--all'] if a.get('all') else []),
        # options first, then `--`: a title, task or question that starts with "-" stays text, never an option
        'backlog_add': lambda: (['backlog', '--source', 'mcp'] + (['--size', size] if size else [])
                                + [x for p in scopes(a.get('scope')) for x in (f'--scope={p}',)]
                                + ([f'--check={a["check"]}'] if a.get('check') else []) + ['--', 'add', a['repo'], a['title']]),
        'delegate_task': lambda: (['run', '--repo', a['project'], '--bg'] + (['--track', a['track']] if a.get('track') else [])
                                  + ['--', 'auto', (f'[size {size}] ' if size else '') + a['task']]),
        'scout_ask': lambda: (['scout', '--no-wait'] + (['--skill', a['skill']] if a.get('skill') else [])
                              + ['--', 'ask', a['scout'], a['question']]),
        'away_mode': lambda: ['owner', 'mode', a['mode']] if a.get('mode') else ['owner', 'status'],
        'main_agent': lambda: ['main', a['agent']] if a.get('agent') else ['main'],
        'backlog_claim': lambda: (['backlog', '--who', a['who']] + (['--account', a['account']] if a.get('account') else [])
                                  + ['claim', a['id']]),
        'task_handoff': lambda: ['handoff', '--task', a['id'], '--who', a['who'], '--', a['section'], a.get('text') or ''],
        'backlog_update': lambda: ['backlog', f'--note={a["note"]}', 'note', a['id']],
        'backlog_release': lambda: ['backlog', '--who', a['who'], 'release', a['id']],
        'backlog_complete': lambda: ['backlog', 'done', a['id']],
        'lease_claim': lambda: ['lease', '--who', a['who'], '--task', a['task'], '--minutes', str(int(a.get('minutes') or 120)),
                                'claim', a['kind'], a['name']],
        'lease_release': lambda: ['lease', '--who', a['who'], 'release', a['kind'], a['name']],
        'leases': lambda: ['lease', 'list'],
        'usage_report': lambda: (['usage'] + ([f'--limit={a["state"]}'] if a.get('state') else [])
                                 + ([f'--session={a["who"]}'] if a.get('who') else []) + ['log', a['account'], str(a['percent'])]),
        'ask_owner': lambda: (['needs'] + [f'--option={o}' for o in scopes(a.get('options'))]
                              + ([f'--why={a["why"]}'] if a.get('why') else []) + ['--', 'ask', a['task'], a['question']]),
        'claude_task': lambda: ['run', '--repo', a['repo'], '--size', a.get('size') or 'S', '--', 'claude', a['task']],
        'outside_work': lambda: ['intake'],
    }
    return table[name]() if name in table else None


def run_tutti(cfg_path, args, timeout=TIMEOUT):
    """(exit code, output) of `tutti --config <path> <args>`."""
    cmd = [sys.executable, BIN] + (['--config', cfg_path] if cfg_path else []) + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, f'`tutti {" ".join(args)}` took longer than {timeout} s; run it in a terminal to see why'
    return p.returncode, (p.stdout + (('\n' + p.stderr) if p.stderr.strip() else '')).strip()


def jobs_dir(cfg):
    d = os.path.join(config.state_dir(cfg), 'mcp-jobs')
    os.makedirs(d, exist_ok=True)
    return d


def start_test(cfg, a, popen=subprocess.Popen):
    """test_on: `tutti test` detached, its log and exit code in <state>/mcp-jobs/<id>.{log,rc}."""
    jid = f'test-{int(time.time())}-{secrets.token_hex(2)}'
    base = os.path.join(jobs_dir(cfg), jid)
    args = ['test'] + (['--on', a['machine']] if a.get('machine') else []) + (['--device', a['device']] if a.get('device') else [])
    args += [a['track'], 'sh', '-c', a['command']]
    cmd = [sys.executable, BIN, '--config', cfg['_path'], *args]
    with open(base + '.log', 'w', encoding='utf8') as log:
        popen(['sh', '-c', '"$@"; echo $? > "$0"', base + '.rc', *cmd], stdout=log, stderr=subprocess.STDOUT,
              stdin=subprocess.DEVNULL, start_new_session=True)
    return 0, f'started {jid}: `tutti {" ".join(args[:-3])} {a["track"]}` running `{a["command"]}`. Poll job_status with id {jid}.'


def test_status(cfg, jid):
    base = os.path.join(jobs_dir(cfg), os.path.basename(jid))
    if not os.path.exists(base + '.log'):
        return 1, f'no job {jid}: the ids come from test_on (test-...) or delegate_task'
    with open(base + '.log', encoding='utf8', errors='replace') as f:
        tail = f.read()[-6000:]
    rc = None
    if os.path.exists(base + '.rc'):
        with open(base + '.rc', encoding='utf8') as f:
            rc = f.read().strip()
    head = f'{jid}: running' if rc is None else f'{jid}: finished, exit code {rc}'
    return 0, f'{head}\n--- log (last part) ---\n{tail}'


def job_status(cfg, a):
    jid = (a.get('id') or '').strip()
    if jid.startswith('test-'):
        return test_status(cfg, jid)
    return run_tutti(cfg['_path'], ['runs', 'show', jid] if jid else ['runs', 'list'])


def handoff_advice(cfg):
    rc1, usage = run_tutti(cfg['_path'], ['usage'])
    rc2, main = run_tutti(cfg['_path'], ['main'])
    rule = ('Rule: hand off bounded work while an account is behind its pace or ahead of the reset with room; stop handing '
            'off to an account in panic. Keep judgement, design and anything the user waits on yourself.')
    return max(rc1, rc2), f'{usage}\n\n{main}\n\n{rule}'


def beat(cfg, args, name):
    """The session's heartbeat (tutti/continuity.py); a state folder that cannot be written never fails the call."""
    from . import continuity
    try:
        continuity.beat(cfg, args['who'].strip(), args.get('account') if name in ('session_heartbeat', 'backlog_claim') else None,
                        args.get('transcript') if name == 'session_heartbeat' else None)
    except (OSError, KeyError, TypeError):
        return False
    return True


def call(cfg, name, args, role, popen=subprocess.Popen, runner=None):
    """(is_error, text) of one tool call; refused without the role a change needs."""
    if name not in BY_NAME:
        return True, f'no tool "{name}": the tools are {", ".join(BY_NAME)}'
    args = args or {}
    missing = [k for k in BY_NAME[name]['inputSchema'].get('required', []) if not args.get(k)]
    if missing:
        return True, f'{name} needs {", ".join(missing)}'
    if is_mutating(name, args) and ROLES.index(role) < ROLES.index(MUTATING_ROLE):
        return True, refusal(name, role)
    flag = next((k for k in POSITIONAL if isinstance(args.get(k), str) and args[k].lstrip().startswith('-')), None)
    if flag:
        return True, f'{name}: {flag} may not start with "-" (it would be read as an option of the tutti command)'
    if isinstance(args.get('who'), str) and args['who'].strip():
        beat(cfg, args, name)   # every call that names its session is a sign of life (main-session continuity)
    if name == 'session_heartbeat':
        rc, text = 0, f'{args["who"]}: alive' + (f', spending {args["account"]}' if args.get('account') else '')
    elif name == 'test_on':
        rc, text = start_test(cfg, args, popen)
    elif name == 'job_status':
        rc, text = job_status(cfg, args)
    elif name == 'handoff_advice':
        rc, text = handoff_advice(cfg)
    else:
        rc, text = (runner or run_tutti)(cfg['_path'], argv_of(name, args))
    return rc != 0, text or '(no output)'


def handle(cfg, msg, role, **kw):
    """The JSON-RPC answer to one message, or None for a notification."""
    mid, method, params = msg.get('id'), msg.get('method'), msg.get('params') or {}
    if mid is None:
        return None
    if method == 'initialize':
        result = {'protocolVersion': params.get('protocolVersion') or PROTOCOL, 'capabilities': {'tools': {'listChanged': False}},
                  'serverInfo': {'name': 'tutti', 'version': __version__}, 'instructions': INSTRUCTIONS}
    elif method == 'tools/list':
        result = {'tools': TOOLS}
    elif method == 'tools/call':
        err, text = call(cfg, params.get('name'), params.get('arguments'), role, **kw)
        result = {'content': [{'type': 'text', 'text': text}], 'isError': err}
    elif method == 'ping':
        result = {}
    else:
        return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': f'no method {method}'}}
    return {'jsonrpc': '2.0', 'id': mid, 'result': result}


def answer_line(cfg, line, role, **kw):
    try:
        msg = json.loads(line)
    except ValueError:
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'not JSON'}}
    return handle(cfg, msg, role, **kw) if isinstance(msg, dict) else None


def serve_stdio(cfg, stdin=None, stdout=None, env=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    role = role_of(cfg, env)
    for line in stdin:
        if not line.strip():
            continue
        out = answer_line(cfg, line, role)
        if out is not None:
            stdout.write(json.dumps(out) + '\n')
            stdout.flush()
    return 0


def token_path(cfg):
    return os.path.join(config.state_dir(cfg), 'mcp-token')


def http_token(cfg):
    """The bearer token of the local HTTP endpoint, created once (mode 600)."""
    path = token_path(cfg)
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_urlsafe(32))
    with open(path, encoding='utf8') as f:
        return f.read().strip()


def loopback(host):
    return host in (None, 'auto', 'localhost', '::1') or str(host).startswith('127.')


def foreign_origin(origin, host, port):
    """True when a browser page of another site sends the request (MCP's DNS-rebinding rule): an Origin header that is
    not this endpoint's own. Clients that are not browsers send none."""
    if not origin:
        return False
    shown = f'[{host}]' if ':' in host else host
    own = {f'{scheme}://{h}:{port}' for scheme in ('http', 'https') for h in (shown, 'localhost', '127.0.0.1', '[::1]')}
    return origin.rstrip('/') not in own


def http_server(cfg, port, env=None, host='127.0.0.1'):
    token, role = http_token(cfg), role_of(cfg, env, 'admin' if loopback(host) else 'viewer')

    class Handler(BaseHTTPRequestHandler):
        server_version = 'tutti-mcp'
        sys_version = ''
        timeout = 30   # a client that sends its request slowly (or never) loses the connection

        def log_message(self, fmt, *args):
            return

        def reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path.split('?', 1)[0] != '/mcp':
                return self.reply(404, {'error': 'POST JSON-RPC to /mcp'})
            if foreign_origin(self.headers.get('Origin'), self.server.server_address[0], self.server.server_address[1]):
                return self.reply(403, {'error': 'a web page of another site may not call Tutti\'s MCP endpoint'})
            if not secrets.compare_digest(self.headers.get('Authorization', ''), f'Bearer {token}'):
                return self.reply(401, {'error': f'send Authorization: Bearer <the token in {token_path(cfg)}>'})
            try:
                size = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                size = -1
            if not 0 < size <= MAX_BODY:
                return self.reply(400, {'error': f'send one JSON-RPC message with a Content-Length from 1 to {MAX_BODY} bytes'})
            out = answer_line(cfg, self.rfile.read(size).decode('utf8', 'replace'), role)
            if out is None:
                self.send_response(202)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return None
            return self.reply(200, out)

    # never on the internet: a public address or a wildcard is refused (auto: loopback here)
    host = '127.0.0.1' if host in (None, 'auto') else netpolicy.check(host, 'mcp.http')[0]

    class Server(httpguard.CappedServer):
        address_family = socket.AF_INET6 if ':' in host else socket.AF_INET

    return Server((host, port), Handler)


def cmd(cfg, a):
    if a.http is None:
        return serve_stdio(cfg)
    port = a.http or ((cfg.get('mcp') or {}).get('http_port') or HTTP_PORT)
    try:
        httpd = http_server(cfg, port, host=a.bind or (cfg.get('mcp') or {}).get('bind') or '127.0.0.1')
    except netpolicy.PublicAddress as e:
        sys.stderr.write(f'tutti: {e}\n')
        return 2
    except OSError as e:   # not this computer's address, or the port is taken
        sys.stderr.write(f'tutti: MCP over HTTP cannot listen on {a.bind or "127.0.0.1"}:{port} ({e.strerror}): give an address '
                         'this computer has (`ip addr`) and a free port (--http PORT)\n')
        return 2
    shown = f'[{httpd.server_address[0]}]' if ':' in httpd.server_address[0] else httpd.server_address[0]
    sys.stderr.write(f'tutti: MCP over HTTP on http://{shown}:{httpd.server_address[1]}/mcp '
                     f'(bearer token in {token_path(cfg)})\n')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 130
    return 0
