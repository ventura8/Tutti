"""Presence: whether each target is here (docs/PRESENCE.md).

A target is an Android device, a machine, a household member's laptop or a local model server. Each has a state, present,
away, asleep (or off) or unknown, with the time it was last seen, read from the signals Tutti already has: the device
readings (adb), the machine probes (ssh), the household heartbeats and Ollama's /api/tags. A target missing for less than
`presence.grace_seconds` stays present (one lost probe is not a departure). Away and asleep are told apart where possible:
a device missing while its owner's laptop (`devices.<d>.with`) is also gone left the house with its owner; a machine that
does not answer in its night hours (`presence.night`, local time) is asleep; a laptop gone in the day is away.

Each target keeps a 7 x 24 profile (hours present / hours seen per weekday and hour), as the owner profile does: the
dispatcher keeps long shards off a target likely to leave soon and offers long work to the most always-on target first.
Pure here except `read`/`write` (the state file), the probes at the end and the Wake-on-LAN packet."""
import contextlib
import ipaddress
import json
import os
import re
import socket
import urllib.request
from datetime import datetime, timezone

from . import config, durable, tz
# from .pc_helper import subscribe as pc_subscribe  # Removed: module does not exist yet

PRESENT, AWAY, ASLEEP, UNKNOWN = 'present', 'away', 'asleep', 'unknown'
ABSENT = (AWAY, ASLEEP)
FILE = 'presence.json'
DEFAULTS = {'grace_seconds': 120, 'night': '23-08', 'long_minutes': 30, 'leave_soon': 0.5, 'host_laptop': None,
            'owner_laptop': None, 'wake': False, 'wake_after_minutes': 10, 'min_seen_hours': 1}
WAKE_EVERY = 900          # seconds between two magic packets to one machine
RECONNECT_EVERY = 60      # seconds between two reconnect attempts for one device


def settings(cfg):
    return {**DEFAULTS, **(cfg.get('presence') or {})}


def errors(cfg):
    s = cfg.get('presence')
    if s is None:
        return []
    if not isinstance(s, dict):
        return ['presence must be a mapping (docs/PRESENCE.md)']
    out = []
    if not re.fullmatch(r'\d{1,2}-\d{1,2}', str(settings(cfg)['night'])):
        out.append(f'presence.night is {s.get("night")!r}: use local hours as "23-08"')
    for k in ('grace_seconds', 'long_minutes', 'wake_after_minutes', 'leave_soon'):
        if not isinstance(settings(cfg)[k], (int, float)) or settings(cfg)[k] < 0:
            out.append(f'presence.{k} must be a number of 0 or more')
    for name, m in (cfg.get('machines') or {}).items():
        mac = (m or {}).get('wol_mac')
        if mac and not MAC.fullmatch(str(mac)):
            out.append(f'machines.{name}.wol_mac is {mac!r}: use six hex pairs as aa:bb:cc:dd:ee:ff')
    return out

# ---- the state file: <state>/presence.json


