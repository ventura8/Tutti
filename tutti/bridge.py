"""The client bridge (docs/MOVE.md, *The bridge*): after `tutti move`, the new host runs Tutti's jobs on the old computer.

A move puts the dashboard, the state and the loops on an always-on machine, but the repositories and the worker CLIs'
sign-ins stay where the owner set them up. The old computer keeps one ssh connection to the new host open
(`tutti client`, a user service): over it the host asks the client to do the few things only the client can, and the
client answers. The connection is the client's own ssh to the host, so the host needs no ssh key into the client and no
port is opened anywhere; the same connection forwards the dashboard's port to the client's 127.0.0.1.

- on the host, `tutti bridge serve --machine <client>` (started by that ssh) listens on a unix socket
  `<state>/bridge/<client>.sock` (a folder only this login can open) and passes each request to the client;
- on the client, `tutti client` reads the requests and answers each from its own config: it never runs a command the
  host names, only Tutti's own jobs (OPS). A reader or a reading file is named by its account, a probe by its machine;
  `run` is `tutti run` with the client's config; `exec` is git, gh or one of the client's worker programs in one of its
  repositories or worktrees.

One JSON object per line both ways: the host writes {"id", "op", "args"}; the client writes {"id", "chunk"} while a
long job streams, then {"id", "done": true, "rc", "result"}."""
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time

from . import config

OPS = ('ping', 'probe', 'reader', 'reading-file', 'run', 'exec', 'review', 'jobs', 'stop')
EXEC_PROGRAMS = ('git', 'gh')   # besides the client's own worker programs
FOLDER = 'bridge'
CALL_TIMEOUT = 60          # seconds a quick request may take (a probe, a reading)
RECONNECT = (5, 60)        # the client's back-off between connection attempts, seconds
RUN_FILES = ('report.json', 'report.txt', 'diff.patch', 'worker.log')


class BridgeDown(RuntimeError):
    """The client is not connected to this host; the message names the machine and how to bring it back."""


def folder(cfg):
    d = os.path.join(config.state_dir(cfg), FOLDER)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def sock_path(cfg, machine):
    return os.path.join(folder(cfg), f'{machine}.sock')


def status_path(cfg, machine):
    return os.path.join(folder(cfg), f'{machine}.json')


def down_text(machine):
    return (f'the computer "{machine}" is not connected to this Tutti: its jobs (runs in its repositories, its sign-ins, its '
            f'readings) wait for it. On {machine}, run `systemctl --user start tutti-client` (or `tutti client`), and check that '
            f'it can reach this computer by ssh')


def connected(cfg, machine):
    """{'since', 'pid', 'version'} of a connected client, or None."""
    try:
        with open(status_path(cfg, machine), encoding='utf8') as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    pid = st.get('pid')
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return None
    return st if os.path.exists(sock_path(cfg, machine)) else None


def clients(cfg):
    """Every client connected now: [{'machine', 'since', 'version'}]."""
    out = []
    for n in sorted(os.listdir(folder(cfg))):
        if n.endswith('.json'):
            st = connected(cfg, n[:-5])
            if st:
                out.append({'machine': n[:-5], 'since': st.get('since'), 'version': st.get('version')})
    return out


# ---- the host side: callers in this computer's Tutti


def call(cfg, machine, op, args=None, on_chunk=None, timeout=CALL_TIMEOUT):
    """(rc, result) of one request to the client `machine`. on_chunk(text) gets a streaming job's output as it comes."""
    path = sock_path(cfg, machine)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
    except OSError as e:
        s.close()
        raise BridgeDown(down_text(machine)) from e
    try:
        s.sendall((json.dumps({'op': op, 'args': args or {}}) + '\n').encode())
        for msg in _lines(s):
            if msg.get('done'):
                return msg.get('rc', 1), msg.get('result')
            if on_chunk and 'chunk' in msg:
                on_chunk(msg['chunk'])
    except TimeoutError as e:
        raise BridgeDown(f'the computer "{machine}" did not answer "{op}" within {timeout} s: it may be busy or asleep') from e
    finally:
        s.close()
    raise BridgeDown(f'the connection to "{machine}" ended during "{op}": {down_text(machine)}')


def _lines(s):
    buf = b''
    while True:
        data = s.recv(65536)
        if not data:
            return
        buf += data
        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            try:
                yield json.loads(line)
            except ValueError:
                continue


