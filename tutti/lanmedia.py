"""Media devices on the LAN (docs/MACHINES.md, *Roles*): speakers, soundbars, TVs and Cast targets that Tutti cannot run tests
on, found over mDNS (avahi-browse) and UPnP SSDP, shown on the dashboard's Devices section with their roles.

mDNS service -> role: _airplay._tcp airplay-receiver, _raop._tcp airplay-receiver (audio), _googlecast._tcp
chromecast-builtin, _spotify-connect._tcp spotify-connect; an SSDP MediaRenderer is a dlna-renderer. A Cast device whose
model (the `md=` TXT) names a TV, or a `_airplay` one with a TV model, is also a smart-tv. Pure parsers plus `scan`, which
runs once a day (or `tutti machines --roles --refresh`) and keeps the answer in <state>/lan-devices.json."""
import json
import os
import shutil
import socket
import subprocess
import time

CAST_SVC = '_googlecast._tcp'
SERVICES = {'_airplay._tcp': 'airplay-receiver', '_raop._tcp': 'airplay-receiver', CAST_SVC: 'chromecast-builtin',
            '_spotify-connect._tcp': 'spotify-connect'}
ROLES = {
    'airplay-receiver': 'plays AirPlay (an Apple TV, an AirPlay speaker or soundbar)',
    'airplay-sender': 'sends AirPlay (macOS, iOS)',
    'dlna-renderer': 'a UPnP / DLNA MediaRenderer',
    'spotify-connect': 'a Spotify Connect speaker',
    'bluetooth-audio': 'a Bluetooth speaker or headset paired to a machine',
    'chromecast-builtin': 'Chromecast built-in (audio or video)',
    'smart-tv': 'a smart TV',
}
FILE, EVERY = 'lan-devices.json', 86400
SSDP = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 2\r\n'
        'ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n')
NAME_KEYS = {CAST_SVC: ('fn', 'md'), '_airplay._tcp': ('model',), '_raop._tcp': ('am',), '_spotify-connect._tcp': ()}
TV_WORDS = ('tv', 'bravia', 'shield', 'chromecast with google tv')


def _add(found, key, name, role, why):
    d = found.setdefault(key, {'name': name, 'address': key, 'roles': {}})
    d['roles'].setdefault(role, why)
    if name and (not d['name'] or d['name'] == key):
        d['name'] = name


def _txt(field, key):
    for part in field.split('" "'):
        k, _, v = part.strip('"').partition('=')
        if k == key:
            return v
    return ''


def parse_avahi(text, found=None):
    """`avahi-browse -a -t -r -p` resolved lines (`=;if;IPv4;name;service;domain;host;address;port;txt`) into found."""
    found = {} if found is None else found
    for line in (text or '').splitlines():
        p = line.split(';')
        if len(p) < 10 or p[0] != '=' or p[2] != 'IPv4' or p[4] not in SERVICES:
            continue
        name = _txt(p[9], 'fn') or p[3].split('@')[-1].replace('\\032', ' ')
        role = SERVICES[p[4]]
        _add(found, p[7], name, role, f'detected: {p[4]} over mDNS')
        model = (_txt(p[9], 'md') or _txt(p[9], 'model')).lower()
        if any(w in model or w in name.lower() for w in TV_WORDS):
            _add(found, p[7], name, 'smart-tv', f'detected: model {model or name}')
    return found


def parse_ssdp(replies, found=None):
    """SSDP answers [(text, ip)] to the MediaRenderer search into found."""
    found = {} if found is None else found
    for text, ip in replies:
        if 'mediarenderer' not in (text or '').lower():
            continue
        server = next((ln.split(':', 1)[1].strip() for ln in text.splitlines() if ln.lower().startswith('server:')), '')
        model = [t.split('/')[0] for t in server.split() if not t.lower().startswith(('linux', 'upnp', 'dlna'))]
        _add(found, ip, model[-1] if model else ip, 'dlna-renderer', 'detected: a UPnP MediaRenderer (SSDP)')
    return found


def ssdp(wait=3.0):
    """Sends one M-SEARCH and collects the answers for `wait` seconds."""
    out = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.settimeout(0.5)
        s.sendto(SSDP.encode(), ('239.255.255.250', 1900))
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                data, (ip, _) = s.recvfrom(4096)
                out.append((data.decode('utf8', 'replace'), ip))
            except TimeoutError:
                continue
    except OSError:
        pass
    finally:
        s.close()
    return out


