"""Progress for every running job, in layers, each labelled with where its number comes from; never a made-up precise one.

1. exact: `tutti progress n/m` or a counter in the log ([150/177], pytest's [ 13%], "stage 2 of 4", "static 3/51 eslint",
   docker "Step 3/12", rsync's "45%"), or a run waiting in a machine's slot queue;
2. shape: the job's known phases (`dashboard.shapes`, and the built-in one of `tutti run`): phases started out of all, or
   lines counted against a total (a number, or what `total_command` prints);
3. estimate: elapsed time against the median duration of earlier finished jobs of the same kind on the same machine
   (<state>/durations.json), capped at 95 % until it ends; "taking longer than usual" past the median;
4. agents: a conductor's handoff notes (done of done + next), else the estimate from history, else a rough one from
   `dashboard.typical_minutes`; anything else shows its elapsed time with no bar.

Finished jobs get no bar: their facts (jobfacts.py) say what they did."""
import json
import os
import re
import shlex
import statistics
import subprocess
import threading
import time

from . import config, jobfacts

CAP = 95               # an estimate never says more than 95 % before the job ends
MIN_RUNS = 2           # an estimate needs at least this many earlier runs of the kind
KEEP = 30              # finished durations kept per kind and machine
MIN_SECONDS = 5        # shorter jobs are not worth learning from
VANISH_WINDOW = 3600   # a running job seen in the last hour that is gone now counts as finished
FORGET = 86400         # running records not seen for a day are dropped
WRITE_EVERY = 60
LOG_MAX = 1024 * 1024
TOTAL_TTL = 3600
AGENT_KINDS = ('agent', 'agent task', 'conduct')
SERVICE_KINDS = ('tunnel', 'local model')
ACTIVE = ('running', 'stalled')
# A delegated run (`tutti run`) says each phase in its log.
BUILTIN_SHAPES = {
    'tutti run': {'kind': 'run', 'phases': [
        {'name': 'worktree'},
        {'name': 'the worker works', 'when': r'^run: (?:new worktree|reusing the worktree)'},
        {'name': 'checks', 'when': r'^run: checking the change'}]},
}
AGE = re.compile(r'(?:(\d+)|an?|About an?)\s+(second|minute|hour|day|week)s?\s+ago', re.I)
UNIT = {'second': 1, 'minute': 60, 'hour': 3600, 'day': 86400, 'week': 604800}
_lock = threading.Lock()
_logs = {}
_totals = {}
_histories = {}


# ---- shapes


def shapes(cfg):
    return {**BUILTIN_SHAPES, **((cfg.get('dashboard') or {}).get('shapes') or {})}


def shape_for(job, all_shapes):
    """(name, shape, match groups) of the first shape that fits the job, else (None, None, ())."""
    for name, s in all_shapes.items():
        if s.get('kind') and s['kind'] != job.get('kind'):
            continue
        if s.get('match'):
            m = re.search(s['match'], job.get('title') or '')
            if not m:
                continue
            return name, s, tuple(g for g in m.groups() if g)
        if s.get('kind'):
            return name, s, ()
    return None, None, ()


def log_lines(path):
    """The last MB of a log as lines, cached by size and time (phase markers may lie far above the tail)."""
    try:
        st = os.stat(path)
    except (OSError, TypeError):
        return []
    hit = _logs.get(path)
    if hit and hit[0] == (st.st_size, st.st_mtime):
        return hit[1]
    try:
        with open(path, 'rb') as f:
            f.seek(max(0, st.st_size - LOG_MAX))
            lines = [line.replace('\r', '') for line in f.read().decode('utf8', 'replace').splitlines()]
    except OSError:
        return []
    _logs[path] = ((st.st_size, st.st_mtime), lines)
    return lines


