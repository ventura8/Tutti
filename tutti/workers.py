"""Outside workers: other vendors' CLI agents (Cursor, Antigravity, Claude) and local models. The orchestrator briefs them,
reviews their diff, then gates it; a worker never commits."""
import collections
import json
import shlex
import shutil
import subprocess
import sys
import urllib.request

from . import config

STANDING = ['Work only inside the current directory.', 'Do not commit, push or deploy: leave your edits uncommitted for review.',
            'Do not read or print secrets (.env files, tokens, keys).']


def brief(cfg, task, files=()):
    """The full brief: the task, the files involved, the project's rules and the standing rules."""
    lines = [task.strip(), '']
    if files:
        lines += ['Files involved:'] + [f'- {f}' for f in files] + ['']
    lines += ['Rules:'] + [f'- {r}' for r in [*cfg['brief']['rules'], *STANDING]]
    return '\n'.join(lines)


def command(worker, prompt):
    return [prompt if a == '{prompt}' else a.replace('{prompt}', prompt) for a in worker['command']]


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


def run(cfg, name, task, files=(), cwd=None, out=None, extra=()):
    """Runs a CLI worker on a brief in `cwd` (give each worker its own worktree). Its output is passed through as it comes and
    the last lines are kept, so a limit message in them can be read. Returns (exit code, the last lines)."""
    worker = cfg['workers'][name]
    out = out or sys.stdout
    kept = collections.deque(maxlen=KEEP_LINES)
    with subprocess.Popen(command(worker, brief(cfg, task, files)) + list(extra), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors='replace', bufsize=1) as p:
        for line in p.stdout:
            out.write(line)
            out.flush()
            kept.append(line.rstrip('\n'))
    return p.returncode, list(kept)
