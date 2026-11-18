import unittest
from unittest.mock import patch, MagicMock
from tutti import homeassistant

class TestHomeAssistantNew(unittest.TestCase):
    def setUp(self):
        self.cfg = {'home_assistant': {'url': 'http://ha.example', 'token_file': 'fake_token'}}

    @patch('tutti.homeassistant.call_service')
    def test_control_media_player_power(self, mock_call_service):
        homeassistant.control_media_player(self.cfg, 'media_player.apple_tv', 'turn_on')
        mock_call_service.assert_called_with(self.cfg, 'media_player', 'turn_on', 'media_player.apple_tv', data={})

    @patch('tutti.homeassistant.call_service')
    def test_control_media_player_volume(self, mock_call_service):
        homeassistant.control_media_player(self.cfg, 'media_player.apple_tv', 'volume', volume=0.5)
        mock_call_service.assert_called_with(self.cfg, 'media_player', 'volume_set', 'media_player.apple_tv', data={'volume_level': 0.5})

    @patch('tutti.homeassistant.get_state')
    def test_get_energy_usage(self, mock_get_state):
        mock_get_state.return_value = {'state': '15.5'}
        usage = homeassistant.get_energy_usage(self.cfg, 'sensor.plug_power')
        self.assertEqual(usage, 15.5)

    @patch('tutti.homeassistant.get_state')
    def test_get_temperature(self, mock_get_state):
        mock_get_state.return_value = {'state': '22.5'}
        temp = homeassistant.get_temperature(self.cfg, 'sensor.room_temp')
        self.assertEqual(temp, 22.5)

    @patch('tutti.homeassistant.call_service')
    def test_set_switch_state(self, mock_call_service):
        homeassistant.set_switch_state(self.cfg, 'switch.plug', True)
        mock_call_service.assert_called_with(self.cfg, 'switch', 'turn_on', 'switch.plug', data={})

    @patch('tutti.homeassistant.call_service')
    def test_send_notification(self, mock_call_service):
        homeassistant.send_notification(self.cfg, 'Test message')
        mock_call_service.assert_called_with(self.cfg, 'notify', 'notify', 'persistent_notification', data={'message': 'Test message'})

if __name__ == '__main__':
    unittest.main()
