"""The dashboard's "pause, stop now": a running job is handed over before it stops, never just killed.

- an agent run: a handoff is written from its worktree (the changes, the log tail), the partial work is committed in its
  worktree and kept on a branch `tutti/handoff/<track>`, the run is stopped and the task goes to the next worker with room,
  which continues in the same worktree from the handoff (as `tutti conduct` hands over and the autopilot retries);
- a test or check run (`tutti test`, with or without a device): stopped and started again, from the start, on the machine or
  device routing picks now (the paused one is skipped);
- a file transfer: stopped; the partial file stays, so sending again resumes it.

Each job says what it was handed to in `<log>.handoff`, which the dashboard shows. The stopping side ends the job's whole
process tree (Linux /proc), so a worker CLI or an ssh child does not go on writing after the hand-over."""
import json
import os
import re
import signal
import subprocess
import sys
import time

from . import conduct, config, controls, devices, remote, routing


def read_spec(log):
    try:
        with open(log + '.job', encoding='utf8') as f:
            spec = json.load(f)
    except (OSError, ValueError):
        return {}
    return spec if isinstance(spec, dict) else {}


def tail(log, n=30):
    try:
        with open(log, encoding='utf8', errors='replace') as f:
            return [x.rstrip('\n') for x in f.readlines()[-n:]]
    except OSError:
        return []


def descendants(pid, proc='/proc'):
    """Every process under `pid` (children first found, then theirs), from /proc/<n>/stat."""
    kids = {}
    for d in os.listdir(proc):
        if not d.isdigit():
            continue
        try:
            with open(os.path.join(proc, d, 'stat'), encoding='utf8') as f:
                ppid = int(f.read().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(d))
    out, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


def end_tree(pid, kill=os.kill, find=descendants):
    """SIGTERM to the job and everything it started. Returns the pids signalled."""
    done = []
    for p in [*find(pid), pid]:
        try:
            kill(p, signal.SIGTERM)
            done.append(p)
        except OSError:
            pass
    return done


def spawn(cfg, args, name):
    """`tutti <args>` detached, its output in <state>/handover/<time>-<name>.log. Returns the log's path."""
    folder = os.path.join(config.state_dir(cfg), 'handover')
    os.makedirs(folder, exist_ok=True)
    log = os.path.join(folder, f'{int(time.time())}-{name}.log')
    with open(log, 'a', encoding='utf8') as out:
        subprocess.Popen([sys.executable, remote.BIN, '--config', cfg['_path'], *args], stdout=out, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    return log


SAFE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,80}')


def git(wt, *args):
    return subprocess.run(['git', *args], cwd=wt, capture_output=True, text=True, timeout=60, check=False)


def worktree_of(cfg, track):
    """The run's worktree, rebuilt from the config as `tutti run --repo` makes it (<worktrees>/<delegate.prefix><track>),
    never a path read from a file; None for a track name that is not a plain name."""
    if not SAFE.fullmatch(track or ''):
        return None
    return os.path.join(config.expand(cfg['project']['worktrees']), f'{cfg["delegate"]["prefix"]}{track}')


def keep_work(wt, track, run_id):
    """Snapshots what the agent changed in its worktree as a commit on `tutti/handoff/<track>`, without moving HEAD or touching
    the files: the next worker continues in the same tree, and its diff against HEAD still holds the earlier work."""
    if not wt or not os.path.isdir(wt):
        return 'no worktree found: nothing to keep'
    git(wt, 'add', '-A')
    try:
        if git(wt, 'diff', '--cached', '--quiet').returncode == 0:
            return 'no changes to keep'
        tree = git(wt, 'write-tree').stdout.strip()
        r = git(wt, 'commit-tree', tree, '-p', 'HEAD', '-m', f'chore(handoff): partial work of run {run_id}, stopped from the dashboard')
        if r.returncode != 0 or not tree:
            return f'the partial work could not be snapshotted ({(r.stderr or r.stdout).strip()[:200]}); it stays in {wt}'
        git(wt, 'branch', '-f', f'tutti/handoff/{track}', r.stdout.strip())
    finally:
        git(wt, 'reset', '-q')
    return f'the partial work is kept on tutti/handoff/{track} and stays in {wt}'


def pick_worker(cfg):
    from datetime import datetime, timezone

    from . import cli  # cli imports this module
    return cli.choose_worker(cfg, datetime.now(timezone.utc))


def check_args(checks):
    """`--check` flags of a hand-over's checks, with `--check-where route` when they ran on a routed machine."""
    runs = [c.get('run') if isinstance(c, dict) else c for c in checks or []]
    where = {c.get('where', 'local') for c in checks or [] if isinstance(c, dict)}
    return [f'--check={r}' for r in runs if r] + (['--check-where', 'route'] if where == {'route'} else [])


