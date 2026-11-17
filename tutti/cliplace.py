"""Where a CLI worker's run happens (docs/WORKERS.md, *On another machine*). `workers.<w>.machines: [nuc, laptop]` (or
`machine: nuc`) says the CLI is installed and signed in on those machines; the autopilot sends each run to the one that fits
best now, from that machine's own share, heat, load and free memory, a machine nobody is using before one in use, and another
machine before the main (this computer, transport local). A worker without the key runs here, as before.

Each machine's room is its own: the runs it may take more = the smaller of its idle cores at its share (`run_cpu` a run) and its
free memory above `reserve_gb` (`run_gb` a run), the same numbers as tutti/parallel.py. So the main being in use (its share 0)
no longer stops the runs that can go to the other machines. Pure."""
from . import parallel, share

RUNS_ON = ('local', 'ssh', 'wsl')   # a phone (android) or a member's machine (household) never runs a CLI worker for us


def main_name(cfg):
    """This computer's machine name (transport local), or ''."""
    return next((n for n, m in (cfg.get('machines') or {}).items() if m.get('transport') == 'local'), '')


def machines_of(cfg, worker):
    """The machines a CLI worker may run on, in the order given; [] when it names none (it runs here)."""
    w = (cfg.get('workers') or {}).get(worker) or {}
    if w.get('kind') != 'cli':
        return []
    names = w.get('machines') or ([w['machine']] if w.get('machine') else [])
    return [n for n in names if n in (cfg.get('machines') or {})] if isinstance(names, list) else []


def on(cfg):
    """True when some CLI worker names machines: only then does the autopilot place runs per machine."""
    return any(machines_of(cfg, n) for n in cfg.get('workers') or {})


def errors(cfg):
    """workers.<w>.machines: a list of names under machines:, each one a computer that can run a CLI (local, ssh or wsl)."""
    errs = []
    for name, w in (cfg.get('workers') or {}).items():
        v = (w or {}).get('machines')
        if v is None:
            continue
        key = f'workers.{name}.machines'
        if (w or {}).get('kind') != 'cli':
            errs.append(f'{key}: only a cli worker runs on other machines (a local model\'s machine is workers.{name}.machine)')
            continue
        if not isinstance(v, list) or not v or not all(isinstance(n, str) for n in v):
            errs.append(f'{key} is {v!r}: a list of machine names where the CLI is installed and signed in, e.g. [nuc, laptop]')
            continue
        for n in v:
            m = (cfg.get('machines') or {}).get(n)
            if m is None:
                errs.append(f'{key} names "{n}", which is not under machines:')
            elif m.get('transport') not in RUNS_ON:
                errs.append(f'{key} names "{n}", a {m.get("transport")} machine: a CLI worker runs only on a local, ssh or wsl machine')
    return errs


def in_use(row):
    """True when someone (a person, a VNC viewer, an interactive agent) uses the machine, from its dashboard row."""
    return bool((row or {}).get('in_use'))


DOWN = ('offline', 'held', 'off', 'paused')


def room(cfg, row, here=None):
    """(runs that may start there now, why none). `row`: the machine's dashboard row (state.machine_state); `here`: this
    computer's measure at its share (autopilot.local_share), used for the main instead of its row's numbers."""
    if not row:
        return 0, 'not probed yet'
    name = row.get('name', '?')
    if row.get('status') in DOWN:
        return 0, f'{name}: {row["status"]} ({row.get("reason") or "no reason given"})'
    pct = row.get('use_pct')
    if pct is not None and pct <= 0:
        return 0, f'{name}: its share for Tutti is 0 %' + (f' ({row["use_why"]})' if row.get('use_why') else '')
    s = parallel.settings(cfg)
    found = []
    cores = (here or {}).get('cores') if here else share.scaled(row.get('cores') or 0, 100 if pct is None else pct)
    load = (here or {}).get('load') if here else row.get('load')
    if cores and load is not None:
        found.append((max(0, int((cores - load) // s['run_cpu'])), f'{name}: load {load:.1f} of {cores} cores at its share'))
    mem = (here or {}).get('mem_gb') if here else row.get('mem_free_gb')
    if mem is not None:
        found.append((max(0, int((mem - s['reserve_gb']) // s['run_gb'])), f'{name}: {mem:.1f} GB free, {s["reserve_gb"]:g} GB kept'))
    if not found:
        return parallel.UNKNOWN_MEMORY_RUNS, ''
    n, why = min(found)
    return n, ('' if n else why)


def rooms(cfg, rows, here=None):
    """{machine: (runs more, why none)} of every machine a CLI worker runs on, the main included (it runs the others)."""
    names = dict.fromkeys([main_name(cfg), *[m for w in cfg.get('workers') or {} for m in machines_of(cfg, w)]])
    by = {r.get('name'): r for r in rows or ()}
    main = main_name(cfg)
    return {n: room(cfg, by.get(n), here if n == main and here else None) for n in names if n}


def elsewhere(cfg, places):
    """The runs the machines other than the main may take now: added to the adaptive limit's cpu and memory bounds, which
    measure only this computer."""
    main = main_name(cfg)
    return sum(max(0, n) for m, (n, _) in places.items() if m != main)


def pick(cfg, worker, places, rows):
    """(the machine for a run of `worker`, why none): of its machines (else the main) one with room, a machine nobody uses
    first, then one that is not the main, then the most room. None when none has room."""
    main = main_name(cfg)
    names = machines_of(cfg, worker) or [main]
    by = {r.get('name'): r for r in rows or ()}
    fit = [n for n in names if (places.get(n) or (0, ''))[0] > 0]
    if not fit:
        return None, '; '.join((places.get(n) or (0, f'{n}: not measured'))[1] or f'{n}: no room' for n in names)
    return min(fit, key=lambda n: (in_use(by.get(n)), n == main, -places[n][0], names.index(n))), ''


def take(places, machine):
    """One run placed on `machine`: one less room there."""
    n, why = places.get(machine) or (0, '')
    places[machine] = (max(0, n - 1), why or f'{machine}: its room is taken by the runs placed this round')
