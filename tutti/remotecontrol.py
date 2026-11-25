"""Remote control, app launch, AirPlay/media casting, power and volume save/restore via Home Assistant."""
from . import devices, ha

COMMANDS = {
    'up': 'up',
    'down': 'down',
    'left': 'left',
    'right': 'right',
    'select': 'select',
    'menu': 'menu',
    'home': 'home',
    'top_menu': 'top_menu',
    'play_pause': 'play_pause',
    'volume_up': 'volume_up',
    'volume_down': 'volume_down',
    'suspend': 'suspend',
    'wakeup': 'wakeup',
}


def _ha_entity_for(cfg, device_name):
    device = devices.of(cfg, device_name)
    entity = device.get('ha_entity')
    if not entity:
        return None, f'Device {device_name} is not a Home Assistant device'
    if 'plex' in str(entity).lower():
        return None, f'Refused: {entity} is a protected Plex entity'
    return entity, ''


def send_remote_command(cfg, device_name, command, client=None):
    entity, err = _ha_entity_for(cfg, device_name)
    if err:
        return err
    ha_command = COMMANDS.get(command)
    if not ha_command:
        return f'Unknown command: {command} (use: {", ".join(sorted(COMMANDS))})'
    remote_entity = entity.replace('media_player.', 'remote.', 1)
    (client or ha.HAClient(cfg)).call_service('remote', 'send_command', {
        'entity_id': remote_entity,
        'command': [ha_command],
    })
    return f'Sent {ha_command} to {remote_entity}'


def select_source(cfg, device_name, source, client=None):
    entity, err = _ha_entity_for(cfg, device_name)
    if err:
        return err
    (client or ha.HAClient(cfg)).call_service('media_player', 'select_source', {
        'entity_id': entity,
        'source': source,
    })
    return f'Selected {source} on {entity}'


def play_media(cfg, device_name, media_url, media_type='video', client=None):
    entity, err = _ha_entity_for(cfg, device_name)
    if err:
        return err
    (client or ha.HAClient(cfg)).call_service('media_player', 'play_media', {
        'entity_id': entity,
        'media_content_id': media_url,
        'media_content_type': media_type,
    })
    return f'Playing media on {entity}'


def set_power(cfg, device_name, on, client=None):
    entity, err = _ha_entity_for(cfg, device_name)
    if err:
        return err
    service = 'turn_on' if on else 'turn_off'
    (client or ha.HAClient(cfg)).call_service('media_player', service, {'entity_id': entity})
    return f'Called {service} on {entity}'


def save_volume(client, entity_id):
    state = client.get_state(entity_id) or {}
    attr = state.get('attributes') or {}
    return {'volume_level': attr.get('volume_level'), 'is_volume_muted': attr.get('is_volume_muted')}


def restore_volume(client, entity_id, saved):
    if not saved:
        return
    if saved.get('volume_level') is not None:
        client.call_service('media_player', 'volume_set', {
            'entity_id': entity_id,
            'volume_level': saved['volume_level'],
        })
    if saved.get('is_volume_muted') is not None:
        client.call_service('media_player', 'volume_mute', {
            'entity_id': entity_id,
            'is_volume_muted': saved['is_volume_muted'],
        })
