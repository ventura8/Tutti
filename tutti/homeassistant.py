"""Home Assistant as a host Tutti sees (t268, docs/MACHINES.md, *Home Assistant*): the configured instance on the Machines
tab with its version, address, whether it answers and its roles.

Config: `home_assistant: {url: http://homeassistant.local:8123, token_env: TUTTI_HA_TOKEN}`. The long-lived access token
lives in ~/.config/tutti/home-assistant.env (mode 600) under that name and is read at the point of use, never stored,
printed or sent anywhere but that URL. Without a token Tutti still says whether the address answers. Pure `parse` and
`view`; `status` does the one request."""
import json
import re
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

API = '/api/home-assistant'
TIMEOUT = 4
ROLES = [{'role': 'home-automation-hub', 'why': 'Home Assistant runs here'}]
ENV_RX = re.compile(r'[A-Z_][A-Z0-9_]*')


class HaError(Exception):
    pass


def settings(cfg):
    """{url, token_env} or None when Home Assistant is not configured. Raises HaError naming the key and the fix."""
    ha = cfg.get('home_assistant')
    if not ha:
        return None
    if not isinstance(ha, dict) or not isinstance(ha.get('url'), str):
        raise HaError('home_assistant needs url: its address, e.g. http://homeassistant.local:8123')
    parts = urlsplit(ha['url'])
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise HaError(f'home_assistant.url is {ha["url"]!r}: write http(s)://host:port without a user or password in it')
    env = ha.get('token_env') or 'TUTTI_HA_TOKEN'
    if not ENV_RX.fullmatch(str(env)):
        raise HaError(f'home_assistant.token_env is {env!r}: an environment-style name like TUTTI_HA_TOKEN')
    return {'url': ha['url'].rstrip('/'), 'token_env': env}


def parse(doc):
    """What /api/config tells: {version, name, time_zone}; unknown fields are dropped."""
    doc = doc if isinstance(doc, dict) else {}
    return {'version': str(doc.get('version') or ''), 'name': str(doc.get('location_name') or 'Home Assistant'),
            'time_zone': str(doc.get('time_zone') or '')}


def view(s, answer=None, problem=''):
    """The Machines tab's card: {name, url, address, online, version, roles, problem}."""
    info = parse(answer) if answer is not None else {'version': '', 'name': 'Home Assistant', 'time_zone': ''}
    return {'name': info['name'], 'url': s['url'], 'address': urlsplit(s['url']).hostname, 'online': answer is not None or
            problem.startswith('answers'), 'version': info['version'], 'time_zone': info['time_zone'], 'roles': ROLES,
            'problem': problem}


LOOKUP_TRIES = 3
KNOWN = {}   # host name -> the last address it resolved to (a .local name over mDNS fails now and then)


def _name_failed(e):
    reason = getattr(e, 'reason', e)
    return isinstance(reason, socket.gaierror)


def reliable(fetch, url, token, resolve=None, pause=0.3, sleep=time.sleep):
    """`fetch(url, token)` that survives a flaky name lookup: retried, then sent to the last address the name had."""
    resolve = resolve or socket.gethostbyname
    host = urlsplit(url).hostname
    for attempt in range(LOOKUP_TRIES):
        try:
            answer = fetch(url, token)
            try:
                KNOWN[host] = resolve(host)   # resolved just now: cheap, and the fallback for the next failure
            except OSError:
                pass
            return answer
        except (urllib.error.URLError, OSError) as e:
            if isinstance(e, urllib.error.HTTPError) or not _name_failed(e):
                raise
            if attempt + 1 < LOOKUP_TRIES:
                sleep(pause)
    if host in KNOWN:
        return fetch(url.replace(f'//{host}', f'//{KNOWN[host]}', 1), token)
    raise OSError(f'the name {host} did not resolve ({LOOKUP_TRIES} tries): put its address in home_assistant.url')


TOP = 6


def _watts(x):
    try:
        v = float(x.get('state'))
    except (TypeError, ValueError):
        return None   # unavailable, unknown
    unit = (x.get('attributes') or {}).get('unit_of_measurement')
    return v * 1000 if unit == 'kW' else v if unit in ('W', None) else None


