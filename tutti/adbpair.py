"""Pairing an Android device over wireless debugging (Android 11+), with no question but the one the device asks.

The QR flow (phones, tablets): Tutti makes a pairing name and password and shows them as the QR code Android Studio uses,
`WIFI:T:ADB;S:<name>;P:<password>;;`. The user taps Developer options > Wireless debugging > Pair device with QR code and
scans it; the device then announces `_adb-tls-pairing._tcp` under that name over mDNS, Tutti runs `adb pair` against it,
then connects through the device's `_adb-tls-connect._tcp` service and adds it to the config (name from the model, its
machine, its kind and capabilities). The code flow (a TV has no camera): the device shows a 6-digit code and announces its
pairing service; the code is the only thing typed. USB: `adb tcpip 5555` and `adb connect <its Wi-Fi address>:5555`.

The serial kept in the config is the mDNS name (`adb-<serial>-<id>._adb-tls-connect._tcp`): ports change on every reboot
and every time Wireless debugging is turned on, so a device is always found again through mDNS (scripts/device-readings.sh
reconnects it). Pure parsing and choosing here; the machine is reached through a `Host` (tutti/adbhost.py)."""
import re
import secrets
import string
import time

PAIRING, CONNECT = '_adb-tls-pairing._tcp', '_adb-tls-connect._tcp'
FLOWS = ("qr", "code", "usb", "watch")
MIN_ADB = 31   # platform-tools 31: `adb mdns services` and mDNS auto-connect (`adb pair` is 30)
OUR_PREFIX = 'tutti-'

FEATURES = {'android.hardware.camera.any': 'camera', 'android.hardware.telephony': 'telephony',
            'android.hardware.touchscreen': 'touchscreen', 'android.software.leanback': 'tv', 'android.hardware.nfc': 'nfc',
            'android.hardware.wifi': 'wifi', 'android.hardware.bluetooth': 'bluetooth', 'android.software.webview': 'webview',
            'android.hardware.type.watch': 'watch'}

TURN_ON = {
    'phone': ('Settings > About phone: tap "Build number" 7 times (type the PIN if asked): Developer options turn on.',
              'Settings > System > Developer options > Wireless debugging: turn it on; tap Allow ("Always allow on this network").'),
    'tablet': ('Settings > About tablet: tap "Build number" 7 times (type the PIN if asked): Developer options turn on.',
               'Settings > System > Developer options > Wireless debugging: turn it on; tap Allow ("Always allow on this network").'),
    'tv': ('Settings > System > About > "Android TV OS build": press OK 7 times: Developer options turn on.',
           'Settings > System > Developer options > Wireless debugging: turn it on (and "USB debugging" above it).'),
    'watch': ('Settings > System > About: tap "Build number" 7 times: Developer options turn on.',
              'Settings > Developer options > Wireless debugging: turn it on (and keep the watch awake on Wi-Fi).'),
}
LAST_STEP = {'qr': 'Tap "Pair device with QR code" and scan the code below.',
             'code': 'Tap "Pair device with pairing code": it shows a 6-digit code; type that code here.',
             'usb': 'Plug it in by USB and tap Allow on "Allow USB debugging?" (tick "Always allow from this computer").',
             'watch': 'Select the watch below to connect over Wi-Fi.'}


class PairError(Exception):
    """A pairing step failed; the message says what the user sees and what to do."""


def session(rand=secrets.choice):
    """(name, password) of one QR pairing: letters and digits only, so nothing needs escaping in the QR text."""
    alnum = string.ascii_lowercase + string.digits
    name = OUR_PREFIX + ''.join(rand(alnum) for _ in range(6))
    password = ''.join(rand(string.ascii_letters + string.digits) for _ in range(12))
    return name, password


def qr_payload(name, password):
    """The text of the QR code Android's "Pair device with QR code" scanner reads."""
    for v in (name, password):
        if not v or any(c in v for c in '\\;,:"'):
            raise ValueError(f'{v!r} cannot go into a pairing QR code: use letters and digits')
    return f'WIFI:T:ADB;S:{name};P:{password};;'


