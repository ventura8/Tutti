"""A machine's share for Tutti (tutti/share.py), the model servers kept alive (tutti/fleet.py keep_servers, ensure_tunnels)
and the household's use (tutti/householduse.py). Fake runners only; no machine is called."""
from datetime import datetime, timezone
from types import SimpleNamespace

from tests.test_autopilot import FREE, Base
import os
import stat
import subprocess
import tempfile

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

    def test_someone_at_the_keyboard_and_quiet_hours_lower_the_share(self):
        m = {'use_pct': 100, 'quiet_hours': ['00:00-23:59']}
        self.assertEqual(share.pct({'use_pct': 100}, None, NIGHT, idle_s=20), (30, 'someone is using it'))
        self.assertEqual(share.pct({'use_pct': 100}, None, NIGHT, idle_s=3600), (100, ''))
        self.assertEqual(share.pct(m, None, NIGHT)[0], 50)
        self.assertEqual(share.pct({'use_pct': 80, 'in_use_pct': 10}, {'reason': 'too hot: CPU 99 C'}, NIGHT, idle_s=1)[0], 10)
        self.assertEqual(share.scaled(16, 30), 4)
        self.assertEqual(share.scaled(1, 30), 1)                           # a share above 0 keeps one slot
        self.assertEqual(share.scaled(4, 0), 0)

    def test_idle_time_is_read_from_the_session_when_there_is_no_xprintidle(self):
        def run(argv, **k):
            return SimpleNamespace(returncode=0, stdout='IdleHint=yes\nIdleSinceHint=1000000000\n')
        self.assertEqual(share.idle_seconds(run, clock=lambda: 1100.0), 100.0)
        busy = share.idle_seconds(lambda argv, **k: SimpleNamespace(returncode=0, stdout='IdleHint=no\n'))
        self.assertEqual(busy, 0.0)


class KeepAlive(Base):
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
        servers = fleet.keep_servers(cfg, FREE, 1000.0, sh)
        self.assertEqual((servers['nuc']['up'], servers['nuc']['how'], servers['nuc']['fix']), (False, 'user-service', ''))
        fleet.keep_servers(cfg, FREE, 1100.0, sh)
        self.assertEqual(len(calls), 1)                                    # not again within RESTART_EVERY
        fleet.keep_servers(cfg, FREE, 1000.0 + fleet.RESTART_EVERY, sh)
        self.assertEqual(len(calls), 2)

    def test_a_system_service_gets_the_exact_fix_and_a_paused_machine_is_left_alone(self):
        cfg = self.cfg()
        self.stale(cfg)
        servers = fleet.keep_servers(cfg, FREE, 1.0, lambda m, s, **k: SimpleNamespace(stdout='system-service\n'))
        self.assertIn('sudo systemctl restart ollama', servers['nuc']['fix'])
        cfg2 = self.cfg()
        self.stale(cfg2)
        paused = [{**FREE[0], 'status': 'paused'}]
        out = fleet.keep_servers(cfg2, paused, 1.0, lambda *a, **k: self.fail('a yielding machine is not touched'))
        self.assertTrue(out['nuc']['up'])


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