def probe(cfg, client, name):
    """routing.Probe of the machine `name` as the client sees it (a machine this host cannot reach by ssh itself)."""
    from . import routing
    try:
        rc, result = call(cfg, client, 'probe', {'machine': name}, timeout=40)
    except BridgeDown:
        return routing.Probe()
    fields = set(routing.Probe.__dataclass_fields__)
    return routing.Probe(**{k: v for k, v in (result or {}).items() if k in fields}) if rc == 0 else routing.Probe()


_JOBS = {}   # client -> (when, items): the jobs card asks at most every JOBS_EVERY seconds
JOBS_EVERY = 20


def client_jobs(cfg, clock=time.time):
    """The jobs every connected client runs by itself (its scripts, quiet runs, ship checks), each tagged with the client,
    for this host's jobs card; [] for a client that does not answer within a few seconds."""
    out = []
    for c in clients(cfg):
        name = c['machine']
        hit = _JOBS.get(name)
        if not hit or clock() - hit[0] > JOBS_EVERY:
            try:
                rc, result = call(cfg, name, 'jobs', timeout=8)
                items = (result or {}).get('items') or [] if rc == 0 else []
            except BridgeDown:
                items = []
            hit = _JOBS[name] = (clock(), [{**j, 'id': f'{name}:{j.get("id")}', 'machine': j.get('machine') or name, 'client': name}
                                           for j in items if isinstance(j, dict)])
        out += hit[1]
    return out


def read_account(cfg, client, account, force=False):
    """Readers.read's answer for an account, given by the client that holds its sign-in (its own reader and plan commands)."""
    from . import pacing
    empty = {'plan': None, 'used': None, 'resets': None, 'problem': None, 'every_minutes': None}
    try:
        rc, result = call(cfg, client, 'reader', {'account': account, 'force': bool(force)}, timeout=180)
    except BridgeDown as e:
        return {**empty, 'problem': str(e)}
    out = {**empty, **{k: v for k, v in (result or {}).items() if k in empty}}
    if rc != 0 and not out['problem']:
        out['problem'] = f'{client} could not read {account}'
    try:
        out['resets'] = pacing.parse_time(out['resets']) if out['resets'] else None
    except ValueError:
        out['resets'] = None
    return out


def reading_file(cfg, client, account):
    """(text, mtime) of the reading file the client's orchestrator writes for `account`, or (None, None)."""
    try:
        rc, result = call(cfg, client, 'reading-file', {'account': account})
    except BridgeDown:
        return None, None
    return ((result or {}).get('text'), (result or {}).get('mtime')) if rc == 0 else (None, None)


def run_exec(cfg, client):
    """A subprocess.run look-alike that runs git, gh or a worker program on the client (autopilot's publish and land)."""
    def run(argv, cwd=None, input=None, **_kw):
        try:
            rc, result = call(cfg, client, 'exec', {'argv': list(argv), 'cwd': cwd, 'stdin': input}, timeout=3600)
        except BridgeDown as e:
            return subprocess.CompletedProcess(argv, 255, '', str(e))
        result = result or {}
        return subprocess.CompletedProcess(argv, rc, result.get('stdout', ''), result.get('stderr', ''))
    return run


def git_exec(cfg, client):
    """autopilot's git(cwd, *args) on the client."""
    run = run_exec(cfg, client)
    return lambda cwd, *args: run(['git', '-C', cwd, *args])


def review_exec(cfg, client):
    """fill.ask_reviewer's run_worker(argv, folder) on the client: the folder's pr.diff travels with the request."""
    def run_worker(argv, cwd):
        with open(os.path.join(cwd, 'pr.diff'), encoding='utf8') as f:
            diff = f.read()
        try:
            rc, result = call(cfg, client, 'review', {'argv': list(argv), 'diff': diff}, timeout=1900)
        except BridgeDown as e:
            return subprocess.CompletedProcess(argv, 255, '', str(e))
        result = result or {}
        return subprocess.CompletedProcess(argv, rc, result.get('stdout', ''), result.get('stderr') or result.get('problem', ''))
    return run_worker


def jobs_machine(cfg):
    """The machine whose repositories and sign-ins run this host's worker jobs (host.jobs), or None when they run here."""
    h = cfg.get('host') or {}
    return h['jobs'] if h.get('jobs') and h.get('jobs') != h.get('me') else None


def tools(cfg):
    """{'run', 'gh', 'run_worker'} for the autopilot's publish and land on the jobs machine, or {} when jobs run here."""
    j = jobs_machine(cfg)
    return {'run': git_exec(cfg, j), 'gh': run_exec(cfg, j), 'run_worker': review_exec(cfg, j)} if j else {}


