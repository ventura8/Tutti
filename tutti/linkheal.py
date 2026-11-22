"""Two-sided link repair (owner, 2026-11-22: "Tutti should retry to fix the connection on both sides"; docs/NETWORK.md,
*Link repair*). This is the side that reaches hosts; the member side (coordinator.Client) falls back to the coordinator's
other addresses by itself.

Each round, for every host whose probe did not answer: find why (its name does not resolve, its ssh port is closed, ssh
refuses the key, or nothing answers in time), repair what Tutti can (drop a stale shared-connection socket and the cached
reach answer, use the LAN or VPN address when the other does not answer, wake it with Wake-on-LAN when `mac:` is set), and
try again with backoff (1, 2, 5, 10, then every 30 minutes). The host's card says "reconnecting: try 3, next at 21:40 (its
ssh port is closed)"; the round it answers again says "reconnected after 3 tries". A cause Tutti cannot fix (keys refused)
says what the person does. Pure: `cause`, `next_wait`, `step`; `diagnose` and `repair` do the I/O."""
import os
import socket
import subprocess
import time

BACKOFF = (60, 120, 300, 600, 1800)
FILE = 'links.json'
WHY = {
    'dns': ('its name does not resolve', 'check the host name in its config entry (or ~/.ssh/config), or that it is on'),
    'port': ('its ssh port is closed', 'start ssh there (Linux: sudo systemctl enable --now ssh; WSL: sudo service ssh start; '
                                      'macOS: Remote Login)'),
    'auth': ('ssh refuses this computer\'s key', 'run `tutti machines add --host <address>` again, or ssh-copy-id to it'),
    'timeout': ('nothing answers in time', 'it may be asleep or off the network: power it on (Tutti wakes it when `mac:` is set)'),
}


def cause(resolved, port_open, ssh_rc, ssh_err):
    """One of dns, port, auth, timeout, or None when the link works."""
    if not resolved:
        return 'dns'
    if not port_open:
        return 'port' if port_open is False else 'timeout'
    if ssh_rc == 0:
        return None
    err = (ssh_err or '').lower()
    if 'permission denied' in err or 'publickey' in err or 'host key verification failed' in err:
        return 'auth'
    return 'timeout'


def next_wait(tries):
    return BACKOFF[min(max(tries, 1), len(BACKOFF)) - 1]


def step(entry, up, now):
    """(entry, event or None): the per-host record after a probe; `up` is whether the host answered."""
    entry = dict(entry or {})
    if up:
        event = (f'reconnected after {entry["tries"]} tr{"y" if entry["tries"] == 1 else "ies"}'
                 if entry.get('tries') else None)
        return {'up': True, 'since': now if not entry.get('up') else entry.get('since', now)}, event
    if entry.get('up', True):
        entry = {'up': False, 'since': now, 'tries': 0, 'next': now}
    return entry, None


def due(entry, now):
    return not entry.get('up', True) and now >= float(entry.get('next') or 0)


def diagnose(machine, resolve=socket.getaddrinfo, connect=socket.create_connection, run=subprocess.run, target=None):
    """(cause, why): the link's first broken step; (None, '') when it works."""
    from . import remote
    host, port = (target or remote.ssh_target)(machine['host'])
    try:
        resolve(host, port)
        resolved = True
    except OSError:
        resolved = False
    port_open = None
    if resolved:
        try:
            connect((host, port), 5).close()
            port_open = True
        except ConnectionRefusedError:
            port_open = False
        except OSError:
            port_open = None   # no answer in time
    rc, err = 255, ''
    if port_open:
        try:
            r = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '--', remote.dest(machine), 'true'],
                    capture_output=True, text=True, timeout=20)
            rc, err = r.returncode, r.stderr
        except (OSError, subprocess.SubprocessError) as e:
            err = str(e)
    c = cause(resolved, port_open, rc, err)
    return c, (WHY[c][0] if c else '')


def wake(mac, send=None):
    """A Wake-on-LAN magic packet to `mac` on the LAN broadcast (harmless to a computer that is already on)."""
    raw = bytes.fromhex(mac.replace(':', '').replace('-', ''))
    if len(raw) != 6:
        raise ValueError(f'mac {mac!r} is not 6 bytes')
    packet = b'\xff' * 6 + raw * 16
    if send:
        return send(packet)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, ('255.255.255.255', 9))
    return True


def repair(machine, c, send=None):
    """[what was done] for one try: the stale shared connection and the cached reach answer dropped (always), the host
    woken when it answers nothing and `mac:` is set."""
    from . import remote
    done = []
    p = remote.mux_path(machine)
    if os.path.exists(p) and not remote.live_socket(p):
        done.append('dropped a stale shared connection')
    remote.REACH_CACHE.pop(machine['host'], None)
    if machine.get('lan'):
        remote.REACH_CACHE.pop(machine['lan'], None)
        done.append('will try its LAN address too')
    if c in ('timeout', 'port') and machine.get('mac'):
        try:
            wake(machine['mac'], send)
            done.append('sent Wake-on-LAN')
        except (OSError, ValueError) as e:
            done.append(f'Wake-on-LAN failed ({e})')
    return done


def round_(cfg, machines_state, now=None, diagnose_fn=diagnose, repair_fn=repair):
    """One round over the probed hosts: (records, events). Records go to <state>/links.json and the cards."""
    from . import config
    now = now or time.time()
    path = os.path.join(config.state_dir(cfg), FILE)
    records, events = _read(path), []
    by_name = {m.get('name'): m for m in machines_state or []}
    for name, machine in (cfg.get('machines') or {}).items():
        probed = by_name.get(name)
        if probed is None or machine.get('host') in ('local', None) or probed.get('status') in ('paused', 'disabled'):
            continue   # this computer, or a host paused on purpose: nothing to repair
        up = bool(probed.get('reachable'))
        entry, event = step(records.get(name), up, now)
        if event:
            events.append(f'{name}: {event}')
        if due(entry, now):
            c, why = diagnose_fn({**machine, 'name': name})
            if c is None:
                entry.update(next=now + next_wait(1))   # the link works now; the probe will say so next round
            else:
                done = repair_fn({**machine, 'name': name}, c)
                entry['tries'] = int(entry.get('tries') or 0) + 1
                entry.update(cause=c, why=why, fix=WHY[c][1], done=done, next=now + next_wait(entry['tries']))
                events.append(f'{name}: not reachable ({why}); try {entry["tries"]}: {", ".join(done) or "nothing to repair here"}; '
                              f'next try at {time.strftime("%H:%M", time.localtime(entry["next"]))}')
        records[name] = entry
    _write(path, records)
    return records, events


def note(entry):
    """The host card's line for its link, or ''."""
    if not entry or entry.get('up', True):
        return ''
    nxt = time.strftime('%H:%M', time.localtime(float(entry.get('next') or 0)))
    tries = int(entry.get('tries') or 0)
    why = entry.get('why') or 'not answering'
    return f'reconnecting: try {tries}, next at {nxt} ({why}); {entry.get("fix") or ""}'.rstrip('; ')


def _read(path):
    import json
    try:
        with open(path, encoding='utf8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data):
    import json
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + '.tmp', 'w', encoding='utf8') as f:
            json.dump(data, f)
        os.replace(path + '.tmp', path)
    except OSError:
        pass
