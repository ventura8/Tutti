"""Limited hours + heat must keep heavy work (`tutti test`, ship, handover) off a Plex/media NUC (t444).

Pure: Probe objects and UTC clocks only — never a real machine. Proves the gap that let check.sh stampede the NUC
at 105 C during its evening window: workq already skipped limited machines for heavy jobs, but routing.choose /
`tutti test --on` did not."""
import unittest
from datetime import datetime, timezone

from tutti import controls, routing, share, tz

MON_NOON = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)   # Monday noon UTC
MON_EVE = datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc)    # Monday 20:00 UTC → inside 18-01


def free(n=2, temp=40.0, gpu=None):
    return routing.Probe(True, n, 0.1, 16, False, temp, gpu_temp=gpu)


class LimitedHeavy(unittest.TestCase):
    def setUp(self):
        tz.use({'time': {'zone': 'UTC'}})
        self.addCleanup(tz.use, {})
        self.nuc = {'name': 'nuc', 'slots': 4, 'heat': {'cap_c': 85, 'stop_c': 98, 'hot_slots': 1},
                    'limit': {'window': '18-01', 'pct': 40}}
        self.pc = {'name': 'pc', 'slots': 2, 'heat': {'cap_c': 85, 'stop_c': 98}}
        self.machines = {'nuc': self.nuc, 'pc': self.pc}

    def test_heavy_choose_skips_limited_nuc_inside_its_window(self):
        probes = {'nuc': free(4), 'pc': free(1)}
        c = routing.choose(['nuc', 'pc'], self.machines, probes.get, now=MON_EVE, heavy=True)
        self.assertEqual(c.machine, 'pc')
        self.assertFalse(c.queued)
        self.assertIn('limited hours', routing.unfit(self.nuc, probes['nuc'], now=MON_EVE, heavy=True))

    def test_light_work_may_still_use_the_limited_nucs_share(self):
        # heavy=False: limited hours only cut share via probe_at_share, not refuse
        raw = free(4)
        capped = share.probe_at_share(self.nuc, raw, MON_EVE)
        self.assertEqual(capped.free, 1)   # 40 % of 4 slots → 1; busy 0
        self.assertEqual(routing.limit_unfit(self.nuc, MON_EVE, heavy=False), '')
        c = routing.choose(['nuc', 'pc'], self.machines, {'nuc': capped, 'pc': free(0)}.get, now=MON_EVE, heavy=False)
        self.assertEqual(c.machine, 'nuc')

    def test_probe_at_share_halves_free_slots_at_fifty_pct(self):
        m = {'name': 'nuc', 'slots': 4, 'limit': {'window': '18-01', 'pct': 50}}
        raw = free(4)
        self.assertEqual(share.probe_at_share(m, raw, MON_EVE).free, 2)   # slots=4 pct=50 free=4 → free=2
        self.assertEqual(share.probe_at_share(m, raw, MON_NOON).free, 4)  # outside the window: unchanged

    def test_heavy_never_queues_on_a_limited_machine(self):
        probes = {'nuc': free(0), 'pc': free(0)}   # no free slots anywhere
        c = routing.choose(['nuc', 'pc'], self.machines, probes.get, now=MON_EVE, heavy=True)
        self.assertEqual(c.machine, 'pc')          # patient queue: nuc is refused for limited heavy
        self.assertTrue(c.queued)
        self.assertIn('limited hours', routing.refused(self.nuc, probes['nuc'], now=MON_EVE, heavy=True))
        self.assertEqual(routing.refused(self.pc, probes['pc'], now=MON_EVE, heavy=True), '')

    def test_outside_the_window_heavy_may_use_the_nuc(self):
        probes = {'nuc': free(2), 'pc': free(1)}
        c = routing.choose(['nuc', 'pc'], self.machines, probes.get, now=MON_NOON, heavy=True)
        self.assertEqual(c.machine, 'nuc')
        self.assertIsNone(share.limited(self.nuc, MON_NOON))

    def test_on_style_refuse_matches_cli_test_choice(self):
        """`tutti test --on nuc` must raise the same reasons routing.refused / unfit return (cli.test_choice)."""
        hot = free(2, temp=105.0)
        self.assertIn('too hot', routing.refused(self.nuc, hot, now=MON_NOON, heavy=True))
        cool = free(2)
        why = routing.refused(self.nuc, cool, now=MON_EVE, heavy=True) or routing.unfit(self.nuc, cool, now=MON_EVE, heavy=True)
        self.assertIn('limited hours 18-01 at 40 %', why)
        self.assertIn('heavy work goes elsewhere', why)


