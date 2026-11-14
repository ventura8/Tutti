"""How much of the household's capacity Tutti uses (docs/FLEET.md, *Household use*). At each dashboard probe every machine's
capacity (its slots at its share, plus one for a local model server) and what is busy (slots taken, a local model running)
is sampled; the dashboard shows each machine's use over the last hour and day, why it is idle when it is, and the headline
"Household use: X % of available capacity". A paused, offline or yielding machine has no available capacity.
Pure: sample, summarise, view's arithmetic; the I/O: record (one JSON file in the state folder)."""
import json
import os

from . import config

FILE = 'household-use.json'
KEEP = 26 * 3600
UNAVAILABLE = ('held', 'off', 'offline', 'paused')


def sample(machine_row, models, model_busy):
    """[available capacity, busy] of one machine now: its slots at its share for Tutti, plus one for a model server; busy:
    the slots taken (by anyone) and a model run going."""
    from . import share
    if machine_row.get('status') in UNAVAILABLE or machine_row.get('free') is None:
        return [0, 0]
    slots = int(machine_row.get('slots') or 0)
    cap = share.scaled(slots, machine_row.get('use_pct', 100)) + (1 if models else 0)
    busy = int(machine_row.get('busy') or 0) + (1 if model_busy else 0)
    return [max(cap, busy), busy]


def summarise(points, since):
    """(use %, available unit-seconds) over the samples after `since`: busy over available, each weighted by its interval."""
    pts = [p for p in points if p[0] >= since]
    avail = busy = 0.0
    for a, b in zip(pts, pts[1:], strict=False):
        dt = min(b[0] - a[0], 600)
        avail += a[1] * dt
        busy += min(a[2], a[1]) * dt
    return (round(100 * busy / avail) if avail else None), avail


def path(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


def load(cfg):
    try:
        with open(path(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(cfg, machines, now, busy_fn=None):
    """One sample per machine: [time, available, busy, why idle]; samples older than KEEP dropped."""
    from . import energy, fleet
    live = (busy_fn or energy.in_flight)(cfg)
    st = fleet.read_state(cfg).get('machines') or {}
    data = load(cfg)
    for m in machines or ():
        name = m.get('name')
        workers = [n for n in fleet.local_names(cfg) if fleet.machine_of(cfg, n) == name]
        avail, busy = sample(m, (st.get(name) or {}).get('models'), any(w in live for w in workers))
        why = '' if busy >= avail else (m.get('reason') or '')
        data[name] = [p for p in data.get(name, []) if p[0] >= now - KEEP] + [[now, avail, busy, why]]
    tmp = f'{path(cfg)}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(data, f)
    os.replace(tmp, path(cfg))
    return data


def view(cfg, now, data=None):
    """{headline, pct, machines: [{name, hour, day, idle_why}]}: the last hour's use of the household and of each machine."""
    data = load(cfg) if data is None else data
    rows, tot_avail, tot_busy = [], 0.0, 0.0
    for name, pts in sorted(data.items()):
        hour, avail = summarise(pts, now - 3600)
        day, _ = summarise(pts, now - 86400)
        if hour is not None:
            tot_avail += avail
            tot_busy += avail * hour / 100
        last = pts[-1] if pts else None
        idle = last and last[2] < last[1]
        rows.append({'name': name, 'hour': hour, 'day': day,
                     'idle_why': (last[3] or 'free: nothing compatible waits for it') if idle else ''})
    pct = round(100 * tot_busy / tot_avail) if tot_avail else None
    head = f'Household use: {pct} % of available capacity (last hour)' if pct is not None else \
        'Household use: not measured yet (the first hour of probes)'
    return {'headline': head, 'pct': pct, 'machines': rows}
