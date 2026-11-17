"""Before this computer sleeps or shuts down, Tutti hands its running work over (docs/PRESENCE.md).

systemd-logind announces a sleep or a shutdown with PrepareForSleep(true) / PrepareForShutdown(true) and waits for every
delay inhibitor, at most InhibitDelayMaxSec (5 s by default). Tutti holds one such lock while it runs; on the signal it
stops its running jobs as "stop now" does (tutti/handover.py: an agent run's partial work kept on tutti/handoff/<track>),
requeues its dispatch leases with no attempt counted, notes the jobs to start again, flushes its state and lets go of the
lock, never asking for more than the OS allows. On wake (PrepareForSleep(false)) or at the next start the noted jobs start
again and the lock is taken again. Linux with systemd only; elsewhere the power-off path (boot ids, tutti/recovery.py) covers it."""
import contextlib
import re
import shutil
import subprocess
import sys
import threading
import time

from . import autopilot, backlog, config, controls, dispatch, handover, presence, telemetry

SIGNAL = re.compile(r'PrepareFor(Sleep|Shutdown)\s*\((true|false),?\)')
INHIBIT = ['systemd-inhibit', '--what=sleep:shutdown', '--mode=delay', '--who=tutti',
           '--why=Tutti hands its running work over first', 'sleep', 'infinity']
MONITOR = ['gdbus', 'monitor', '--system', '--dest', 'org.freedesktop.login1', '--object-path', '/org/freedesktop/login1']
DELAY = ['busctl', 'get-property', 'org.freedesktop.login1', '/org/freedesktop/login1', 'org.freedesktop.login1.Manager',
         'InhibitDelayMaxUSec']
HOST = 'host:this'


def parse(line):
    """('sleep' | 'shutdown', going down: bool) from a gdbus monitor line, or None."""
    m = SIGNAL.search(line or '')
    return (m.group(1).lower(), m.group(2) == 'true') if m else None


