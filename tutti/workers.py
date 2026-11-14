"""Outside workers: other vendors' CLI agents (Cursor, Antigravity, Claude) and local models. The orchestrator briefs them,
reviews their diff, then gates it; a worker never commits."""
import json
import shutil
import subprocess
import urllib.request

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


def _ollama_model_names(data):
    names = set()
    for m in data.get('models') or []:
        if m.get('name'):
            names.add(m['name'])
        if m.get('model'):
            names.add(m['model'])
    return names


def available(worker, timeout=3):
    """(usable now?, a note). A CLI worker needs its binary on PATH; an ollama worker needs its server and its model."""
    if worker['kind'] == 'cli':
        exe = shutil.which(worker['command'][0])
        return (True, exe) if exe else (False, f'{worker["command"][0]} is not on PATH: install it and sign in once')
    model = worker['model']
    try:
        with urllib.request.urlopen(f'http://{worker["host"]}/api/tags', timeout=timeout) as r:
            names = _ollama_model_names(json.loads(r.read().decode()))
    except ValueError as e:
        return False, f'{worker["host"]} answered /api/tags with something that is not JSON ({e}): is it an ollama server?'
    except OSError as e:
        return False, f'{worker["host"]} does not answer ({e}): start the model server or its tunnel'
    if model not in names:
        return False, f'model missing: {model}: ollama pull {model}'
    return True, f'{model} at {worker["host"]}'


def listing(cfg):
    rows = []
    for name, w in cfg['workers'].items():
        ok, note = available(w)
        rows.append(f'{name:12} {w["kind"]:7} {"ready" if ok else "DOWN":6} {note}')
    return '\n'.join(rows) or 'no workers in the config: add a workers: section (tutti.config.example.yaml)'


def run(cfg, name, task, files=(), cwd=None):
    """Runs a CLI worker on a brief in `cwd` (give each worker its own worktree). Returns its exit code."""
    worker = cfg['workers'][name]
    return subprocess.run(command(worker, brief(cfg, task, files)), cwd=cwd).returncode
