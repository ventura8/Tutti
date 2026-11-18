"""Before this computer sleeps or shuts down, Tutti hands its running work over (docs/PRESENCE.md).

systemd-logind announces a sleep or a shutdown with PrepareForSleep(true) / PrepareForShutdown(true) and waits for every
delay inhibitor, at most InhibitDelayMaxSec (5 s by default). Tutti holds one such lock while it runs; on the signal it
stops its running jobs as "stop now" does (tutti/handover.py: an agent run's partial work kept on tutti/handoff/<track>),
holds its dispatch leases when execution has not been confirmed stopped, notes the jobs to start again, flushes its state and lets go of the
lock, never asking for more than the OS allows. On wake (PrepareForSleep(false)) or at the next start the noted jobs start
again and the lock is taken again. Linux with systemd only; elsewhere the power-off path (boot ids, tutti/recovery.py) covers it."""
import contextlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from . import autopilot, backlog, config, controls, dispatch, handover, presence, procs, telemetry

SIGNAL = re.compile(r'PrepareFor(Sleep|Shutdown)\s*\((true|false),?\)')
# the lock's child waits on this process (tail --pid): it ends with Tutti, never a `sleep infinity` left behind (2026-11-15)
INHIBIT = ['systemd-inhibit', '--what=sleep:shutdown', '--mode=delay', '--who=tutti',
           '--why=Tutti hands its running work over first', 'tail', '--pid={pid}', '-f', '/dev/null']
HOLD_SECONDS = 30   # how often the lock follows whether work runs
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
    again on wake, holds dispatch leases until their execution is checked and flushes the state. Returns the noted jobs."""
    deadline = now() + budget
    why = f'this computer is going to {"sleep" if what == "sleep" else "shut down"}'
    pst = presence.read(cfg)
    found = {log: pid for kind, name in jobs(cfg) for log, pid in running(config.state_dir(cfg), kind, name)}
    seen = []
    with dispatch.locked(cfg) as st:
        for lid, lease in st['leases'].items():
            if not lease.get('uncertain'):
                lease['uncertain'] = (f'{why}; execution stop is unconfirmed; inspect the execution boundary, then use '
                                      f'tutti dispatch resolve {lid} --confirmed-stopped')
                dispatch.event(st, now(), f'{lid}: held: {lease["uncertain"]}')
    for log, pid in found.items():
        if now() >= deadline:
            break
        seen.append(log)
        note = stop_one(cfg, log, pid, why, end, keep)
        if note:
            pst['resume'].append(note)
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
    if spec.get('kind') == 'test' and not track.startswith('dispatch-'):   # dispatch shards keep their leases
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


def inhibit_argv(pid=None):
    return [a.replace('{pid}', str(pid or os.getpid())) for a in INHIBIT]


class Lock:
    """The one delay lock of this Tutti process: taken only while work runs (busy), let go when it ends or the OS goes down."""

    def __init__(self, popen):
        self.popen, self.proc, self.mu = popen, None, threading.Lock()

    def take(self):
        with self.mu:
            if self.proc is None or (hasattr(self.proc, 'poll') and self.proc.poll() is not None):
                env = {**os.environ, procs.MARK: 'inhibit', 'TUTTI_PROC_OWNER': str(os.getpid())}
                self.proc = self.popen(inhibit_argv(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, env=env)

    def drop(self):
        with self.mu:
            p, self.proc = self.proc, None
        with contextlib.suppress(Exception):
            p and p.terminate()
            p and hasattr(p, 'wait') and p.wait(timeout=5)

    def follow(self, busy):
        self.take() if busy else self.drop()


def busy(cfg, running=controls.running_jobs):
    """True while this host runs a job of the config."""
    return any(running(config.state_dir(cfg), kind, name) for kind, name in jobs(cfg))


def watch(cfg, popen=subprocess.Popen, delay=max_delay, down=going_down, up=woke, is_busy=busy, stop=None):
    """Follows logind's signals until the monitor ends; the delay lock is held only while work runs (a second thread)."""
    lock = Lock(popen)
    stop = stop or threading.Event()
    down_now = threading.Event()   # between a going-down signal and the wake: the lock is never taken again

    def hold():
        while not stop.is_set():
            with contextlib.suppress(Exception):
                down_now.is_set() or lock.follow(is_busy(cfg))
            stop.wait(HOLD_SECONDS)
    with contextlib.suppress(Exception):
        lock.follow(is_busy(cfg))
    threading.Thread(target=hold, name='tutti-sleeplock', daemon=True).start()
    mon = popen(MONITOR, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        for line in mon.stdout:
            sig = parse(line)
            if not sig:
                continue
            what, going = sig
            if going:
                down_now.set()
                with contextlib.suppress(Exception):
                    down(cfg, what, delay())
                lock.drop()   # let go: the OS goes on now
            else:
                down_now.clear()
                with contextlib.suppress(Exception):
                    up(cfg)
                with contextlib.suppress(Exception):
                    lock.follow(is_busy(cfg))
    finally:
        stop.set()
        lock.drop()
        with contextlib.suppress(Exception):
            mon.terminate()


_STARTED = []   # one watcher (and so one lock) per Tutti process, however often the dashboard starts


def start_in_background(cfg):
    """The watcher as a daemon thread of the dashboard: Linux with systemd-inhibit and gdbus, presence.sleep_watch not false."""
    if not sys.platform.startswith('linux') or presence.settings(cfg).get('sleep_watch') is False or not procs.daemons_allowed():
        return None
    if _STARTED:
        return _STARTED[0]
    if not (shutil.which('systemd-inhibit') and shutil.which('gdbus')):
        return None
    t = threading.Thread(target=watch, args=(cfg,), name='tutti-sleepwatch', daemon=True)
    t.start()
    _STARTED.append(t)
    return t