def flow_for(kind=None, asked=None, usb=False):
    """The flow a pairing uses: the one asked for; a TV's is the code (no camera); a watch uses watch auto-connect;
    a device on USB uses USB when asked only (QR keeps the mDNS serial that survives reboots); else the QR code."""
    if asked:
        if asked not in FLOWS:
            raise PairError(f'there is no "{asked}" pairing: use one of {", ".join(FLOWS)}')
        return asked
    if kind == 'tv':
        return 'code'
    if kind == 'watch':
        return 'watch'
    return 'qr'


def steps(kind, flow):
    """The exact taps on the device, in order: the only things the user does."""
    kind = kind if kind in TURN_ON else 'phone'
    return [*TURN_ON[kind], LAST_STEP.get(flow, LAST_STEP['qr'])]


def parse_services(text):
    """`adb mdns services` as [(instance, service, 'ip:port')]; older adb ends the service with a dot."""
    out = []
    for line in (text or '').splitlines():
        parts = line.split()
        if len(parts) == 3 and re.fullmatch(r'[\d.]+:\d+|\[[0-9a-fA-F:]+\]:\d+', parts[2]):
            out.append((parts[0], parts[1].rstrip('.'), parts[2]))
    return out


def parse_avahi(text):
    """`avahi-browse -t -r -p` resolved lines (`=;if;IPv4;name;service;domain;host;address;port;txt`) as parse_services
    tuples, IPv4 only."""
    out = []
    for line in (text or '').splitlines():
        f = line.split(';')
        if len(f) >= 9 and f[0] == '=' and f[2] == 'IPv4' and (f[3], f[4], f'{f[7]}:{f[8]}') not in out:
            out.append((f[3], f[4], f'{f[7]}:{f[8]}'))
    return out


def pairing_service(services, name=None, ignore=()):
    """The pairing service to use: ours by name (QR), or with no name the one a device opened for its code (not one of
    ours, not one already there before); None while there is none."""
    for inst, svc, addr in services:
        if svc != PAIRING:
            continue
        if name and inst == name:
            return addr
        if not name and not inst.startswith(OUR_PREFIX) and (inst, addr) not in ignore:
            return addr
    return None


def connect_address(services, guid):
    """The device's ip:port to connect to now (it changes after a reboot), from its connect service; None when unseen."""
    return next((addr for inst, svc, addr in services if svc == CONNECT and inst == guid), None)


def serial_of(guid):
    return f'{guid}.{CONNECT}'


def guid_of(serial):
    """`adb-XYZ-abc` from the serial `adb-XYZ-abc._adb-tls-connect._tcp`, else ''."""
    s = str(serial or '')
    return s[:-len(CONNECT) - 1] if s.endswith('.' + CONNECT) else ''


def parse_paired(text):
    """The device's mDNS name from `adb pair`'s answer ("Successfully paired to ip:port [guid=adb-XYZ-abc]"), else ''."""
    m = re.search(r'Successfully paired to \S+ \[guid=([^\]\s]+)\]', text or '')
    return m.group(1) if m else ''


def parse_version(text):
    """The platform-tools major version from `adb version` ("Version 35.0.2-12147458"), else 0."""
    m = re.search(r'^Version (\d+)\.', text or '', re.M)
    return int(m.group(1)) if m else 0


def kind_of(characteristics, features):
    chars = set((characteristics or '').replace(' ', '').split(','))
    if 'watch' in chars or 'android.hardware.type.watch' in features:
        return 'watch'
    if 'tv' in chars or 'android.software.leanback' in features:
        return 'tv'
    return 'tablet' if 'tablet' in chars else 'phone'


def parse_features(text):
    return {line.split(':', 1)[1].strip().split('=')[0] for line in (text or '').splitlines() if line.startswith('feature:')}


def capabilities(features):
    return sorted({word for feat, word in FEATURES.items() if feat in features})


def name_for(model, taken):
    """`pixel9pro` from "Pixel 9 Pro"; `pixel9pro2` when that name is taken."""
    base = re.sub(r'[^a-z0-9]', '', (model or '').lower()) or 'device'
    name, n = base, 2
    while name in taken:
        name, n = f'{base}{n}', n + 1
    return name


def entry(serial, machine, info, adb=None):
    """The config entry of a paired device."""
    out = {'serial': serial, 'machine': machine, 'kind': info['kind'], 'model': info['model'], 'capabilities': info['capabilities']}
    if adb:
        out['adb'] = adb
    return out


