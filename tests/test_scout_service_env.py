"""The scout under a service manager (2026-11-22: it stopped after every dashboard restart). The dashboard passes its own
environment, which has no XDG_CONFIG_HOME under systemd and had its secrets scrubbed at start: the secret file must still be
read. A scout that cannot run says why instead of being skipped in silence."""
import os
import tempfile
import unittest
from unittest import mock

from tutti import scouts, secretenv


class ServiceEnv(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        os.makedirs(os.path.join(d.name, 'tutti'))
        with open(os.path.join(d.name, 'tutti', 'grok.env'), 'w') as f:
            f.write('TUTTI_TEST_URL=https://example.invalid/hook\n')
        self.home = d.name

    def test_the_process_environment_still_reads_the_secret_file(self):
        fake_environ = {'HOME': self.home}   # what a systemd user service has: no XDG_CONFIG_HOME, no secrets
        with mock.patch.object(os, 'environ', fake_environ), mock.patch('tutti.config.xdg_tutti_dir',
                                                                       return_value=os.path.join(self.home, 'tutti')):
            self.assertEqual(secretenv.get('TUTTI_TEST_URL', os.environ), 'https://example.invalid/hook')
        self.assertEqual(secretenv.get('TUTTI_TEST_URL', {'HOME': self.home}), '')   # a test's own dict: only itself

    def test_a_scout_that_cannot_run_says_why(self):
        cfg = {'scouts': {'grok': {}}}
        sc = {'name': 'grok', 'auto': True, 'url_env': 'TUTTI_NOPE_URL', 'key_env': 'TUTTI_NOPE_KEY'}
        with mock.patch.object(scouts, 'settings', return_value=sc):
            with self.assertRaisesRegex(scouts.ScoutError, 'scout grok cannot run: TUTTI_NOPE_URL, TUTTI_NOPE_KEY'):
                scouts.tick(cfg, {}, env={})
            sc['auto'] = False
            self.assertEqual(scouts.tick(cfg, {}, env={}), [])   # auto off: nothing to say


if __name__ == '__main__':
    unittest.main()
