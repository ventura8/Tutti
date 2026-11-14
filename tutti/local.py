"""Local model workers (Ollama's HTTP API): ask, rewrite one file, or loop edit -> check -> retry. Bulky reading and drafting
costs no cloud tokens. A worker waits while its machine's yield flag is up, and a low-priority call lets other local jobs go first."""
import atexit
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

from . import config, remote, waiting

FENCE = re.compile(r'```[a-zA-Z]*\n(.*?)```', re.S)
FILE_BLOCK = re.compile(r'=== FILE: (\S+) ===\n```[a-zA-Z]*\n(.*?)```', re.S)


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


def post(host, payload, timeout):
    req = urllib.request.Request(config.ollama_url(host, '/api/generate'), json.dumps(payload).encode(),
                                 {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


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


def generate(cfg, worker, prompt, wait=False, low_priority=False, timeout=1800):
    """One answer from the worker's model. wait=True retries forever while the host is down (for runs that must not skip)."""
    wait_for_yield(cfg, worker)
    while low_priority and others_busy(cfg):
        time.sleep(5)
    options = {'temperature': worker.get('temperature', 0.1), 'num_ctx': worker.get('num_ctx', 16384)}
    if worker.get('threads'):
        options['num_thread'] = worker['threads']
    payload = {'model': worker['model'], 'prompt': prompt, 'stream': False, 'think': False, 'options': options}
    tries = 0
    while True:
        try:
            return post(worker['host'], payload, timeout)['response'].strip()
        except OSError as e:
            if not wait:
                raise RuntimeError(f'the model server {worker["host"]} ({worker["model"]}) did not answer: {e}. Start it, or its '
                                   'tunnel (`tutti tunnel start <worker>`), and run this again') from e
            tries += 1
            log(f'{worker["host"]} unreachable, waiting ({e}); try {tries}')
            time.sleep(min(60, 10 * tries))


def read(path):
    with open(path, encoding='utf8', errors='replace') as f:
        return f.read()


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
            with open(os.path.join(base, path), 'w', encoding='utf8') as f:
                f.write(content)
            changed.append(path)
    return changed


def agent(cfg, worker, task, check, files, rounds=4, base='.'):
    """Edit, run the check, read the failure, try again: up to `rounds` times. Edits stay uncommitted. True when the check passes."""
    busy_marker(cfg)
    feedback = ''
    for n in range(1, rounds + 1):
        sources = ''.join(f'\n=== FILE: {f} ===\n```\n{read(os.path.join(base, f))}```\n' for f in files)
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