def max_delay(run=subprocess.run):
    """The seconds logind waits for a delay lock (InhibitDelayMaxUSec), 5 when unreadable; Tutti keeps a second in hand."""
    try:
        out = run(DELAY, capture_output=True, text=True, timeout=2).stdout
        usec = int(out.split()[-1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        usec = 5_000_000
    return max(0.5, usec / 1e6 - 1.0)


def jobs(cfg):
    """[(kind, name)] whose running jobs a sleep stops: every machine, device and worker in the config."""
    return [(k, n) for k, sec in (('machine', 'machines'), ('device', 'devices'), ('worker', 'workers')) for n in (cfg.get(sec) or {})]


def going_down(cfg, what, budget, now=time.time, running=controls.running_jobs, end=handover.end_tree, keep=handover.keep_work):
    """Stops this host's running jobs within `budget` seconds, keeps an agent run's partial work, notes each job to start
    again on wake, requeues the dispatch leases (not counted as failures) and flushes the state. Returns the noted jobs."""
    deadline = now() + budget
    why = f'this computer is going to {"sleep" if what == "sleep" else "shut down"}'
    pst = presence.read(cfg)
    found = {log: pid for kind, name in jobs(cfg) for log, pid in running(config.state_dir(cfg), kind, name)}
    seen = []
    for log, pid in found.items():
        if now() >= deadline:
            break
        seen.append(log)
        note = stop_one(cfg, log, pid, why, end, keep)
        if note:
            pst['resume'].append(note)
    with dispatch.locked(cfg) as st:
        leases, st['leases'] = st['leases'], {}
        for lease in leases.values():
            dispatch.stop_pid(lease['pid'])
            dispatch.requeue(st, lease, f'stopped because {why}: queued again, not counted as a failure', now())
    with backlog.locked(cfg) as data:
        for t in [t for t in data['tasks'] if t['status'] == 'running']:
            autopilot.pause(t, why)
    rec = pst['targets'].setdefault(HOST, presence.new_record('host', 'this computer'))
    rec.update(state=presence.ASLEEP, since=now(), reason=why)
    presence.write(cfg, pst)
    telemetry.record('feature', 'presence.sleep', what=what, jobs=len(seen))
    return pst['resume']


def stop_one(cfg, log, pid, why, end, keep):
    """One running job stopped: its result written (143), an agent run's partial work kept. Returns what to start again."""
    spec = handover.read_spec(log)
    end(pid)
    with open(log + '.rc', 'w', encoding='utf8') as f:
        f.write('143\n')
    with open(log + '.handoff', 'w', encoding='utf8') as f:
        f.write(f'stopped because {why}; it starts again when the computer is back\n')
    track = str(spec.get('track') or spec.get('run_id') or '')
    if spec.get('kind') == 'run':
        kept = keep(handover.worktree_of(cfg, track), track, str(spec.get('run_id') or '?'))
        # an autopilot attempt is queued again by the autopilot itself (paused below), never started a second way
        return None if track.startswith(autopilot.TRACK_PREFIX) else {'kind': 'run', 'spec': spec, 'why': why, 'kept': kept}
    if spec.get('kind') == 'test' and not track.startswith('dispatch-'):   # a shard: its lease is requeued below
        return {'kind': 'test', 'args': ['test', *(spec.get('args') or [])], 'track': spec.get('track', 'run')}
    return None


def woke(cfg, now=time.time, start=handover.spawn):
    """Starts the jobs a sleep stopped (an agent run continues in its worktree from its handoff) and marks this host present."""
    pst = presence.read(cfg)
    todo, pst['resume'] = pst['resume'], []
    rec = pst['targets'].setdefault(HOST, presence.new_record('host', 'this computer'))
    rec.update(state=presence.PRESENT, since=now(), last_seen=now(), reason='')
    presence.write(cfg, pst)   # written first: a crash below never starts the same job twice
    out = []
    for j in todo:
        if j['kind'] == 'run':
            out.append(_resume_run(cfg, j['spec'], j, start))
        else:
            out.append(start(cfg, j['args'], f'test-{j.get("track", "run")}'))
    return out


def _resume_run(cfg, spec, j, start):
    track = str(spec.get('track') or spec.get('run_id') or '')
    task = f'{spec.get("task", "")}\n\nThe last run stopped because {j["why"]}; {j.get("kept", "")}. Continue from the worktree.'
    args = ['run', spec.get('worker', ''), task, *[str(f) for f in spec.get('files') or []], '--repo', spec.get('repo', ''),
            '--track', track, '--bg', *handover.check_args(spec.get('checks'))]
    return start(cfg, args, f'run-{track}')


def _inhibit(popen):
    """A fresh delay lock process, or None when spawn fails (presence still follows the next edge)."""
    with contextlib.suppress(Exception):
        return popen(INHIBIT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return None


def _drop(lock):
    """Ends the delay lock so the OS can sleep; returns None."""
    with contextlib.suppress(Exception):
        lock and lock.terminate()
    return None


def _edge(cfg, what, going, asleep, lock, popen, delay, down, up):
    """One PrepareForSleep/Shutdown edge: sleep → hand over and drop the lock; wake → resume and take it again.
    Duplicate signals (same side as the latch) are ignored so rapid cycles never double-handover or leave a stale host."""
    if going == asleep:
        return asleep, lock
    if going:
        with contextlib.suppress(Exception):
            down(cfg, what, delay())
        return True, _drop(lock)
    with contextlib.suppress(Exception):
        up(cfg)
    return False, lock or _inhibit(popen)


def watch(cfg, popen=subprocess.Popen, delay=max_delay, down=going_down, up=woke):
    """Holds the delay lock and follows logind's signals until the monitor ends."""
    lock, asleep = _inhibit(popen), False
    mon = popen(MONITOR, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        for line in mon.stdout:
            sig = parse(line)
            if not sig:
                continue
            asleep, lock = _edge(cfg, sig[0], sig[1], asleep, lock, popen, delay, down, up)
    finally:
        for p in (lock, mon):
            with contextlib.suppress(Exception):
                p and p.terminate()


def start_in_background(cfg):
    """The watcher as a daemon thread of the dashboard: Linux with systemd-inhibit and gdbus, presence.sleep_watch not false."""
    if not sys.platform.startswith('linux') or presence.settings(cfg).get('sleep_watch') is False:
        return None
    if not (shutil.which('systemd-inhibit') and shutil.which('gdbus')):
        return None
    t = threading.Thread(target=watch, args=(cfg,), name='tutti-sleepwatch', daemon=True)
    t.start()
    return t