def _label(entity, attrs):
    name = str(attrs.get('friendly_name') or entity.split('.', 1)[1].replace('_', ' '))
    name = re.sub(r'\s+', ' ', re.sub(r'\b[Pp]ower\b', '', name)).strip()
    words = name.split(' ')
    half = len(words) // 2
    if len(words) % 2 == 0 and words[:half] == words[half:]:   # a device and its entity named alike: "NUC Plug NUC Plug"
        name = ' '.join(words[:half])
    return name


def overview(states, machines=(), plugs=None):
    """{people, power_w, top, machines} from /api/states (t233): who is home, live power now (power sensors, unavailable
    ones left out) with the biggest users, and each Tutti machine's own plug: `home_assistant.plugs.<machine>` names its
    sensor, else a power sensor whose id has the machine's name as a word (sensor.nuc_plug_power is nuc's)."""
    people, power = [], []
    for x in states if isinstance(states, list) else []:
        entity, attrs = str(x.get('entity_id', '')), x.get('attributes') or {}
        if entity.startswith('person.'):
            people.append({'name': str(attrs.get('friendly_name') or entity[7:]), 'home': x.get('state') == 'home'})
        elif entity.startswith('sensor.') and attrs.get('device_class') == 'power':
            w = _watts(x)
            if w is not None:
                power.append((entity, _label(entity, attrs), w))
    plugs = plugs or {}
    per_machine = {}
    for m in machines:
        want = plugs.get(m)
        hit = next((p for p in power if p[0] == want), None) if want else \
            next((p for p in power if re.search(rf'(^|[._]){re.escape(m)}([._]|$)', p[0].split('.', 1)[1])), None)
        if hit:
            per_machine[m] = round(hit[2], 1)
    top = sorted(power, key=lambda p: -p[2])[:TOP]
    return {'people': people, 'power_w': round(sum(p[2] for p in power)), 'top': [{'name': n, 'w': round(w)} for _e, n, w in top],
            'machines': per_machine, 'sensors': len(power)}


def status(cfg, fetch=None, token_fn=None):
    """{configured, item} for GET /api/home-assistant; one request to <url>/api/config with the token, if there is one."""
    from . import secretenv
    try:
        s = settings(cfg)
    except HaError as e:
        return {'configured': True, 'item': None, 'problem': str(e)}
    if not s:
        return {'configured': False, 'item': None, 'problem': ''}
    token = (token_fn or secretenv.get)(s['token_env']).strip()
    fetch = fetch or _fetch
    try:
        answer = reliable(fetch, f'{s["url"]}/api/config', token)
    except urllib.error.HTTPError as e:
        why = (f'answers, but the token was refused ({e.code}): make a long-lived access token in your Home Assistant profile '
               f'and put it in ~/.config/tutti/home-assistant.env as {s["token_env"]}=...') if e.code in (401, 403) \
            else f'answers with {e.code}: check home_assistant.url'
        return {'configured': True, 'item': view(s, problem=why), 'problem': ''}
    except (OSError, ValueError) as e:
        return {'configured': True, 'item': view(s, problem=f'does not answer ({e}): is it on, and is home_assistant.url right?'),
                'problem': ''}
    if answer is None:
        return {'configured': True, 'item': view(s, problem=f'answers; add a token as {s["token_env"]} in '
                                                         '~/.config/tutti/home-assistant.env to read its version'), 'problem': ''}
    item = view(s, answer)
    try:   # presence and power (t233): the card still shows the instance when this read fails
        states = reliable(fetch, f'{s["url"]}/api/states', token)
        ha = cfg.get('home_assistant') or {}
        item['overview'] = overview(states, list(cfg.get('machines') or {}), ha.get('plugs') if isinstance(ha.get('plugs'), dict) else None)
    except (urllib.error.URLError, OSError, ValueError) as e:
        item['problem'] = f'its people and power could not be read ({e})'
    return {'configured': True, 'item': item, 'problem': ''}


def _fetch(url, token):
    """The /api/config answer, or None when there is no token and the address answers at all."""
    if not token:
        with urllib.request.urlopen(url.rsplit('/api/', 1)[0] + '/', timeout=TIMEOUT):   # the front page: is it there?
            return None
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read(20_000_000).decode('utf8', 'replace'))   # /api/states: thousands of entities
