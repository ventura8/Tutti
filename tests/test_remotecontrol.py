import unittest
from unittest.mock import MagicMock
from tutti.remotecontrol import send_remote_command

class TestRemoteControl(unittest.TestCase):
    def test_send_remote_command(self):
        cfg = {
            'devices': {
                'atv': {
                    'ha_entity': 'media_player.apple_tv'
                }
            }
        }
        client = MagicMock()
        
        # We need to mock ha.HAClient to return our client
        import tutti.ha
        tutti.ha.HAClient = MagicMock(return_value=client)
        
        result = send_remote_command(cfg, 'atv', 'select')
        
        self.assertEqual(result, "Sent select to remote.apple_tv")
        client.call_service.assert_called_once_with('remote', 'send_command', {
            'entity_id': 'remote.apple_tv',
            'command': ['select']
        })
