"""Home Assistant connector: device control, presence, energy, and notifications."""

import json
import math
import re
import urllib.request
from contextlib import contextmanager
from dataclasses import replace
from http.client import HTTPException

from . import config

# HA API endpoints
API_STATE = 'api/states'
API_SERVICES = 'api/services'


def settings(cfg):
    value = cfg.get('home_assistant')
    return value if isinstance(value, dict) else {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(req, **kwargs):
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(req, **kwargs)


def _get_token(cfg):
    """Reads the HA token from the configured file (default: ~/.config/tutti/secrets/home-assistant.token)."""
    default = '~/.config/tutti/secrets/home-assistant.token'
    filename = settings(cfg).get('token_file') or default
    if not isinstance(filename, str):
        return ''
    token_path = config.expand(filename)
    try:
        with open(token_path, encoding='utf8') as f:
            return f.read().strip()
    except (OSError, UnicodeError):
        return ''


def _call_ha(cfg, method, endpoint, data=None, timeout=2):
    """Sends a request to Home Assistant."""
    url = settings(cfg).get('url')
    if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
        return None
    token = _get_token(cfg)
    if not token:
        return None

    try:
        req = urllib.request.Request(f'{url.rstrip("/")}/{endpoint}', method=method)
        req.add_header('Authorization', f'Bearer {token}')
        req.add_header('Content-Type', 'application/json')
        body = json.dumps(data).encode('utf8') if data is not None else None
        with _open(req, data=body, timeout=timeout) as res:
            return json.loads(res.read().decode('utf8'))
    except (OSError, ValueError, HTTPException):
        return None


ENTITY = re.compile(r'[a-z0-9_]+\.[a-z0-9_]+')
SLUG = re.compile(r'[a-z0-9_]+')
PRESENT_STATES = ('home', 'on', 'occupied')
MEDIA_ACTIONS = {'turn_on': 'turn_on', 'turn_off': 'turn_off', 'play': 'media_play', 'pause': 'media_pause'}
SERVICE_FIELDS = {
    'light': {'turn_on': ('brightness',), 'turn_off': ()},
    'media_player': {**dict.fromkeys(MEDIA_ACTIONS.values(), ()), 'volume_set': ('volume_level',),
                     'select_source': ('source',), 'play_media': ('media_content_type', 'media_content_id')},
    'remote': {'turn_on': ('activity',), 'turn_off': (), 'send_command': ('command',)},
    'switch': {'turn_on': (), 'turn_off': ()},
}


def get_state(cfg, entity_id):
    """Gets the state of a Home Assistant entity."""
    if not isinstance(entity_id, str) or not ENTITY.fullmatch(entity_id):
        return None
    result = _call_ha(cfg, 'GET', f'{API_STATE}/{entity_id}')
    return result if isinstance(result, dict) else None


def _writable(cfg, entity_id):
    allowed = settings(cfg).get('writable_entities')
    return (isinstance(entity_id, str) and bool(ENTITY.fullmatch(entity_id))
            and isinstance(allowed, (list, tuple)) and entity_id in allowed)


def call_service(cfg, domain, service, entity_id, data=None):
    """Calls a service on exactly one allowed entity."""
    fields = SERVICE_FIELDS.get(domain, {}).get(service)
    if fields is None or not _writable(cfg, entity_id) or not entity_id.startswith(f'{domain}.'):
        return None
    payload = {k: v for k, v in (data or {}).items() if k in fields}
    payload['entity_id'] = entity_id
    return _call_ha(cfg, 'POST', f'{API_SERVICES}/{domain}/{service}', data=payload)


def control_media_player(cfg, entity_id, action, **kwargs):
    """Controls a media player in Home Assistant."""
    if action in MEDIA_ACTIONS:
        return call_service(cfg, 'media_player', MEDIA_ACTIONS[action], entity_id, data={})
    controls = {'volume': ('volume_set', {'volume_level': kwargs.get('volume')}),
                'source': ('select_source', {'source': kwargs.get('source')}),
                'app': ('play_media', {'media_content_type': 'app', 'media_content_id': kwargs.get('app')})}
    if action in controls:
        service, data = controls[action]
        return call_service(cfg, 'media_player', service, entity_id, data=data)
    return None


def press_key(cfg, remote, command):
    return call_service(cfg, 'remote', 'send_command', remote, data={'command': command})


def launch_activity(cfg, remote, activity):
    return call_service(cfg, 'remote', 'turn_on', remote, data={'activity': activity})


@contextmanager
def temporary_volume(cfg, entity_id, level):
    if not _writable(cfg, entity_id) or not entity_id.startswith('media_player.'):
        raise ValueError('Home Assistant volume denied: add the test media player to writable_entities')
    previous = (get_state(cfg, entity_id) or {}).get('attributes', {}).get('volume_level')
    if previous is None:
        raise ValueError('Home Assistant volume could not be saved: check the media player volume_level attribute')
    try:
        if control_media_player(cfg, entity_id, 'volume', volume=level) is None:
            raise ValueError('Home Assistant test volume could not be set: check the media player and volume level')
        yield
    finally:
        if control_media_player(cfg, entity_id, 'volume', volume=previous) is None:
            raise ValueError('Home Assistant volume could not be restored: check the media player and restore its volume')


def get_energy_usage(cfg, entity_id):
    """Gets the power usage in watts of a smart plug."""
    state = get_state(cfg, entity_id) or {}
    try:
        watts = float(state.get('state'))
    except (TypeError, ValueError):
        return None
    unit = state.get('attributes', {}).get('unit_of_measurement', 'W')
    if unit not in ('W', 'kW') or not math.isfinite(watts) or watts < 0:
        return None
    return watts * (1000 if unit == 'kW' else 1)


def get_temperature(cfg, entity_id):
    """Gets the temperature of a sensor."""
    state = get_state(cfg, entity_id)
    if state and 'state' in state:
        try:
            return float(state['state'])
        except (TypeError, ValueError):
            pass
    return None


def set_switch_state(cfg, entity_id, state):
    """Turns a switch on or off."""
    service = 'turn_on' if state else 'turn_off'
    return call_service(cfg, 'switch', service, entity_id, data={})


def send_notification(cfg, message):
    """Sends a notification to the explicitly configured mobile app service."""
    target = settings(cfg).get('notify_service', '')
    if isinstance(target, str) and target.startswith('mobile_app_') and SLUG.fullmatch(target):
        return _call_ha(cfg, 'POST', f'{API_SERVICES}/notify/{target}', data={'message': message})
    return None


def notify(cfg, message):
    """Announce a status without passing task text or logs to Home Assistant."""
    send_notification(cfg, message)
    ha = settings(cfg)
    speaker, engine = ha.get('tts_speaker'), ha.get('tts_engine')
    if _tts_allowed(cfg, engine, 'tts.') and _tts_allowed(cfg, speaker, 'media_player.'):
        _call_ha(cfg, 'POST', f'{API_SERVICES}/tts/speak',
                 data={'entity_id': engine, 'media_player_entity_id': speaker, 'message': message})


def _tts_allowed(cfg, entity, prefix):
    return _writable(cfg, entity) and entity.startswith(prefix)


def publish_status(cfg, snapshot):
    """Publish only aggregate work, usage and energy from an active dashboard."""
    entity = settings(cfg).get('status_entity')
    if not _writable(cfg, entity) or not entity.startswith('sensor.'):
        return None
    summary, models = snapshot['summary'], snapshot['local_models']
    usage = [a['used'] for a in snapshot['usage']['accounts'] if isinstance(a.get('used'), (float, int))]
    return _call_ha(cfg, 'POST', f'{API_STATE}/{entity}', data={
        'state': str(summary['running']),
        'attributes': {
            'stalled': summary['stalled'], 'queued': snapshot['queue']['queued'], 'peak_account_usage_percent': max(usage, default=0),
            'local_model_cost_this_month': round(sum(c['periods']['month']['cost'] for c in models['cards']), 3),
            'currency': models['tariff']['currency'],
        },
    })


def get_now_playing(cfg, entity_id):
    """Gets the now playing status of a media player."""
    state = get_state(cfg, entity_id)
    if state and 'attributes' in state:
        attr = state['attributes']
        return {
            'media_title': attr.get('media_title'),
            'media_artist': attr.get('media_artist'),
            'app_id': attr.get('app_id'),
            'app_name': attr.get('app_name'),
            'state': state.get('state')
        }
    return None


def any_present(cfg, entities):
    for entity_id in entities:
        state = get_state(cfg, entity_id)
        if state and state.get('state') in PRESENT_STATES:
            return True
    return False


def is_person_present(cfg):
    return any_present(cfg, settings(cfg).get('presence_entities') or ())


def machine_probe(cfg, name, probe):
    """Reserve the machine's slots while an HA presence sensor reports it occupied."""
    entities = (settings(cfg).get('machine_presence') or {}).get(name) or ()
    if probe.reachable and any_present(cfg, entities):
        return replace(probe, yielding=True)
    return probe


def _test_script(cfg, entity_id):
    if not _writable(cfg, entity_id) or not entity_id.startswith('script.'):
        return None
    return _call_ha(cfg, 'POST', f'{API_SERVICES}/script/{entity_id.removeprefix("script.")}', data={}, timeout=120)


def test_power_machine(cfg, track):
    spec = (settings(cfg).get('test_power') or {}).get(track) or {}
    return spec.get('machine')


def check_test_presence(cfg, machine, managed=False):
    name = config.physical(cfg, machine)
    entities = (settings(cfg).get('machine_presence') or {}).get(name, ())
    for entity in entities:
        state = (get_state(cfg, entity) or {}).get('state')
        if state in PRESENT_STATES:
            raise config.ConfigError(f'{name} is reserved by Home Assistant ({entity}): wait or choose another machine')
        if managed and state in (None, 'unknown', 'unavailable'):
            raise config.ConfigError(f'{name} power request blocked: restore Home Assistant presence sensor {entity} and retry')


@contextmanager
def test_power(cfg, track, machine):
    """Run owner-configured HA scripts around a test on its named machine."""
    if machine is None or test_power_machine(cfg, track) != machine:
        if settings(cfg).get('machine_presence') and machine is not None:
            check_test_presence(cfg, machine)
        yield
        return
    with _managed_test_power(cfg, track, machine):
        yield


@contextmanager
def _managed_test_power(cfg, track, machine):
    from . import transfer

    spec = settings(cfg)['test_power'][track]
    with transfer.machine_lock(cfg['machines'][machine], 'home-assistant-test-power'):
        check_test_presence(cfg, machine, managed=True)
        if _test_script(cfg, spec['start']) is None:
            raise config.ConfigError(
                f'home_assistant.test_power.{track}.start failed: check the HA script, writable_entities and plug state')
        try:
            yield
        finally:
            if _test_script(cfg, spec['stop']) is None:
                raise config.ConfigError(f'home_assistant.test_power.{track}.stop failed: check the HA script and writable_entities')


def _mapping_errors(ha, key, valid):
    entries = ha.get(key, {})
    if not isinstance(entries, dict) or not all(valid(v) for v in entries.values()):
        return [f'home_assistant.{key}: use a mapping with valid Home Assistant entities or power scripts']
    return []


def _entity_list(values):
    return isinstance(values, list) and all(isinstance(v, str) and ENTITY.fullmatch(v) for v in values)


def _power_spec(spec, machines, allowed):
    if not isinstance(spec, dict) or not isinstance(spec.get('machine'), str):
        return False
    machine = machines.get(spec['machine']) or {}
    if machine.get('transport') not in ('ssh', 'wsl'):
        return False
    scripts = [spec.get(key) for key in ('start', 'stop')]
    return all(isinstance(v, str) and v.startswith('script.') and v in allowed for v in scripts)


def errors(cfg):
    ha = cfg.get('home_assistant')
    if ha is None:
        return []
    if not isinstance(ha, dict):
        return ['home_assistant: use a mapping with url and token_file']
    allowed = ha.get('writable_entities', [])
    errs = [f'home_assistant.{k}: list individual entity IDs' for k in ('writable_entities', 'presence_entities')
            if not _entity_list(ha.get(k, []))]
    errs += _mapping_errors(ha, 'energy_entities', lambda v: isinstance(v, str) and bool(ENTITY.fullmatch(v)))
    errs += _mapping_errors(ha, 'machine_presence', _entity_list)
    errs += _mapping_errors(ha, 'test_power', lambda v: _power_spec(v, cfg['machines'], allowed if _entity_list(allowed) else []))
    status_entity = ha.get('status_entity')
    if status_entity is not None and (not _writable(cfg, status_entity) or not status_entity.startswith('sensor.')):
        errs.append('home_assistant.status_entity: add the sensor entity ID to writable_entities')
    return errs
