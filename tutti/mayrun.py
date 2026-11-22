"""`tutti may-run <machine>...`: the one check every script, agent and the MCP makes before it uses a machine (docs/FLEET.md,
*May it run*).

A machine may take a heavy run when it is not paused (by its owner, the heat guard or blocked hours), not at or above its
heat.stop_c (or capped at heat.cap_c), outside its limited hours, reachable, without its yield flag, under its load ratio
and with a free slot. A script that goes straight to `ssh <machine>` skips Tutti's routing, so it asks here first; on a
refusal it tries the next machine it named or waits (`--wait`).

A slot lease (`--lease <minutes>`) holds one of the machine's slots for the caller between the check and the start (and
for the run, until `--release <id>`): live leases of other callers count against `slots`, so two scripts that ask at the
same moment do not both take the last slot. Leases live in <project.state>/slot-leases.json and lapse at their end.

Pure core: `verdict`, `pick`, `claim`, `release`; the probe and the file are the thin edge (`run`).
"""
import contextlib
import json
import os
import secrets
import time

from . import config, durable, routing

try:
    import fcntl
except ImportError:   # Windows: one dashboard per state folder, no concurrent writers
    fcntl = None

LEASES = 'slot-leases.json'
DEFAULT_LEASE_MINUTES = 120
WAIT_POLL_S = 60
REFUSED = 3   # the exit code of a refusal: 2 stays a usage error


class Refused(Exception):
    """No machine named may run now; the message has one line per machine with its reason."""


def leased_by_others(leases, name, who, now):
    """The live slot leases of `name` that another caller holds."""
    return [v for v in leases.values() if v.get('machine') == name and v.get('who') != who and v.get('until', 0) > now]


def verdict(machine, probe, leases, who, now, now_dt=None):
    """Why `machine` may not take a heavy run now, or '' when it may. `probe` is a routing.Probe, `leases` the slot leases."""
    why = routing.unfit(machine, probe, now=now_dt, heavy=True)
    if why:
        return why
    others = leased_by_others(leases, machine['name'], who, now)
    slots = int(machine.get('slots') or 1)
    if len(others) >= slots:
        return f'all {slots} slots are leased ({", ".join(sorted(v["who"] for v in others))}): wait, or ask them to release'
    return ''


def pick(machines, names, probe_fn, leases, who, now, now_dt=None):
    """(the first named machine that may run, its notes) or raises Refused with each machine's reason, in order."""
    notes = []
    for name in names:
        m = machines.get(name)
        if m is None:
            notes.append(f'{name}: not a machine in this config (`tutti machines` lists them)')
            continue
        why = routing.owner_unfit(m) or verdict(m, probe_fn(name), leases, who, now, now_dt)   # paused: never probed
        if not why:
            return name, notes
        notes.append(f'{name}: {why}')
    raise Refused('\n'.join(notes))


def claim(leases, name, who, minutes, now, token=None):
    """Records a slot lease of `name` for `who`; returns its id. Expired leases are dropped."""
    if not who:
        raise ValueError('a slot lease needs --who <caller>: the script or session that uses it (e.g. ship.sh)')
    if minutes < 1:
        raise ValueError(f'--lease {minutes}: a slot lease lasts at least 1 minute')
    for k in [k for k, v in leases.items() if v.get('until', 0) <= now]:
        leases.pop(k)
    lid = token or secrets.token_hex(6)
    leases[lid] = {'machine': name, 'who': who, 'at': now, 'until': now + minutes * 60}
    return lid


def release(leases, lid):
    """Drops lease `lid`; True when it was there."""
    return leases.pop(lid, None) is not None


# ---- the thin edge: the lease file and the probe


def leases_path(cfg):
    return os.path.join(config.state_dir(cfg), LEASES)


def read(cfg):
    if not (cfg.get('project') or {}).get('state'):
        return {}
    try:
        with open(leases_path(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data.get('leases') if isinstance(data.get('leases'), dict) else {}


@contextlib.contextmanager
def locked(cfg):
    p = leases_path(cfg)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + '.lock', 'w', encoding='utf8') as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        data = read(cfg)
        yield data
        durable.dump_json(p, {'leases': data}, indent=1)


def live_probe(cfg, name):
    """The probe routing uses for a heavy run, at the machine's limited share."""
    from datetime import datetime, timezone

    from . import remote, share
    m = cfg['machines'][name]
    return share.probe_at_share(m, remote.probe(m), datetime.now(timezone.utc))


def check(cfg, names, who='', lease_minutes=0, probe_fn=None, clock=time.time):
    """(machine, lease id or '') for the first fit machine of `names`; raises Refused. With `lease_minutes`, a slot lease is
    taken under the file lock, so the check and the claim are one step."""
    from datetime import datetime, timezone
    probe_fn = probe_fn or (lambda n: live_probe(cfg, n))
    now = clock()
    now_dt = datetime.fromtimestamp(now, timezone.utc)
    if not lease_minutes:
        return pick(cfg['machines'], names, probe_fn, read(cfg), who, now, now_dt)[0], ''
    with locked(cfg) as leases:
        name, _ = pick(cfg['machines'], names, probe_fn, leases, who, now, now_dt)
        return name, claim(leases, name, who, lease_minutes, now)


def refusal(cfg, name, probe_fn=None):
    """Why `name` may not take a heavy run now ('' when it may): what `tutti test --on`, a device's machine and the
    Windows-tree path ask before they start."""
    try:
        check(cfg, [name], probe_fn=probe_fn)
    except Refused as e:
        return str(e).split(': ', 1)[-1]
    return ''


def run(cfg, names, who, lease_minutes, wait_minutes, say, sleep=time.sleep, clock=time.time, probe_fn=None):
    """`tutti may-run`: prints `<machine>` (and ` <lease id>`) and returns 0, or says each reason and returns REFUSED.
    With `wait_minutes`, asks again every minute until one is fit or the time is up."""
    deadline = clock() + max(0, wait_minutes) * 60
    while True:
        try:
            name, lid = check(cfg, names, who, lease_minutes, probe_fn, clock)
        except Refused as e:
            if clock() + WAIT_POLL_S > deadline:
                for line in str(e).splitlines():
                    say(f'may-run: refused: {line}')
                say('may-run: fix: wait until it cools or is resumed (`tutti machines`), or name another machine')
                return REFUSED
            sleep(WAIT_POLL_S)
            continue
        print(f'{name} {lid}' if lid else name)
        return 0