def baseline(host):
    """The pairing services open now: a code pairing takes only one that opens after this."""
    return {(i, a) for i, s, a in parse_services(host.adb('mdns', 'services')[1]) if s == PAIRING}


def wait_for(fn, wait, sleep=time.sleep, poll=2):
    """fn()'s first truthy answer, asking every `poll` seconds for `wait` seconds; None after that."""
    waited = 0
    while True:
        got = fn()
        if got or waited >= wait:
            return got
        sleep(poll)
        waited += poll


def pair(host, *, name=None, password=None, code=None, ignore=(), wait=180, sleep=time.sleep, say=lambda m: None):
    """Pairs one device and connects it: QR (name and password) or code. Returns (the serial to keep: its mDNS name, the
    serial adb lists now: that name or ip:port). The pairing service is looked for over mDNS until `wait` seconds pass;
    the code flow skips the ones in `ignore` (open before this pairing started)."""
    say('waiting for the device: scan the QR code' if name else 'waiting for the device\'s pairing screen')
    addr = wait_for(lambda: pairing_service(parse_services(host.adb('mdns', 'services')[1]), name, ignore), wait, sleep)
    if not addr:
        raise PairError(f'no device asked to pair within {wait} s: check Wireless debugging is on and the device is on the same '
                        'Wi-Fi as this machine, then try again')
    rc, out = host.adb('pair', addr, password if name else code, timeout=60)
    guid = parse_paired(out)
    if rc != 0 or not guid:
        raise PairError('the device refused the pairing (a wrong code, or the pairing screen closed): open the pairing '
                        'screen again and retry')
    say(f'paired with {guid}; connecting')
    return serial_of(guid), connect(host, guid, wait=wait, sleep=sleep)


def connect(host, guid, wait=60, sleep=time.sleep):
    """Connects to the device through its mDNS name (adb resolves it), else through the ip:port its connect service
    announces now. Returns the serial adb lists it under (the name, or that ip:port); PairError when it does not come up."""
    serial = serial_of(guid)

    def up():
        if host.state(serial) == 'device':
            return serial
        host.adb('connect', serial, timeout=30)
        if host.state(serial) == 'device':
            return serial
        addr = connect_address(parse_services(host.adb('mdns', 'services')[1]), guid)
        if addr:
            host.adb('connect', addr, timeout=30)
            if host.state(serial) != 'device' and host.state(addr) == 'device':
                return addr   # this adb lists it by ip:port; the config keeps the mDNS name (readings reconnect by it)
        return serial if host.state(serial) == 'device' else None
    live = wait_for(up, wait, sleep, poll=3)
    if live:
        return live
    raise PairError(f'{guid} paired but does not connect: keep Wireless debugging on and the device on this Wi-Fi, '
                    'then run `tutti devices` (it reconnects by itself)')


def describe(host, serial):
    """{model, kind, capabilities, sdk} of a connected device."""
    def prop(p):
        return host.adb('-s', serial, 'shell', 'getprop', p)[1].strip()
    features = parse_features(host.adb('-s', serial, 'shell', 'pm', 'list', 'features')[1])
    sdk = prop('ro.build.version.sdk')
    return {'model': prop('ro.product.model') or 'Android device', 'kind': kind_of(prop('ro.build.characteristics'), features),
            'capabilities': capabilities(features), 'sdk': int(sdk) if sdk.isdigit() else None}


def _watch_entry(host, inst, addr, known, cfg_devs):
    """Builds a watch dict for one connect service, or None if known as another device kind."""
    if inst in known:
        name, d = known[inst]
        if d.get('kind') == 'watch' or 'watch' in name.lower() or d.get('kind') not in ('phone', 'tablet', 'tv'):
            return {'instance': inst, 'serial': serial_of(inst), 'addr': addr,
                    'name': name, 'model': d.get('model', 'Wear OS watch'), 'kind': 'watch'}
        return None
    live = inst if host.state(inst) == 'device' else (addr if host.state(addr) == 'device' else '')
    if live:
        try:
            info = describe(host, live)
            if info.get('kind') == 'watch':
                return {'instance': inst, 'serial': serial_of(inst), 'addr': addr,
                        'name': name_for(info['model'], cfg_devs), 'model': info['model'], 'kind': 'watch'}
        except Exception:
            pass
    return {'instance': inst, 'serial': serial_of(inst), 'addr': addr,
            'name': inst, 'model': 'Wear OS watch', 'kind': 'watch'}


