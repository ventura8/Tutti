"""How many autopilot runs may go at once: adaptive, not a fixed number. The limit is the smallest of
  - accounts: the runs going plus the room the ladder's accounts have now (the planner's tasks);
  - memory: the runs going plus what this machine's free memory holds above `reserve_gb`, `run_gb` per run (an agent run
    is mostly cloud-model calls: its CLI and worktree are light here; its gate goes to a machine with room, below);
  - cpu: the runs going plus the idle cores, `run_cpu` per run;
  - gates: the runs going plus the free gate slots of the fit machines times `gate_share` (a run holds a slot only for its
    gate, a small part of its time);
  - `autopilot.max_parallel`, an optional cap the owner sets in Settings, and `ceiling`.
Re-measured every round, so it rises with free capacity and drops under memory pressure.

A heavy gate (lint, tsc, tests: `heavy`) of a repo whose checks run `local` goes to the routed machines instead
(`--check-where route`), and so does any gate while this machine is short of memory: the laptop never runs ESLint into an
out-of-memory. Pure but for `host()`."""
import os
import re

DEFAULTS = {'run_gb': 0.6, 'run_cpu': 0.25, 'reserve_gb': 4.0, 'gate_share': 3, 'ceiling': 12,
            'heavy': r'eslint|\btsc\b|vitest|jest|pytest|playwright|PIPELINE_STAGE|npm (run )?(lint|test)|\bmypy\b|gradle|'
                     r'dotnet (build|test)'}
UNKNOWN_MEMORY_RUNS = 2
NUMBERS = ('run_gb', 'run_cpu', 'reserve_gb', 'gate_share', 'ceiling')


def settings(cfg):
    v = (cfg.get('autopilot') or {}).get('parallel') or {}
    return {**DEFAULTS, **(v if isinstance(v, dict) else {})}


def errors(v):
    if v is None:
        return []
    if not isinstance(v, dict):
        return [f'autopilot.parallel is {v!r}: a mapping such as {{run_gb: 0.6, reserve_gb: 4}}']
    errs = [f'autopilot.parallel.{k} is {v[k]!r}: a number above 0' for k in NUMBERS
            if k in v and (isinstance(v[k], bool) or not isinstance(v[k], (int, float)) or v[k] <= 0)]
    if 'heavy' in v:
        try:
            re.compile(str(v['heavy']))
        except re.error as e:
            errs.append(f'autopilot.parallel.heavy is not a valid regular expression ({e})')
    return errs + [f'autopilot.parallel.{k} is not a key: use {", ".join(DEFAULTS)}' for k in v if k not in DEFAULTS]


def host(meminfo='/proc/meminfo'):
    """{mem_gb, cores, load} of this machine: free memory (MemAvailable), cores and the 1-minute load. None when unreadable."""
    mem = None
    try:
        with open(meminfo, encoding='utf8') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    mem = int(line.split()[1]) / 1048576
    except (OSError, ValueError, IndexError):
        mem = None
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = None
    return {'mem_gb': mem, 'cores': os.cpu_count() or 1, 'load': load}


def bounds(cfg, running, room, here, gate_slots):
    """{name: (runs at most, why)} of every bound that is known."""
    s = settings(cfg)
    out = {'ceiling': (int(s['ceiling']), f'autopilot.parallel.ceiling {s["ceiling"]:g}')}
    cap = (cfg.get('autopilot') or {}).get('max_parallel')
    if cap:
        out['cap'] = (int(cap), f'max_parallel {cap} (Settings)')
    if room is not None:
        out['accounts'] = (running + max(0, int(room)), f'room in the accounts for {max(0, int(room))} more')
    if here and here.get('mem_gb') is not None:
        more = max(0, int((here['mem_gb'] - s['reserve_gb']) // s['run_gb']))
        out['memory'] = (running + more, f'{here["mem_gb"]:.1f} GB free here, {s["reserve_gb"]:g} GB kept')
    elif not cap:   # free memory unknown (no /proc/meminfo) and no cap set: stay low rather than guess
        out['memory'] = (max(running, UNKNOWN_MEMORY_RUNS), f'free memory unknown here: at most {UNKNOWN_MEMORY_RUNS} runs')
    if here and here.get('load') is not None:
        more = max(0, int((here['cores'] - here['load']) // s['run_cpu']))
        out['cpu'] = (running + more, f'load {here["load"]:.1f} of {here["cores"]} cores')
    if gate_slots is not None:
        out['gates'] = (running + int(gate_slots * s['gate_share']), f'{gate_slots} free gate slot(s) on the machines')
    return out


def limit(cfg, running, room, here, gate_slots):
    """{limit, running, by, why, bounds}: how many runs may go at once now and which bound holds it."""
    b = bounds(cfg, running, room, here, gate_slots)
    by = min(b, key=lambda k: b[k][0])
    n = max(0, b[by][0])
    others = max(0, min(v[0] for k, v in b.items() if k != 'accounts'))
    return {'limit': n, 'others': others, 'running': running, 'by': by, 'why': b[by][1], 'bounds': {k: v[0] for k, v in b.items()}}


def line(p):
    """The dashboard's line: running N of M possible: limited by <reason>."""
    if not p:
        return 'not measured yet'
    return f'running {p["running"]} of {p["limit"]} possible: limited by {p["by"]} ({p["why"]})'


def current(cfg, fallback=2):
    """The limit the last round measured, for the fill and the household (else max_parallel, else `fallback`)."""
    return cfg.get('_parallel_limit') or (cfg.get('autopilot') or {}).get('max_parallel') or fallback


def check_where(cfg, where, check, here):
    """Where a run's gate goes: `route` instead of `local` when it is heavy or this machine is short of memory and there is a
    machine to route to; otherwise as the repo says."""
    if where != 'local' or not check or not any(m.get('transport') != 'local' for m in (cfg.get('machines') or {}).values()):
        return where
    s = settings(cfg)
    short = here and here.get('mem_gb') is not None and here['mem_gb'] < s['reserve_gb']
    return 'route' if short or re.search(s['heavy'], check) else where
