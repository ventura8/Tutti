"""The sleep watcher (tutti/sleepwatch.py): logind's signals parsed, running work stopped and noted before a sleep, started
again on wake, the delay lock let go even when the hand-over fails. Every OS call is a fake: nothing sleeps or runs."""
import contextlib
import json
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

import tmpdirs

from tutti import autopilot, handover, presence, sleepwatch

SLEEP = '/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (true,)'
WAKE = '/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (false,)'


class FakeProc:
    def __init__(self, lines=()):
        self.stdout = iter(lines)
        self.terminated = False

    def terminate(self):
        self.terminated = True


class ParseTest(unittest.TestCase):
    def test_reads_sleep_and_shutdown_signals(self):
        self.assertEqual(sleepwatch.parse(SLEEP), ('sleep', True))
        self.assertEqual(sleepwatch.parse(WAKE), ('sleep', False))
        self.assertEqual(sleepwatch.parse('x.PrepareForShutdown (true,)'), ('shutdown', True))

    def test_ignores_other_lines_and_empty_input(self):
        self.assertIsNone(sleepwatch.parse('x.PrepareForReboot (true,)'))
        self.assertIsNone(sleepwatch.parse(''))
        self.assertIsNone(sleepwatch.parse(None))


class MaxDelayTest(unittest.TestCase):
    def test_keeps_a_second_in_hand(self):
        run = mock.Mock(return_value=SimpleNamespace(stdout='t 6000000\n'))
        self.assertEqual(sleepwatch.max_delay(run=run), 5.0)
        self.assertEqual(run.call_args.args[0], sleepwatch.DELAY)

    def test_never_below_half_a_second(self):
        run = mock.Mock(return_value=SimpleNamespace(stdout='t 1000000\n'))
        self.assertEqual(sleepwatch.max_delay(run=run), 0.5)

    def test_unreadable_falls_back_to_the_default(self):
        self.assertEqual(sleepwatch.max_delay(run=mock.Mock(side_effect=subprocess.SubprocessError('no bus'))), 4.0)
        self.assertEqual(sleepwatch.max_delay(run=mock.Mock(side_effect=OSError('no busctl'))), 4.0)
        self.assertEqual(sleepwatch.max_delay(run=mock.Mock(return_value=SimpleNamespace(stdout=''))), 4.0)
        self.assertEqual(sleepwatch.max_delay(run=mock.Mock(return_value=SimpleNamespace(stdout='t many'))), 4.0)


class JobsTest(unittest.TestCase):
    def test_every_machine_device_and_worker(self):
        cfg = {'machines': {'m1': {}}, 'devices': {'d1': {}}, 'workers': {'w1': {}, 'w2': {}}}
        self.assertEqual(sleepwatch.jobs(cfg), [('machine', 'm1'), ('device', 'd1'), ('worker', 'w1'), ('worker', 'w2')])

    def test_missing_or_empty_sections(self):
        self.assertEqual(sleepwatch.jobs({}), [])
        self.assertEqual(sleepwatch.jobs({'machines': None}), [])