def total_of(command, cwd, groups, run=subprocess.run, clock=time.time):
    """The number a shape's `total_command` prints (run by the shell in the job's folder, `{1}` = the title's first group),
    cached for an hour; None when it fails or prints no whole number."""
    for i, g in enumerate(groups, 1):
        command = command.replace('{' + str(i) + '}', shlex.quote(g))
    key = (command, cwd)
    hit = _totals.get(key)
    if hit and clock() - hit[0] < TOTAL_TTL:
        return hit[1]
    try:
        out = run(command, shell=True, cwd=cwd if cwd and os.path.isdir(cwd) else None, capture_output=True, text=True, timeout=10).stdout
        found = int(out.strip().split()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        found = None
    _totals[key] = (clock(), found if found and found > 0 else None)
    return _totals[key][1]


def shape_progress(name, shape, groups, job, lines):
    """Layer 2: phases started out of all, or lines counted against a total; None when the log says nothing yet."""
    phases = shape.get('phases') or []
    if phases:
        at = 0
        for i, p in enumerate(phases):
            if p.get('when') and any(re.search(p['when'], line) for line in lines):
                at = i
        return {'pct': round(100 * at / len(phases)), 'done': at, 'total': len(phases), 'layer': 'shape',
                'label': f'phase {at + 1} of {len(phases)}: {phases[at]["name"]} (the known phases of {name})', 'step': phases[at]['name']}
    if shape.get('count'):
        total = shape.get('total') or (total_of(shape['total_command'], job.get('cwd'), groups) if shape.get('total_command') else None)
        if not total:
            return None
        done = min(sum(1 for line in lines if re.search(shape['count'], line)), total)
        return {'pct': min(99, round(100 * done / total)), 'done': done, 'total': int(total), 'layer': 'shape',
                'label': f'{done} of {total} counted in its log ({name})'}
    return None


# ---- history


class History:
    """Durations of finished jobs per (kind key, machine), and the running jobs being watched to learn them."""

    def __init__(self, path, clock=time.time):
        self.path, self.clock = path, clock
        self.data = self.load()
        self.written = 0.0
        self.dirty = False

    def load(self):
        try:
            with open(self.path, encoding='utf8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {'running': data.get('running') or {}, 'runs': data.get('runs') or {}}
        except (OSError, ValueError):
            pass
        return {'running': {}, 'runs': {}}

    def save(self, force=False):
        if not self.dirty and not force:
            return
        if not force and self.clock() - self.written < WRITE_EVERY:
            return
        tmp = f'{self.path}.{os.getpid()}.tmp'
        try:
            with open(tmp, 'w', encoding='utf8') as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
            self.written, self.dirty = self.clock(), False
        except OSError:
            pass

    def durations(self, key):
        return [r['s'] for r in self.data['runs'].get(key, [])]

    def record(self, key, run_id, seconds_taken):
        if seconds_taken < MIN_SECONDS:
            return
        runs = self.data['runs'].setdefault(key, [])
        if any(r['id'] == run_id for r in runs):
            return
        runs.append({'id': run_id, 's': round(seconds_taken), 'at': round(self.clock())})
        del runs[:-KEEP]
        self.dirty = True

    def observe(self, jobs, now, reachable=None):
        """Learn from what changed since the last look: a finished job's duration (done ones, and running ones that are gone)."""
        seen = set()
        running = self.data['running']
        for j in jobs:
            key, started = j.get('_key'), j.get('_started')
            if not key:
                continue
            seen.add(j['id'])
            if j['status'] in ACTIVE:
                if j['id'] not in running:
                    self.dirty = True
                running[j['id']] = {'key': key, 'started': started or now, 'last': now, 'kind': j.get('kind'),
                                    'machine': j.get('machine')}
            elif j['status'] == 'done':
                end = jobfacts.seconds(j.get('updated')) or now
                if started:
                    self.record(key, j['id'], end - started)
                self.dirty = self.dirty or running.pop(j['id'], None) is not None
            else:
                self.dirty = self.dirty or running.pop(j['id'], None) is not None
        for jid in [i for i in running if i not in seen]:
            r = running.pop(jid)
            self.dirty = True
            gone_while_probed = r.get('kind') != 'remote' or (reachable is not None and r.get('machine') in reachable)
            if now - r['last'] < VANISH_WINDOW and gone_while_probed:
                self.record(r['key'], jid, r['last'] - r['started'])
        for jid in [i for i, r in running.items() if now - r['last'] > FORGET]:
            running.pop(jid)
        self.save()

    def first_seen(self, job_id):
        r = self.data['running'].get(job_id)
        return r['started'] if r else None


def history(cfg):
    path = os.path.join(config.expand(cfg['project']['state']), 'durations.json')
    if path not in _histories:
        _histories[path] = History(path)
    return _histories[path]


def key_of(job, name, groups):
    """What makes two jobs "the same kind": the shape and its match, else the job's kind and program; and the machine."""
    if name:
        what = ' '.join([name, *groups])
    elif job.get('kind') == 'remote':
        parts = (job.get('title') or '').split(' ', 1)[0].split('-')   # <prefix>-<machine>-<track>-<random>: the track varies
        what = 'remote ' + '-'.join(parts[:-2] if len(parts) >= 4 else [re.sub(r'\d+$', '', '-'.join(parts))])
    elif job.get('kind') == 'agent task':
        what = 'agent task (an agent)' if jobfacts.is_transcript(job.get('path')) else 'agent task (a command)'
    else:
        first = re.sub(r'\d+', '#', (job.get('title') or '').split(' ', 1)[0])
        what = f'{job.get("kind")} {first}'.strip()
    return f'{what} @ {job.get("machine") or "local"}'


def started_at(job, hist, now):
    """When the job started: its record, a container's age ("3 minutes ago"), else when Tutti first saw it running."""
    started = jobfacts.seconds(job.get('started'))
    if not started and job.get('kind') == 'agent task' and job.get('path'):
        started = jobfacts.transcript_started(job['path'])
    if started:
        return started
    m = AGE.search(job.get('title') or '')
    if m and now:
        return now - int(m.group(1) or 1) * UNIT[m.group(2).lower()]
    return hist.first_seen(job['id']) or now


# ---- the layers


def eta(pct, elapsed):
    """Seconds left at this pace, when the figure is far enough along to say."""
    return round(elapsed * (100 - pct) / pct) if pct and pct >= 5 and elapsed else None


def exact(job, elapsed):
    p = job.get('progress')
    if not p or p.get('source') == 'finished':
        return None
    if job.get('kind') == 'agent task' and jobfacts.is_transcript(job.get('path')):
        return None   # "[3/4]" inside an agent's transcript is text it read or wrote, not its progress
    where = {'reported': 'reported with tutti progress', 'queue': 'its machine\'s slot queue'}.get(p.get('source'), 'a counter in its log')
    label = f'{p["done"]}/{p["total"]}{" " + p["unit"] if p.get("unit") else ""} ({where})'
    if p.get('source') == 'queue':
        label = f'waiting for a slot: all {p["total"]} are busy ({where})'
    return {**p, 'layer': 'exact', 'label': label, 'eta_seconds': eta(p['pct'], elapsed)}


def estimate(hist, key, elapsed):
    """Layer 3: elapsed against the median of earlier runs, at most CAP %."""
    past = hist.durations(key)
    if len(past) < MIN_RUNS:
        return None
    median = statistics.median(past)
    pct = min(CAP, round(100 * elapsed / median)) if median else CAP
    late = elapsed > median
    label = f'~{pct} % (estimated from {len(past)} earlier runs, usually {minutes(median)})'
    if late:
        label = f'taking longer than usual: {minutes(elapsed)} so far, usually {minutes(median)} ({len(past)} earlier runs)'
    return {'pct': pct, 'layer': 'estimate', 'label': label, 'eta_seconds': None if late else round(median - elapsed), 'late': late}


def handoff(job):
    """A conductor run: its handoff notes, done of done + next."""
    done, nxt = job.get('handoff_done'), job.get('handoff_next')
    if not done and not nxt:
        return None
    total = done + nxt
    return {'pct': round(100 * done / total), 'done': done, 'total': total, 'layer': 'agent',
            'label': f'{done} step(s) done, {nxt} next (its handoff notes)'}


def typical(cfg, job, elapsed):
    """Layer 4's last resort for an agent: a rough estimate from `dashboard.typical_minutes`, said as such."""
    mins = ((cfg.get('dashboard') or {}).get('typical_minutes') or {}).get(job.get('kind'))
    if not mins:
        return None
    pct = min(CAP, round(100 * elapsed / (mins * 60)))
    late = elapsed > mins * 60
    label = f'~{pct} % (rough: no earlier runs yet, a typical {job["kind"]} takes {mins} min)'
    if late:
        label = f'taking longer than a typical {job["kind"]} ({mins} min): {minutes(elapsed)} so far (rough, no earlier runs yet)'
    return {'pct': pct, 'layer': 'agent', 'late': late, 'eta_seconds': None if late else round(mins * 60 - elapsed), 'label': label}


def elapsed_only(job, elapsed):
    if job.get('kind') in SERVICE_KINDS:
        return {'pct': None, 'layer': 'elapsed', 'label': f'up {minutes(elapsed)} (a service: it has no end)'}
    return {'pct': None, 'layer': 'elapsed', 'label': f'{minutes(elapsed)} so far (no earlier run of this kind to estimate from yet)'}


def layered(cfg, job, hist, now, all_shapes):
    """The job's progress dict, from the most exact layer that has something to say."""
    name, shape, groups = shape_for(job, all_shapes)
    if shape and shape.get('machine') and not job.get('machine'):
        job['machine'] = shape['machine']
    job['_key'] = key_of(job, name, groups)
    job['_started'] = started_at(job, hist, now)
    elapsed = max(0, now - job['_started'])
    found = exact(job, elapsed)
    if not found and shape and job.get('log'):
        found = shape_progress(name, shape, groups, job, log_lines(job['log']))
        if found:
            found['eta_seconds'] = eta(found['pct'], elapsed)
    if not found and job.get('kind') == 'conduct':
        found = handoff(job)
    if not found and job.get('kind') not in SERVICE_KINDS:
        found = estimate(hist, job['_key'], elapsed)
    if not found and job.get('kind') in AGENT_KINDS:
        found = typical(cfg, job, elapsed)
    found = found or elapsed_only(job, elapsed)
    found['determinate'] = found.get('pct') is not None
    found['elapsed_seconds'] = round(elapsed)
    return found


def minutes(s):
    s = int(s)
    if s < 90:
        return f'{s} s'
    if s < 5400:
        return f'{round(s / 60)} min'
    return f'{s / 3600:.1f} h'


def annotate(cfg, items, now, worker_account=lambda w: None, machines=None):
    """Every job gets `progress` (running: the layered figure; finished: None) and `facts` (what it did so far or did)."""
    hist = history(cfg)
    folder = config.expand(cfg['project']['state'])
    all_shapes = shapes(cfg)
    with _lock:
        for j in items:
            if j['status'] in ACTIVE:
                j['progress'] = layered(cfg, j, hist, now, all_shapes)
            else:
                name, _, groups = shape_for(j, all_shapes)
                j['_key'], j['_started'] = key_of(j, name, groups), started_at(j, hist, None)
            j['facts'] = jobfacts.facts({**j, 'first_seen': j['_started']}, folder, worker_account, now)
            if j['status'] not in ACTIVE:
                j['progress'] = None
            elif j['progress'].get('step'):
                j['facts']['step'] = j['progress']['step']
        reachable = {m['name'] for m in machines or [] if m.get('reachable')}
        hist.observe(items, now, reachable if machines is not None else None)
        for j in items:
            j.pop('_key', None)
            j.pop('_started', None)
    return items


def conduct_job(cfg, now, alive):
    """The running `tutti conduct` run as a job, with its handoff notes counted (done and next)."""
    path = os.path.join(config.expand(cfg['project']['state']), 'conduct', 'current.json')
    run = jobfacts.read_json(path)
    if not run or run.get('status') != 'running' or not alive(run.get('pid')):
        return None
    done = nxt = 0
    try:
        with open(run.get('progress') or '', encoding='utf8') as f:
            section = None
            for line in f:
                if line.startswith('## '):
                    section = line[3:].strip().lower()
                elif line.startswith('- ') and section in ('done', 'next'):
                    done, nxt = (done + 1, nxt) if section == 'done' else (done, nxt + 1)
    except OSError:
        pass
    agents = run.get('agents') or []
    who = agents[-1].get('agent') if agents and isinstance(agents[-1], dict) else None
    return {'id': f'conduct:{run.get("id")}', 'kind': 'conduct', 'title': f'conduct: {(run.get("goal") or "")[:150]}',
            'pid': run.get('pid'), 'log': None, 'cwd': run.get('cwd'),
            'started': run.get('started'),
            'updated': None, 'idle_seconds': None, 'rc': None, 'status': 'running', 'tail': [], 'progress': None,
            'agent': who, 'handoff_done': done, 'handoff_next': nxt}
