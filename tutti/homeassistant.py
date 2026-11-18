"""Home Assistant on the LAN (docs/MACHINES.md, *Hosts*): discover it over mDNS (`_home-assistant._tcp`) or from
`home_assistant.url`, probe version / uptime / online, and show it on the dashboard's Hosts card with its roles.

The long-lived access token lives only in `~/.config/tutti/secrets/home-assistant.token` (mode 600), never in the config.
Without a token Tutti still lists a discovered instance (version from mDNS, online when the HTTP port answers); with a
token it also reads uptime and which capability roles the instance offers (presence, power/UPS, smart plug, sensors).
Pure parsers plus thin I/O; nothing personal is hard-coded."""
import json
import os
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import lanmedia

HA_SVC = '_home-assistant._tcp'
ROLES = {
    'home-automation-hub': 'the household Home Assistant instance',
    'presence': 'phones home/away and room occupancy from Home Assistant',
    'power/UPS': 'power meters and UPS state from Home Assistant',
    'smart-plug': 'smart plugs through Home Assistant',
    'sensors': 'environment and device sensors from Home Assistant',
}
TOKEN_FILE = 'home-assistant.token'
FILE, EVERY = 'home-assistant.json', 60   # re-probe about once a minute (Hosts card freshness)
PRESENCE_DOMAINS = ('person', 'device_tracker', 'binary_sensor')
POWER_CLASSES = ('power', 'energy', 'battery', 'voltage', 'current', 'enum')
SENSOR_CLASSES = ('temperature', 'humidity', 'pressure', 'illuminance', 'co2', 'volatile_organic_compounds')
PLUG_DOMAINS = ('switch', 'outlet')


def errors(cfg):
    """Config errors of `home_assistant`, each naming the key and the fix."""
    ha = cfg.get('home_assistant')
    if ha is None:
        return []
    if not isinstance(ha, dict):
        return [f'home_assistant is {ha!r}: write {{url: "", enabled: true}} or remove the key']
    errs = []
    if 'enabled' in ha and not isinstance(ha['enabled'], bool):
        errs.append(f'home_assistant.enabled is {ha["enabled"]!r}: use true or false')
    url = ha.get('url') or ''
    if url and (not isinstance(url, str) or urlparse(url).scheme not in ('http', 'https') or not urlparse(url).netloc):
        errs.append(f'home_assistant.url is {url!r}: an http(s) URL such as http://homeassistant.local:8123')
    return errs


def token_path(cfg, env=None):
    """The file that holds the long-lived token (`~/.config/tutti/secrets/home-assistant.token`), or a path the config sets."""
    from . import config
    own = ((cfg or {}).get('home_assistant') or {}).get('token_file') or ''
    if own:
        return os.path.expanduser(own)
    return os.path.join(config.xdg_tutti_dir(env), 'secrets', TOKEN_FILE)


def read_token(cfg, env=None):
    """The token string, or '' when the file is missing or empty. Never logs the value."""
    path = token_path(cfg, env)
    try:
        with open(path, encoding='utf8') as f:
            return f.read().strip()
    except OSError:
        return ''


def _txt(data, key):
    return lanmedia._txt_value(data, key)


def parse_mdns(replies, found=None):
    """mDNS answers [(bytes, ip)]: each `_home-assistant._tcp` reply becomes {uuid: {name, address, port, version, url}}."""
    found = {} if found is None else found
    label = lanmedia._labels(HA_SVC)
    for data, ip in replies or []:
        if label not in data:
            continue
        if _txt(data, 'landingpage').lower() in ('true', '1'):
            continue   # the pre-install landing page is not a running core
        version = _txt(data, 'version')
        if version == '0000.0.0':
            continue
        uuid = _txt(data, 'uuid') or ip
        name = _txt(data, 'location_name') or 'Home Assistant'
        url = (_txt(data, 'internal_url') or _txt(data, 'base_url') or f'http://{ip}:8123').rstrip('/')
        host, port = _host_port(url, ip)
        found[uuid] = {'name': name, 'address': f'{host}:{port}', 'host': host, 'port': port,
                       'version': version or '', 'url': url, 'uuid': uuid, 'source': 'mdns'}
    return found


def _host_port(url, fallback_host=''):
    u = urlparse(url)
    host = u.hostname or fallback_host
    port = u.port or (443 if u.scheme == 'https' else 8123 if u.scheme == 'http' else 80)
    return host, port


def mdns(wait=2.0):
    """Asks for `_home-assistant._tcp` once and collects answers for `wait` seconds."""
    out = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.settimeout(0.5)
        s.sendto(lanmedia.mdns_query(HA_SVC), ('224.0.0.251', 5353))
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


def from_url(url):
    """A stub host from a configured URL (filled in by probe)."""
    base = url.rstrip('/')
    host, port = _host_port(base)
    return {'name': 'Home Assistant', 'address': f'{host}:{port}' if host else base, 'host': host, 'port': port,
            'version': '', 'url': base, 'uuid': base, 'source': 'config'}


def discover(cfg, mdns_fn=mdns):
    """[{…}] from mDNS and/or `home_assistant.url`. Empty when Home Assistant is disabled."""
    ha = (cfg or {}).get('home_assistant') or {}
    if ha.get('enabled', True) is False:
        return []
    found = parse_mdns(mdns_fn())
    url = (ha.get('url') or '').rstrip('/')
    if url:
        stub = from_url(url)
        match = next((x for x in found.values() if x['url'].rstrip('/') == url or x['host'] == stub['host']), None)
        if match:
            match['source'] = 'config+mdns'
            match['url'] = url
        else:
            found[stub['uuid']] = stub
    return list(found.values())


