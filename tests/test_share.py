"""A machine's share for Tutti (tutti/share.py), the model servers kept alive (tutti/fleet.py keep_servers, ensure_tunnels)
and the household's use (tutti/householduse.py). Fake runners only; no machine is called."""
import os
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from tests.test_autopilot import FREE, Base
from tutti import fleet, householduse, local, remote, routing, share, state

NIGHT = datetime(2026, 11, 14, 23, 30, tzinfo=timezone.utc)
TAGS = {'models': [{'name': 'qwen3.5:9b', 'details': {'parameter_size': '9B'}}]}


class Share(Base):
    def test_use_pct_caps_the_free_slots_and_the_model_threads(self):
        m = {'name': 'nuc', 'transport': 'ssh', 'host': 'nuc.example', 'slots': 4, 'min_free_gb': 6, 'use_pct': 50}
        row = state.machine_state(m, routing.Probe(True, 4, 0.1, 16), 1)
        self.assertEqual((row['free'], row['use_pct']), (2, 50))
        busy = state.machine_state(m, routing.Probe(True, 2, 0.1, 16), 1)   # 2 already running: the share is used up
        self.assertEqual((busy['free'], busy['status']), (0, 'busy'))
        self.assertIn('share for Tutti is 50 %', busy['reason'])
        cfg = self.cfg()
        cfg['machines']['nuc']['use_pct'] = 50
        cfg['workers']['pc'].update(machine='nuc', threads=8)
        self.assertEqual(local.threads_at_share(cfg, {**cfg['workers']['pc'], '_name': 'pc'}, NIGHT.replace(hour=12)), 4)
        cfg['machines']['nuc']['use_pct'] = 0
        self.assertEqual(local.threads_at_share(cfg, {**cfg['workers']['pc'], '_name': 'pc'}, NIGHT.replace(hour=12)), 1)

    def test_someone_at_the_keyboard_and_quiet_hours_lower_the_share(self):
        m = {'use_pct': 100, 'quiet_hours': ['00:00-23:59']}
        self.assertEqual(share.pct({'use_pct': 100}, None, NIGHT, idle_s=20), (0, 'someone is at its keyboard'))
        self.assertEqual(share.pct({'use_pct': 100}, None, NIGHT, idle_s=3600), (100, ''))
        self.assertEqual(share.pct(m, None, NIGHT)[0], 50)
        self.assertEqual(share.pct({'use_pct': 80, 'in_use_pct': 10}, {'reason': 'too hot: CPU 99 C'}, NIGHT, idle_s=1)[0], 10)
        hot = share.pct({'use_pct': 100}, {'reason': 'too hot: CPU 99 C'}, NIGHT, idle_s=None)
        self.assertEqual(hot, (50, 'it runs hot'))
        self.assertEqual(share.scaled(16, 30), 4)
        self.assertEqual(share.scaled(1, 30), 1)                           # a share above 0 keeps one slot
        self.assertEqual(share.scaled(4, 0), 0)

    def test_idle_time_is_read_from_the_session_when_there_is_no_xprintidle(self):
        def fake(hint, since):
            def run(argv, **k):
                if 'list-sessions' in argv:
                    return SimpleNamespace(returncode=0, stdout='2 1000 me seat0 1 user tty2 no -\n')
                if 'show-session' in argv:
                    return SimpleNamespace(returncode=0, stdout=f'Type=x11\nIdleHint={hint}\nIdleSinceHint={since}\nLockedHint=no\n')
                return SimpleNamespace(returncode=1, stdout='')   # no GNOME idle monitor, no xprintidle
            return run
        with mock.patch('shutil.which', lambda n: None):
            self.assertEqual(share.idle_seconds(fake('yes', 1000000000), clock=lambda: 1100.0), 100.0)
            self.assertEqual(share.idle_seconds(fake('no', 5)), 0.0)
            self.assertIsNone(share.idle_seconds(fake('no', 0)))           # a desktop that never sets the hint: not known


