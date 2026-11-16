import unittest
from unittest import mock

from tutti import fleet


def cfg():
    return {'accounts': {'claude': {}, 'cursor': {}, 'agy-gemini': {}}}


class OverflowWithOnePanicTest(unittest.TestCase):
    """One account in panic must not idle the others that have room (claude at 98 %, 2026-11-17)."""

    def overflow(self, states):
        with mock.patch('tutti.autopilot.ladder', return_value=['claude', 'cursor', 'agy-gemini']), \
                mock.patch('tutti.autopilot.trigger_accounts', return_value=[]):
            return fleet.overflow(cfg(), states, {}, [])

    def test_one_account_in_panic_spares_nobody(self):
        self.assertEqual(self.overflow({'claude': {'verdict': 'panic', 'panic': True}, 'cursor': {'verdict': 'behind'},
                                        'agy-gemini': {'verdict': 'behind'}}), '')

    def test_some_ahead_others_with_room_spares_nobody(self):
        self.assertEqual(self.overflow({'claude': {'verdict': 'panic'}, 'cursor': {'verdict': 'ahead'},
                                        'agy-gemini': {'verdict': 'behind'}}), '')

    def test_every_account_ahead_or_in_panic_spares_them(self):
        self.assertIn('ahead of pace', self.overflow({'claude': {'verdict': 'panic'}, 'cursor': {'verdict': 'ahead'},
                                                      'agy-gemini': {'verdict': 'spent'}}))


if __name__ == '__main__':
    unittest.main()
