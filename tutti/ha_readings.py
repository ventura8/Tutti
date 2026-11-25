"""Map Home Assistant media_player states to Tutti device readings and discover TV/media targets."""

OFFLINE_STATES = ('unavailable', 'unknown')
SCREEN_OFF_STATES = ('off', 'standby', 'unavailable', 'unknown')


def _is_blocked_entity(entity_id):
    return 'plex' in str(entity_id or '').lower()


def _tutti_status(ha_state):
    if ha_state in OFFLINE_STATES:
        return 'offline'
    if ha_state == 'playing':
        return 'busy'
    return 'ready'


def get_ha_readings(client, entity_id):
    """Read power, screen, now-playing and volume from a Home Assistant media_player entity."""
    if _is_blocked_entity(entity_id):
        return {'state': 'offline', 'ha_state': 'blocked', 'screen': 0, 'adb': 'ok'}
    state = client.get_state(entity_id)
    if not state:
        return {'state': 'unreachable', 'ha_state': 'unavailable', 'screen': 0, 'adb': 'ok'}

    ha_state = str(state.get('state') or 'unavailable')
    attr = state.get('attributes') or {}
    return {
        'state': _tutti_status(ha_state),
        'ha_state': ha_state,
        'screen': 0 if ha_state in SCREEN_OFF_STATES else 1,
        'media_title': attr.get('media_title'),
        'source': attr.get('source'),
        'source_list': attr.get('source_list'),
        'volume': attr.get('volume_level'),
        'muted': attr.get('is_volume_muted'),
        'adb': 'ok',
    }


def discover_media_players(client):
    """Return discoverable Home Assistant media_player entities, excluding blocked entities like Plex."""
    found = []
    for item in client.list_states():
        entity_id = str(item.get('entity_id') or '')
        if not entity_id.startswith('media_player.') or _is_blocked_entity(entity_id):
            continue
        attr = item.get('attributes') or {}
        found.append({
            'entity_id': entity_id,
            'name': attr.get('friendly_name') or entity_id.split('.', 1)[1],
            'state': item.get('state'),
            'source_list': attr.get('source_list') or [],
        })
    return found
