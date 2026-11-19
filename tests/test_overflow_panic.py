"""Overflow decisions do not let one account suppress other eligible accounts."""
import unittest
from unittest import mock

from tutti import fleet


class OverflowPanicTest(unittest.TestCase):
    def overflow(self, states, rooms=None, triggers=()):
        cfg = {'accounts': {'alpha': {}, 'beta': {}, 'gamma': {}}}
        with mock.patch('tutti.autopilot.ladder', return_value=['alpha', 'beta', 'gamma']), \
                mock.patch('tutti.autopilot.trigger_accounts', return_value=list(triggers)):
            return fleet.overflow(cfg, states, rooms or {}, [])

    def test_one_panicked_account_does_not_spare_the_others(self):
        states = {'alpha': {'verdict': 'panic', 'panic': True}, 'beta': {'verdict': 'behind'}, 'gamma': {'verdict': 'behind'}}
        self.assertEqual(self.overflow(states, {'beta': (1, '')}, ['beta']), '')

    def test_all_ahead_or_unavailable_spares_online_accounts(self):
        states = {'alpha': {'verdict': 'panic'}, 'beta': {'verdict': 'ahead'}, 'gamma': {'verdict': 'spent'}}
        self.assertIn('ahead of pace', self.overflow(states))

    def test_no_trigger_room_still_spares_online_accounts(self):
        states = {'alpha': {'verdict': 'panic'}, 'beta': {'verdict': 'behind'}, 'gamma': {'verdict': 'behind'}}
        self.assertIn('no online account has room', self.overflow(states, {'beta': (0, '')}, ['beta']))
