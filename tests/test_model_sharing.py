import unittest
from unittest import mock
from tutti import config, model_sharing

class TestModelSharing(unittest.TestCase):
    def setUp(self):
        self.cfg = {'project': {'state': '~/.local/state/tutti'}, 'sharing': {}, 'accounts': {}}

    def test_is_sharing_enabled_default_new_install(self):
        # Default new install (no sharing configured, no accounts)
        cfg = self.cfg.copy()
        # Mock owner_of to return no accounts
        with mock.patch('tutti.owner.owner_of', return_value={'accounts': []}):
            self.assertFalse(model_sharing.is_sharing_enabled(cfg, 'local'))
            self.assertFalse(model_sharing.is_sharing_enabled(cfg, 'online'))

    def test_is_sharing_enabled_default_owner(self):
        # Owner installation (accounts present)
        cfg = self.cfg.copy()
        cfg['accounts'] = {'me': {}}
        # Mock owner_of to return accounts
        with mock.patch('tutti.owner.owner_of', return_value={'accounts': ['me']}):
            self.assertTrue(model_sharing.is_sharing_enabled(cfg, 'local'))
            self.assertTrue(model_sharing.is_sharing_enabled(cfg, 'online'))

    def test_is_sharing_enabled_explicit_off(self):
        # Explicitly disabled
        cfg = self.cfg.copy()
        cfg['sharing'] = {'local': False, 'online': False}
        cfg['accounts'] = {'me': {}}
        self.assertFalse(model_sharing.is_sharing_enabled(cfg, 'local'))
        self.assertFalse(model_sharing.is_sharing_enabled(cfg, 'online'))

    def test_is_sharing_enabled_explicit_on(self):
        # Explicitly enabled
        cfg = self.cfg.copy()
        cfg['sharing'] = {'local': True, 'online': True}
        # Even if not owner, explicit true should hold
        self.assertTrue(model_sharing.is_sharing_enabled(cfg, 'local'))
        self.assertTrue(model_sharing.is_sharing_enabled(cfg, 'online'))
