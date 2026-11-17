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


def statuses(cfg, available_fn=None):
    """{worker: (ready, note)}: every worker asked at once. The one status `tutti workers`, the dashboard's Workers card, its
    Local models cards and Needs you all show (a local model is ready when its server answers through the worker's own host,
    its tunnel included, whatever the machine's ssh probe says)."""
    from concurrent.futures import ThreadPoolExecutor
    available_fn = available_fn or available
    names = list(cfg['workers'])
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(names)))) as pool:
        return dict(zip(names, pool.map(lambda n: available_fn(cfg['workers'][n]), names), strict=True))


def on_machines(cfg, ready_fn=None):
    """{worker: {machine: (ready, note)}} of the CLI workers that run on other machines (workers.<w>.machines), each machine
    asked over its transport whether the CLI answers `--version` and its login files (workers.<w>.login) exist. This computer
    is answered by `available`."""
    from concurrent.futures import ThreadPoolExecutor

    from . import cliplace, remote
    ready_fn = ready_fn or remote.cli_ready
    main = cliplace.main_name(cfg)
    asks = [(w, m) for w in cfg['workers'] for m in cliplace.machines_of(cfg, w) if m != main]
    if not asks:
        return {}

    def one(pair):
        w, m = pair
        worker = cfg['workers'][w]
        logins = worker.get('login') or []
        return ready_fn(cfg['machines'][m], worker['command'][0], [logins] if isinstance(logins, str) else logins)
    with ThreadPoolExecutor(max_workers=min(8, len(asks))) as pool:
        found = list(pool.map(one, asks))
    out = {}
    for (w, m), r in zip(asks, found, strict=True):
        out.setdefault(w, {})[m] = r
    return out


def listing(cfg, available_fn=None, ready_fn=None):
    rows = []
    checks = statuses(cfg, available_fn)
    away = on_machines(cfg, ready_fn)
    for name, w in cfg['workers'].items():
        ok, note = checks[name]
        rows.append(f'{name:12} {w["kind"]:7} {"ready" if ok else "DOWN":6} {note}')
        rows += [f'{"":12} {"on " + m:15} {"ready" if r_ok else "DOWN":6} {r_note}' for m, (r_ok, r_note) in away.get(name, {}).items()]
    return '\n'.join(rows) or 'no workers in the config: add a workers: section (tutti.config.example.yaml)'


KEEP_LINES = 2000
# secrets a worker never needs (it edits a worktree; Tutti pushes, opens pull requests and asks scouts itself): a worker steered
# by a prompt injected through a repository, an issue or a Sentry event cannot hand them on (docs/SECURITY-AUDIT-2026-11.md)
NOT_FOR_WORKERS = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN', 'GITLAB_TOKEN', 'SENTRY_AUTH_TOKEN',
                   'TUTTI_SENTRY_DSN', 'TUTTI_GROK_KEY', 'TUTTI_GROK_URL', 'NPM_TOKEN', 'NODE_AUTH_TOKEN', 'TWINE_PASSWORD',
                   'PYPI_TOKEN', 'SONAR_TOKEN', 'CODECOV_TOKEN', 'DOCKER_PASSWORD', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN')


def worker_env(cfg, base=None):
    """The environment of a worker or a reviewer: this one without NOT_FOR_WORKERS and the variables that hold a scout's
    webhook URL and key (scouts.<name>.url_env, key_env). The worker CLI's own sign-in (its config files, its own API key
    variable) stays."""
    base = os.environ if base is None else base
    drop = dropped(cfg)
    return {k: v for k, v in base.items() if k not in drop}


def dropped(cfg):
    """The variables a worker never gets: NOT_FOR_WORKERS and the scouts' webhook URL and key variables."""
    drop = set(NOT_FOR_WORKERS)
    for sc in ((cfg or {}).get('scouts') or {}).values():
        drop |= {sc.get(k) for k in ('url_env', 'key_env') if isinstance(sc, dict) and isinstance(sc.get(k), str)}
    return drop



TERM_WAIT = 10   # seconds a CLI stopped at the threshold gets to exit before it is killed
COMPACT_RC = 76   # the run was stopped at the context threshold, to be started again with a handoff (tutti/autocompact.py)


def grant_tree(argv, cwd):
    """A headless CLI with an allow-list (a JSON settings argument with permissions.allow, as agy's --settings) may read and
    write its own worktree: `read_file(<tree>)` and `write_file(<tree>)` are added, never a wider grant. Without them agy
    auto-denies every file read in headless mode and the run ends with no output (2026-11-14: 23 wasted agy runs)."""
    if not cwd:
        return argv
    tree = os.path.abspath(cwd)
    out = []
    for a in argv:
        data = None
        if a.lstrip().startswith('{'):
            try:
                data = json.loads(a)
            except ValueError:
                data = None
        perms = data.get('permissions') if isinstance(data, dict) else None
        allow = perms.get('allow') if isinstance(perms, dict) else None
        if isinstance(allow, list):
            perms['allow'] = [*allow, *[g for g in (f'read_file({tree})', f'write_file({tree})') if g not in allow]]
            a = json.dumps(data, separators=(',', ':'))
        out.append(a)
    return out


