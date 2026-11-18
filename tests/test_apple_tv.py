import unittest
from unittest.mock import MagicMock
from tutti.ha_readings import get_ha_readings

class TestAppleTV(unittest.TestCase):
    def test_get_ha_readings_playing(self):
        client = MagicMock()
        client.get_state.return_value = {
            'state': 'playing',
            'attributes': {
                'media_title': 'My Show',
                'source': 'Plex',
                'volume_level': 0.5
            }
        }
        
        readings = get_ha_readings(client, 'media_player.apple_tv')
        
        self.assertEqual(readings['state'], 'busy')
        self.assertEqual(readings['media_title'], 'My Show')
        self.assertEqual(readings['source'], 'Plex')
        self.assertEqual(readings['volume'], 0.5)

    def test_get_ha_readings_idle(self):
        client = MagicMock()
        client.get_state.return_value = {
            'state': 'idle',
            'attributes': {}
        }
        
        readings = get_ha_readings(client, 'media_player.apple_tv')
        
        self.assertEqual(readings['state'], 'ready')

    def test_get_ha_readings_unavailable(self):
        client = MagicMock()
        client.get_state.return_value = {
            'state': 'unavailable',
            'attributes': {}
        }
        
        readings = get_ha_readings(client, 'media_player.apple_tv')
        
        self.assertEqual(readings['state'], 'offline')