def hand_run(cfg, log, spec, why, pick=pick_worker, start=spawn):
    """An agent run, already stopped: handoff note, partial work kept, the task to the next worker with room in the same
    worktree, with the same files and checks."""
    track = str(spec.get('track') or spec.get('run_id') or '')
    run_id = str(spec.get('run_id') or '')
    kept = keep_work(worktree_of(cfg, track), track, run_id if SAFE.fullmatch(run_id) else '?')
    choice = pick(cfg)
    to = choice.get('worker')
    note = conduct.handoff_note(spec.get('task', ''), spec.get('worker', '?'), to, why, '', kept, tail(log))
    with open(log[:-4] + '.handoff.md', 'w', encoding='utf8') as f:
        f.write(note)
    repo = spec.get('repo') or ''
    if not to or not SAFE.fullmatch(track) or not os.path.isabs(repo):
        why = choice.get('reason') or 'the run left no usable track or repository'
        return f'not handed over: {why}; the handoff and the kept work wait in the worktree'
    task = f'{spec.get("task", "")}\n\nContinue from this handoff (the worktree already holds the earlier work):\n\n{note}'
    args = ['run', to, task, *[str(f) for f in spec.get('files') or []], '--repo', repo, '--track', track, '--bg',
            *[f'--extra={x}' for x in choice.get('args') or []], *check_args(spec.get('checks'))]
    start(cfg, args, f'run-{track}')
    return f'handed over to {to} (same worktree, track {track}); {kept}'


def free_device(cfg, readings_fn, skip=''):
    """The first device that is ready now (its host machine not paused, the device not paused, not busy), or ''."""
    for name, d in (cfg.get('devices') or {}).items():
        if name == skip or d.get('paused') or (cfg['machines'].get(d['machine']) or {}).get('paused'):
            continue
        r = readings_fn(cfg['machines'][d['machine']], d)
        if devices.status(devices.policy(d), r, d['serial'])[0] == 'ready':
            return name
    return ''


def hand_test(cfg, spec, kind, name, probe_fn=remote.probe, readings_fn=remote.device_readings, start=spawn):
    """A test or check run, already stopped: started again, from the start, where routing (or a ready device) says now."""
    args = list(spec.get('args') or [])
    if spec.get('device'):
        dev = free_device(cfg, readings_fn, skip=name if kind == 'device' else '')
        args = ['test', '--device', dev or 'any', *args]
        where = f'device {dev}' if dev else 'the first device that gets ready (--device any)'
    else:
        try:
            c = routing.choose(cfg['routing']['order'], cfg['machines'], lambda n: probe_fn(cfg['machines'][n]))
        except routing.NoMachine as e:
            return f'not handed over: {e}'
        args = ['test', '--on', c.machine, *args]
        where = f'{c.machine}{" (queued)" if c.queued else ""}'
    log = start(cfg, args, f'test-{spec.get("track", "run")}')
    return f'handed over to {where}, from the start; its output: {log}'


def wait_gone(pid, is_alive=controls.alive, sleep=time.sleep, seconds=15):
    """Waits up to `seconds` for the stopped job to end, so nothing writes to its tree while it is handed over."""
    for _ in range(int(seconds * 4)):
        if not is_alive(pid):
            return True
        sleep(0.25)
    return False


def stop_now(cfg, kind, name, why=None, end=end_tree, gone=wait_gone, **hands):
    """Stops every running tutti job bound to the item, waits for it to end, then hands it over. Returns
    [(log, what it was handed to)]. The job's result file is written (143, stopped), so its pid is never taken for it again."""
    why = why or f'{kind} {name} was paused with "stop now"'
    out = []
    for log, pid in controls.running_jobs(config.state_dir(cfg), kind, name):
        spec = read_spec(log)
        end(pid)
        gone(pid)
        with open(log + '.rc', 'w', encoding='utf8') as f:
            f.write('143\n')
        if spec.get('kind') == 'run':
            handed = hand_run(cfg, log, spec, why, **{k: v for k, v in hands.items() if k in ('pick', 'start')})
        elif spec.get('kind') == 'test':
            handed = hand_test(cfg, spec, kind, name, **{k: v for k, v in hands.items() if k in ('probe_fn', 'readings_fn', 'start')})
        else:
            handed = 'stopped; the partial file is kept, so sending it again resumes where it stopped'
        with open(log + '.handoff', 'w', encoding='utf8') as f:
            f.write(handed + '\n')
        out.append((log, handed))
    return out