def discover_watches(host, configured=None):
    """Discovers Wear OS watches over mDNS. Returns list of watch dicts."""
    raw_services = parse_services(host.adb('mdns', 'services')[1])
    connects = [(inst, addr) for inst, svc, addr in raw_services if svc == CONNECT]
    cfg_devs = configured or {}
    known = {guid_of(d.get('serial', '')): (n, d) for n, d in cfg_devs.items() if guid_of(d.get('serial', ''))}
    watches, seen = [], set()
    for inst, addr in connects:
        if inst not in seen:
            seen.add(inst)
            item = _watch_entry(host, inst, addr, known, cfg_devs)
            if item:
                watches.append(item)
    return watches


def _is_connected(rc, out):
    text = (out or '').strip()
    return rc == 0 and ('connected to' in text or 'already connected' in text) and 'cannot' not in text and 'failed' not in text


def _resolve_watch(host, target):
    """(instance, addr) from target dict or string (addr or serial/guid). Raises PairError if not found."""
    if isinstance(target, dict):
        return target.get('instance', ''), target.get('addr', '')
    target_str = str(target or '').strip()
    if ':' in target_str:
        services = parse_services(host.adb('mdns', 'services')[1])
        inst = next((i for i, s, a in services if s == CONNECT and a == target_str), '')
        return inst, target_str
    guid = guid_of(target_str) or target_str
    services = parse_services(host.adb('mdns', 'services')[1])
    addr = next((a for i, s, a in services if s == CONNECT and i == guid), None)
    if not addr:
        raise PairError(f'{target_str}: address not found in adb mdns services; check Wireless debugging is on')
    return guid, addr


def connect_watch(host, target, wait=15, sleep=time.sleep):
    """Connects to a selected Wear OS watch using ADB over Wi-Fi. Returns (serial, live_serial). Raises PairError on failure."""
    inst, addr = _resolve_watch(host, target)
    serial = serial_of(inst) if inst else addr

    def try_connect():
        rc, out = host.adb('connect', addr, timeout=30)
        return (addr if host.state(addr) == 'device' else serial) if _is_connected(rc, out) else None

    live = try_connect() or (wait_for(try_connect, wait, sleep=sleep, poll=2) if wait > 0 else None)
    if live:
        return serial, live
    rc, last_out = host.adb('connect', addr, timeout=10)
    lines = (last_out or '').strip().splitlines()
    reason = lines[-1] if lines else f'exit {rc}'
    raise PairError(
        f'could not connect to {addr} ({reason}): the watch\'s Wi-Fi or wireless debugging may be sleeping; '
        'wake the watch, tap the screen or check Wireless debugging is on, then try again'
    )


def usb_serials(devices_text):
    """Serials `adb devices` lists over USB (not ip:port, not an mDNS name) and allowed."""
    out = []
    for line in (devices_text or '').splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == 'device' and ':' not in parts[0] and not guid_of(parts[0]):
            out.append(parts[0])
    return out


def wifi_address(text):
    """The device's Wi-Fi IPv4 address from `ip -f inet addr show wlan0`, else ''."""
    m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)/', text or '')
    return m.group(1) if m else ''


def via_usb(host, usb, sleep=time.sleep):
    """USB fallback: switch the device to adb over TCP on 5555 and connect to its Wi-Fi address. Returns ip:5555."""
    ip = wifi_address(host.adb('-s', usb, 'shell', 'ip', '-f', 'inet', 'addr', 'show', 'wlan0')[1])
    if not ip:
        raise PairError('the device is not on Wi-Fi: connect it to the same Wi-Fi as this machine, then try again')
    host.adb('-s', usb, 'tcpip', '5555', timeout=30)
    serial = f'{ip}:5555'
    if wait_for(lambda: host.adb('connect', serial, timeout=30)[0] == 0 and host.state(serial) == 'device', 30, sleep, poll=3):
        return serial
    raise PairError(f'{serial} does not answer over Wi-Fi: check this machine and the device are on the same network')


def open_settings(host, usb):
    """Opens Developer options on a device plugged in by USB, so the user lands on the right page."""
    return host.adb('-s', usb, 'shell', 'am', 'start', '-a', 'android.settings.APPLICATION_DEVELOPMENT_SETTINGS')[0] == 0
