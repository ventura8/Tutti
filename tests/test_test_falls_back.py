"""`tutti test --on <machine>` hands the run to the next fit machine in routing.order when that machine is unfit now
(limited hours, busy, paused), unless --only/--strict; with none fit it refuses and lists every reason. No real machine."""
import argparse
import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

import tmpdirs

from tutti import cli, config, routing, tz


def free(n=2):
    return routing.Probe(True, n, 0.1, 16, False, 40.0)


def cfg_of(**over):
    state = tmpdirs.mkdtemp()
    machines = {
        'nuc': {'name': 'nuc', 'slots': 2, 'limit': {'window': '00-24', 'pct': 0}},   # limited all day: heavy work goes elsewhere
        'pc': {'name': 'pc', 'slots': 2},
        'old': {'name': 'old', 'slots': 1},
    }
    for k, v in over.items():
        machines[k].update(v)
    return {'_path': 'x.yaml', 'project': {'state': state}, 'machines': machines, 'routing': {'order': ['nuc', 'pc', 'old']}}


def args(on=None, only=False):
    return argparse.Namespace(on=on, only=only, device=None, wait_minutes=1)


class TestFallsBack(unittest.TestCase):
    def setUp(self):
        tz.use({'time': {'zone': 'UTC'}})
        self.addCleanup(tz.use, {})
        self.probes = {'nuc': free(2), 'pc': free(2), 'old': free(1)}

    def choose(self, cfg, a):
        err = io.StringIO()
        with mock.patch.object(cli.remote, 'probe', side_effect=lambda m: self.probes[m['name']]), redirect_stderr(err):
            try:
                return cli.test_choice(cfg, a)[0], err.getvalue()
            except config.ConfigError as e:
                return e, err.getvalue()

    def test_limited_machine_hands_the_run_to_the_next_fit_one(self):
        c, said = self.choose(cfg_of(), args(on='nuc'))
        self.assertEqual(c.machine, 'pc')
        self.assertFalse(c.queued)
        self.assertIn('nuc: limited hours 00-24', said)
        self.assertIn('running on pc instead', said)

    def test_busy_next_machine_is_skipped(self):
        self.probes['pc'] = free(0)
        c, _ = self.choose(cfg_of(), args(on='nuc'))
        self.assertEqual(c.machine, 'old')

    def test_paused_machine_falls_back_too(self):
        cfg = cfg_of(pc={'paused': {'by': 'owner (cli)', 'at': 1, 'note': 'owner'}})
        c, _ = self.choose(cfg, args(on='nuc'))
        self.assertEqual(c.machine, 'old')

    def test_fit_machine_named_runs_there_without_a_note(self):
        c, said = self.choose(cfg_of(), args(on='pc'))
        self.assertEqual((c.machine, said), ('pc', ''))

    def test_only_keeps_the_refusal(self):
        e, _ = self.choose(cfg_of(), args(on='nuc', only=True))
        self.assertIsInstance(e, config.ConfigError)
        self.assertIn('limited hours', str(e))
        self.assertIn('--only', str(e))

    def test_none_fit_refuses_with_every_reason_and_never_queues(self):
        self.probes['pc'], self.probes['old'] = free(0), free(0)
        e, said = self.choose(cfg_of(), args(on='nuc'))
        self.assertIsInstance(e, config.ConfigError)
        for word in ('nuc: limited hours', 'pc:', 'old:', 'no other machine fits'):
            self.assertIn(word, str(e))
        self.assertNotIn('running on', said)

    def test_no_other_machine_in_the_order_refuses(self):
        cfg = cfg_of()
        cfg['routing']['order'] = ['nuc']
        e, _ = self.choose(cfg, args(on='nuc'))
        self.assertIn('routing.order names no other machine', str(e))

    def test_without_on_the_best_free_machine_in_order_wins(self):
        c, _ = self.choose(cfg_of(), args())
        self.assertEqual(c.machine, 'pc')

    def test_late_only_and_strict_flags_are_read(self):
        for flag in ('--only', '--strict'):
            a = cli.late_options(argparse.Namespace(on=None, only=False, command=[flag, '--on', 'nuc', '--', 'make']))
            self.assertTrue(a.only)
            self.assertEqual((a.on, a.command), ('nuc', ['make']))


if __name__ == '__main__':
    unittest.main()