class KeepAlive(Base):
    def free_reading(self):
        return [{**FREE[0], 'reachable': True, 'busy': 0, 'load': 0.1, 'cores': 4}]

    def stale(self, cfg):
        fleet.save(cfg, {'machines': {'nuc': {'models': [fleet.classify(TAGS['models'][0])], 'stale': True}}})

    def test_a_dropped_tunnel_is_started_again(self):
        cfg = self.cfg()
        cfg['workers']['pc'].update(machine='nuc', tunnel={'ssh': 'nuc.example', 'remote': '127.0.0.1:11434'})
        calls = []
        started = fleet.ensure_tunnels(cfg, FREE, {'pc': False}, lambda argv, **k: calls.append(argv) or SimpleNamespace(returncode=0))
        self.assertEqual(started, ['pc'])
        self.assertEqual(fleet.read_state(cfg)['woke']['workers'], ['pc'])

    def test_a_quiet_ollama_is_restarted_once_per_interval(self):
        cfg = self.cfg()
        self.stale(cfg)
        calls = []

        def sh(machine, script, **k):
            calls.append(script)
            return SimpleNamespace(stdout='user-service\n')
        servers = fleet.keep_servers(cfg, self.free_reading(), 1000.0, sh, observed_at=1000.0)
        self.assertEqual((servers['nuc']['up'], servers['nuc']['how'], servers['nuc']['fix']), (False, 'user-service', ''))
        fleet.keep_servers(cfg, self.free_reading(), 1100.0, sh, observed_at=1100.0)
        self.assertEqual(len(calls), 1)                                    # not again within RESTART_EVERY
        fleet.keep_servers(cfg, self.free_reading(), 1000.0 + fleet.RESTART_EVERY, sh, observed_at=1000.0 + fleet.RESTART_EVERY)
        self.assertEqual(len(calls), 2)

    def test_a_system_service_gets_the_exact_fix_and_a_paused_machine_is_left_alone(self):
        cfg = self.cfg()
        self.stale(cfg)
        servers = fleet.keep_servers(cfg, self.free_reading(), 1.0, lambda m, s, **k: SimpleNamespace(stdout='system-service\n'),
                                     observed_at=1.0)
        self.assertIn('sudo systemctl restart ollama', servers['nuc']['fix'])
        cfg2 = self.cfg()
        self.stale(cfg2)
        paused = [{**FREE[0], 'status': 'paused'}]
        out = fleet.keep_servers(cfg2, paused, 1.0, lambda *a, **k: self.fail('a yielding machine is not touched'))
        self.assertFalse(out['nuc']['up'])
        self.assertEqual(out['nuc']['how'], 'deferred')

    def test_model_restart_waits_for_complete_free_machine_reading(self):
        cfg = self.cfg()
        self.stale(cfg)
        calls = []
        def sh(*a, **k):
            calls.append(a)
            return SimpleNamespace(stdout='user-service\n')
        good = self.free_reading()[0]
        for changes in ({'status': 'busy'}, {'status': 'loaded'}, {'busy': 1}, {'load': None}, {'cores': None},
                        {'load': float('nan')}, {'reachable': None}, {'stale': True}, {'free': None}, {'status': 'unknown'}):
            servers = fleet.keep_servers(cfg, [{**good, **changes}], 100.0, sh, observed_at=100.0)
            self.assertEqual((servers['nuc']['up'], servers['nuc']['how']), (False, 'deferred'))
        self.assertEqual(calls, [])
        for at in (None, -21, 101, float('nan')):
            self.assertEqual(fleet.keep_servers(cfg, [good], 100.0, sh, observed_at=at)['nuc']['how'], 'deferred')
        servers = fleet.keep_servers(cfg, [good], 101.0, sh, observed_at=101.0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(servers['nuc']['how'], 'user-service')

    def test_restart_rechecks_age_after_a_blocking_restart(self):
        cfg = self.cfg()
        cfg['machines']['pcbox'] = {**cfg['machines']['nuc'], 'name': 'pcbox'}
        model = fleet.classify(TAGS['models'][0])
        fleet.save(cfg, {'machines': {'nuc': {'models': [model], 'stale': True},
                                      'pcbox': {'models': [model], 'stale': True}}})
        calls = []
        times = iter([1000.0, 1130.0])

        def sh(machine, script, **k):
            calls.append(machine['name'])
            return SimpleNamespace(stdout='user-service\n')

        good = self.free_reading()[0]
        rows = [{**good, 'name': 'nuc'}, {**good, 'name': 'pcbox'}]
        servers = fleet.keep_servers(cfg, rows, 1000.0, sh, observed_at=1000.0, clock=lambda: next(times))
        self.assertEqual(calls, ['nuc'])
        self.assertEqual(servers['nuc']['how'], 'user-service')
        self.assertEqual((servers['pcbox']['up'], servers['pcbox']['how']), (False, 'deferred'))

    def test_after_probe_uses_machine_observation_time_and_marks_recovery(self):
        cfg = self.cfg()
        self.stale(cfg)
        calls = []
        def sh(*a, **k):
            calls.append(a)
            return SimpleNamespace(stdout='user-service\n')
        # machines_at alone is not enough: the gate uses the batch start (machines_started_at).
        fresh = {'machines': self.free_reading(), 'machines_at': 201.0, 'machines_started_at': 1.0,
                 'workers': {'items': []}, 'energy': {}}
        with mock.patch.object(fleet, 'refresh'), mock.patch.object(fleet, 'apply'), \
                mock.patch('tutti.power.after_probe'), mock.patch('tutti.householduse.record'), \
                mock.patch.object(fleet, 'ensure_tunnels', return_value=[]), \
                mock.patch('tutti.fleet.time.time', return_value=201.0):
            fleet.after_probe(cfg, fresh, 201.0, sh=sh)
            self.assertEqual(calls, [])
            self.assertEqual(fleet.read_state(cfg)['servers']['nuc']['how'], 'deferred')
            fresh['machines_started_at'] = 201.0
            fleet.after_probe(cfg, fresh, 201.0, sh=sh)
            self.assertEqual(len(calls), 1)
            data = fleet.read_state(cfg)
            data['machines']['nuc']['stale'] = False
            fleet.save(cfg, data)
            fleet.after_probe(cfg, fresh, 202.0, sh=sh)
            server = fleet.read_state(cfg)['servers']['nuc']
            self.assertTrue(server['up'])
            self.assertEqual((server['how'], server['fix']), ('', ''))


class Use(Base):
    def test_the_headline_and_each_machine_use(self):
        cfg = self.cfg()
        rows = [{'name': 'nuc', 'status': 'free', 'slots': 3, 'free': 2, 'busy': 1, 'use_pct': 100, 'reason': 'fit'}]
        for t in (0, 60, 120):
            householduse.record(cfg, rows, float(t), busy_fn=lambda c: {})
        v = householduse.view(cfg, 120.0)
        self.assertEqual(v['pct'], 33)
        self.assertIn('Household use: 33 % of available capacity', v['headline'])
        self.assertEqual(v['machines'][0]['idle_why'], 'fit')
        off = householduse.sample({'status': 'paused', 'slots': 3, 'free': 3}, True, False)
        self.assertEqual(off, [0, 0])                                      # a yielding machine has nothing available


class Probe(Base):
    def test_an_nvidia_smi_without_its_driver_does_not_break_the_summary_line(self):
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, 'nvidia-smi')
            with open(fake, 'w', encoding='utf8') as f:
                f.write('#!/bin/sh\necho "NVIDIA-SMI has failed because it could not communicate with the NVIDIA driver."\nexit 9\n')
            os.chmod(fake, stat.S_IRWXU)
            out = subprocess.run(['bash', '-c', remote.GPU_TEMP + 'echo "[$g]"'], capture_output=True, text=True,
                                 env={**os.environ, 'PATH': f'{d}:/usr/bin:/bin'}).stdout
            self.assertNotIn('NVIDIA-SMI', out)                                 # the NUC read as unreachable with it (2026-11-14)
            with open(fake, 'w', encoding='utf8') as f:
                f.write('#!/bin/sh\necho 55\n')
            ok = subprocess.run(['bash', '-c', remote.GPU_TEMP + 'echo "[$g]"'], capture_output=True, text=True,
                                env={**os.environ, 'PATH': f'{d}:/usr/bin:/bin'}).stdout
            self.assertIn('[55]', ok)                                           # a real reading still comes through


