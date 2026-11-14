"""Keeps a wireless device reachable with no step from the user (docs/DEVICES.md, *Wireless debugging stays on*).

Android turns Wireless debugging off after a reboot or a Wi-Fi change. Its setting is `settings global adb_wifi_enabled`,
which any adb connection may set: a device that dropped off is switched back on through any machine that still reaches
it, over Wi-Fi or a USB cable on any household machine. Only when no machine reaches it is the user asked, once, with
the reason and the exact taps (LAST_STEP)."""
import time

from . import adbpair

WIFI_SETTING = ('settings', 'global', 'adb_wifi_enabled')
EVERY = 300   # seconds between two tries for the same device (each asks every machine's adb)
LAST_STEP = ('Wireless debugging is off on the device (Android turns it off after a reboot or a Wi-Fi change) and no '
             'computer reaches it by cable or Wi-Fi to turn it back on: on the device, Settings > System > Developer options > '
             'Wireless debugging: on. Tutti keeps it on from then on; or plug it into any household computer by USB.')
_TRIED = {}


def usb_serial_of(guid):
    """The USB serial inside an mDNS name: `adb-46191FDAP003RM-OikUIT` -> `46191FDAP003RM`, else ''."""
    if not guid.startswith('adb-') or guid.count('-') < 2:
        return ''
    return guid[4:].rsplit('-', 1)[0]


def listed(text):
    """The serials `adb devices` lists as connected and allowed."""
    return {p[0] for p in (line.split() for line in (text or '').splitlines()[1:]) if len(p) >= 2 and p[1] == 'device'}


def get(host, serial):
    """adb_wifi_enabled now ('1', '0', or '' when unreadable)."""
    rc, out = host.adb('-s', serial, 'shell', 'settings', 'get', 'global', 'adb_wifi_enabled', timeout=20)
    v = out.strip().splitlines()[-1].strip() if rc == 0 and out.strip() else ''
    return v if v in ('0', '1') else ''


def turn_on(host, serial):
    """Sets Wireless debugging on through this connection; True when it reads 1 after."""
    host.adb('-s', serial, 'shell', 'settings', 'put', 'global', 'adb_wifi_enabled', '1', timeout=20)
    return get(host, serial) == '1'


def revive(cfg, d, hosts_fn, clock=time.time):
    """Turns Wireless debugging back on through any machine that reaches the device (Wi-Fi first, then USB). Returns
    what it did, '' when no machine reaches it (LAST_STEP is then the one thing to ask), or None when it was tried
    within EVERY seconds."""
    guid = adbpair.guid_of(d['serial'])
    if not guid:
        return ''
    now = clock()
    if now - _TRIED.get(d['name'], -EVERY) < EVERY:
        return None
    _TRIED[d['name']] = now
    usb = usb_serial_of(guid)
    for name, m in cfg['machines'].items():
        if m.get('transport') == 'android' or m.get('paused'):
            continue
        host = hosts_fn(name, d.get('adb') if name == d['machine'] else None)
        rc, out = host.adb('devices', timeout=20)
        found = listed(out) if rc == 0 else set()
        via = next((s for s in (str(d['serial']), usb) if s and s in found), None)
        if via and turn_on(host, via):
            return f'Wireless debugging was off; turned back on through {name} ({"USB" if via == usb else "Wi-Fi"})'
    return ''


def hosts(cfg):
    from . import adbhost
    return lambda name, adb=None: adbhost.Host(name, cfg['machines'][name], adb=adb)


def readings_with_revive(cfg, readings_fn, hosts_fn=None):
    """readings_fn for devices_state: a wireless device not connected is revived (above) and read again; when nothing
    reaches it the readings carry LAST_STEP as the reason."""
    mk = hosts_fn or hosts(cfg)

    def read(machine, d, **kw):
        r = readings_fn(machine, d, **kw)
        if r.get('adb') != 'ok' or r.get('state') == 'device' or not adbpair.guid_of(d.get('serial')):
            return r
        did = revive(cfg, d, mk)
        if did:
            r = {**readings_fn(machine, d, **kw), 'revived': did}
        elif did == '':
            r = {**r, 'last_step': LAST_STEP}
        return r
    return read
