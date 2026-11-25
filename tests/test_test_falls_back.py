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


class TestRepoNeedsItsRunner(unittest.TestCase):
    """autopilot.repos.<repo>.runner: a desktop shell machine is unfit for that repo; the run moves on and says why."""
    NEED = {'repo': 'map', 'kind': 'docker', 'image': 'q:local'}

    def setUp(self):
        tz.use({'time': {'zone': 'UTC'}})
        self.addCleanup(tz.use, {})
        self.probes = {'nuc': free(2), 'pc': free(2), 'old': free(1)}

    def cfg(self):
        return cfg_of(nuc={'limit': None, 'runner': {'kind': 'shell'}}, pc={'runner': {'kind': 'docker', 'image': 'q:local'}},
                      old={'runner': {'kind': 'docker', 'image': 'q:local'}})

    def choose(self, cfg, a, need=NEED):
        err = io.StringIO()
        with mock.patch.object(cli.remote, 'probe', side_effect=lambda m: self.probes[m['name']]), redirect_stderr(err):
            try:
                return cli.test_choice(cfg, a, need)[0], err.getvalue()
            except config.ConfigError as e:
                return e, err.getvalue()

    def test_on_a_shell_machine_moves_to_the_next_fit_one_and_says_why(self):
        c, said = self.choose(self.cfg(), args(on='nuc'))
        self.assertEqual((c.machine, c.queued), ('pc', False))
        self.assertIn('nuc: runs checks in its shell runner; map needs its docker runner', said)
        self.assertIn('running on pc instead', said)
        self.assertNotIn('heavy work elsewhere', said)

    def test_routed_run_skips_the_shell_machine_first_in_order(self):
        c, _ = self.choose(self.cfg(), args())
        self.assertEqual(c.machine, 'pc')

    def test_only_refuses_naming_the_runner(self):
        e, _ = self.choose(self.cfg(), args(on='nuc', only=True))
        self.assertIsInstance(e, config.ConfigError)
        self.assertIn('needs its docker runner', str(e))

    def test_no_docker_machine_fit_refuses_and_never_runs_on_the_shell_one(self):
        self.probes['pc'], self.probes['old'] = free(0), free(0)
        e, said = self.choose(self.cfg(), args(on='nuc'))
        self.assertIsInstance(e, config.ConfigError)
        self.assertIn('no other machine fits', str(e))
        self.assertNotIn('running on', said)

    def test_without_a_need_the_shell_machine_is_used_as_before(self):
        c, _ = self.choose(self.cfg(), args(on='nuc'), need=None)
        self.assertEqual(c.machine, 'nuc')

    def test_need_comes_from_the_trees_repo_and_a_desktop_command_is_exempt(self):
        decl = {'kind': 'docker', 'desktop': 'emulator'}
        with mock.patch('tutti.projects.repo_runner', return_value=('map', decl)):
            self.assertEqual(cli.test_need({}, '/t', ['npm', 'test'])['kind'], 'docker')
            self.assertIsNone(cli.test_need({}, '/t', ['run-emulator', '--avd', 'x']))
        with mock.patch('tutti.projects.repo_runner', return_value=None):
            self.assertIsNone(cli.test_need({}, '/t', ['npm', 'test']))


class TestRoutedCheckNeedsItsRunner(unittest.TestCase):
    def test_a_routed_check_skips_the_shell_machine_and_never_runs_there(self):
        from tutti import ship
        cfg = config.normalise({'project': {'state': tmpdirs.mkdtemp()},
                                'machines': {'desk': {'host': 'desk.example', 'runner': {'kind': 'shell'}},
                                             'box': {'host': 'box.example', 'runner': {'kind': 'docker', 'image': 'q:local'}}},
                                'routing': {'order': ['desk', 'box']}})
        ran, err = [], io.StringIO()
        with mock.patch('tutti.projects.repo_runner', return_value=('map', {'kind': 'docker'})), \
                mock.patch.object(cli.remote, 'probe', return_value=free(2)), \
                mock.patch.object(ship.remote, 'run', side_effect=lambda c, m, t, top, run: ran.append(m['name']) or 0), \
                redirect_stderr(err):
            rc, where = ship.run_check(cfg, '/t', {'run': 'npm test', 'where': 'route'}, {}, 'trk')
        self.assertEqual((rc, where, ran), (0, 'box', ['box']))
        self.assertIn('desk: runs checks in its shell runner', err.getvalue())

    def test_no_docker_machine_means_no_run_and_the_reason(self):
        from tutti import ship
        cfg = config.normalise({'project': {'state': tmpdirs.mkdtemp()},
                                'machines': {'desk': {'host': 'desk.example', 'runner': {'kind': 'shell'}}},
                                'routing': {'order': ['desk']}})
        err = io.StringIO()
        with mock.patch('tutti.projects.repo_runner', return_value=('map', {'kind': 'docker'})), \
                mock.patch.object(ship.remote, 'run') as run, redirect_stderr(err):
            rc, where = ship.run_check(cfg, '/t', {'run': 'npm test', 'where': 'route'}, {}, 'trk')
        self.assertEqual((rc, where), (3, 'none'))
        run.assert_not_called()
        self.assertIn('check not run', err.getvalue())


if __name__ == '__main__':
    unittest.main()
