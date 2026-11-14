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
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, config

PROTOCOL = '2025-06-18'
ROLES = ('pending', 'viewer', 'admin')   # the same order as the dashboard sign-in's (a higher index may do more)
MUTATING_ROLE = 'admin'
BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'bin', 'tutti')
TIMEOUT = 120          # seconds a read or a quick change may take; long work runs detached and is polled with job_status
HTTP_PORT = 4779

INSTRUCTIONS = ('Tutti runs bounded coding tasks on cheaper workers (cursor-agent, Antigravity, local models) and shared '
                'machines, paced against each account\'s usage. Hand off a task when it is bounded (one change with a clear '
                'check: a fix, a test, a refactor of a few files) and you are ahead of your pace; keep judgement, design '
                'and anything the user is waiting on. Call handoff_advice first, then delegate_task, then poll job_status.')


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
    tool('away_mode', 'The owner\'s mode for pacing: status, or set active, away (the rest of today), auto, or vacation until a '
         'date. While away, Tutti fills the owner\'s room with backlog work.',
         {'mode': _s('empty for the status; or active, away, auto, vacation-until-YYYY-MM-DD')}),
]
BY_NAME = {t['name']: t for t in TOOLS}


def is_mutating(name, args):
    if name == 'away_mode':
        return bool((args or {}).get('mode'))
    return not BY_NAME[name]['annotations']['readOnlyHint']


def role_of(cfg, env=None):
    """The caller's role: $TUTTI_ROLE, else mcp.role in the config, else admin (the person who owns this computer)."""
    env = os.environ if env is None else env
    r = env.get('TUTTI_ROLE') or ((cfg or {}).get('mcp') or {}).get('role') or 'admin'
    return r if r in ROLES else 'pending'


def refusal(name, role):
    return (f'{name} changes something and needs the {MUTATING_ROLE} role; this Tutti gives you "{role}". Ask an admin to '
            'raise your role on the dashboard (Settings, Users), or set mcp.role: admin in your own tutti.config.yaml.')


def argv_of(name, a):
    """The `tutti` arguments of a tool call (a list), or None for a tool handled here."""
    size = a.get('size')
    table = {
        'usage': lambda: ['usage'],
        'machines': lambda: ['machines'],
        'devices': lambda: ['devices'],
        'backlog_list': lambda: ['backlog', 'list'] + (['--all'] if a.get('all') else []),
        'backlog_add': lambda: (['backlog', 'add', a['repo'], a['title'], '--source', 'mcp'] + (['--size', size] if size else [])
                                + [x for p in a.get('scope') or [] for x in ('--scope', p)]
                                + (['--check', a['check']] if a.get('check') else [])),
        'delegate_task': lambda: (['run', 'auto', (f'[size {size}] ' if size else '') + a['task'], '--repo', a['project'], '--bg']
                                  + (['--track', a['track']] if a.get('track') else [])),
        'scout_ask': lambda: ['scout', 'ask', a['scout'], a['question'], '--no-wait'] + (['--skill', a['skill']] if a.get('skill') else []),
        'away_mode': lambda: ['owner', 'mode', a['mode']] if a.get('mode') else ['owner', 'status'],
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
    if name == 'test_on':
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


def http_server(cfg, port, env=None):
    token, role = http_token(cfg), role_of(cfg, env)

    class Handler(BaseHTTPRequestHandler):
        server_version = 'tutti-mcp'

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
            if not secrets.compare_digest(self.headers.get('Authorization', ''), f'Bearer {token}'):
                return self.reply(401, {'error': f'send Authorization: Bearer <the token in {token_path(cfg)}>'})
            size = int(self.headers.get('Content-Length') or 0)
            out = answer_line(cfg, self.rfile.read(size).decode('utf8', 'replace'), role)
            if out is None:
                self.send_response(202)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return None
            return self.reply(200, out)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def cmd(cfg, a):
    if a.http is None:
        return serve_stdio(cfg)
    port = a.http or ((cfg.get('mcp') or {}).get('http_port') or HTTP_PORT)
    httpd = http_server(cfg, port)
    sys.stderr.write(f'tutti: MCP over HTTP on http://127.0.0.1:{httpd.server_address[1]}/mcp '
                     f'(bearer token in {token_path(cfg)})\n')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 130
    return 0