def exec_run(cfg, client, run_id, args, out=None):
    """`tutti bridge exec`: `tutti run <args>` on the client; its output streams to `out`; the run's report files are copied
    into this host's run folder, so the autopilot reads them as from a run of its own. Returns the run's exit code."""
    from . import delegate
    out = out or sys.stdout

    def chunk(text):
        out.write(text)
        out.flush()
    try:
        rc, result = call(cfg, client, 'run', {'args': list(args), 'id': run_id}, on_chunk=chunk, timeout=6 * 3600)
    except BridgeDown as e:
        chunk(f'tutti: {e}\n')
        return 255
    if run_id:
        p = delegate.paths(cfg, run_id)
        os.makedirs(p['dir'], exist_ok=True)
        for name, text in ((result or {}).get('files') or {}).items():
            if name in RUN_FILES or (name.endswith('.log') and '/' not in name and '..' not in name):
                with open(os.path.join(p['dir'], name), 'w', encoding='utf8') as f:
                    f.write(text)
        with open(p['rc'], 'w', encoding='utf8') as f:
            f.write(f'{rc}\n')
    return rc


# ---- the host side: `tutti bridge serve`, the end of the client's ssh


class Server:
    """Passes requests from this host's callers (the socket) to the client (stdout) and the answers back (stdin)."""

    def __init__(self, cfg, machine, stdin=None, stdout=None, version=''):
        self.cfg, self.machine, self.version = cfg, machine, version
        self.stdin, self.stdout = stdin or sys.stdin, stdout or sys.stdout
        self.pending, self.lock, self.next = {}, threading.Lock(), 0

    def send(self, msg):
        with self.lock:
            self.stdout.write(json.dumps(msg) + '\n')
            self.stdout.flush()

    def accept(self, srv):
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=self.forward, args=(conn,), daemon=True).start()

    def forward(self, conn):
        f = conn.makefile('rb')
        try:
            req = json.loads(f.readline() or b'null')
        except ValueError:
            req = None
        if not isinstance(req, dict) or req.get('op') not in OPS:
            why = {'problem': f'op must be one of {", ".join(OPS)}'}
            conn.sendall((json.dumps({'done': True, 'rc': 2, 'result': why}) + '\n').encode())
            conn.close()
            return
        with self.lock:
            self.next += 1
            rid = self.next
            self.pending[rid] = conn
        self.send({'id': rid, 'op': req['op'], 'args': req.get('args') or {}})

    def answer(self, line):
        try:
            msg = json.loads(line)
        except ValueError:
            return
        conn = self.pending.get(msg.get('id')) if isinstance(msg, dict) else None
        if not conn:
            return
        try:
            conn.sendall((json.dumps(msg) + '\n').encode())
        except OSError:
            msg['done'] = True
        if msg.get('done'):
            self.pending.pop(msg['id'], None)
            conn.close()

    def serve(self):
        path = sock_path(self.cfg, self.machine)
        if os.path.exists(path):
            os.unlink(path)   # a connection that ended without cleaning up; a new one replaces it
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        os.chmod(path, 0o600)
        srv.listen(16)
        with open(status_path(self.cfg, self.machine), 'w', encoding='utf8') as f:
            json.dump({'pid': os.getpid(), 'since': time.time(), 'version': self.version}, f)
        threading.Thread(target=self.accept, args=(srv,), daemon=True).start()
        try:
            for line in self.stdin:
                self.answer(line)
        finally:
            srv.close()
            for p in (path, status_path(self.cfg, self.machine)):
                if os.path.exists(p):
                    os.unlink(p)
            for conn in [*self.pending.values()]:
                conn.close()
        return 0


# ---- the client side: `tutti client`


def allowed_dirs(cfg):
    """Where `exec` may run: the client's repositories and its worktrees folder."""
    roots = [config.expand(r['path']) for r in (cfg['autopilot'].get('repos') or {}).values() if r.get('path')]
    return [*roots, config.expand(cfg['project']['worktrees'])]


def worker_programs(cfg):
    return {os.path.basename(w['command'][0]) for w in cfg['workers'].values() if w.get('command')}


def inside(path, roots):
    real = os.path.realpath(path or '')
    return any(real == os.path.realpath(r) or real.startswith(os.path.realpath(r).rstrip('/') + '/') for r in roots)


def refuse(why):
    return 2, {'problem': why}


