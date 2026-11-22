"""A run that ended with no report and an unverified process (bridged, routed or unknown work) is held so it is never run
twice, but not forever (2026-11-22: 20 such runs held all 20 slots for 33 h and the autopilot started nothing): after
UNREPORTED_MAX its slot is given back, the attempt counted, the task queued again."""
import unittest
from unittest import mock

from tutti import autopilot


def task(started):
    return {'id': 't1', 'status': 'running', 'started': started, 'run': 'r1', 'step': 'laptop', 'boot': 'other-boot',
            'check_where': 'route', 'job_bridge': '', 'attempts': []}


class Hold(unittest.TestCase):
    def setUp(self):
        for name, value in (('reconcile', 'worktree kept'), ('tree_of', '/wt'), ('wip_ref', 'ref'), ('wip_remote', ''),
                            ('record_attempt', None), ('pid_of', 4242)):
            p = mock.patch.object(autopilot, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(autopilot.durable, 'died_with_host', return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_a_recent_unreported_run_is_held(self):
        t, events = task(10_000), []
        autopilot.hold_or_resume_unreported({}, t, events, now=10_000 + 3600)
        self.assertEqual((t['status'], events), ('running', []))
        self.assertIn('ownership is unverified', t['recovery_note'])

    def test_an_old_one_gives_its_slot_back_counted(self):
        t, events, killed = {**task(10_000), 'quota_hold': {'step': 'laptop'}}, [], []
        with mock.patch.object(autopilot.telemetry, 'record') as said:
            autopilot.hold_or_resume_unreported({}, t, events, now=10_000 + autopilot.UNREPORTED_MAX + 60,
                                                is_alive=lambda pid: True, kill=killed.append)
        self.assertEqual(killed, [4242])   # its own process, when still alive here, is stopped first
        self.assertEqual(t['status'], 'queued')
        for gone in ('recovery_note', 'run', 'quota_hold'):   # no old run id for a late report to match; no quota hold
            self.assertNotIn(gone, t)
        self.assertEqual(t['last_run'], 'r1')
        a = t['attempts'][-1]
        self.assertEqual((a['ok'], a['class'], a['step'], a['run']), (False, 'environment', 'laptop', 'r1'))
        self.assertIn('ended without a report (held 6 h', a['why'])
        self.assertNotIn('interrupted', t)   # an attempt, not a power-off
        said.assert_not_called()   # no 'interrupted by a reboot' telemetry
        self.assertIn('never reported in 6 h: slot given back', events[0])

    def test_now_zero_is_a_time(self):
        t, events = task(-autopilot.UNREPORTED_MAX - 60), []
        autopilot.hold_or_resume_unreported({}, t, events, now=0)
        self.assertEqual(t['status'], 'queued')


if __name__ == '__main__':
    unittest.main()
