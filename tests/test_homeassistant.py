import unittest
from unittest.mock import patch, MagicMock
from tutti import homeassistant

class TestHomeAssistant(unittest.TestCase):
    def setUp(self):
        self.cfg = {'home_assistant': {'url': 'http://ha.example', 'token_file': 'fake_token'}}
        
    @patch('tutti.homeassistant.open', create=True)
    @patch('urllib.request.urlopen')
    def test_get_state(self, mock_urlopen, mock_open):
        mock_open.return_value.__enter__.return_value.read.return_value = 'fake_token'
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"entity_id": "light.test", "state": "on"}'
        mock_urlopen.return_value.__enter__.return_value = mock_response
        
        state = homeassistant.get_state(self.cfg, 'light.test')
        self.assertEqual(state['state'], 'on')
        self.assertTrue(mock_urlopen.called)

    @patch('tutti.homeassistant.open', create=True)
    @patch('urllib.request.urlopen')
    def test_call_service(self, mock_urlopen, mock_open):
        mock_open.return_value.__enter__.return_value.read.return_value = 'fake_token'
        mock_response = MagicMock()
        mock_response.read.return_value = b'[]'
        mock_urlopen.return_value.__enter__.return_value = mock_response
        
        homeassistant.call_service(self.cfg, 'light', 'turn_on', 'light.test')
        self.assertTrue(mock_urlopen.called)
        args, kwargs = mock_urlopen.call_args
        self.assertEqual(kwargs['data'], b'{"entity_id": "light.test"}')

if __name__ == '__main__':
    unittest.main()