class Client:
    """Answers the host's requests from this computer's own config (never a command the host names)."""

    def __init__(self, cfg, write, popen=subprocess.Popen, run=subprocess.run):
        self.cfg, self.write, self.popen, self.run = cfg, write, popen, run
        self.running = {}   # run id -> pid of the `tutti run` this computer runs for the host

    def op_stop(self, args, _chunk):
        """The host hands a run over (a move's --jobs handover, "stop now"): its process tree here ends."""
        from . import handover
        pid = self.running.get(args.get('id'))
        return (0, {'stopped': handover.end_tree(pid)}) if pid else (1, {'problem': f'no run {args.get("id")!r} runs here'})

    def handle(self, msg):
        rid, op, args = msg.get('id'), msg.get('op'), msg.get('args') or {}
        fn = getattr(self, 'op_' + str(op).replace('-', '_'), None) if op in OPS else None
        try:
            rc, result = fn(args, lambda text: self.write({'id': rid, 'chunk': text})) if fn else refuse(f'unknown op {op!r}')
        except Exception as e:   # one bad request never ends the bridge: the host gets the reason
            rc, result = 1, {'problem': f'{op}: {type(e).__name__}: {e}'}
        self.write({'id': rid, 'done': True, 'rc': rc, 'result': result})

    def op_ping(self, _args, _chunk):
        from . import __version__
        return 0, {'version': __version__, 'host': socket.gethostname()}

    def op_probe(self, args, _chunk):
        from . import remote
        m = self.cfg['machines'].get(args.get('machine'))
        if not m:
            return refuse(f'no machine "{args.get("machine")}" in this computer\'s config')
        return 0, dict(vars(remote.probe(m)))

    def op_reader(self, args, _chunk):
        from . import readers
        name = args.get('account')
        a = dict(self.cfg['accounts'].get(name) or {})
        if not (readers.automatic_reader(name, a) or a.get('plan')):
            return refuse(f'account "{name}" has no reader, usage page or Codex worker in this computer\'s config')
        a.pop('read_on', None)
        out = readers.Readers(self.cfg).read(name, a, force=bool(args.get('force')))
        out['resets'] = out['resets'].isoformat() if out.get('resets') else None
        return 0, out

    def op_jobs(self, _args, _chunk):
        """The jobs this computer runs by itself (its own state folder and processes), running or recent."""
        from . import state
        jobs = state.jobs_state(self.cfg, time.time())
        return 0, {'items': [j for j in jobs['items'] if j.get('status') in ('running', 'stalled')][:30]}

    def op_reading_file(self, args, _chunk):
        a = self.cfg['accounts'].get(args.get('account')) or {}
        f = config.expand(a['reading_file']) if a.get('reading_file') else None
        if not f:
            return refuse(f'account "{args.get("account")}" has no reading_file in this computer\'s config')
        try:
            with open(f, encoding='utf8') as fh:
                return 0, {'text': fh.read(), 'mtime': os.path.getmtime(f)}
        except OSError as e:
            return 1, {'problem': f'{f}: {e.strerror}'}

    def program(self, argv, allowed):
        """The full path of argv[0] when it is a bare name in `allowed` found on PATH, else None (a path is never taken)."""
        name = str(argv[0]) if argv else ''
        if not name or os.sep in name or (os.altsep and os.altsep in name) or name not in allowed:
            return None
        return shutil.which(name)

    def op_exec(self, args, _chunk):
        argv, cwd = [str(x) for x in args.get('argv') or []], args.get('cwd')
        exe = self.program(argv, (*EXEC_PROGRAMS, *worker_programs(self.cfg)))
        if not exe:
            return refuse(f'{argv[0] if argv else "(nothing)"} is not git, gh or a worker program of this computer (by name, on its '
                          'PATH): refused')
        roots = allowed_dirs(self.cfg)
        name, rest = argv[0], argv[1:]
        git_dir = rest[1] if name == 'git' and len(rest) > 1 and rest[0] == '-C' else None
        where = cwd or git_dir
        if not where or not inside(where, roots) or (git_dir and not inside(git_dir, roots)):
            return refuse(f'{where or "no folder"} is not one of this computer\'s repositories or worktrees: refused')
        r = self.run([exe, *argv[1:]], cwd=cwd or None, input=args.get('stdin'), capture_output=True, text=True, timeout=3600)
        return r.returncode, {'stdout': r.stdout, 'stderr': r.stderr}

    def op_review(self, args, _chunk):
        """A cheap reviewer run on this computer over the host's diff (fill.ask_reviewer), in an empty folder of its own."""
        import tempfile
        argv = [str(x) for x in args.get('argv') or []]
        exe = self.program(argv, worker_programs(self.cfg))
        if not exe:
            return refuse(f'{argv[0] if argv else "(nothing)"} is not a worker program of this computer: refused')
        with tempfile.TemporaryDirectory(prefix='tutti-review-') as d:
            with open(os.path.join(d, 'pr.diff'), 'w', encoding='utf8') as f:
                f.write(args.get('diff') or '')
            r = self.run([exe, *argv[1:]], cwd=d, capture_output=True, text=True,
                         stdin=subprocess.DEVNULL, timeout=1800)
        return r.returncode, {'stdout': r.stdout, 'stderr': r.stderr}

    def op_run(self, args, chunk):
        """`tutti run` with this computer's config; the output streams; the run folder's report files come back."""
        from . import delegate, remote
        tail = [str(x) for x in args.get('args') or []]
        if not tail or tail[0] != 'run' or '--config' in tail:
            return refuse('a run request is `run <worker> <task> ...` without --config')
        run_id = args.get('id')
        if run_id is not None and not delegate.valid_id(run_id):
            return refuse(f'{run_id!r} is not a run id')
        argv = [sys.executable, remote.BIN, '--config', self.cfg['_path'], *tail]
        p = self.popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, errors='replace',
                       env={**os.environ, 'TUTTI_LOCAL': '1', 'TUTTI_BACKGROUND': '1'})
        if run_id:
            self.running[run_id] = p.pid
        for line in p.stdout:
            chunk(line)
        rc = p.wait()
        self.running.pop(run_id, None)
        return rc, {'files': run_files(self.cfg, run_id) if run_id else {}}