def mdns_query(service):
    """One DNS-SD PTR question for `service` (e.g. _airplay._tcp.local), as bytes; QU bit set so answers come unicast."""
    q = b''.join(bytes([len(p)]) + p.encode() for p in (service + '.local').split('.')) + b'\0'
    return b'\0\0\0\0\0\1\0\0\0\0\0\0' + q + b'\0\x0c\x80\x01'


def _txt_value(data, key):
    """A TXT string `key=value` in a DNS packet: its length byte comes just before it, so a key inside another one does
    not match."""
    k, i = key.encode() + b'=', -1
    while True:
        i = data.find(k, i + 1)
        if i <= 0:
            return ''
        n = data[i - 1]
        v = data[i + len(k):i + n]
        if n > len(k) and i + n <= len(data) and v.isascii() and v.decode('ascii').isprintable():
            return v.decode('ascii')


def _service_answer(found, data, ip, svc, role):
    """One service named in one mDNS reply: the role, the name, smart-tv for a TV model."""
    vals = [_txt_value(data, k) for k in NAME_KEYS[svc]]
    group = 'group' in ' '.join(vals[1:]).lower()   # a speaker group shares its leader's address: the leader's answer names it
    name = '' if group else next((v for v in vals if v), '')
    if svc == CAST_SVC and name and ip in found:
        found[ip]['name'] = name   # the name the owner gave it in Google Home wins over a model
    _add(found, ip, name, role, f'detected: {svc} over mDNS')
    model = ' '.join(vals).lower()
    if any(w in model or w in name.lower() for w in TV_WORDS):
        _add(found, ip, name, 'smart-tv', f'detected: model {model or name}')


def _labels(svc):
    a, b = svc.split('.')
    return bytes([len(a)]) + a.encode() + bytes([len(b)]) + b.encode()


def parse_mdns(replies, found=None):
    """mDNS answers [(bytes, ip)]: a reply that names one of SERVICES gives its sender that role; `fn=` / `md=` in its TXT
    the name. No avahi needed (a machine without avahi-browse still finds the soundbar)."""
    found = {} if found is None else found
    for data, ip in replies:
        for svc, role in SERVICES.items():
            if _labels(svc) in data:
                _service_answer(found, data, ip, svc, role)
    return found


def mdns(wait=3.0):
    """Asks every service once over multicast DNS and collects the answers for `wait` seconds."""
    out = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.settimeout(0.5)
        for svc in SERVICES:
            s.sendto(mdns_query(svc), ('224.0.0.251', 5353))
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                data, (ip, _) = s.recvfrom(9000)
                out.append((data, ip))
            except TimeoutError:
                continue
    except OSError:
        pass
    finally:
        s.close()
    return out


def view(found):
    """[{name, address, roles: [{role, why, source}]}] for the dashboard, sorted by name."""
    return [{'name': d['name'], 'address': d['address'],
             'roles': [{'role': r, 'why': w, 'source': 'detected'} for r, w in sorted(d['roles'].items())]}
            for d in sorted(found.values(), key=lambda d: d['name'].lower())]


def scan(run=subprocess.run, which=shutil.which, ssdp_fn=ssdp, mdns_fn=mdns):
    found = parse_mdns(mdns_fn())
    if which('avahi-browse'):
        try:
            r = run(['avahi-browse', '-a', '-t', '-r', '-p'], capture_output=True, text=True, timeout=15)
            parse_avahi(r.stdout, found)
        except (OSError, subprocess.SubprocessError):
            pass
    parse_ssdp(ssdp_fn(), found)
    return view(found)


def devices(cfg, now, folder=None, refresh=False, scan_fn=scan):
    """The LAN media devices, scanned at most once a day; an admin's `lan_devices.<address>.roles` overrides a device's."""
    from . import config, roles
    path = os.path.join(folder or config.state_dir(cfg), FILE)
    try:
        with open(path, encoding='utf8') as f:
            rec = json.load(f)
    except (OSError, ValueError):
        rec = {}
    if refresh or 'at' not in rec or now - rec['at'] > EVERY:
        rec = {'at': now, 'devices': scan_fn()}
        try:
            with open(path, 'w', encoding='utf8') as f:
                json.dump(rec, f)
        except OSError:
            pass
    own = cfg.get('lan_devices') or {}
    out = []
    for d in rec.get('devices') or []:
        detected = {x['role']: x['why'] for x in d['roles']}
        out.append({**d, 'roles': roles.effective(own.get(d['address']), detected)})
    return out