class RemoteIdle(Base):
    def test_windows_console_idle_time_and_the_probe_field(self):
        head = ' USERNAME   SESSIONNAME   ID  STATE   IDLE TIME  LOGON TIME\n'
        self.assertEqual(remote.parse_query_user(head + ' ventu  console  1  Active  none  12.11.2026 19:23\n'), 0.0)
        self.assertEqual(remote.parse_query_user(head + ' ventu  console  1  Active  1:05  12.11.2026 19:23\n'), 3900.0)
        self.assertEqual(remote.parse_query_user(head + ' ventu  console  1  Active  2+00:10  12.11.2026\n'), 2 * 86400.0 + 600)
        self.assertIsNone(remote.parse_query_user(head + ' ventu  rdp-tcp#1  2  Active  5  12.11.2026\n'))
        self.assertEqual(routing.parse_probe('1 0.5 8 0 50000 - 9.5 120').idle_s, 120.0)
        self.assertIsNone(routing.parse_probe('1 0.5 8 0 50000 - 9.5 -').idle_s)

    def test_a_remote_machine_in_use_lowers_its_share(self):
        m = {'name': 'pc', 'transport': 'wsl', 'host': 'pc.example', 'slots': 4, 'min_free_gb': 6}
        row = state.machine_state(m, routing.Probe(True, 4, 0.1, 16), 1, NIGHT.replace(hour=12), idle_s=10)
        self.assertEqual((row['use_pct'], row['free']), (0, 0))   # a person there: no new work

    def idle_in(self, shell, sessions):
        """The IDLE script run by `shell` with a fake loginctl: {session: (type, hint, since_us)}."""
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, 'loginctl')
            cases = ' '.join(f'{n}) case "$4" in Type) echo {t};; IdleHint) echo {h};; IdleSinceHint) echo {u};; esac;;'
                             for n, (t, h, u) in sessions.items())
            with open(fake, 'w', encoding='utf8') as f:
                f.write('#!/bin/sh\nif [ "$1" = list-sessions ]; then printf "%s\\n" ' + ' '.join(sessions) + '; exit 0; fi\n'
                        'case "$2" in ' + cases + ' esac\n')
            os.chmod(fake, 0o755)
            nogdbus = os.path.join(d, 'gdbus')   # no GNOME idle monitor in these cases
            with open(nogdbus, 'w', encoding='utf8') as f:
                f.write('#!/bin/sh\nexit 1\n')
            os.chmod(nogdbus, 0o755)
            env = {**os.environ, 'PATH': d + os.pathsep + os.environ['PATH']}
            r = subprocess.run([shell, '-c', remote.IDLE + 'echo "$i"'], env=env, capture_output=True, text=True, check=False)
            return r.returncode, r.stdout.strip(), r.stderr

    def test_the_idle_script_runs_in_a_posix_sh_as_in_bash(self):
        import time
        since = int((time.time() - 100) * 1000000)
        for shell in ('sh', 'bash'):
            rc, out, err = self.idle_in(shell, {'3': ('x11', 'yes', since), '7': ('tty', 'no', 0)})
            self.assertEqual(rc, 0, err)
            self.assertTrue(95 <= int(out) <= 130, out)
            self.assertEqual(self.idle_in(shell, {'3': ('x11', 'no', 0)})[1], '-')   # never set the hint: not known

    def test_the_idle_script_skips_a_desktop_that_never_sets_the_hint(self):
        self.assertIn('IdleSinceHint', remote.IDLE)
        self.assertIn('${i:--}', remote.probe_script({'yield_file': '~/y', 'name': 'n', 'transport': 'ssh', 'slots': 1,
                                                      'lock_prefix': '/tmp/x-'}))