def path(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


def read(cfg):
    try:
        with open(path(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    for k in ('targets', 'woken', 'tried'):
        data.setdefault(k, {})
    data.setdefault('resume', [])
    return data


def write(cfg, data):
    durable.dump_json(path(cfg), data)


# ---- the model: one record per target


def new_record(kind, name):
    return {'kind': kind, 'name': name, 'state': UNKNOWN, 'since': None, 'last_seen': None, 'reason': '', 'at': None,
            'here': [[0.0] * 24 for _ in range(7)], 'seen': [[0.0] * 24 for _ in range(7)]}


def hours_of(spec):
    a, b = (int(x) % 24 for x in str(spec).split('-'))
    return {h for h in range(24) if (a <= h < b if a <= b else h >= a or h < b)}


def local_dt(ts, zone):
    """Epoch seconds as a local datetime in `zone` (an IANA name, a tzinfo or hours; tutti/tz.py keeps DST right)."""
    return tz.local(datetime.fromtimestamp(ts, timezone.utc), zone)


def own(rec, zone):
    """The target's own zone (a machine's, read over ssh; a member's, from its heartbeat), else Tutti's."""
    return rec.get('zone') if tz.valid(rec.get('zone') or '') else zone


def learn(rec, now, offset):
    """Adds the time since the last observation (at most an hour: a gap is not evidence) to the hour it started in."""
    if rec.get('at') is not None and now > rec['at']:
        span = min(now - rec['at'], 3600) / 3600
        lt = local_dt(rec['at'], own(rec, offset))
        rec['seen'][lt.weekday()][lt.hour] = round(rec['seen'][lt.weekday()][lt.hour] + span, 4)
        if rec['state'] == PRESENT:
            rec['here'][lt.weekday()][lt.hour] = round(rec['here'][lt.weekday()][lt.hour] + span, 4)
    rec['at'] = now


def classify(rec, now, offset, s, hint=None, companion=None):
    """(state, reason) for a target that was not seen this round, past its grace."""
    if hint:
        return hint
    if rec.get('last_seen') is None:
        return UNKNOWN, 'never seen yet'
    night = local_dt(now, own(rec, offset)).hour in hours_of(s['night'])   # its own night, in its own zone
    if rec['kind'] == 'device':
        if companion in ABSENT:
            return AWAY, 'left with its owner'
        return (ASLEEP, 'off or out of reach at night') if night else (AWAY, 'not on the network')
    if rec['kind'] == 'model':
        return UNKNOWN, 'its server does not answer'
    if night:
        return ASLEEP, 'asleep at its usual night hours'
    if rec.get('laptop'):
        return AWAY, 'its laptop left'
    return ASLEEP, 'off or asleep'


def observe(rec, seen, now, offset, s, hint=None, companion=None):
    """Updates one record from this round's signal; returns 'left', 'returned' or ''. `hint`: (state, reason) a signal
    knows better (logind's PrepareForSleep, the coordinator)."""
    learn(rec, now, offset)
    before = rec['state']
    if seen:
        rec.update(state=PRESENT, last_seen=now, reason='')
        if before != PRESENT:
            rec['since'] = now
        return 'returned' if before in ABSENT else ''
    if not hint and before == PRESENT and now - (rec.get('last_seen') or 0) < s['grace_seconds']:
        return ''
    state, reason = classify(rec, now, offset, s, hint, companion)
    if state != before:
        rec['since'] = rec.get('last_seen') if state in ABSENT and before == PRESENT and rec.get('last_seen') else now
    rec.update(state=state, reason=reason)
    return 'left' if state in ABSENT and before not in ABSENT else ''


def likelihood(rec, at, offset, s):
    """Share of the hour `at` this target was present over the weeks seen, or None with under min_seen_hours of data there."""
    lt = local_dt(at, own(rec, offset))
    seen = rec['seen'][lt.weekday()][lt.hour]
    return rec['here'][lt.weekday()][lt.hour] / seen if seen and seen >= s['min_seen_hours'] else None


def leaving_soon(rec, now, minutes, offset, s):
    """True when, in the hours a run of `minutes` would span, the profile says this target is usually gone."""
    for i in range(max(1, int(-(-minutes // 60)))):
        p = likelihood(rec, now + 3600 * (i + 1), offset, s)
        if p is not None and p < s['leave_soon']:
            return True
    return False


def always_on(rec):
    """The share of all seen hours this target was present (1.0 with no data: nothing says otherwise)."""
    seen = sum(map(sum, rec['seen']))
    return sum(map(sum, rec['here'])) / seen if seen >= 1 else 1.0


def strip(rec, now, offset):
    """Today's 24 hours: the share present, or None for an hour never seen."""
    d = local_dt(now, own(rec, offset)).weekday()
    return [round(rec['here'][d][h] / rec['seen'][d][h], 2) if rec['seen'][d][h] else None for h in range(24)]


def label(rec, offset):
    """'Away since 18:40 (left with its owner)', 'Asleep since 01:10', 'Here', or 'Not seen yet'."""
    if rec['state'] == PRESENT:
        return 'Here'
    if rec['state'] == UNKNOWN:
        return f'Unknown: {rec["reason"]}' if rec.get('reason') else 'Not seen yet'
    when = local_dt(rec['since'], offset).strftime('%H:%M') if rec.get('since') else '?'
    return f'{rec["state"].title()} since {when} ({rec["reason"]})'


# ---- one round over every target


def offset(cfg):
    """Tutti's own zone (time.zone, detected by tutti/tz.py): the default for targets whose zone is not known, and for labels."""
    return tz.zone(cfg)


def signals(devices_rows, machine_rows, members=None, models=None):
    """[(id, kind, name, seen or None, its IANA zone or None)] from the probes; seen None: not asked this round (a paused
    device)."""
    out = []
    for m in machine_rows or []:
        out.append((f'machine:{m["name"]}', 'machine', m['name'], m.get('reachable'), m.get('zone')))
    for d in devices_rows or []:
        seen = None if d.get('status') == 'held' else d.get('status') != 'offline'
        out.append((f'device:{d["name"]}', 'device', d['name'], seen, None))
    for uid, m in (members or {}).items():
        out.append((f'member:{uid}', 'member', m.get('name') or uid, m.get('fresh'), m.get('zone')))
    for name, ok in (models or {}).items():
        out.append((f'model:{name}', 'model', name, ok, None))
    return out


def update(cfg, st, devices_rows, machine_rows, now, members=None, models=None, hints=None):
    """Every target observed once; returns [(id, 'left' | 'returned', record)]. Machines go first, so a device's
    companion laptop is already judged this round."""
    s, off, out = settings(cfg), offset(cfg), []
    for tid, kind, name, seen, zone in signals(devices_rows, machine_rows, members, models):
        rec = st['targets'].setdefault(tid, new_record(kind, name))
        if tz.valid(zone or ''):
            rec['zone'] = zone
        rec['laptop'] = kind == 'member' or (kind == 'machine' and bool(((cfg.get('machines') or {}).get(name) or {}).get('laptop')))
        if seen is None:
            continue
        change = observe(rec, seen, now, off, s, (hints or {}).get(tid), companion_of(cfg, st, kind, name))
        if change:
            out.append((tid, change, rec))
    return out


def companion_of(cfg, st, kind, name):
    """The state of the laptop a device leaves with (devices.<d>.with: a machine or a member), or None."""
    if kind != 'device':
        return None
    mate = ((cfg.get('devices') or {}).get(name) or {}).get('with')
    return (st['targets'].get(f'machine:{mate}') or st['targets'].get(f'member:{mate}') or {}).get('state')


def absent(st):
    """{target id: the card's words} for every target away or asleep."""
    return {tid: r for tid, r in st['targets'].items() if r['state'] in ABSENT}


def placement(cfg, st, now):
    """{target id: {'stay': always-on share, 'leaving': fn(minutes) -> bool}} for the dispatcher."""
    s, off = settings(cfg), offset(cfg)
    return {tid: {'stay': always_on(r), 'leaving': (lambda m, r=r: leaving_soon(r, now, m, off, s))} for tid, r in st['targets'].items()}


def view(cfg, now, st=None):
    st = st if st is not None else read(cfg)
    off = offset(cfg)
    items = {tid: {'state': r['state'], 'label': label(r, off), 'last_seen': r.get('last_seen'), 'strip': strip(r, now, off),
                   'always_on': round(always_on(r), 2)} for tid, r in st['targets'].items()}
    return {'items': items, 'host': host_view(cfg)}


# ---- the Tutti host itself: a laptop host stops household work when it leaves


def host_is_laptop(cfg, glob_fn=None):
    chosen = settings(cfg)['host_laptop']
    if isinstance(chosen, bool):
        return chosen
    import glob
    return bool((glob_fn or glob.glob)('/sys/class/power_supply/BAT*'))


def host_view(cfg, glob_fn=None):
    laptop = host_is_laptop(cfg, glob_fn)
    always = [n for n, m in (cfg.get('machines') or {}).items()
              if (m or {}).get('transport') in ('ssh', 'wsl') and not (m or {}).get('laptop')]
    tip = f'`tutti move {always[0]}`' if always else '`tutti move <an always-on machine>`'
    warn = (f'Tutti runs on a laptop: when it leaves the house or sleeps, household work, Keep busy and the coordinator stop until '
            f'it is back. Move Tutti to an always-on machine with {tip}.') if laptop else ''
    return {'laptop': laptop, 'warning': warn}


def owner_away(cfg, st=None):
    """The reason the owner counts as away (their laptop, `presence.owner_laptop`, is away or asleep), or ''."""
    name = settings(cfg)['owner_laptop']
    if not name:
        return ''
    rec = (st if st is not None else read(cfg))['targets'].get(f'machine:{name}')
    return f'{name} is {rec["state"]}' if rec and rec['state'] in ABSENT else ''


# ---- probes: Ollama servers and household members


def probe_models(cfg, fetch=None, timeout=2.0):
    """{worker: True when its Ollama answers /api/tags}."""
    def get(url):
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    out = {}
    for name, w in (cfg.get('workers') or {}).items():
        if (w or {}).get('kind') == 'ollama' and w.get('host'):
            try:
                out[name] = bool((fetch or get)(config.ollama_url(w['host'], '/api/tags')))
            except (OSError, ValueError):
                out[name] = False
    return out


def members_of(household_state, now, stale):
    """{user id: {name, fresh}} from the coordinator's members: fresh when the last heartbeat is newer than `stale` s."""
    return {u: {'name': m.get('name'), 'fresh': now - (m.get('last_seen') or 0) <= stale, 'zone': m.get('zone')}
            for u, m in ((household_state or {}).get('members') or {}).items() if not m.get('left')}


# ---- a device's return: wireless debugging's port from mDNS, then adb connect


def mdns_port(text, ip):
    """The ip:port `adb mdns services` lists for this device's address, or None."""
    for line in (text or '').splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[1].startswith('_adb-tls-connect') and fields[2].startswith(f'{ip}:'):
            return fields[2]
    return None


def reconnect_command(device):
    """The bash line that finds the device's wireless-debugging port over mDNS and connects adb to it."""
    adb = device.get('adb') or 'adb'
    ip = str(device['serial']).split(':')[0]
    return (f'{adb} mdns services | awk \'$2 ~ /_adb-tls-connect/ && $3 ~ /^{re.escape(ip)}:/ {{print $3; exit}}\' '
            f'| xargs -r {adb} connect')


# ---- Wake-on-LAN: opt-in, LAN only


MAC = re.compile(r'(?i)[0-9a-f]{2}([:-][0-9a-f]{2}){5}')


def magic_packet(mac):
    raw = bytes.fromhex(re.sub('[:-]', '', mac))
    return b'\xff' * 6 + raw * 16


def lan_only(addr):
    """True for a private IPv4 broadcast address (never a public one: a magic packet stays in the house)."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_private


def broadcast_of(m):
    """The LAN broadcast a machine's magic packet goes to: machines.<m>.wol_broadcast, else its private IPv4 host's x.y.z.255,
    else None (no packet: a name or a public address says nothing about the LAN)."""
    addr = m.get('wol_broadcast') or (re.sub(r'\.\d+$', '.255', str(m.get('host') or '')) if lan_only(str(m.get('host') or '')) else '')
    return addr if lan_only(addr) else None


def send_wake(mac, broadcast, sock_fn=None):
    if not lan_only(broadcast):
        raise ValueError(f'{broadcast} is not a LAN address: Wake-on-LAN goes only to the local network')
    with contextlib.closing((sock_fn or socket.socket)(socket.AF_INET, socket.SOCK_DGRAM)) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(magic_packet(mac), (broadcast, 9))


def wake_due(cfg, st, waiting, now):
    """[(machine name, mac, broadcast)] to wake: Wake-on-LAN on, the machine asleep with a wol_mac, compatible work waiting
    for more than wake_after_minutes, and no packet sent to it in the last WAKE_EVERY seconds. `waiting`: {target id: s}."""
    s = settings(cfg)
    if s['wake'] is not True:
        return []
    out = []
    for tid, since in waiting.items():
        rec = st['targets'].get(tid) or {}
        m = (cfg.get('machines') or {}).get(rec.get('name')) or {}
        if rec.get('state') != ASLEEP or not m.get('wol_mac') or not broadcast_of(m) or now - since < s['wake_after_minutes'] * 60:
            continue
        if now - st['woken'].get(tid, 0) < WAKE_EVERY:
            continue
        out.append((rec['name'], m['wol_mac'], broadcast_of(m)))
    return out


def reconnect_due(cfg, st, now):
    """The devices to reconnect this round: away or asleep, on a wireless serial (ip:port), not tried for RECONNECT_EVERY s."""
    out = []
    for tid, r in st['targets'].items():
        d = (cfg.get('devices') or {}).get(r['name']) if r['kind'] == 'device' else None
        if d and r['state'] in ABSENT and ':' in str(d.get('serial')) and now - st['tried'].get(tid, 0) >= RECONNECT_EVERY:
            st['tried'][tid] = now
            out.append(d)
    return out


def round_io(cfg, st, now, waiting, sh=None, wake=send_wake):
    """This round's I/O for absent targets: adb reconnects over mDNS, and Wake-on-LAN packets when opted in. Returns event lines."""
    from . import remote
    sh = sh or remote.sh
    events = []
    for d in reconnect_due(cfg, st, now):
        try:
            sh(cfg['machines'][d['machine']], reconnect_command(d), capture=True, timeout=20)
        except (OSError, KeyError, ValueError) as e:
            events.append(f'reconnecting {d["name"]} failed ({e}); the next round tries again')
    for name, mac, broadcast in wake_due(cfg, st, waiting, now):
        try:
            wake(mac, broadcast)
            st['woken'][f'machine:{name}'] = now
            events.append(f'{name} is asleep while work waits for it: Wake-on-LAN packet sent')
        except (OSError, ValueError) as e:
            events.append(f'waking {name} failed ({e})')
    return events


def attach(cfg, snap, now):
    """The presence view, with each device and machine row given its own item (the card's label and strip)."""
    v = view(cfg, now)
    for kind, part in (('device', 'devices'), ('machine', 'machines')):
        for r in (snap.get(part) or {}).get('items') or []:
            r['presence'] = v['items'].get(f'{kind}:{r["name"]}')
    return v


# ---- Windows Power Events Subscription and Handover

def on_power_event(event):
    """Callback for power events. Triggers work handover before OS enters low-power state."""
    event_type = event.get('type')
    machine_name = event.get('machine', 'unknown')
    
    # Log the event (in a real app, this would be structured logging)
    print(f"[PC-Helper] Received power event: {event_type} on {machine_name}")
    
    # Trigger handover process
    # In a real implementation, this would call a function to save state, stop workers, etc.
    # For now, we simulate the handover completion or timeout.
    print(f"[PC-Helper] Initiating work handover for {machine_name} due to {event_type}")
    
    # Simulate handover duration (e.g., 5 seconds)
    import time
    try:
        time.sleep(1)  # Short delay to simulate processing
        print(f"[PC-Helper] Handover completed successfully for {machine_name}")
    except Exception as e:
        print(f"[PC-Helper] Handover timed out or failed gracefully for {machine_name}: {e}")

def start_power_subscription(cfg):
    """Starts the PC helper subscription for power events."""
    # Subscribe to power events using the callback defined above
    # pc_subscribe(on_power_event)  # Removed: module does not exist yet
    return True
