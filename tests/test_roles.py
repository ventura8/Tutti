import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tutti import adbpair, cli, devices, devicesweb, prep, remote, roles, state


class TestRoles(unittest.TestCase):
    def test_terminal_matrix_can_refresh_or_print_once(self):
        cfg = {}
        with patch.object(cli, 'print_role_matrix') as render, patch('builtins.input', side_effect=['', 'q']) as read:
            self.assertEqual(cli.cmd_roles(cfg, SimpleNamespace(interactive=True)), 0)
            self.assertEqual(render.call_count, 2)
            self.assertEqual(cli.cmd_roles(cfg, SimpleNamespace(interactive=False)), 0)
            self.assertEqual(render.call_count, 3)
            self.assertEqual(read.call_count, 2)

    def test_machine_can_have_multiple_roles(self):
        row = {'name': 'build', 'reachable': True, 'transport': 'wsl', 'disk_gb': 20, 'min_free_gb': 10, 'idle_s': 0,
               'tools': ['cursor-agent', 'git', 'python3', 'adb', 'java', 'sdkmanager', 'chromium']}
        matrix = roles.machine_matrix(row, backup=True, powered=True)
        assigned = {name for name, value in matrix.items() if value == (True, '')}
        self.assertEqual(assigned, set(matrix) - {'cast_receiver', 'notification_target'})
        cast = roles.machine_matrix({**row, 'cast_roles': ['cast-receiver']})
        self.assertEqual(cast['cast_receiver'], (True, ''))
        self.assertFalse(cast['cast_sender'][0])

    def test_machines_without_ollama_still_get_roles_from_probe(self):
        result = SimpleNamespace(returncode=0, stdout='O Linux x86_64\nK agy\nK adb\n2 0.1 4 0 0 - - - 10\n')
        with patch.object(remote, 'sh', return_value=result):
            probe = remote.probe({'transport': 'ssh', 'yield_file': '/tmp/yield', 'locks': ['/tmp/lock']})
        row = {'name': 'build', 'reachable': probe.reachable, 'tools': probe.tools, 'transport': 'ssh', 'os': probe.os}
        cfg = {'machines': {'build': {'transport': 'ssh'}}}
        state.with_role_matrix(cfg, [row])
        self.assertEqual(probe.os, 'Linux x86_64')
        self.assertEqual(row['role_matrix']['worker'], (True, ''))
        self.assertEqual(row['role_matrix']['device_host'], (True, ''))
        probe_missing = {**row, 'tools': ['adb']}
        state.with_role_matrix(cfg, [probe_missing])
        self.assertFalse(probe_missing['role_matrix']['worker'][0])

    def test_offline_machine_has_no_confirmed_roles(self):
        matrix = roles.machine_matrix({'reachable': False, 'tools': ['git', 'python3', 'ollama']})
        self.assertTrue(all(not assigned and reason for assigned, reason in matrix.values()))

    def test_device_uses_live_connection_and_detected_cast(self):
        device = {'name': 'display', 'serial': 'display.example:5555', 'machine': 'build', 'kind': 'tv'}
        readings = {'state': 'device', 'cast': '1,0,0,1', 'screen': 0}
        row = devices.device_state(device, readings)
        self.assertTrue(row['role_matrix']['cast_receiver'][0])
        self.assertTrue(row['role_matrix']['test_runner'][0])
        self.assertTrue(row['role_matrix']['presence_source'][0])
        self.assertFalse(row['role_matrix']['device_host'][0])
        offline = devices.device_state(device, {'state': 'offline', 'cast': readings['cast']})
        self.assertFalse(any(assigned for assigned, _ in offline['role_matrix'].values()))
        held = devices.device_state(device, {}, 'Paused by owner')
        self.assertEqual(held['role_matrix']['test_runner'], (False, 'Paused by owner'))
        unauthorized = devices.device_state(device, {'state': 'unauthorized'})
        self.assertIn('authorize', unauthorized['role_matrix']['test_runner'][1])

    def test_watch_preset_and_unplugged_policy(self):
        watch = {'kind': 'watch'}
        self.assertEqual(adbpair.kind_of('', {'android.hardware.type.watch'}), 'watch')
        self.assertEqual(adbpair.flow_for('watch'), 'code')
        policy = devices.policy(watch, termux=True)
        readings = {'state': 'device', 'battery': 90, 'battery_c': 30, 'wifi': 1, 'screen': 0, 'charging': 0}
        self.assertIn('not charging', devices.unfit(policy, readings))
        self.assertFalse(devices.unfit(policy, {**readings, 'charging': 1}))
        self.assertTrue(devices.unfit(policy, {**readings, 'charging': 1, 'battery_c': 35}))
        day = {x['setting']: x['value'] for x in prep.wanted(watch, night=False) if 'setting' in x}
        night = {x['setting']: x['value'] for x in prep.wanted(watch, night=True) if 'setting' in x}
        self.assertEqual(day['system screen_brightness'], 77)
        self.assertEqual(night['system screen_brightness'], 0)

    def test_adb_repair_reuses_pinned_installer_only_on_eligible_machine(self):
        row = {'name': 'build', 'os': 'Linux x86_64', 'transport': 'ssh', 'status': 'free', 'busy': 0,
               'tools': ['curl', 'sha256sum', 'python3']}
        collector = SimpleNamespace(cfg={'machines': {'build': {}}}, snapshot=lambda: {'machines': {'items': [row]}})
        host = Mock()
        host.path.return_value = ''
        host.install_tools.return_value = (0, '')
        request = b'{"action":"install-adb","machine":"build"}'
        self.assertEqual(devicesweb.pair_post(collector, request, lambda _: host)[0], 200)
        host.install_tools.assert_called_once()
        row['status'] = 'busy'
        self.assertEqual(devicesweb.pair_post(collector, request, lambda _: host)[0], 409)
        host.install_tools.assert_called_once()
