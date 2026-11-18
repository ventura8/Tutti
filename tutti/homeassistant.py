"""Home Assistant connector: device control, presence, energy, and notifications."""

import json
import os
import time
import urllib.request
import urllib.error
from . import config

# HA API endpoints
API_STATE = 'api/states'
API_SERVICES = 'api/services'

# Cache for presence check
_presence_cache = {'time': 0, 'result': False}
PRESENCE_CACHE_SECONDS = 60

def _get_token(cfg):
    """Reads the HA token from the configured file (default: ~/.config/tutti/secrets/home-assistant.token)."""
    token_path = os.path.expanduser(config.expand((cfg.get('home_assistant') or {}).get('token_file', '~/.config/tutti/secrets/home-assistant.token')))
    try:
        with open(token_path, encoding='utf8') as f:
            return f.read().strip()
    except OSError:
        return ''

def _call_ha(cfg, method, endpoint, data=None):
    """Sends a request to Home Assistant."""
    url = (cfg.get('home_assistant') or {}).get('url', '').rstrip('/')
    if not url:
        return None
    token = _get_token(cfg)
    if not token:
        return None

    req = urllib.request.Request(f'{url}/{endpoint}', method=method)
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Content-Type', 'application/json')
    
    body = json.dumps(data).encode('utf8') if data else None
    
    try:
        with urllib.request.urlopen(req, data=body, timeout=2) as res:
            return json.loads(res.read().decode('utf8'))
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None

def get_state(cfg, entity_id):
    """Gets the state of a Home Assistant entity."""
    return _call_ha(cfg, 'GET', f'{API_STATE}/{entity_id}')

def call_service(cfg, domain, service, entity_id, data=None):
    """Calls a service on a Home Assistant entity."""
    payload = {'entity_id': entity_id}
    if data:
        payload.update(data)
    return _call_ha(cfg, 'POST', f'{API_SERVICES}/{domain}/{service}', data=payload)

def control_media_player(cfg, entity_id, action, **kwargs):
    """Controls a media player in Home Assistant."""
    if action == 'turn_on':
        return call_service(cfg, 'media_player', 'turn_on', entity_id, data={})
    elif action == 'turn_off':
        return call_service(cfg, 'media_player', 'turn_off', entity_id, data={})
    elif action == 'volume':
        return call_service(cfg, 'media_player', 'volume_set', entity_id, data={'volume_level': kwargs.get('volume')})
    elif action == 'play':
        return call_service(cfg, 'media_player', 'media_play', entity_id, data={})
    elif action == 'pause':
        return call_service(cfg, 'media_player', 'media_pause', entity_id, data={})
    return None

def get_energy_usage(cfg, entity_id):
    """Gets the power usage in watts of a smart plug."""
    state = get_state(cfg, entity_id)
    if state and 'state' in state:
        try:
            return float(state['state'])
        except ValueError:
            pass
    return None

def get_temperature(cfg, entity_id):
    """Gets the temperature of a sensor."""
    state = get_state(cfg, entity_id)
    if state and 'state' in state:
        try:
            return float(state['state'])
        except ValueError:
            pass
    return None

def set_switch_state(cfg, entity_id, state):
    """Turns a switch on or off."""
    service = 'turn_on' if state else 'turn_off'
    return call_service(cfg, 'switch', service, entity_id, data={})

def send_notification(cfg, message):
    """Sends a notification to Home Assistant."""
    return call_service(cfg, 'notify', 'notify', 'persistent_notification', data={'message': message})

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

def is_person_present(cfg):
    """Checks if any configured presence entity is 'home' or 'on', with caching."""
    now = time.time()
    if now - _presence_cache['time'] < PRESENCE_CACHE_SECONDS:
        return _presence_cache['result']
        
    entities = (cfg.get('home_assistant') or {}).get('presence_entities', [])
    present = False
    for entity_id in entities:
        state = get_state(cfg, entity_id)
        if state and state.get('state') in ('home', 'on', 'occupied'):
            present = True
            break
    
    _presence_cache['time'] = now
    _presence_cache['result'] = present
    return present
