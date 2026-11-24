"""LAN/VPN device discovery for the dashboard's Discovered tab.

Finds devices and computers not yet configured in Tutti via ARP/neighbour table, ADB, mDNS (Avahi),
SSDP/DLNA, AirPlay/Chromecast, and SSH port checks. Parsers, classification, merging and filtering
are pure functions; I/O lives in `scan()` and the thin probe helpers with short timeouts.
"""
import os
import re
import socket
import subprocess
import time

PI_OUIS = ('b8:27:eb', 'dc:a6:32', 'e4:5f:01', '28:cd:c1', 'd8:3a:dd', '2c:cf:67')
PLUG_HINTS = ('plug', 'shelly', 'tasmota', 'kasa', 'tapo', 'sonoff', 'wemo')
SSDP_ADDR = ('239.255.255.250', 1900)
SSDP_QUERY = (
    'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
    'MAN: "ssdp:discover"\r\nMX: 1\r\nST: ssdp:all\r\n\r\n'
).encode('ascii')

MDNS_MAP = (
    ('_adb', 'android', 'adb'),
    ('_airplay', 'airplay', 'airplay'),
    ('_raop', 'airplay', 'airplay'),
    ('_googlecast', 'chromecast', 'chromecast'),
    ('_ipp', 'printer', 'printer'),
    ('_printer', 'printer', 'printer'),
    ('_pdl-datastream', 'printer', 'printer'),
    ('_smb', 'nas', 'nas'),
    ('_afpovertcp', 'nas', 'nas'),
    ('_nfs', 'nas', 'nas'),
    ('_ssh', 'computer', 'ssh'),
    ('_workstation', 'computer', 'workstation'),
)

POSSIBLE_ROLES = {
    'android': ['adb', 'ssh'],
    'computer': ['ssh', 'worker'],
    'raspberry pi': ['ssh', 'worker'],
    'tv': ['display', 'cast'],
    'chromecast': ['display', 'cast'],
    'airplay': ['display', 'cast'],
    'printer': ['printer'],
    'nas': ['storage', 'ssh'],
    'smart plug': ['power'],
}

TYPE_RANK = {
    'computer': 1, 'media': 2, 'nas': 3, 'printer': 4, 'smart plug': 5,
    'tv': 6, 'airplay': 7, 'chromecast': 8, 'raspberry pi': 9, 'android': 10,
}


def possible_roles_for(dev_type, roles):
    """Roles a discovered item can take in Tutti (Android can be both adb device and Termux ssh host)."""
    base = list(POSSIBLE_ROLES.get(dev_type, ['ssh']))
    for role in roles or []:
        if role not in base:
            base.append(role)
    return base


def _looks_like_plug(text):
    low = (text or '').lower()
    return any(hint in low for hint in PLUG_HINTS)


def _arp_mac(parts):
    if 'lladdr' not in parts:
        return ''
    idx = parts.index('lladdr') + 1
    return parts[idx].lower() if idx < len(parts) else ''


def _arp_line(line):
    parts = line.split()
    if len(parts) < 3 or parts[-1] not in ('REACHABLE', 'STALE', 'DELAY'):
        return None
    ip = parts[0]
    dev_type = 'raspberry pi' if _arp_mac(parts).startswith(PI_OUIS) else 'computer'
    return {'name': ip, 'ip': ip, 'type': dev_type, 'roles': [], 'sources': ['ARP']}


def parse_arp(text):
    """Parse `ip neigh` output into discovered device dicts."""
    return [item for line in (text or '').splitlines() if (item := _arp_line(line))]


def parse_adb(text):
    """Parse `adb devices -l` or `adb devices` output into discovered Android device dicts."""
    items = []
    for line in (text or '').splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[1] != 'device':
            continue
        serial = parts[0]
        model = next((p.split(':', 1)[1] for p in parts[2:] if p.startswith('model:')), '')
        items.append({'name': model or serial, 'ip': serial, 'serial': serial, 'type': 'android', 'roles': ['adb'], 'sources': ['ADB']})
    return items


def _mdns_kind(name, service):
    if _looks_like_plug(name) or '_esphomelib' in service:
        return 'smart plug', ['power']
    for needle, dev_type, role in MDNS_MAP:
        if needle in service:
            return dev_type, [role]
    return 'computer', []


def parse_mdns(text):
    """Parse `avahi-browse -rt -p` (or semicolon-delimited) output into discovered device dicts."""
    items = []
    for line in (text or '').splitlines():
        if not line.startswith('=') or ';IPv4;' not in line:
            continue
        parts = line.split(';')
        if len(parts) < 8:
            continue
        name, service, ip = parts[3], parts[4], parts[7]
        dev_type, roles = _mdns_kind(name, service)
        items.append({'name': name or ip, 'ip': ip or name, 'type': dev_type, 'roles': roles, 'sources': ['mDNS']})
    return items


SSDP_MAP = (
    (('mediarenderer', 'dlna', 'dial-multiscreen'), 'tv', 'dlna'),
    (('printer',), 'printer', 'printer'),
    (('mediaserver', 'synology', 'qnap'), 'nas', 'dlna'),
)


def _ssdp_kind(headers):
    blob = ' '.join(headers.values()).lower()
    if _looks_like_plug(blob):
        return 'smart plug', ['power']
    for needles, dev_type, role in SSDP_MAP:
        if any(n in blob for n in needles):
            return dev_type, [role]
    return 'media', ['ssdp']


