import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock

from tutti import config, devices, ha
from tutti.ha_readings import discover_media_players, get_ha_readings


class _FakeResp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode('utf8')

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestAppleTV(unittest.TestCase):
    def test_ha_client_get_state_and_404(self):
        seen = []

        def opener(req, timeout=10):
            seen.append((req.get_method(), req.full_url, req.get_header('Authorization')))
            if req.full_url.endswith('/media_player.missing'):
                raise urllib.error.HTTPError(req.full_url, 404, 'Not Found', {}, io.BytesIO(b''))
            return _FakeResp({'state': 'idle', 'attributes': {'source': 'YouTube'}})

        cfg = {'home_assistant': {'url': 'http://ha.example:8123/'}}
        client = ha.HAClient(cfg, opener=opener, token='test-token')
        self.assertEqual(client.get_state('media_player.apple_tv')['state'], 'idle')
        self.assertIsNone(client.get_state('media_player.missing'))
        self.assertEqual(seen[0], ('GET', 'http://ha.example:8123/api/states/media_player.apple_tv', 'Bearer test-token'))

    def test_get_ha_readings_playing(self):
        client = MagicMock()
        client.get_state.return_value = {
            'state': 'playing',
            'attributes': {
                'media_title': 'My Show',
                'source': 'YouTube',
                'volume_level': 0.5,
                'is_volume_muted': False,
            },
        }
        readings = get_ha_readings(client, 'media_player.apple_tv')
        self.assertEqual(readings['state'], 'busy')
        self.assertEqual(readings['ha_state'], 'playing')
        self.assertEqual(readings['screen'], 1)
        self.assertEqual(readings['media_title'], 'My Show')
        self.assertEqual(readings['source'], 'YouTube')
        self.assertEqual(readings['volume'], 0.5)

    def test_get_ha_readings_idle_off_and_unavailable(self):
        client = MagicMock()
        client.get_state.return_value = {'state': 'idle', 'attributes': {}}
        self.assertEqual(get_ha_readings(client, 'media_player.apple_tv')['state'], 'ready')

        client.get_state.return_value = {'state': 'off', 'attributes': {}}
        off = get_ha_readings(client, 'media_player.apple_tv')
        self.assertEqual(off['state'], 'ready')
        self.assertEqual(off['screen'], 0)

        client.get_state.return_value = {'state': 'unavailable', 'attributes': {}}
        self.assertEqual(get_ha_readings(client, 'media_player.apple_tv')['state'], 'offline')

        client.get_state.return_value = None
        self.assertEqual(get_ha_readings(client, 'media_player.apple_tv')['state'], 'unreachable')

    def test_discover_media_players_skips_plex(self):
        client = MagicMock()
        client.list_states.return_value = [
            {'entity_id': 'media_player.apple_tv', 'state': 'idle',
             'attributes': {'friendly_name': 'Apple TV', 'source_list': ['YouTube', 'Netflix']}},
            {'entity_id': 'media_player.plex_living_room', 'state': 'idle', 'attributes': {}},
            {'entity_id': 'light.kitchen', 'state': 'on', 'attributes': {}},
        ]
        found = discover_media_players(client)
        self.assertEqual([f['entity_id'] for f in found], ['media_player.apple_tv'])

    def test_device_state_for_ha_tv(self):
        dev = {'name': 'atv', 'kind': 'tv', 'ha_entity': 'media_player.apple_tv', 'machine': 'pc'}
        st = devices.device_state(dev, {'state': 'busy', 'ha_state': 'playing', 'screen': 1, 'media_title': 'Demo'})
        self.assertEqual(st['status'], 'busy')
        self.assertEqual(st['media_title'], 'Demo')
        self.assertEqual(st['ha_state'], 'playing')

        off = devices.device_state(dev, {'state': 'offline', 'ha_state': 'unavailable', 'screen': 0})
        self.assertEqual(off['status'], 'offline')

    def test_ha_config_errors(self):
        self.assertEqual(config._ha_errors({'home_assistant': {'url': 'http://ha.example:8123'}}), [])
        self.assertTrue(config._ha_errors({'home_assistant': 'bad'}))
        self.assertTrue(config._ha_errors({'home_assistant': {'url': 'ha.example:8123'}}))
