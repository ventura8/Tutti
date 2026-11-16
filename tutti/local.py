"""Local model workers (Ollama's HTTP API): ask, rewrite one file, or loop edit -> check -> retry. Bulky reading and drafting
costs no cloud tokens. A worker waits while its machine's yield flag is up, and a low-priority call lets other local jobs go first."""
import atexit
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

from . import config, remote, waiting

FENCE = re.compile(r'```[a-zA-Z]*\n(.*?)```', re.S)
FILE_BLOCK = re.compile(r'=== FILE: (\S+) ===\n```[a-zA-Z]*\n(.*?)```', re.S)
IDLE_STATE = 'local-idle.json'
_IDLE_LOCK = threading.Lock()


def log(msg):
    sys.stderr.write(f'tutti: {msg}\n')


def busy_marker(cfg):
    """Marks this process as a running local-AI job; removed at exit. Low-priority jobs wait while another marker is live."""
    path = os.path.join(config.state_dir(cfg), f'llm-busy-{os.getpid()}')
    open(path, 'w', encoding='utf8').close()
    atexit.register(lambda: os.path.exists(path) and os.remove(path))
    return path


def others_busy(cfg):
    mine = str(os.getpid())
    pids = [p.rsplit('-', 1)[-1] for p in glob.glob(os.path.join(config.state_dir(cfg), 'llm-busy-*'))]
    return any(pid != mine and pid.isdigit() and not waiting.pid_gone(int(pid))() for pid in pids)


