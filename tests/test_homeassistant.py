import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, call, patch

from tutti import config, homeassistant, quietmode, routing


class TestHomeAssistant(unittest.TestCase):
    def setUp(self):
        self.cfg = {'home_assistant': {'url': 'http://ha.example', 'token_file': 'fake_token', 'writable_entities': ['light.test']}}

    @patch('tutti.homeassistant.open', create=True)
    @patch('tutti.homeassistant._open')
    def test_get_state(self, mock_urlopen, mock_open):
        mock_open.return_value.__enter__.return_value.read.return_value = 'fake_token'
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"entity_id": "light.test", "state": "on"}'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        state = homeassistant.get_state(self.cfg, 'light.test')
        self.assertEqual(state['state'], 'on')
        self.assertTrue(mock_urlopen.called)

    @patch('tutti.homeassistant.open', create=True)
    @patch('tutti.homeassistant._open')
    def test_call_service(self, mock_urlopen, mock_open):
        mock_open.return_value.__enter__.return_value.read.return_value = 'fake_token'
        mock_response = MagicMock()
        mock_response.read.return_value = b'[]'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        homeassistant.call_service(self.cfg, 'light', 'turn_on', 'light.test')
        self.assertTrue(mock_urlopen.called)
        _, kwargs = mock_urlopen.call_args
        self.assertEqual(kwargs['data'], b'{"entity_id": "light.test"}')

    @patch('tutti.homeassistant._open')
    def test_call_service_refuses_unlisted_entity(self, mock_urlopen):
        self.assertIsNone(homeassistant.call_service(self.cfg, 'media_player', 'turn_off', 'media_player.plex_other'))
        self.assertFalse(mock_urlopen.called)

    @patch('tutti.homeassistant._call_ha', return_value=[])
    def test_payload_cannot_replace_or_expand_its_target(self, request):
        homeassistant.call_service(self.cfg, 'light', 'turn_on', 'light.test',
                                   data={'entity_id': 'light.other', 'area_id': 'all', 'brightness': 30})
        request.assert_called_once_with(self.cfg, 'POST', 'api/services/light/turn_on',
                                        data={'entity_id': 'light.test', 'brightness': 30})

    @patch('tutti.homeassistant._call_ha', return_value=[])
    def test_tts_requires_both_targets_and_cannot_be_called_as_a_generic_service(self, request):
        ha = self.cfg['home_assistant']
        ha.update(tts_engine='tts.example', tts_speaker='media_player.speaker')
        ha['writable_entities'].append('tts.example')
        homeassistant.call_service(self.cfg, 'tts', 'speak', 'tts.example', {'media_player_entity_id': 'media_player.speaker'})
        homeassistant.notify(self.cfg, 'Finished')
        request.assert_not_called()
        ha['writable_entities'].append('media_player.speaker')
        homeassistant.notify(self.cfg, 'Finished')
        request.assert_called_once_with(self.cfg, 'POST', 'api/services/tts/speak',
                                        data={'entity_id': 'tts.example', 'media_player_entity_id': 'media_player.speaker',
                                              'message': 'Finished'})

    @patch('tutti.homeassistant._call_ha')
    def test_volume_is_restored_when_setting_the_test_level_times_out(self, request):
        player = 'media_player.test'
        self.cfg['home_assistant']['writable_entities'].append(player)
        request.side_effect = [{'attributes': {'volume_level': 0.2}}, None, []]
        with self.assertRaisesRegex(ValueError, 'could not be set'), homeassistant.temporary_volume(self.cfg, player, 0.5):
            self.fail('the volume was never confirmed')
        request.assert_called_with(self.cfg, 'POST', 'api/services/media_player/volume_set',
                                   data={'entity_id': player, 'volume_level': 0.2})

    @patch('tutti.homeassistant.get_state')
    def test_presence_drives_quiet_mode_and_machine_availability(self, read):
        self.cfg['home_assistant'].update(presence_entities=['person.example'], machine_presence={'test': ['binary_sensor.room']})
        probe = routing.Probe(reachable=True, free=2)
        read.side_effect = lambda cfg, entity: {'state': 'home' if entity.startswith('person.') else 'on'}
        self.assertTrue(quietmode.status(self.cfg, idle_fn=lambda: None, mem_fn=lambda: None)['quiet'])
        self.assertTrue(homeassistant.machine_probe(self.cfg, 'test', probe).yielding)
        read.side_effect = None
        read.return_value = None
        self.assertIs(homeassistant.machine_probe(self.cfg, 'test', probe), probe)
        self.assertFalse(quietmode.status(self.cfg, idle_fn=lambda: None, mem_fn=lambda: None)['quiet'])

    @patch('tutti.homeassistant._call_ha', return_value=[])
    def test_power_cleanup_runs_when_the_test_raises(self, request):
        ha = self.cfg['home_assistant']
        self.cfg['machines'] = {'builder': {'host': 'builder.example', 'transport': 'ssh'}}
        ha['test_power'] = {'test': {'machine': 'builder', 'start': 'script.test_start', 'stop': 'script.test_stop'}}
        ha['writable_entities'] += ['script.test_start', 'script.test_stop']
        with patch('tutti.transfer.machine_lock', return_value=nullcontext()), self.assertRaisesRegex(RuntimeError, 'test failed'):
            with homeassistant.test_power(self.cfg, 'test', 'builder'):
                raise RuntimeError('test failed')
        self.assertEqual(request.call_args_list, [
            call(self.cfg, 'POST', 'api/services/script/test_start', data={}, timeout=120),
            call(self.cfg, 'POST', 'api/services/script/test_stop', data={}, timeout=120),
        ])
        ha['writable_entities'].remove('script.test_start')
        request.reset_mock()
        with patch('tutti.transfer.machine_lock', return_value=nullcontext()), self.assertRaisesRegex(config.ConfigError, 'start failed'):
            with homeassistant.test_power(self.cfg, 'test', 'builder'):
                self.fail('unlisted start script ran the test')
        request.assert_not_called()

    @patch('tutti.homeassistant._test_script')
    @patch('tutti.homeassistant.get_state', return_value={'state': 'unavailable'})
    def test_unavailable_presence_sensor_blocks_managed_power(self, _read, script):
        self.cfg['machines'] = {'builder': {}}
        self.cfg['home_assistant'].update(
            machine_presence={'builder': ['binary_sensor.room']},
            test_power={'test': {'machine': 'builder', 'start': 'script.start', 'stop': 'script.stop'}},
        )
        with patch('tutti.transfer.machine_lock', return_value=nullcontext()), self.assertRaisesRegex(config.ConfigError, 'power request'):
            with homeassistant.test_power(self.cfg, 'test', 'builder'):
                self.fail('the test ran with unknown occupancy')
        script.assert_not_called()


if __name__ == '__main__':
    unittest.main()
