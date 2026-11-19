"""Dashboard watchdog probes and starts without touching a real service or network."""
import io
import json
import os
import shlex
import subprocess
import threading
import unittest
from unittest import mock

import tmpdirs

from tutti import cli, failover, install, move, selfheal, waiting, watchdog


class HealthTest(unittest.TestCase):
    def test_configured_addresses_and_bad_responses(self):
        for bind, target in [('192.0.2.4', '192.0.2.4'), ('0.0.0.0', '127.0.0.1'), ('::', '::1'), ('::1', '::1')]:
            with self.subTest(bind=bind), mock.patch.object(move.http.client, 'HTTPConnection') as connection:
                response = connection.return_value.getresponse.return_value
                response.status = 200
                response.read.return_value = b'{"ok": true}'
                self.assertEqual(move.local_health(4777, host=bind), {'ok': True})
                connection.assert_called_once_with(target, 4777, timeout=3)
                connection.return_value.request.assert_called_once_with('GET', '/api/health', headers={'Host': '127.0.0.1:4777'})
                response.status = 500
                self.assertIsNone(move.local_health(4777, host=bind))
                response.status = 200
                response.read.return_value = b'not json'
                self.assertIsNone(move.local_health(4777, host=bind))

    def test_config_bind_is_passed_to_probe(self):
        with mock.patch.object(move, 'local_health', return_value={'ok': True}) as probe:
            self.assertTrue(watchdog.dashboard_health({'dashboard': {'bind': '192.0.2.4', 'port': 1234}}))
            probe.assert_called_once_with(1234, host='192.0.2.4')


class StartTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {'project': {'state': tmpdirs.mkdtemp()}}

    def test_pinned_service_self_update_honors_nondefault_opt_out(self):
        path = '/home/example/nondefault.yaml'
        unit = install.systemd_unit('/home/example/bin/tutti', path)
        command = next(line.partition('=')[2] for line in unit.splitlines() if line.startswith('ExecStart='))
        shell = shlex.split(command)[2]
        update, dashboard = shell.split('; ', 1)
        update_args = shlex.split(update)[1:]
        dashboard_args = shlex.split(dashboard)[2:]
        self.assertEqual(dashboard_args, ['--config', path, 'dashboard'])
        args = cli.parser().parse_args(update_args)
        with mock.patch.object(install, 'is_release_install', return_value=True), \
                mock.patch.object(cli.config, 'load', return_value={'self_update': {'enabled': False}}) as load, \
                mock.patch.object(install, 'self_update') as update_call:
            self.assertEqual(cli.cmd_self_update(None, args), 0)
            load.assert_called_once_with(path)
            update_call.assert_not_called()

    def test_managed_or_unknown_unit_fails_closed(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0))
        start = mock.Mock()
        lines = watchdog.ensure_dashboard(self.cfg, health=lambda: False, unit=lambda: 'managed', matches=lambda: False,
                                           run=run, start=start)
        self.assertIn('unverified', lines[0])
        run.assert_not_called()
        start.assert_not_called()
        self.assertIn('unknown', watchdog.ensure_dashboard(self.cfg, health=lambda: False, unit=lambda: 'unknown', start=start)[0])
        start.assert_not_called()

    def test_recorded_process_has_bounded_grace_and_unverified_identity(self):
        from tutti import failover
        with open(failover.dashboard_pid_file(self.cfg), 'w', encoding='utf8') as f:
            f.write('123')
        stamp = os.path.getmtime(failover.dashboard_pid_file(self.cfg))
        args = {'health': lambda: False, 'unit': lambda: 'unmanaged', 'recorded': lambda: 123, 'start': self.fail}
        self.assertIn('allowing', watchdog.ensure_dashboard(self.cfg, now=stamp + 10, **args)[0])
        self.assertIn('unverified', watchdog.ensure_dashboard(self.cfg, now=stamp + 61, **args)[0])

    def test_unmanaged_concurrent_watchdogs_start_once(self):
        started = []
        barrier = threading.Barrier(2)

        def check():
            barrier.wait()
            watchdog.ensure_dashboard(self.cfg, health=lambda: False, unit=lambda: 'unmanaged',
                                      recorded=lambda: bool(started), start=lambda: started.append(True), may_write=lambda: True)

        threads = [threading.Thread(target=check) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(started, [True])
        self.assertEqual(watchdog.ensure_dashboard(self.cfg, health=lambda: True, unit=lambda: self.fail('healthy')), [])

    def test_service_detection(self):
        def result(code, out):
            return lambda *a, **k: mock.Mock(returncode=code, stdout=out)
        self.assertEqual(watchdog.dashboard_unit(which=lambda _: None, platform='linux'), 'unknown')
        self.assertEqual(watchdog.dashboard_unit(which=lambda _: None, platform='darwin'), 'unmanaged')
        self.assertEqual(watchdog.dashboard_unit(run=result(0, 'LoadState=loaded\n'), which=lambda _: 'systemctl'), 'managed')
        self.assertEqual(watchdog.dashboard_unit(run=result(1, 'LoadState=not-found\n'), which=lambda _: 'systemctl'), 'unmanaged')
        self.assertEqual(watchdog.dashboard_unit(run=result(1, ''), which=lambda _: 'systemctl'), 'unknown')
        self.assertEqual(watchdog.dashboard_unit(run=mock.Mock(side_effect=subprocess.TimeoutExpired('systemctl', 5)),
                                                 which=lambda _: 'systemctl'), 'unknown')

    def test_only_inactive_unchanged_explicit_unit_matches(self):
        cfg = {'_path': '/home/example/config.yaml'}
        unit_path = '/home/example/.config/systemd/user/tutti-dashboard.service'
        unit = install.systemd_unit('/home/example/bin/tutti', cfg['_path'])
        props = {'FragmentPath': unit_path, 'DropInPaths': '', 'NeedDaemonReload': 'no', 'ActiveState': 'inactive'}

        def run(*args, **kwargs):
            return mock.Mock(returncode=0, stdout=''.join(f'{k}={v}\n' for k, v in props.items()))

        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': '/home/example/.config'}):
            self.assertTrue(watchdog.managed_unit_matches(cfg, run, read=lambda _: unit))
            self.assertFalse(watchdog.managed_unit_matches({'_path': '/other/config.yaml'}, run, read=lambda _: unit))
            self.assertFalse(watchdog.managed_unit_matches(cfg, run, read=lambda _: install.systemd_unit('/home/example/bin/tutti')))
            for key, bad in [('DropInPaths', '/tmp/override.conf'), ('NeedDaemonReload', 'yes'),
                             ('ActiveState', 'active'), ('FragmentPath', '/other/unit')]:
                before = props[key]
                props[key] = bad
                self.assertFalse(watchdog.managed_unit_matches(cfg, run, read=lambda _: unit), key)
                props[key] = before
            props['ActiveState'] = 'failed'
            self.assertTrue(watchdog.managed_unit_matches(cfg, run, read=lambda _: unit))

    def test_verified_inactive_unit_is_started_by_systemd(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0))
        result = watchdog.ensure_dashboard(self.cfg, health=lambda: False, unit=lambda: 'managed',
                                           matches=lambda: True, run=run, start=self.fail, may_write=lambda: True)
        self.assertIn('verified inactive', result[0])
        run.assert_called_once_with(['systemctl', '--user', 'start', '--no-block', 'tutti-dashboard.service'],
                                    capture_output=True, text=True, timeout=5)
        run.return_value.returncode = 1
        self.assertIn('could not be started', watchdog.ensure_dashboard(self.cfg, health=lambda: False, unit=lambda: 'managed',
                                                                        matches=lambda: True, run=run, start=self.fail,
                                                                        may_write=lambda: True)[0])

    def test_warning_is_shown_but_not_recorded_as_repair(self):
        ctx = selfheal.Ctx(self.cfg)
        with mock.patch.object(selfheal, 'record') as record, mock.patch.object(selfheal, 'trim'):
            lines = selfheal.round_(ctx, [('dashboard', lambda _: [watchdog.WarningLine('unhealthy')])])
        self.assertEqual(lines, ['dashboard: unhealthy'])
        record.assert_not_called()
        with mock.patch.object(selfheal, 'round_', return_value=lines), mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            args = mock.Mock(once=True, json=True)
            self.assertEqual(cli.cmd_selfheal(self.cfg, args), 0)
            self.assertEqual(json.loads(out.getvalue()), {'repairs': [], 'warnings': ['dashboard: unhealthy']})

    def test_healthy_dashboard_never_waits_for_start_lock(self):
        with mock.patch.object(watchdog, 'start_lock', side_effect=AssertionError('locked')):
            self.assertEqual(watchdog.ensure_dashboard(self.cfg, health=lambda: True), [])

    def test_future_pid_mtime_and_expired_lease_fail_closed(self):
        with open(failover.dashboard_pid_file(self.cfg), 'w', encoding='utf8') as f:
            f.write('123')
        stamp = os.path.getmtime(failover.dashboard_pid_file(self.cfg))
        warning = watchdog.ensure_dashboard(self.cfg, health=lambda: False, unit=lambda: 'unmanaged',
                                             recorded=lambda: 123, now=stamp - 5, start=self.fail)
        self.assertIn('uncertain', warning[0])
        warning = watchdog.ensure_dashboard(self.cfg, health=lambda: False, unit=lambda: 'unmanaged',
                                             recorded=lambda: None, may_write=lambda: False, start=self.fail)
        self.assertIn('lease expired', warning[0])
        run = mock.Mock()
        warning = watchdog.ensure_dashboard(self.cfg, health=lambda: False, unit=lambda: 'managed', matches=lambda: True,
                                             may_write=lambda: False, run=run)
        self.assertIn('lease expired', warning[0])
        run.assert_not_called()

    def test_recorded_pid_uses_non_signaling_probe(self):
        with open(failover.dashboard_pid_file(self.cfg), 'w', encoding='utf8') as f:
            f.write('123')
        with mock.patch.object(waiting, 'pid_gone', return_value=lambda: False) as probe:
            self.assertEqual(failover.recorded_pid(self.cfg), 123)
            probe.assert_called_once_with(123)

    def test_child_is_stopped_if_pid_cannot_be_written(self):
        child = mock.Mock(pid=123)
        self.cfg['_path'] = '/config.yaml'
        with mock.patch.object(failover, 'dashboard_pid_file', return_value='/missing-dir/pid'), \
                self.assertRaises(OSError):
            failover.start_dashboard(self.cfg, popen=lambda *a, **k: child)
        child.kill.assert_called_once_with()
        child.wait.assert_called_once_with()
