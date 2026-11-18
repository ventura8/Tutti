
def get_ha_readings(client, entity_id):
    state = client.get_state(entity_id)
    if not state:
        return {'state': 'unreachable'}

    attr = state.get('attributes', {})

    # Map HA state to Tutti device readings
    # 'state' in Tutti is ready/waiting/busy/offline
    # HA states: playing/paused/idle/off/unavailable

    ha_state = state.get('state')
    tutti_status = 'ready'
    if ha_state == 'unavailable':
        tutti_status = 'offline'
    elif ha_state == 'playing':
        tutti_status = 'busy'
    elif ha_state == 'off':
        tutti_status = 'ready'
    elif ha_state == 'idle':
        tutti_status = 'ready'

    return {
        'state': tutti_status,
        'media_title': attr.get('media_title'),
        'source': attr.get('source'),
        'volume': attr.get('volume_level'),
        'adb': 'ok' # To pass the check in readings_with_revive
    }
