"""Easy setup of a found computer (t225): key, key login, ssh-copy-id only when needed (the password is typed into ssh, never
seen by Tutti), key login again, then the config; each failure names its step and the fix. No real machine is touched."""
import os
import tempfile
import unittest
from unittest import mock

from tutti import discovered, hostsetup


class Pure(unittest.TestCase):
    def test_names_entries_and_checks(self):
        self.assertEqual(hostsetup.name_for('192.0.2.10', set()), 'host-10')
        self.assertEqual(hostsetup.name_for('nas.example', {'nas'}), 'nas-2')
        self.assertEqual(hostsetup.entry('192.0.2.10', None), {'host': '192.0.2.10', 'slots': 1})
        self.assertEqual(hostsetup.entry('h.example', 'me'), {'host': 'me@h.example', 'slots': 1})   # every ssh call uses it
        for args, why in ((('-oProxyCommand=x', None, None, 22), 'not an address'), (('h', 'Bad Name', None, 22), '--name'),
                          (('h', None, '-l', 22), '--user'), (('h', None, None, 2222), 'Port 2222')):
            with self.assertRaisesRegex(hostsetup.SetupError, why):
                hostsetup.check(*args)

    def test_discovered_offers_the_command(self):
        self.assertEqual(discovered.setup_command({'kind': 'computer', 'address': '192.0.2.10'}),
                         'tutti machines add --host 192.0.2.10')
        self.assertEqual(discovered.setup_command({'kind': 'android', 'address': 'EMU1', 'name': 'Pixel_9'}),
                         'tutti devices add --name pixel-9 --serial EMU1')
        self.assertEqual(discovered.setup_command({'kind': 'media', 'address': '192.0.2.20'}), '')


class Setup(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.home = d.name
        self.calls, self.saved, self.said = [], [], []

    def runner(self, probes):
        probes = list(probes)

        def run(cmd, **_kw):
            self.calls.append(cmd[0])
            if cmd[0] == 'ssh':
                return mock.Mock(returncode=probes.pop(0))
            if cmd[0] == 'ssh-keygen':
                open(cmd[-1] + '.pub', 'w').close()
            return mock.Mock(returncode=0 if cmd[0] != 'ssh-copy-id' or self.copy_ok else 1)
        return run

    def go(self, probes, copy_ok=True, cfg=None, store=None):
        self.copy_ok = copy_ok
        return hostsetup.setup(cfg or {'machines': {}}, '192.0.2.10', user='me', run=self.runner(probes), home=self.home,
                               store=store or (lambda c, ch: self.saved.extend(ch)), say=self.said.append)

    def test_a_new_host_gets_a_key_the_copy_and_a_config_entry(self):
        self.assertEqual(self.go([255, 0]), 'host-10')
        self.assertEqual(self.calls, ['ssh-keygen', 'ssh', 'ssh-copy-id', 'ssh'])
        self.assertEqual(self.saved, [(['machines', 'host-10'], {'host': 'me@192.0.2.10', 'slots': 1})])
        self.assertTrue(any('Tutti does not see it' in s for s in self.said))

    def test_a_host_that_already_takes_the_key_is_only_saved(self):
        os.makedirs(os.path.join(self.home, '.ssh'))
        for f in ('id_rsa', 'id_rsa.pub'):
            open(os.path.join(self.home, '.ssh', f), 'w').close()
        self.go([0])
        self.assertEqual(self.calls, ['ssh'])

    def test_a_key_without_its_pub_is_kept_and_the_pub_derived(self):
        os.makedirs(os.path.join(self.home, '.ssh'))
        open(os.path.join(self.home, '.ssh', 'id_ed25519'), 'w').close()
        self.go([0])
        self.assertEqual(self.calls, ['ssh-keygen', 'ssh'])   # -y, never a new key over it
        self.assertTrue(os.path.exists(os.path.join(self.home, '.ssh', 'id_ed25519.pub')))

    def test_a_missing_tool_is_a_step_not_a_traceback(self):
        def run(cmd, **_kw):
            raise FileNotFoundError(cmd[0])
        with self.assertRaisesRegex(hostsetup.SetupError, 'step 1: ssh-keygen could not start'):
            hostsetup.setup({'machines': {}}, '192.0.2.1', run=run, home=self.home, store=lambda c, ch: None, say=self.said.append)

    def test_each_failure_names_its_step_and_saves_nothing(self):
        with self.assertRaisesRegex(hostsetup.SetupError, 'step 3: ssh-copy-id'):
            self.go([255], copy_ok=False)
        with self.assertRaisesRegex(hostsetup.SetupError, 'step 3: the key was sent'):
            self.go([255, 255])
        self.assertEqual(self.saved, [])

        def refuse(c, ch):
            raise OSError('read-only')
        with self.assertRaisesRegex(hostsetup.SetupError, 'step 4: .*read-only'):
            self.go([0], store=refuse)
        with self.assertRaisesRegex(hostsetup.SetupError, 'already in the config'):
            hostsetup.setup({'machines': {'nas': {}}}, '192.0.2.1', name='nas', run=self.runner([]), home=self.home)


if __name__ == '__main__':
    unittest.main()
