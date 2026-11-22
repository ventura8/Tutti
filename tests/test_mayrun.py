"""`tutti may-run`: the check before any run on a machine (paused, hot, no slot, slot leases), the MCP tool, the config keys,
and the heat watchdog that throttles or stops only Tutti's containers. No real machine: probes and the shell are fakes."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout

from tutti import config, controls, mayrun, mcpserver
from tutti.routing import Probe

NOW = 1_800_000_000.0
COOL = Probe(True, 2, 1.0, 16, temp=60.0)


def machines():
    return {'nuc': {'name': 'nuc', 'slots': 2, 'heat': {'cap_c': 85, 'stop_c': 95}},
            'pc': {'name': 'pc', 'slots': 1}}


class Verdict(unittest.TestCase):
    def test_fit_machine_may_run(self):
        self.assertEqual(mayrun.verdict(machines()['nuc'], COOL, {}, 'me', NOW), '')

    def test_paused_machine_is_refused(self):
        m = {**machines()['nuc'], 'paused': {'by': 'owner (cli)', 'at': NOW, 'note': 'too hot'}}
        self.assertIn('paused', mayrun.verdict(m, COOL, {}, 'me', NOW))

    def test_hot_machine_is_refused(self):
        why = mayrun.verdict(machines()['nuc'], Probe(True, 2, 1.0, 16, temp=98.0), {}, 'me', NOW)
        self.assertIn('too hot', why)

    def test_no_free_slot_is_refused(self):
        self.assertIn('no free slot', mayrun.verdict(machines()['nuc'], Probe(True, 0, 1.0, 16, temp=60.0), {}, 'me', NOW))

    def test_slots_leased_by_others_refuse_but_own_lease_does_not(self):
        leases = {'a': {'machine': 'nuc', 'who': 'x', 'until': NOW + 60}, 'b': {'machine': 'nuc', 'who': 'y', 'until': NOW + 60}}
        self.assertIn('leased', mayrun.verdict(machines()['nuc'], COOL, leases, 'me', NOW))
        self.assertEqual(mayrun.verdict(machines()['nuc'], COOL, leases, 'x', NOW), '')

    def test_expired_lease_does_not_count(self):
        leases = {'a': {'machine': 'pc', 'who': 'x', 'until': NOW - 1}}
        self.assertEqual(mayrun.verdict(machines()['pc'], COOL, leases, 'me', NOW), '')


class Pick(unittest.TestCase):
    def test_falls_back_to_the_next_fit_machine(self):
        probes = {'nuc': Probe(True, 2, 1.0, 16, temp=99.0), 'pc': COOL}
        name, notes = mayrun.pick(machines(), ['nuc', 'pc'], probes.get, {}, 'me', NOW)
        self.assertEqual(name, 'pc')
        self.assertIn('nuc: too hot', notes[0])

    def test_refuses_with_every_reason(self):
        with self.assertRaises(mayrun.Refused) as e:
            mayrun.pick(machines(), ['nuc', 'ghost'], lambda n: Probe(), {}, 'me', NOW)
        self.assertIn('nuc: unreachable', str(e.exception))
        self.assertIn('ghost: not a machine', str(e.exception))

    def test_a_paused_machine_is_not_probed(self):
        ms = {**machines(), 'pc': {'name': 'pc', 'slots': 1, 'paused': {'by': 'owner (cli)', 'at': NOW}}}
        asked = []
        with self.assertRaises(mayrun.Refused):
            mayrun.pick(ms, ['pc'], lambda n: asked.append(n) or COOL, {}, 'me', NOW)
        self.assertEqual(asked, [])

    def test_paused_wsl_is_not_reached_but_a_heat_pause_is(self):
        from tutti import remote
        self.assertTrue(remote.asleep_on_purpose({'transport': 'wsl', 'paused': {'by': 'owner (cli)'}}))
        self.assertFalse(remote.asleep_on_purpose({'transport': 'wsl', 'paused': {'by': 'owner (heat)'}}))
        self.assertFalse(remote.asleep_on_purpose({'transport': 'ssh', 'paused': {'by': 'owner (cli)'}}))
        self.assertFalse(remote.probe({'name': 'pc', 'transport': 'wsl', 'paused': {'by': 'x (cli)'}}).reachable)

    def test_claim_and_release(self):
        leases = {'old': {'machine': 'pc', 'who': 'z', 'until': NOW - 5}}
        lid = mayrun.claim(leases, 'pc', 'ship.sh', 10, NOW, token='t1')
        self.assertEqual((lid, list(leases)), ('t1', ['t1']))
        self.assertTrue(mayrun.release(leases, 't1'))
        self.assertFalse(mayrun.release(leases, 't1'))

    def test_claim_needs_who_and_minutes(self):
        with self.assertRaises(ValueError):
            mayrun.claim({}, 'pc', '', 10, NOW)
        with self.assertRaises(ValueError):
            mayrun.claim({}, 'pc', 'me', 0, NOW)


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {'project': {'state': self.tmp.name}, 'machines': machines()}
        self.said = []

    def go(self, probes, lease=0, wait=0, who='ship.sh'):
        out, slept = io.StringIO(), []
        clock = iter(NOW + 60 * i for i in range(100))
        with redirect_stdout(out):
            rc = mayrun.run(self.cfg, ['nuc', 'pc'], who, lease, wait, self.said.append, sleep=slept.append,
                            clock=lambda: next(clock), probe_fn=lambda n: probes(n, len(slept)))
        return rc, out.getvalue().split(), slept

    def test_prints_the_fit_machine(self):
        rc, out, _ = self.go(lambda n, i: COOL)
        self.assertEqual((rc, out), (0, ['nuc']))

    def test_refusal_exits_3_with_reasons(self):
        rc, out, _ = self.go(lambda n, i: Probe())
        self.assertEqual((rc, out), (mayrun.REFUSED, []))
        self.assertTrue(any('nuc: unreachable' in s for s in self.said))

    def test_wait_asks_again_until_fit(self):
        rc, out, slept = self.go(lambda n, i: COOL if i >= 2 else Probe(), wait=5)
        self.assertEqual((rc, out[0], len(slept)), (0, 'nuc', 2))

    def test_lease_is_written_and_counted(self):
        self.cfg['machines']['pc']['slots'] = 1
        rc, out, _ = self.go(lambda n, i: COOL if n == 'pc' else Probe(), lease=30)
        self.assertEqual((rc, out[0]), (0, 'pc'))
        with open(mayrun.leases_path(self.cfg), encoding='utf8') as f:
            self.assertIn(out[1], json.load(f)['leases'])
        rc, _, _ = self.go(lambda n, i: COOL if n == 'pc' else Probe(), lease=30, who='other')
        self.assertEqual(rc, mayrun.REFUSED)

    def test_refusal_helper(self):
        self.assertEqual(mayrun.refusal(self.cfg, 'nuc', probe_fn=lambda n: COOL), '')
        self.assertIn('too hot', mayrun.refusal(self.cfg, 'nuc', probe_fn=lambda n: Probe(True, 2, 1.0, 16, temp=97.0)))


class Watchdog(unittest.TestCase):
    def test_below_stop_does_nothing(self):
        self.assertEqual(controls.watchdog_script(machines()['nuc'], 90.0), '')
        self.assertEqual(controls.watchdog_script(machines()['pc'], 120.0), '')

    def test_throttles_only_tuttis_containers(self):
        m = {**machines()['nuc'], 'containers': ['hcm-nuc-', 'tutti-']}
        line = controls.watchdog_script(m, 98.0)
        self.assertIn("grep -E '^(hcm\\-nuc\\-|tutti\\-)'", line)
        self.assertIn('docker update --cpus 1', line)

    def test_stop_mode(self):
        m = {**machines()['nuc'], 'heat': {'stop_c': 95, 'on_stop': 'stop', 'throttle_cpus': 2}}
        self.assertIn('docker stop', controls.watchdog_script(m, 96.0))

    def test_round_runs_on_hot_machines_and_survives_a_failure(self):
        ran = []

        def fake(m, script):
            ran.append(m['name'])
            raise OSError('ssh failed')
        cfg = {'machines': machines()}
        notes = controls.watchdog_round(cfg, {'nuc': {'temp_c': 99}, 'pc': {'temp_c': 99}}, fake)
        self.assertEqual(ran, ['nuc'])
        self.assertIn('could not reach', notes[0])

    def test_round_notes_a_throttle(self):
        notes = controls.watchdog_round({'machines': machines()}, {'nuc': {'temp_c': 99}}, lambda m, s: None)
        self.assertIn('throttled', notes[0])


class WslOrphans(unittest.TestCase):
    def test_the_script_only_matches_tuttis_helper_and_dead_parents(self):
        from tutti import remote
        ps = remote.wsl_reap_script(["run64.sh", "o'k"])
        self.assertIn("$_.CommandLine -like '*run64.sh*'", ps)
        self.assertIn("'*o''k*'", ps)
        self.assertIn('-not (Get-Process -Id $_.ParentProcessId', ps)

    def test_reap_counts_and_skips_other_machines(self):
        from unittest import mock

        from tutti import remote
        m = {'name': 'pc', 'transport': 'wsl', 'host': 'pc.example', 'wsl_helper': '/home/u/run64.sh'}
        self.assertIsNone(remote.reap_wsl_orphans({'transport': 'ssh'}))
        with mock.patch('subprocess.run', return_value=mock.Mock(returncode=0, stdout='2\n')):
            self.assertEqual(remote.reap_wsl_orphans(m), 2)
        with mock.patch('subprocess.run', side_effect=OSError('down')):
            self.assertIsNone(remote.reap_wsl_orphans(m))


class ConfigAndMcp(unittest.TestCase):
    def test_bad_watchdog_keys_are_errors(self):
        errs = config._heat_errors('nuc', {'stop_c': 95, 'on_stop': 'kill', 'throttle_cpus': 0})
        self.assertEqual(len(errs), 2)
        self.assertEqual(config._heat_errors('nuc', {'stop_c': 95, 'on_stop': 'stop', 'throttle_cpus': 2}), [])

    def test_mcp_may_run_argv(self):
        self.assertEqual(mcpserver.argv_of('may_run', {'machine': 'nuc pc'}), ['may-run', '--', 'nuc', 'pc'])


if __name__ == '__main__':
    unittest.main()