class GoingDownTest(unittest.TestCase):
    def setUp(self):
        self.pst = {'targets': {}, 'resume': []}
        self.leases = {'leases': {'l1': {'pid': 42}}}
        self.tasks = {'tasks': [{'id': 't1', 'status': 'running'}, {'id': 't2', 'status': 'done'}]}
        self.fakes = {}
        for name, kw in (
            ('presence.read', {'return_value': self.pst}),
            ('presence.write', {}),
            ('config.state_dir', {'return_value': 'state'}),
            ('dispatch.locked', {'side_effect': lambda cfg: contextlib.nullcontext(self.leases)}),
            ('dispatch.stop_pid', {}),
            ('dispatch.requeue', {}),
            ('backlog.locked', {'side_effect': lambda cfg: contextlib.nullcontext(self.tasks)}),
            ('autopilot.pause', {}),
            ('telemetry.record', {}),
            ('sleepwatch.stop_one', {'return_value': {'kind': 'test', 'args': ['test'], 'track': 'run'}}),
        ):
            patcher = mock.patch(f'tutti.{name}', **kw)
            self.fakes[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def down(self, budget):
        running = mock.Mock(return_value=[('job.log', 7)])
        return sleepwatch.going_down({'machines': {'m1': {}}}, 'sleep', budget, now=lambda: 100.0, running=running,
                                     end='end', keep='keep')

    def test_stops_jobs_requeues_leases_and_notes_what_to_start_again(self):
        resume = self.down(10)
        self.assertEqual(resume, [{'kind': 'test', 'args': ['test'], 'track': 'run'}])
        self.fakes['sleepwatch.stop_one'].assert_called_once_with(mock.ANY, 'job.log', 7, 'this computer is going to sleep', 'end', 'keep')
        self.fakes['dispatch.stop_pid'].assert_called_once_with(42)
        self.assertIn('not counted as a failure', self.fakes['dispatch.requeue'].call_args.args[2])
        self.assertEqual(self.leases['leases'], {})
        self.fakes['autopilot.pause'].assert_called_once_with(self.tasks['tasks'][0], 'this computer is going to sleep')
        self.assertEqual(self.pst['targets'][sleepwatch.HOST]['state'], presence.ASLEEP)
        self.fakes['presence.write'].assert_called_once()

    def test_no_budget_left_stops_no_job_but_still_hands_over(self):
        self.assertEqual(self.down(0), [])
        self.fakes['sleepwatch.stop_one'].assert_not_called()
        self.fakes['dispatch.stop_pid'].assert_called_once_with(42)
        self.assertEqual(self.pst['targets'][sleepwatch.HOST]['state'], presence.ASLEEP)


class StopOneTest(unittest.TestCase):
    def setUp(self):
        self.log = os.path.join(tmpdirs.mkdtemp(), 'job.log')
        self.end, self.keep = mock.Mock(), mock.Mock(return_value='work kept')

    def stop(self, spec):
        if spec is not None:
            with open(self.log + '.job', 'w', encoding='utf8') as f:
                json.dump(spec, f)
        with mock.patch('tutti.handover.worktree_of', return_value='wt'):
            return sleepwatch.stop_one({}, self.log, 7, 'going to sleep', self.end, self.keep)

    def read(self, ext):
        with open(self.log + ext, encoding='utf8') as f:
            return f.read()

    def test_an_agent_run_keeps_its_work_and_is_noted(self):
        spec = {'kind': 'run', 'track': 'fix', 'run_id': 'r1'}
        self.assertEqual(self.stop(spec), {'kind': 'run', 'spec': spec, 'why': 'going to sleep', 'kept': 'work kept'})
        self.end.assert_called_once_with(7)
        self.keep.assert_called_once_with('wt', 'fix', 'r1')
        self.assertEqual(self.read('.rc'), '143\n')
        self.assertIn('going to sleep', self.read('.handoff'))

    def test_a_test_job_is_noted_with_its_args(self):
        note = self.stop({'kind': 'test', 'track': 'ui', 'args': ['--fast']})
        self.assertEqual(note, {'kind': 'test', 'args': ['test', '--fast'], 'track': 'ui'})
        self.keep.assert_not_called()

    def test_jobs_started_another_way_are_not_noted(self):
        self.assertIsNone(self.stop({'kind': 'run', 'track': autopilot.TRACK_PREFIX + 't1'}))
        self.assertIsNone(self.stop({'kind': 'test', 'track': 'dispatch-1'}))

    def test_an_unreadable_spec_still_stops_the_job(self):
        self.assertIsNone(self.stop(None))
        self.end.assert_called_once_with(7)
        self.assertEqual(self.read('.rc'), '143\n')


class WokeTest(unittest.TestCase):
    def wake(self, resume):
        pst = {'targets': {}, 'resume': resume}
        order = []
        start = mock.Mock(side_effect=lambda cfg, args, track: order.append(('start', track)) or track)
        with mock.patch('tutti.presence.read', return_value=pst), \
                mock.patch('tutti.presence.write', side_effect=lambda cfg, data: order.append(('write', list(data['resume'])))):
            out = sleepwatch.woke({}, now=lambda: 100.0, start=start)
        return out, pst, start, order

    def test_starts_the_noted_jobs_after_marking_the_host_present(self):
        spec = {'task': 'fix it', 'worker': 'w1', 'repo': 'repo', 'track': 'fix', 'files': ['a.py']}
        run = {'kind': 'run', 'spec': spec, 'why': 'it slept', 'kept': 'work kept'}
        out, pst, start, order = self.wake([run, {'kind': 'test', 'args': ['test'], 'track': 'ui'}])
        self.assertEqual(out, ['run-fix', 'test-ui'])
        self.assertEqual(order[0], ('write', []))   # written before anything starts: a crash never starts a job twice
        self.assertEqual(pst['targets'][sleepwatch.HOST]['state'], presence.PRESENT)
        args = start.call_args_list[0].args[1]
        self.assertEqual(args[:2], ['run', 'w1'])
        self.assertIn('it slept', args[2])
        self.assertEqual(args[3:], ['a.py', '--repo', 'repo', '--track', 'fix', '--bg', *handover.check_args(None)])

    def test_nothing_noted_starts_nothing(self):
        out, pst, start, _ = self.wake([])
        self.assertEqual(out, [])
        start.assert_not_called()
        self.assertEqual(pst['targets'][sleepwatch.HOST]['state'], presence.PRESENT)


class WatchTest(unittest.TestCase):
    def watch(self, lines, down):
        mon, locks = FakeProc(lines), []

        def popen(cmd, **kw):
            if cmd == sleepwatch.MONITOR:
                return mon
            locks.append(FakeProc())
            return locks[-1]

        up = mock.Mock()
        sleepwatch.watch({}, popen=popen, delay=lambda: 3.0, down=down, up=up)
        return mon, locks, up

    def test_hands_over_lets_go_and_takes_the_lock_again_on_wake(self):
        down = mock.Mock()
        mon, locks, up = self.watch(['noise\n', SLEEP, WAKE], down)
        down.assert_called_once_with({}, 'sleep', 3.0)
        up.assert_called_once_with({})
        self.assertEqual(len(locks), 2)
        self.assertTrue(locks[0].terminated and locks[1].terminated and mon.terminated)

    def test_a_failed_hand_over_still_lets_the_os_go_on(self):
        down = mock.Mock(side_effect=RuntimeError('state unreadable'))
        mon, locks, up = self.watch([SLEEP, SLEEP], down)
        down.assert_called_once()   # the second signal finds no lock held: nothing to hand over twice
        self.assertEqual(len(locks), 1)
        self.assertTrue(locks[0].terminated and mon.terminated)
        up.assert_not_called()


class StartInBackgroundTest(unittest.TestCase):
    def start(self, platform='linux', settings=None, which='/usr/bin/x'):
        threading = mock.Mock()
        with mock.patch('tutti.sleepwatch.sys', SimpleNamespace(platform=platform)), \
                mock.patch('tutti.presence.settings', return_value=settings or {}), \
                mock.patch('tutti.sleepwatch.shutil', SimpleNamespace(which=lambda name: which)), \
                mock.patch('tutti.sleepwatch.threading', threading):
            return sleepwatch.start_in_background({}), threading.Thread

    def test_starts_a_daemon_thread_on_linux_with_the_tools(self):
        thread, made = self.start()
        made.assert_called_once_with(target=sleepwatch.watch, args=({},), name='tutti-sleepwatch', daemon=True)
        thread.start.assert_called_once()

    def test_off_elsewhere_when_switched_off_or_without_the_tools(self):
        for kw in ({'platform': 'darwin'}, {'settings': {'sleep_watch': False}}, {'which': None}):
            with self.subTest(**kw):
                thread, made = self.start(**kw)
                self.assertIsNone(thread)
                made.assert_not_called()


if __name__ == '__main__':
    unittest.main()