def ollama_get(host, path, timeout=3):
    """An Ollama GET (/api/ps, /api/tags) as JSON, or None when the server does not answer."""
    try:
        with urllib.request.urlopen(config.ollama_url(host, path), timeout=timeout) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def post(host, payload, timeout):
    req = urllib.request.Request(config.ollama_url(host, '/api/generate'), json.dumps(payload).encode(),
                                 {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def model_name(name):
    return name if ':' in name else f'{name}:latest'


def model_key(host, model):
    return f'{host}::{model_name(model)}'


def idle_path(cfg):
    return os.path.join(config.state_dir(cfg), IDLE_STATE)


def idle_read(cfg):
    try:
        with open(idle_path(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def idle_write(cfg, data):
    path = idle_path(cfg)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def idle_minutes(cfg):
    value = ((cfg.get('fleet') or {}).get('idle_unload_minutes'))
    if value is None:
        return 15.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 15.0


def indefinite(keep_alive):
    return str(keep_alive).strip() == '-1'


def effective_keep_alive(worker):
    if worker.get('pinned'):
        return -1
    if 'keep_alive' in worker:
        return worker['keep_alive']
    return None


def ollama_workers(cfg):
    return [w for w in (cfg.get('workers') or {}).values() if (w or {}).get('kind') == 'ollama' and w.get('host') and w.get('model')]


def idle_policies(cfg):
    out = {}
    for w in ollama_workers(cfg):
        key = model_key(w['host'], w['model'])
        out.setdefault(key, {'host': w['host'], 'model': model_name(w['model']), 'indefinite': False, 'pinned': False})
        out[key]['indefinite'] = out[key]['indefinite'] or indefinite(w.get('keep_alive'))
        out[key]['pinned'] = out[key]['pinned'] or bool(w.get('pinned'))
    return out


def note_used(cfg, worker, now=None):
    host, model = worker.get('host'), worker.get('model')
    if not host or not model:
        return
    at = float(time.time() if now is None else now)
    key = model_key(host, model)
    with _IDLE_LOCK:
        state = idle_read(cfg)
        state[key] = {'last_used': at}
        idle_write(cfg, state)


def unload_idle_models(cfg, now=None, ps_fn=ollama_get, post_fn=post):
    minutes = idle_minutes(cfg)
    if minutes <= 0:
        return []
    at = float(time.time() if now is None else now)
    limit = at - minutes * 60.0
    policies = idle_policies(cfg)
    if not policies:
        return []
    with _IDLE_LOCK:
        state, changed = idle_read(cfg), False
        loaded_by_host, unloaded = {}, []
        for key, policy in policies.items():
            if policy['pinned'] or not policy['indefinite']:
                continue
            last = (state.get(key) or {}).get('last_used')
            if not isinstance(last, (int, float)) or last > limit:
                continue
            host = policy['host']
            if host not in loaded_by_host:
                ps = ps_fn(host, '/api/ps', timeout=3) or {}
                loaded_by_host[host] = {model_name(str(m.get('name') or m.get('model') or ''))
                                        for m in (ps.get('models') or []) if isinstance(m, dict)}
            loaded = loaded_by_host[host]
            if policy['model'] not in loaded:
                continue
            try:
                post_fn(host, {'model': policy['model'], 'keep_alive': 0}, 60)
            except OSError as e:
                log(f'idle unload of {policy["model"]} skipped: {e}')
                continue
            loaded.discard(policy['model'])
            unloaded.append(policy['model'])
            state.pop(key, None)
            changed = True
        if changed:
            idle_write(cfg, state)
    return unloaded


_YIELD = {}


def yield_cached(machine, ttl=60, clock=time.monotonic):
    """The machine's yield note, looked up at most once a minute (the look is an ssh round trip)."""
    at, note = _YIELD.get(machine['name'], (None, None))
    if at is None or clock() - at >= ttl:
        at, note = clock(), remote.yield_note(machine)
        _YIELD[machine['name']] = (at, note)
    return note


def wait_for_yield(cfg, worker, poll=60, sleep=time.sleep):
    """While the worker's machine has its yield flag up, send nothing. The models are unloaded only when the flag's text
    holds the word `unload`; otherwise they stay loaded for whoever asked. Resumes when the flag is gone."""
    machine = cfg['machines'].get(worker.get('machine', ''))
    unloaded = False
    while machine and (note := yield_cached(machine)) is not None:
        if 'unload' in note.lower() and not unloaded:
            try:
                post(worker['host'], {'model': worker['model'], 'keep_alive': 0}, 60)
            except OSError as e:
                log(f'unload of {worker["model"]} skipped: {e}')
            unloaded = True
        state = 'model unloaded' if unloaded else 'model left loaded'
        log(f'{machine["name"]} is lent out (yield flag {machine["yield_file"]}): paused, {state}; checking again in {poll} s')
        sleep(poll)


def threads_at_share(cfg, worker, now=None, idle_fn=None):
    """The model's threads at its machine's share for Tutti (tutti/share.py): workers.<w>.threads (else none set: Ollama's
    own choice, unless the share is lower, then the machine's cores at it)."""
    from datetime import datetime, timezone

    from . import fleet, share
    name = fleet.machine_of(cfg, worker.get('_name') or next((n for n, w in cfg['workers'].items() if w is worker), ''))
    m = cfg['machines'].get(name) or {}
    idle = (idle_fn or share.idle_seconds)() if m.get('transport') == 'local' else None
    pct, _ = share.pct(m, None, now or datetime.now(timezone.utc), idle, (cfg.get('autopilot') or {}).get('quiet_hours'))
    if pct >= 100:
        return worker.get('threads')
    # the target machine's cores: this computer's own, else machines.<m>.cores, else a cautious 8
    base = worker.get('threads') or (os.cpu_count() if m.get('transport') == 'local' else None) or m.get('cores') or 8
    return max(1, share.scaled(base, pct))   # a share of 0 still sends one thread, never Ollama's all


def wait_for_preempt(cfg, worker, poll=2, sleep=time.sleep):
    """While Tutti's own job holds this machine's best model (tutti/brain.py's pre-emption), other work waits here between
    its requests: nothing is killed, its state stays where it is, and it goes on as soon as the marker is lowered."""
    from . import brain
    waited = 0.0
    machine = worker.get('machine') or next((n for n, m in (cfg.get('machines') or {}).items() if m.get('transport') == 'local'), '')
    while brain.preempted_now(cfg, machine) is not None:
        sleep(poll)
        waited += poll
    return waited


def generate(cfg, worker, prompt, wait=False, low_priority=False, timeout=1800, own=False):
    """One answer from the worker's model. wait=True retries forever while the host is down (for runs that must not skip).
    own=True: Tutti's own job (tutti/brain.py), which never waits for its own pre-emption marker."""
    wait_for_yield(cfg, worker)
    if not own:
        wait_for_preempt(cfg, worker)
    unload_idle_models(cfg)
    while low_priority and others_busy(cfg):
        time.sleep(5)
    options = {'temperature': worker.get('temperature', 0.1), 'num_ctx': worker.get('num_ctx', 16384)}
    threads = threads_at_share(cfg, worker)
    if threads:
        options['num_thread'] = threads
    payload = {'model': worker['model'], 'prompt': prompt, 'stream': False, 'think': False, 'options': options}
    keep_alive = effective_keep_alive(worker)
    if keep_alive is not None:
        payload['keep_alive'] = keep_alive
    tries = 0
    while True:
        try:
            answer = _timed(cfg, worker, lambda: post(worker['host'], payload, timeout))['response'].strip()
            note_used(cfg, worker)
            unload_idle_models(cfg)
            return answer
        except OSError as e:
            if not wait:
                raise RuntimeError(f'the model server {worker["host"]} ({worker["model"]}) did not answer: {e}. Start it, or its '
                                   'tunnel (`tutti tunnel start <worker>`), and run this again') from e
            tries += 1
            log(f'{worker["host"]} unreachable, waiting ({e}); try {tries}')
            time.sleep(min(60, 10 * tries))


def _timed(cfg, worker, call):
    """The call, with a marker while it runs (the dashboard counts the machine busy) and its tokens logged after
    (tutti/energy.py): the energy card's busy time, jobs and tokens."""
    from . import energy
    name = worker.get('_name') or next((n for n, w in cfg['workers'].items() if w is worker), worker.get('host', ''))
    marker = os.path.join(config.state_dir(cfg), f'local-run-{os.getpid()}-{time.monotonic_ns()}')
    start = time.time()
    try:
        with open(marker, 'w', encoding='utf8') as f:
            f.write(f'{name} {start}')
    except OSError:
        marker = None
    try:
        answer = call()
        energy.log_run(cfg, name, start, time.time(), answer)
        return answer
    finally:
        if marker and os.path.exists(marker):
            os.remove(marker)


def read(path):
    with open(path, encoding='utf8', errors='replace') as f:
        return f.read()


def current(path):
    """A file's text for a brief; a file the task is to create does not exist yet."""
    return read(path) if os.path.exists(path) else '(a new file: write it)\n'


def ask(cfg, worker, instruction, files=(), stdin_text=''):
    parts = [instruction] + [f'--- {p} ---\n{read(p)}' for p in files]
    if stdin_text:
        parts.append('--- stdin ---\n' + stdin_text)
    busy_marker(cfg)
    return generate(cfg, worker, '\n\n'.join(parts))


def rules_text(cfg):
    rules = cfg['brief']['rules']
    return ('Rules:\n' + '\n'.join(f'- {r}' for r in rules) + '\n') if rules else ''


def code(cfg, worker, path, instruction, context=()):
    """The model rewrites one file in place; the orchestrator reviews `git diff <file>`."""
    source = read(path)
    hints = ''.join(f'\n--- read-only context: {c} ---\n{read(c)}' for c in context)
    prompt = (f'You edit one source file. Keep existing comments and style; change only what the instruction needs.\n{rules_text(cfg)}'
              f'INSTRUCTION:\n{instruction}\n{hints}\n--- file to edit: {path} ---\n{source}\n'
              'Return ONLY the complete new file content inside one ```fenced``` block, nothing else.')
    busy_marker(cfg)
    match = FENCE.search(generate(cfg, worker, prompt))
    if not match:
        raise RuntimeError(f'the model returned no fenced file for {path}: nothing was written; shorten the instruction or the file')
    with open(path, 'w', encoding='utf8') as f:
        f.write(match.group(1))
    return f'{path} rewritten ({len(source.splitlines())} -> {len(match.group(1).splitlines())} lines); review with git diff'


def apply_blocks(answer, allowed, base):
    """Writes the `=== FILE: path ===` blocks of an answer, only for the files the task named. Returns the paths written."""
    changed = []
    for path, content in FILE_BLOCK.findall(answer):
        if path in allowed:
            os.makedirs(os.path.dirname(os.path.join(base, path)) or '.', exist_ok=True)   # a file the task creates
            with open(os.path.join(base, path), 'w', encoding='utf8') as f:
                f.write(content)
            changed.append(path)
    return changed


def agent(cfg, worker, task, check, files, rounds=4, base='.'):
    """Edit, run the check, read the failure, try again: up to `rounds` times. Edits stay uncommitted. True when the check passes."""
    busy_marker(cfg)
    feedback = ''
    for n in range(1, rounds + 1):
        sources = ''.join(f'\n=== FILE: {f} ===\n```\n{current(os.path.join(base, f))}```\n' for f in files)
        prompt = (f'{rules_text(cfg)}TASK:\n{task}\n{feedback}\nCurrent files:{sources}\n'
                  'Answer ONLY with the files you change, each as "=== FILE: <path> ===" followed by its complete content in one '
                  'fenced block.')
        changed = apply_blocks(generate(cfg, worker, prompt, timeout=3600), files, base)
        result = subprocess.run(['bash', '-c', check], cwd=base, capture_output=True, text=True)
        print(f'round {n}: changed {changed or "nothing"}; check rc={result.returncode}', flush=True)
        if result.returncode == 0:
            return True
        tail = (result.stdout + result.stderr)[-6000:]
        feedback = f'\nThe check `{check}` FAILED after your last edit; its output ends with:\n{tail}\nFix it.'
    return False