def _ssdp_headers(packet):
    headers = {}
    for line in (packet or '').splitlines():
        if ':' in line:
            k, v = line.split(':', 1)
            headers[k.strip().lower()] = v.strip()
    return headers


def parse_ssdp(responses):
    """Parse raw (ip, packet_text) SSDP responses into discovered device dicts."""
    items = []
    for ip, packet in responses or []:
        headers = _ssdp_headers(packet)
        if not headers:
            continue
        dev_type, roles = _ssdp_kind(headers)
        server = headers.get('server', '')
        name = re.sub(r'\s+', ' ', server).split('/')[0].strip() if server else ip
        items.append({'name': name or ip, 'ip': ip, 'type': dev_type, 'roles': roles, 'sources': ['SSDP']})
    return items


def _entry_keys(entry):
    if not isinstance(entry, dict):
        return set()
    return {str(entry[field]) for field in ('host', 'ip', 'serial', 'address') if entry.get(field)}


def _configured_keys(cfg):
    keys = set()
    for section in ('devices', 'machines'):
        for name, entry in (cfg.get(section) or {}).items():
            keys.add(name)
            keys.update(_entry_keys(entry))
    return keys


def _prefer_name(existing, incoming):
    if existing['name'] == existing.get('ip') and incoming['name'] != incoming.get('ip'):
        existing['name'] = incoming['name']


def _merge_one(existing, incoming):
    if TYPE_RANK.get(incoming['type'], 0) > TYPE_RANK.get(existing['type'], 0):
        existing['type'] = incoming['type']
    _prefer_name(existing, incoming)
    existing['roles'] = sorted(set(existing['roles']) | set(incoming.get('roles') or []))
    existing['sources'] = sorted(set(existing['sources']) | set(incoming.get('sources') or []))
    existing['serial'] = existing.get('serial') or incoming.get('serial') or ''


def _item_key(item):
    return item.get('ip') or item.get('serial') or item.get('name') or ''


def _fresh_item(item, key):
    return {
        'name': item.get('name') or key, 'ip': item.get('ip') or '',
        'serial': item.get('serial') or '', 'type': item.get('type') or 'computer',
        'roles': sorted(set(item.get('roles') or [])), 'sources': sorted(set(item.get('sources') or [])),
    }


def _merge_items(raw_items):
    merged = {}
    for item in raw_items or []:
        key = _item_key(item)
        if not key:
            continue
        if key in merged:
            _merge_one(merged[key], item)
        else:
            merged[key] = _fresh_item(item, key)
    return merged


def merge_and_filter(cfg, raw_items):
    """Deduplicate discovered items by IP/serial/name, compute possible roles, and drop configured items."""
    merged = _merge_items(raw_items)
    known = _configured_keys(cfg or {})
    out = []
    for item in merged.values():
        if {item['name'], item['ip'], item['serial']} & known:
            continue
        item['possible_roles'] = possible_roles_for(item['type'], item['roles'])
        out.append(item)
    return sorted(out, key=lambda d: (d['type'], d['name']))


def _run_cmd(argv, timeout=2):
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return res.stdout if res.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def _probe_ssdp(timeout=1.0):
    responses = []
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.sendto(SSDP_QUERY, SSDP_ADDR)
            while (remaining := deadline - time.monotonic()) > 0:
                sock.settimeout(remaining)
                data, addr = sock.recvfrom(4096)
                responses.append((addr[0], data.decode('utf8', 'replace')))
    except OSError:
        pass
    return responses


def _probe_ssh(ip, port=22, timeout=0.3):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ssh_items(arp_items, ssh_fn):
    found = []
    for item in arp_items[:32]:
        ip = item.get('ip')
        if not ip:
            continue
        if ssh_fn(ip, 22):
            found.append({'name': ip, 'ip': ip, 'type': 'computer', 'roles': ['ssh'], 'sources': ['SSH']})
        elif ssh_fn(ip, 8022):
            found.append({'name': ip, 'ip': ip, 'type': 'android', 'roles': ['ssh'], 'sources': ['SSH']})
    return found


def _is_default_probes(run_fn, ssdp_fn, ssh_fn):
    return run_fn is _run_cmd and ssdp_fn is _probe_ssdp and ssh_fn is _probe_ssh


def _skip_real_probes(run_fn, ssdp_fn, ssh_fn):
    blocked = bool(os.environ.get('TUTTI_OFFLINE') or os.environ.get('TUTTI_NO_DAEMONS'))
    return blocked and _is_default_probes(run_fn, ssdp_fn, ssh_fn)


def scan(cfg, run_fn=_run_cmd, ssdp_fn=_probe_ssdp, ssh_fn=_probe_ssh):
    """Scan LAN/VPN for unconfigured devices across ARP, ADB, mDNS, SSDP and SSH."""
    if _skip_real_probes(run_fn, ssdp_fn, ssh_fn):
        return []
    arp = parse_arp(run_fn(['ip', 'neigh']))
    raw = [
        *arp,
        *parse_adb(run_fn(['adb', 'devices', '-l'])),
        *parse_mdns(run_fn(['avahi-browse', '-art', '-p'])),
        *parse_ssdp(ssdp_fn()),
        *_ssh_items(arp, ssh_fn),
    ]
    return merge_and_filter(cfg, raw)
