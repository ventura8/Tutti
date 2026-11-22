"""The Discovered tab (t220, docs/DASHBOARD.md, *Discovered*): every device or computer Tutti finds on the network that is
not set up yet, what it seems to be, the roles it has now and the roles it could take.

Sources: the media scan (tutti/lanmedia.py: mDNS, SSDP/DLNA, AirPlay, Cast), the neighbour table (`ip neigh`), `adb devices`
and an ssh port check of the neighbours. Pure parsers and `view`; `gather` does the I/O, each source on its own so one that
fails leaves the others. A machine or device the config already has is left out (matched by address or serial)."""
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor

API = '/api/discovered'
MAX_PROBED, SSH_TIMEOUT = 64, 0.4
LIVE = ('REACHABLE', 'STALE', 'DELAY', 'PROBE', 'PERMANENT')
CAN = {
    'computer': ['machine: runs work (`tutti connect <address>` sets it up)'],
    'android': ['device: runs tests on it (`tutti devices add <name> --serial <serial>`)'],
    'media': ['media target: plays what the household casts (roles below)'],
    'host': ['unknown: open its address to see what it is; a computer with ssh on becomes a machine'],
}


def parse_neigh(text):
    """[{address, mac}] from `ip neigh show`: live IPv4 entries with a hardware address."""
    out, seen = [], set()
    for line in (text or '').splitlines():
        f = line.split()
        if not f or ':' in f[0] or 'lladdr' not in f or f[-1] not in LIVE or f[0] in seen:
            continue
        seen.add(f[0])
        out.append({'address': f[0], 'mac': f[f.index('lladdr') + 1]})
    return out


def known(cfg, resolve=None):
    """The addresses and serials the config already has: machines (host, its address), devices (serial, ip of ip:port)."""
    out = set()
    for m in (cfg.get('machines') or {}).values():
        for h in (m or {}).get('host'), (m or {}).get('lan'):
            if h:
                out.add(str(h))
                if resolve:
                    out.add(resolve(str(h)) or '')
    ha = cfg.get('home_assistant') or {}
    if isinstance(ha, dict) and isinstance(ha.get('url'), str):
        from urllib.parse import urlsplit
        host = urlsplit(ha['url']).hostname
        out.update({host, resolve(host) if resolve and host else ''} - {None})
    for d in (cfg.get('devices') or {}).values():
        serial = str((d or {}).get('serial') or '')
        if serial:
            out.update({serial, serial.split(':')[0]})
    return out - {''}


def view(cfg, lan=(), neigh=(), adb=(), ssh=(), resolve=None):
    """[{name, address, kind, roles, can, found_by}] not set up yet, sorted by kind then address."""
    have = known(cfg, resolve)
    items = {}

    def add(key, name, kind, found_by, roles=()):
        if key in have:
            return
        item = items.setdefault(key, {'name': name, 'address': key, 'kind': kind, 'roles': [], 'found_by': []})
        if kind != 'host' and item['kind'] == 'host':
            item.update(kind=kind, name=name)
        item['roles'] += [r for r in roles if r not in item['roles']]
        if found_by not in item['found_by']:
            item['found_by'].append(found_by)
    for d in lan:
        add(d['address'], d['name'], 'media', 'mDNS / SSDP', [r['role'] for r in d.get('roles') or []])
    for n in neigh:
        add(n['address'], n['address'], 'host', 'neighbour table')
    for a in ssh:
        add(a, a, 'computer', 'ssh answers')
    for d in adb:
        if d.get('state') == 'device':
            add(d['serial'], d.get('model') or d['serial'], 'android', 'adb')
    order = ('computer', 'android', 'media', 'host')
    return [{**i, 'can': CAN[i['kind']], 'setup': setup_command(i)}
            for i in sorted(items.values(), key=lambda i: (order.index(i['kind']), i['address']))]


def setup_command(item):
    """The one command that sets it up (t225), run in a terminal here; '' when there is nothing to set up (media roles are
    detected). A computer's password is typed into ssh-copy-id there, never into the dashboard."""
    if item['kind'] in ('computer', 'host'):
        return f'tutti machines add --host {item["address"]}'
    if item['kind'] == 'android':
        name = re.sub(r'[^a-z0-9-]', '-', str(item.get('name') or 'phone').lower()).strip('-')[:30] or 'phone'
        return f'tutti devices add --name {name} --serial {item["address"]}'
    return ''


def ssh_open(address, timeout=SSH_TIMEOUT):
    try:
        with socket.create_connection((address, 22), timeout=timeout):
            return True
    except OSError:
        return False


def _run(cmd, run):
    try:
        return run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def gather(cfg, now, run=subprocess.run, which=None, probe=ssh_open, lan_fn=None):
    """(items, problems): every source read once; a source that is missing or fails is named in problems."""
    import shutil

    from . import lanmedia, setup
    which = which or shutil.which
    problems = []
    try:
        lan = (lan_fn or lanmedia.devices)(cfg, now)
    except (OSError, ValueError, KeyError) as e:
        lan, _ = [], problems.append(f'the media scan failed ({e})')
    neigh = parse_neigh(_run(['ip', 'neigh', 'show'], run)) if which('ip') else []
    if not which('ip'):
        problems.append('`ip` is not installed: the neighbour table is not read')
    adb = setup.adb_devices(_run(['adb', 'devices', '-l'], run)) if which('adb') else []
    candidates = [n['address'] for n in neigh][:MAX_PROBED]
    with ThreadPoolExecutor(max_workers=16) as pool:
        ssh = [a for a, ok in zip(candidates, pool.map(probe, candidates), strict=True) if ok]

    def resolve(host):
        try:
            return socket.gethostbyname(host)
        except OSError:
            return None
    return view(cfg, lan, neigh, adb, ssh, resolve), problems


def get(cfg, now):
    """(status, answer) for GET /api/discovered (admin-only: it maps the network)."""
    items, problems = gather(cfg, now)
    return 200, {'items': items, 'problems': problems}
