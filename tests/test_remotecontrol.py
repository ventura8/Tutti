import types
import unittest
from unittest.mock import MagicMock, patch

from tutti import devicescmd, remotecontrol


class TestRemoteControl(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            'devices': {
                'atv': {'kind': 'tv', 'ha_entity': 'media_player.apple_tv'},
                'phone': {'kind': 'phone', 'serial': 'ABC123'},
                'plex_tv': {'kind': 'tv', 'ha_entity': 'media_player.plex_tv'},
            }
        }

    def test_send_remote_command_good_and_bad(self):
        client = MagicMock()
        result = remotecontrol.send_remote_command(self.cfg, 'atv', 'select', client=client)
        self.assertEqual(result, 'Sent select to remote.apple_tv')
        client.call_service.assert_called_once_with('remote', 'send_command', {
            'entity_id': 'remote.apple_tv',
            'command': ['select'],
        })

        self.assertIn('Unknown command', remotecontrol.send_remote_command(self.cfg, 'atv', 'bogus', client=client))
        self.assertIn('not a Home Assistant', remotecontrol.send_remote_command(self.cfg, 'phone', 'select', client=client))
        self.assertIn('Refused', remotecontrol.send_remote_command(self.cfg, 'plex_tv', 'select', client=client))

    def test_select_source_play_media_and_power(self):
        client = MagicMock()
        remotecontrol.select_source(self.cfg, 'atv', 'YouTube', client=client)
        client.call_service.assert_called_with('media_player', 'select_source', {
            'entity_id': 'media_player.apple_tv',
            'source': 'YouTube',
        })

        remotecontrol.play_media(self.cfg, 'atv', 'http://media.example/video.mp4', client=client)
        client.call_service.assert_called_with('media_player', 'play_media', {
            'entity_id': 'media_player.apple_tv',
            'media_content_id': 'http://media.example/video.mp4',
            'media_content_type': 'video',
        })

        remotecontrol.set_power(self.cfg, 'atv', False, client=client)
        client.call_service.assert_called_with('media_player', 'turn_off', {'entity_id': 'media_player.apple_tv'})

    def test_save_and_restore_volume(self):
        client = MagicMock()
        client.get_state.return_value = {'attributes': {'volume_level': 0.35, 'is_volume_muted': True}}
        saved = remotecontrol.save_volume(client, 'media_player.apple_tv')
        self.assertEqual(saved, {'volume_level': 0.35, 'is_volume_muted': True})

        remotecontrol.restore_volume(client, 'media_player.apple_tv', saved)
        self.assertEqual(client.call_service.call_count, 2)

    def test_cmd_remote_cli(self):
        self.assertEqual(devicescmd.cmd_remote(self.cfg, types.SimpleNamespace(names=['atv'])), 2)
        with patch('tutti.remotecontrol.send_remote_command', return_value='Sent home to remote.apple_tv'):
            self.assertEqual(devicescmd.cmd_remote(self.cfg, types.SimpleNamespace(names=['atv', 'home'])), 0)
