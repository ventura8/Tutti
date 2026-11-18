from . import ha, devices

def send_remote_command(cfg, device_name, command):
    device = devices.of(cfg, device_name)
    ha_entity = device.get('ha_entity')
    if not ha_entity:
        return f"Device {device_name} is not a Home Assistant device"
    
    # Map Tutti remote command to HA service
    # Apple TV in HA:
    # entity_id: media_player.apple_tv -> remote.apple_tv
    # remote.apple_tv -> send_command
    
    remote_entity = ha_entity.replace('media_player.', 'remote.')
    
    client = ha.HAClient(cfg)
    
    # Map common commands
    command_map = {
        'up': 'up',
        'down': 'down',
        'left': 'left',
        'right': 'right',
        'select': 'select',
        'menu': 'menu',
        'home': 'home',
        'play_pause': 'play_pause',
        'volume_up': 'volume_up',
        'volume_down': 'volume_down'
    }
    
    ha_command = command_map.get(command)
    if not ha_command:
        return f"Unknown command: {command}"
    
    client.call_service('remote', 'send_command', {
        'entity_id': remote_entity,
        'command': [ha_command]
    })
    
    return f"Sent {ha_command} to {remote_entity}"