def run_files(cfg, run_id):
    from . import delegate
    d = delegate.paths(cfg, run_id)['dir']
    out = {}
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if n in RUN_FILES or n.endswith('.log'):
            try:
                with open(os.path.join(d, n), encoding='utf8', errors='replace') as f:
                    out[n] = f.read()[-400000:]
            except OSError:
                continue
    return out


def ssh_argv(cfg):
    """The client's one ssh to the host: the bridge, plus the dashboard's port on this computer's 127.0.0.1."""
    h = cfg['host']
    port = int(h.get('port') or cfg['dashboard']['port'])
    remote_cmd = shlex.join([h.get('tutti') or 'tutti', '--config', h['config'], 'bridge', 'serve', '--machine', h['me']])
    # local models on this computer's loopback reach the host's own loopback (host.reverse, set by `tutti move`)
    reverse = [x for p, here in h.get('reverse') or [] for x in ('-R', f'127.0.0.1:{int(p)}:{here}')]
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', '-o', 'ExitOnForwardFailure=no',
            '-L', f'127.0.0.1:{port}:127.0.0.1:{port}', *reverse, h['ssh'], remote_cmd]


class Pipe:
    """Writes one JSON line at a time to the ssh's standard input (requests are answered on several threads)."""

    def __init__(self, p):
        self.p, self.lock = p, threading.Lock()

    def __call__(self, msg):
        with self.lock:
            try:
                self.p.stdin.write(json.dumps(msg) + '\n')
                self.p.stdin.flush()
            except (OSError, ValueError):
                pass


def answer_all(c, lines):
    """Each request read from the host is answered on its own thread."""
    for line in lines:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict):
            threading.Thread(target=c.handle, args=(msg,), daemon=True).start()


def connect_once(cfg, say, popen=subprocess.Popen):
    """One ssh connection to the host, answered until it ends. Returns ssh's exit code."""
    p = popen(ssh_argv(cfg), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    say(f'client: connected to {cfg["host"]["machine"]} ({cfg["host"]["ssh"]}); the dashboard is at '
        f'http://127.0.0.1:{cfg["host"].get("port") or cfg["dashboard"]["port"]}/')
    answer_all(Client(cfg, Pipe(p)), p.stdout)
    return p.wait()


def client_loop(cfg, say, popen=subprocess.Popen, sleep=time.sleep, once=False):
    """`tutti client`: keeps the bridge to the host up, answering its requests, until stopped."""
    wait = RECONNECT[0]
    while True:
        started = time.time()
        rc = connect_once(cfg, say, popen)
        if once:
            return rc
        wait = RECONNECT[0] if time.time() - started > 120 else min(RECONNECT[1], wait * 2)
        say(f'client: the connection to {cfg["host"]["machine"]} ended (ssh exited {rc}); trying again in {wait} s')
        sleep(wait)
