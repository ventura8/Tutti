"""Outside workers: other vendors' CLI agents (Cursor, Antigravity, Claude) and local models. The orchestrator briefs them,
reviews their diff, then gates it; a worker never commits."""
import collections
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.request

from . import config, inuse

STANDING = ['Work only inside the current directory.', 'Do not commit, push or deploy: leave your edits uncommitted for review.',
            'Do not read or print secrets (.env files, tokens, keys).']


def brief(cfg, task, files=()):
    """The full brief: the task, the files involved, the project's rules and the standing rules."""
    lines = [task.strip(), '']
    if files:
        lines += ['Files involved:'] + [f'- {f}' for f in files] + ['']
    lines += ['Rules:'] + [f'- {r}' for r in [*cfg['brief']['rules'], *STANDING]]
    return '\n'.join(lines)


PLACEHOLDER = re.compile(r'\{(prompt|log)\}')


def command(worker, prompt, log=None):
    """The worker's argv: `{prompt}` is the brief, `{log}` a log file of this run (agy --log-file {log}, where its
    permission denials are written). Without a log path an argument pair ending in `{log}` is dropped."""
    out = []
    for a in worker['command']:
        if '{log}' in a and not log:
            if out and out[-1].startswith('-'):
                out.pop()
            continue
        # one pass over the template: a prompt that mentions {log} keeps it as written
        out.append(PLACEHOLDER.sub(lambda m: {'prompt': prompt, 'log': log or ''}[m.group(1)], a))
    return out


def pairs(args):
    """[(flag, value or None) or (word, None)] of an argument list: `--x v` is one pair when v does not start with `-`."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a.startswith('-') and i + 1 < len(args) and not args[i + 1].startswith('-'):
            out.append((a, args[i + 1]))
            i += 2
        else:
            out.append((a, None))
            i += 1
    return out


def with_args(argv, extra):
    """The worker's argv with an account's extra arguments: a `--flag value` the command already has gets the account's value
    (agy-claude's `--settings` replaces the worker's per-command allow list, its `--model` the default), the rest is added."""
    out = list(argv)
    for flag, value in pairs(list(extra)):
        at = out.index(flag) if flag.startswith('-') and value is not None and flag in out[1:] else -1
        if at > 0 and at + 1 < len(out) and not out[at + 1].startswith('-'):
            out[at + 1] = value
        else:
            out += [flag] if value is None else [flag, value]
    return out


def _tagged(name):
    """Ollama's own identity for a model: an untagged `llama3` is `llama3:latest`."""
    return name if ':' in name.rsplit('/', 1)[-1] else f'{name}:latest'


def _ollama_model_names(data):
    """The models an /api/tags answer lists, tagged; ValueError when the answer has another shape."""
    models = data.get('models') if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise ValueError('no "models" list')
    return {_tagged(m[k]) for m in models if isinstance(m, dict) for k in ('name', 'model') if isinstance(m.get(k), str) and m[k]}


def available(worker, timeout=3):
    """(usable now?, a note). A CLI worker needs its binary on PATH; an ollama worker needs its server and its model."""
    if worker['kind'] == 'cli':
        exe = shutil.which(worker['command'][0])
        return (True, exe) if exe else (False, f'{worker["command"][0]} is not on PATH: install it and sign in once')
    model = worker['model']
    try:
        with urllib.request.urlopen(config.ollama_url(worker['host'], '/api/tags'), timeout=timeout) as r:
            names = _ollama_model_names(json.loads(r.read().decode()))
    except ValueError as e:
        return False, f'{worker["host"]} answered /api/tags with something that is not an ollama model list ({e}): is it an ollama server?'
    except OSError as e:
        return False, f'{worker["host"]} does not answer ({e}): start the model server or its tunnel'
    if _tagged(model) not in names:
        pull = f'OLLAMA_HOST={shlex.quote(worker["host"])} ollama pull {shlex.quote(model)}'
        return False, f'model missing: {model}: {pull}'
    return True, f'{model} at {worker["host"]}'


def listing(cfg):
    rows = []
    for name, w in cfg['workers'].items():
        ok, note = available(w)
        rows.append(f'{name:12} {w["kind"]:7} {"ready" if ok else "DOWN":6} {note}')
    return '\n'.join(rows) or 'no workers in the config: add a workers: section (tutti.config.example.yaml)'


KEEP_LINES = 2000


TERM_WAIT = 10   # seconds a CLI stopped at the threshold gets to exit before it is killed
COMPACT_RC = 76   # the run was stopped at the context threshold, to be started again with a handoff (tutti/autocompact.py)


def run(cfg, name, task, files=(), cwd=None, out=None, extra=(), log=None):
    """Runs a CLI worker on a brief in `cwd` (give each worker its own worktree). Its output is passed through as it comes and
    the last lines are kept, so a limit message in them can be read. Returns (exit code, the last lines); COMPACT_RC when it
    was stopped at the autocompact threshold (a CLI without native compaction whose output shows its context use)."""
    from . import autocompact
    worker = cfg['workers'][name]
    out = out or sys.stdout
    kept = collections.deque(maxlen=KEEP_LINES)
    argv = with_args(command(worker, brief(cfg, task, files), log), extra)
    env = {**os.environ, **autocompact.env_for(cfg, worker)}
    watch = autocompact.Watch(cfg, worker, log=log)
    stopped = False
    # the use lock: `tutti update` never replaces the program while this run uses it (tutti/inuse.py)
    with inuse.using(config.state_dir(cfg), argv[0]), \
            subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors='replace', bufsize=1, env=env) as p:
        for line in p.stdout:
            out.write(line)
            out.flush()
            kept.append(line.rstrip('\n'))
            if watch.line(line) and p.poll() is None:   # only a run still going is stopped; a final status line is not
                stopped = True
                p.terminate()
                try:
                    p.wait(timeout=TERM_WAIT)
                except subprocess.TimeoutExpired:   # it ignored SIGTERM: the restart must not hang on it
                    p.kill()
                out.write(f'tutti: context at {watch.pct:g} % (autocompact.threshold_pct {watch.limit}): stopped, restarting '
                          'with a handoff\n')
                break
    return (COMPACT_RC if stopped else p.returncode), list(kept)
