"""Unit tests for automatic host setup (`tutti connect <address>` and dashboard setup_host)."""
import io
import json
import os
import unittest
from unittest import mock

import tmpdirs

from tutti import connect, dashboard, hostsetup


def _ok(stdout=''):
    return mock.Mock(returncode=0, stdout=stdout, stderr='')


def _fail(rc=1):
    return mock.Mock(returncode=rc, stdout='', stderr='failed')


class HostSetupTest(unittest.TestCase):
    def setUp(self):
        self.home = tmpdirs.mkdtemp()
        self.script = os.path.join(self.home, 'install.sh')
        with open(self.script, 'w', encoding='utf8') as f:
            f.write('#!/usr/bin/env bash\necho ok\n')

    def _make_key(self):
        ssh_dir = os.path.join(self.home, '.ssh')
        os.makedirs(ssh_dir, exist_ok=True)
        for name in ('id_ed25519', 'id_ed25519.pub'):
            with open(os.path.join(ssh_dir, name), 'w', encoding='utf8') as f:
                f.write('stub\n')

    def test_is_address_and_parse(self):
        self.assertTrue(hostsetup.is_address('alice@box.example'))
        self.assertTrue(hostsetup.is_address('alice@box.example:2222'))
        self.assertTrue(hostsetup.is_address('192.0.2.10'))
        self.assertTrue(hostsetup.is_address('adb:device123'))
        self.assertFalse(hostsetup.is_address('claude-code'))
        self.assertFalse(hostsetup.is_address(''))
        self.assertEqual(hostsetup.parse_address('alice@box.example:2222'),
                         {'kind': 'ssh', 'target': 'alice@box.example', 'port': '2222'})
        self.assertEqual(hostsetup.parse_address('adb:serial1'), {'kind': 'adb', 'serial': 'serial1'})

    def test_setup_host_happy_path_generates_key_copies_installs_and_detects_services(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            if cmd[0] == 'ssh-keygen':
                self._make_key()
                return _ok()
            if cmd[0] == 'ssh' and 'docker ps' in cmd[-1]:
                return _ok('pihole\nflaresolverr\n')
            return _ok()

        r = hostsetup.setup_host(
            'alice@box.example', password='one-time-secret', home=self.home,
            run=fake_run, which=lambda b: f'/usr/bin/{b}', script_path=self.script,
        )
        self.assertEqual(r['status'], 'connected')
        self.assertEqual(r['services'], ['pihole', 'flaresolverr'])
        self.assertIn('pihole', r['note'])
        self.assertNotIn('one-time-secret', json.dumps(r))
        self.assertEqual(calls[0][0][0], 'ssh-keygen')
        self.assertEqual(calls[1][0][:2], ['sshpass', '-e'])
        self.assertEqual(calls[1][1]['env']['SSHPASS'], 'one-time-secret')
        self.assertEqual(calls[2][0][-2:], ['bash', '-s'])

    def test_setup_host_bad_address_and_ssh_failure(self):
        bad = hostsetup.setup_host('not-an-address', home=self.home)
        self.assertEqual(bad['status'], 'error')
        self._make_key()
        r = hostsetup.setup_host('alice@box.example', home=self.home, run=lambda cmd, **kw: _fail(), script_path=self.script)
        self.assertEqual(r['status'], 'error')
        self.assertIn('SSH key install failed', r['note'])

    def test_connect_cmd_routes_address_to_hostsetup(self):
        out = io.StringIO()
        fake = {'agent': 'alice@box.example', 'status': 'connected', 'changed': [], 'note': 'ok'}
        args = mock.Mock(repo=None, agent='alice@box.example', all=False, status=False, test=False, undo=False, json=False)
        with mock.patch.object(hostsetup, 'setup_host', return_value=fake) as m:
            rc = connect.cmd({'_path': '/cfg/tutti.config.yaml'}, args, out)
        self.assertEqual(rc, 0)
        m.assert_called_once_with('alice@box.example')
        self.assertIn('alice@box.example: connected', out.getvalue())

    def test_dashboard_save_connect_setup_host(self):
        collector = mock.Mock(cfg={'_path': '/cfg/tutti.config.yaml'})
        code, body = dashboard.save_connect(collector, json.dumps({'action': 'setup_host', 'address': 'bad'}).encode())
        self.assertEqual(code, 400)
        fake = {'agent': 'alice@box.example', 'status': 'connected', 'changed': [], 'note': 'ok'}
        with mock.patch.object(hostsetup, 'setup_host', return_value=fake):
            code, body = dashboard.save_connect(
                collector, json.dumps({'action': 'setup_host', 'address': 'alice@box.example'}).encode(), home=self.home,
            )
        self.assertEqual(code, 200)
        self.assertEqual(body['status'], 'connected')