def stop_tree(p):
    """SIGTERM to the process group the CLI leads (its own session: the pid we started and every child, even one forked
    after a /proc scan); on Windows its process tree."""
    if os.name == 'nt':
        from . import handover
        handover.end_tree(p.pid)
        return
    import signal
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except OSError:
        pass


def run(cfg, name, task, files=(), cwd=None, out=None, extra=(), log=None):
    """Runs a CLI worker on a brief in `cwd` (give each worker its own worktree). Its output is passed through as it comes and
    the last lines are kept, so a limit message in them can be read. Returns (exit code, the last lines); COMPACT_RC when it
    was stopped at the autocompact threshold (a CLI without native compaction whose output shows its context use)."""
    from . import autocompact
    worker = cfg['workers'][name]
    out = out or sys.stdout
    kept = collections.deque(maxlen=KEEP_LINES)
    argv = grant_tree(with_args(command(worker, brief(cfg, task, files), log), extra), cwd)
    env = {**worker_env(cfg), **autocompact.env_for(cfg, worker)}
    watch = autocompact.Watch(cfg, worker)
    # the use lock: `tutti update` never replaces the program while this run uses it (tutti/inuse.py)
    with inuse.using(config.state_dir(cfg), argv[0]), \
            subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors='replace', bufsize=1, env=env, start_new_session=os.name != 'nt') as p:
        try:
            stopped = pass_through(p, out, kept, watch)
        except KeyboardInterrupt:   # its own session gets no Ctrl-C from the terminal: it is ended with us
            stop_tree(p)
            raise
    return (COMPACT_RC if stopped else p.returncode), list(kept)


def remote_script(cfg, name, task, files, path, extra=(), log=None):
    """The bash line that runs CLI worker `name` headless in `path` on another machine (docs/WORKERS.md, *On another machine*):
    the same brief, account arguments and allow-list (its own tree granted) as here, the variables a worker never gets unset
    there too, its process group's id written to `<path>.pid` so a stop ends every child."""
    from . import autocompact
    worker = cfg['workers'][name]
    argv = grant_tree(with_args(command(worker, brief(cfg, task, files), log), extra), path)
    drop = sorted(dropped(cfg))
    env = ['env', *[a for k in drop for a in ('-u', k)], 'TUTTI_BACKGROUND=1',
           *[f'{k}={v}' for k, v in autocompact.env_for(cfg, worker).items()]]
    q = shlex.quote(path)
    return f'cd {q} && echo $$ > {shlex.quote(path + ".pid")} && exec {shlex.join([*env, *argv])}'


def run_remote(cfg, name, task, files, machine, path, out=None, extra=(), log=None, popen=None, stop=None):
    """`run` on another machine: the CLI over the machine's transport in its copy at `path`. Returns (exit code, the last
    lines); remote.DROPPED (255) with a line saying so when the connection failed or dropped (the attempt fails, the next
    round retries it); COMPACT_RC when it was stopped at the autocompact threshold."""
    from . import autocompact, remote
    popen = popen or remote.cli_popen
    stop = stop or (lambda: remote.cli_stop(machine, path + '.pid'))
    out = out or sys.stdout
    kept = collections.deque(maxlen=KEEP_LINES)
    watch = autocompact.Watch(cfg, cfg['workers'][name])
    p = popen(machine, remote_script(cfg, name, task, files, path, extra, log))
    stopped = False
    try:
        for line in p.stdout:
            out.write(line)
            out.flush()
            kept.append(line.rstrip('\n'))
            if watch.line(line) and p.poll() is None:
                stop()
                stopped = True
                out.write(f'tutti: context at {watch.pct:g} % (autocompact.threshold_pct {watch.limit}): stopped on '
                          f'{machine["name"]}, restarting with a handoff\n')
                break
    except KeyboardInterrupt:
        stop()
        raise
    finally:
        if stopped:
            p.kill()
        rc = p.wait()
    if stopped:
        return COMPACT_RC, list(kept)
    if rc == remote.DROPPED:
        note = (f'tutti: the connection to {machine["name"]} failed or dropped (ssh exit {rc}): this attempt failed; the worker '
                f'there may still be running in {path} and is stopped')
        stop()
        out.write(note + '\n')
        kept.append(note)
    return rc, list(kept)


def pass_through(p, out, kept, watch):
    """The CLI's output passed through and kept; True when it was stopped at the autocompact threshold."""
    for line in p.stdout:
        out.write(line)
        out.flush()
        kept.append(line.rstrip('\n'))
        if watch.line(line) and p.poll() is None:   # only a run still going is stopped; a final status line is not
            stop_tree(p)   # the CLI and everything it started: no child outlives the stop (t128)
            try:
                p.wait(timeout=TERM_WAIT)
            except subprocess.TimeoutExpired:   # it ignored SIGTERM: the restart must not hang on it
                p.kill()
            finally:
                stop_tree(p)   # a child that ignored the first signal or outlived its parent
            out.write(f'tutti: context at {watch.pct:g} % (autocompact.threshold_pct {watch.limit}): stopped, restarting '
                      'with a handoff\n')
            return True
    return False