class HeatStop(unittest.TestCase):
    def test_stop_c_refuses_and_choose_falls_over(self):
        machines = {
            'nuc': {'slots': 2, 'heat': {'cap_c': 85, 'stop_c': 98}},
            'pc': {'slots': 1, 'heat': {'cap_c': 85, 'stop_c': 98}},
        }
        probes = {'nuc': free(2, temp=105.0), 'pc': free(1, temp=50.0)}
        self.assertIn('too hot: CPU 105 C', routing.heat_unfit(machines['nuc'], probes['nuc']))
        c = routing.choose(['nuc', 'pc'], machines, probes.get, now=MON_NOON, heavy=True)
        self.assertEqual(c.machine, 'pc')

    def test_heat_guard_pauses_at_stop_and_clears_below_cap(self):
        machines = {'nuc': {'heat': {'cap_c': 85, 'stop_c': 98}}}
        notes = controls.heat_guard(machines, {'nuc': free(1, temp=105.0)}, now=1_000)
        self.assertTrue(any('paused for heat' in n for n in notes))
        self.assertTrue(controls.heat_pause(machines['nuc']['paused']))
        # still hot but below stop, above cap: pause stays (only clear under cap_c)
        machines['nuc']['paused'] = {'by': 'heat', 'at': 1_000, 'note': 'CPU 105 C >= stop_c 98: no new work until it cools'}
        notes = controls.heat_guard(machines, {'nuc': free(1, temp=90.0)}, now=1_100)
        self.assertEqual(notes, [])
        self.assertIn('paused', machines['nuc'])
        notes = controls.heat_guard(machines, {'nuc': free(1, temp=70.0)}, now=1_200)
        self.assertTrue(any('heat pause cleared' in n for n in notes))
        self.assertNotIn('paused', machines['nuc'])

    def test_heat_guard_never_overwrites_an_owner_pause(self):
        machines = {'nuc': {'heat': {'cap_c': 85, 'stop_c': 98},
                            'paused': {'by': 'owner (cli)', 'at': 1, 'note': 'owner'}}}
        notes = controls.heat_guard(machines, {'nuc': free(1, temp=110.0)}, now=2)
        self.assertEqual(notes, [])
        self.assertEqual(machines['nuc']['paused']['by'], 'owner (cli)')

    def test_pct_zero_is_blocked_hours_spec(self):
        """limit.pct 0: not used at all — same blocked-hours path as a device's hours window."""
        entry = {'limit': {'window': '18-01', 'pct': 0}}
        self.assertEqual(controls.blocked_spec('machine', entry), ('18-01', None))
        self.assertIsNone(controls.blocked_spec('machine', {'limit': {'window': '18-01', 'pct': 40}}))


class ShareCap(unittest.TestCase):
    def test_probe_at_share_cuts_free_slots_inside_the_window(self):
        """Share-capped probes are what ship/test must use: raw free slots ignored the limit (t444)."""
        tz.use({'time': {'zone': 'UTC'}})
        self.addCleanup(tz.use, {})
        nuc = {'slots': 4, 'limit': {'window': '18-01', 'pct': 50}}
        raw = free(4)
        capped = share.probe_at_share(nuc, raw, MON_EVE)
        self.assertEqual(capped.free, 2)   # 50 % of 4
        self.assertEqual(share.probe_at_share(nuc, raw, MON_NOON).free, 4)  # outside window: unchanged


class HeatRound(unittest.TestCase):
    def test_heat_round_persists_hands_over_once_and_clears(self):
        import tmpdirs
        from tutti import config
        folder = tmpdirs.mkdtemp()
        cfg = config.normalise({'project': {'state': folder},
                                'machines': {'nuc': {'host': 'nuc.example', 'heat': {'cap_c': 85, 'stop_c': 98}}}})
        stopped = []
        notes = controls.heat_round(cfg, {'nuc': free(1, temp=105.0)}, now=5_000, stop_fn=lambda k, n: stopped.append(n))
        self.assertTrue(any('heat pause started' in n for n in notes))
        self.assertEqual(stopped, ['nuc'])
        self.assertTrue(controls.heat_pause(cfg['machines']['nuc']['paused']))
        self.assertIn('machine:nuc', controls.read(folder)['paused'])
        stopped.clear()
        self.assertEqual(controls.heat_round(cfg, {'nuc': free(1, temp=105.0)}, now=5_100, stop_fn=lambda k, n: stopped.append(n)), [])
        self.assertEqual(stopped, [])
        notes = controls.heat_round(cfg, {'nuc': free(1, temp=70.0)}, now=5_200)
        self.assertTrue(any('heat pause cleared' in n for n in notes))
        self.assertNotIn('paused', cfg['machines']['nuc'])
        self.assertNotIn('machine:nuc', controls.read(folder)['paused'])


if __name__ == '__main__':
    unittest.main()