def _request(url, token='', timeout=3):
    """(status_code, body_text_or_None). Connection failures return (None, None)."""
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf8', 'replace') if e.fp else ''
    except (OSError, ValueError):
        return None, None


def _json(body):
    try:
        return json.loads(body) if body else None
    except ValueError:
        return None


def uptime_seconds(state_body, now=None):
    """Seconds since `sensor.uptime`'s ISO timestamp, or None."""
    data = _json(state_body) if isinstance(state_body, str) else state_body
    if not isinstance(data, dict):
        return None
    raw = data.get('state')
    try:
        started = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0, int((now - started).total_seconds()))


def format_uptime(seconds):
    """A short uptime string, or '' when unknown."""
    if seconds is None:
        return ''
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f'{days}d {hours}h'
    if hours:
        return f'{hours}h {mins}m'
    return f'{mins}m'


def roles_from_states(states):
    """{role: why} from a list of HA state objects (domains and device classes)."""
    out = {'home-automation-hub': 'detected: a Home Assistant instance'}
    if not isinstance(states, list):
        return out
    domains = {str(s.get('entity_id', '')).split('.', 1)[0] for s in states if isinstance(s, dict)}
    classes = {(s.get('attributes') or {}).get('device_class') for s in states if isinstance(s, dict)}
    if domains & set(PRESENCE_DOMAINS) or 'presence' in classes:
        out['presence'] = 'detected: person / device_tracker entities'
    if classes & set(POWER_CLASSES) or any('ups' in str(s.get('entity_id', '')).lower() for s in states if isinstance(s, dict)):
        out['power/UPS'] = 'detected: power / UPS entities'
    if domains & set(PLUG_DOMAINS):
        out['smart-plug'] = 'detected: switch / outlet entities'
    if classes & set(SENSOR_CLASSES) or 'sensor' in domains:
        out['sensors'] = 'detected: sensor entities'
    return out


def _is_online(code):
    """True when the HTTP port answered (200 OK or 401 needing a token both mean the instance is up)."""
    return code in (200, 401)


def _enrich(host, token, get, now):
    """Version, name, uptime and capability roles when a token is present."""
    base, version, uptime = host['url'].rstrip('/'), host.get('version') or '', None
    detected = {'home-automation-hub': f'detected: {_source_why(host)}'}
    c, cfg_body = get(f'{base}/api/config', token)
    data = _json(cfg_body) if c == 200 else None
    if isinstance(data, dict):
        version = str(data.get('version') or version)
        if data.get('location_name'):
            host = {**host, 'name': str(data['location_name'])}
    c, up_body = get(f'{base}/api/states/sensor.uptime', token)
    if c == 200:
        uptime = uptime_seconds(up_body, now=now)
    c, st_body = get(f'{base}/api/states', token)
    if c == 200:
        detected = roles_from_states(_json(st_body) or [])
        detected['home-automation-hub'] = f'detected: {_source_why(host)}'
    return host, version, uptime, detected


def probe_one(host, token='', get=_request, now=None):
    """Fills online/offline, version, uptime and capability roles on one discovered host dict."""
    code, _ = get(f'{host["url"].rstrip("/")}/api/', token)
    online = _is_online(code)
    version, uptime = host.get('version') or '', None
    detected = {'home-automation-hub': f'detected: {_source_why(host)}'}
    if token and online:
        host, version, uptime, detected = _enrich(host, token, get, now)
    return {**host, 'version': version, 'status': 'online' if online else 'offline',
            'uptime_seconds': uptime, 'uptime': format_uptime(uptime), 'roles_detected': detected}


def _source_why(host):
    src = host.get('source') or 'mdns'
    if 'config' in src and 'mdns' in src:
        return 'mDNS and home_assistant.url'
    return 'home_assistant.url' if src == 'config' else 'mDNS _home-assistant._tcp'


def view_one(host, cfg):
    """One Hosts-card row: roles effective against `home_assistant` overrides."""
    from . import roles
    detected = host.get('roles_detected') or {'home-automation-hub': f'detected: {_source_why(host)}'}
    rows = roles.effective((cfg or {}).get('home_assistant'), detected)
    return {'name': host.get('name') or 'Home Assistant', 'kind': 'home-assistant',
            'address': host.get('address') or '', 'url': host.get('url') or '',
            'version': host.get('version') or '', 'uptime': host.get('uptime') or '',
            'uptime_seconds': host.get('uptime_seconds'), 'status': host.get('status') or 'offline',
            'uuid': host.get('uuid') or '', 'source': host.get('source') or '', 'roles': rows}


def scan(cfg, mdns_fn=mdns, get=_request, token=None, now=None):
    """Discover, probe and return Hosts rows. `token=None` reads the secrets file; pass '' to skip it (tests)."""
    tok = read_token(cfg) if token is None else token
    return [view_one(probe_one(h, token=tok, get=get, now=now), cfg) for h in discover(cfg, mdns_fn=mdns_fn)]


def hosts(cfg, now, folder=None, refresh=False, scan_fn=None):
    """Cached Hosts rows for Home Assistant (about once a minute); [] when disabled or nothing found."""
    from . import config
    ha = (cfg or {}).get('home_assistant') or {}
    if ha.get('enabled', True) is False:
        return []
    path = os.path.join(folder or config.state_dir(cfg), FILE)
    try:
        with open(path, encoding='utf8') as f:
            rec = json.load(f)
    except (OSError, ValueError):
        rec = {}
    if refresh or 'at' not in rec or now - rec['at'] > EVERY:
        rec = {'at': now, 'hosts': (scan_fn or (lambda: scan(cfg)))()}
        try:
            with open(path, 'w', encoding='utf8') as f:
                json.dump(rec, f)
        except OSError:
            pass
    return list(rec.get('hosts') or [])
