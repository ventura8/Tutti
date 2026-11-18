"""Planned absences (`tutti pause <kind> <name> --from <time> --until <time|unknown>`): nothing new is scheduled from 30 min
before, running work is handed over at the start, the end comes at `until` or, left open, when presence sees it back."""
import contextlib
import io
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace

from tutti import cli, controls

NOW = 1_800_000_000.0
HOUR = 3600


class AbsenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cfg = {'project': {'state': self.tmp}}

    def plan(self, start, until):
        controls.set_paused(self.tmp, 'device', 'pixel9pro', True, source='cli', note='with the owner', clock=lambda: NOW,
                            start=start, until=until)
        return controls.read(self.tmp)['paused']['device:pixel9pro']

    def test_the_window_is_planned_then_active_from_30_min_before_then_over(self):
        v = self.plan(NOW + 2 * HOUR, NOW + 10 * HOUR)
        self.assertEqual(controls.window(v, NOW), 'planned')
        self.assertEqual(controls.window(v, NOW + 2 * HOUR - 29 * 60), 'active')   # no new work just before it starts
        self.assertEqual(controls.window(v, NOW + 10 * HOUR), 'over')
        raw = {'devices': {'pixel9pro': {'serial': 'x'}}}
        self.assertNotIn('paused', controls.apply(raw, controls.read(self.tmp))['devices']['pixel9pro'])   # not yet
        self.assertIn('from', controls.paused_note({'paused': v}))

    def test_work_is_handed_over_once_at_the_start_and_it_ends_at_until(self):
        self.plan(NOW, NOW + HOUR)
        stopped = []
        controls.planned_round(self.cfg, {}, NOW + 1, stop_fn=lambda k, n: stopped.append(n))
        controls.planned_round(self.cfg, {}, NOW + 60, stop_fn=lambda k, n: stopped.append(n))
        self.assertEqual(stopped, ['pixel9pro'])
        done = controls.planned_round(self.cfg, {}, NOW + HOUR, stop_fn=lambda k, n: None)
        self.assertIn('planned absence over', done[0])
        self.assertEqual(controls.read(self.tmp)['paused'], {})

    def test_an_open_end_closes_when_presence_sees_it_away_then_back(self):
        self.plan(NOW, None)
        here = {'targets': {'device:pixel9pro': {'state': 'present'}}}
        away = {'targets': {'device:pixel9pro': {'state': 'away'}}}
        controls.planned_round(self.cfg, here, NOW + 1)   # still at home at 07:00: not over yet
        self.assertIn('device:pixel9pro', controls.read(self.tmp)['paused'])
        controls.planned_round(self.cfg, away, NOW + 60)
        done = controls.planned_round(self.cfg, here, NOW + 12 * HOUR)
        self.assertIn('back early: presence saw it again', done[0])
        self.assertEqual(controls.read(self.tmp)['paused'], {})

    def test_an_open_end_closes_when_it_stays_here_without_ever_looking_away(self):
        """The owner's case: away "until it is seen again", yet the phone stayed here (adb ready) for a day."""
        self.plan(NOW, None)
        here = {'targets': {'device:pixel9pro': {'state': 'present', 'since': NOW - HOUR}}}
        controls.planned_round(self.cfg, here, NOW + HOUR)   # an hour in: may still be leaving
        self.assertIn('device:pixel9pro', controls.read(self.tmp)['paused'])
        done = controls.planned_round(self.cfg, here, NOW + 24 * HOUR)
        self.assertIn('back early: presence saw it again', done[0])
        self.assertEqual(controls.read(self.tmp)['paused'], {})

    def test_times_are_read_in_local_time_and_a_bad_one_says_the_forms(self):
        self.assertEqual(controls.parse_when('now', now=NOW), NOW)
        t = controls.parse_when('07:00', now=NOW)
        self.assertTrue(NOW < t <= NOW + 24 * HOUR)
        from tutti import tz
        tomorrow = tz.local_now(controls.parse_when('tomorrow 07:00', now=NOW))
        self.assertEqual(((tomorrow.date() - tz.local_now(NOW).date()).days, tomorrow.hour, tomorrow.minute), (1, 7, 0))
        self.assertGreater(controls.parse_when('2026-11-16 07:00'), time.mktime((2026, 11, 15, 0, 0, 0, 0, 0, -1)))
        with self.assertRaisesRegex(ValueError, 'tomorrow 07:00'):
            controls.parse_when('next week')


    def test_the_cli_refuses_an_end_before_the_start_and_stop_now(self):
        cfg = {'_path': '/x', 'project': {'state': self.tmp}, 'raw_entries': {'devices': {'p': {}}}, 'devices': {'p': {}}}
        a = SimpleNamespace(cmd='pause', kind='device', name='p', note='', stop_now=False, start='2026-11-16 07:00',
                            until='2026-11-15 07:00')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_control(cfg, a), 2)
        a.until, a.stop_now = 'unknown', True
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_control(cfg, a), 2)
        self.assertEqual(controls.read(self.tmp)['paused'], {})


if __name__ == '__main__':
    unittest.main()
