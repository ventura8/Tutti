"""Attempt estimates use comparable successful data and do not promise a queue start."""
import unittest

from tutti import autopilot, tasketa


def attempt(duration, step='worker', ok=True):
    return {'started': 100, 'at': 100 + duration, 'step': step, 'ok': ok}


class TaskEtaTest(unittest.TestCase):
    def test_running_estimate_uses_median_of_comparable_successes(self):
        task = {'repo': 'app', 'size': 'S', 'status': 'running', 'started': 1000}
        records = [dict(task, attempts=[attempt(100), attempt(200), attempt(300), attempt(999, ok=False)]),
                   {'repo': 'other', 'size': 'S', 'attempts': [attempt(10000)]},
                   {'repo': 'app', 'size': 'M', 'attempts': [attempt(10000)]},
                   dict(task, attempts=[attempt(10000, step='other')])]
        result = tasketa.estimate(task, 'worker', tasketa.profiles(records), 1050)
        self.assertEqual((result['duration_seconds'], result['remaining_seconds'], result['finish_at'], result['samples']),
                         (200, 150, 1200, 3))
        self.assertEqual(result['confidence'], 'rough')

    def test_missing_invalid_or_overdue_data_never_invents_finish(self):
        task = {'repo': 'app', 'size': 'S', 'status': 'queued'}
        records = [dict(task, attempts=[attempt(100), attempt(200), attempt(-5), {'started': True, 'at': 500, 'ok': True,
                                                                                  'step': 'worker'}])]
        history = tasketa.profiles(records)
        self.assertEqual(tasketa.estimate(task, 'worker', history, 1000)['state'], 'unknown')
        records[0]['attempts'].append(attempt(300))
        history = tasketa.profiles(records)
        queued = tasketa.estimate(task, 'worker', history, 1000)
        self.assertEqual(queued['state'], 'duration')
        self.assertNotIn('finish_at', queued)
        running = dict(task, status='running', started=1000)
        overdue = tasketa.estimate(running, 'worker', history, 1200)
        self.assertEqual(overdue['state'], 'overdue')
        self.assertNotIn('finish_at', overdue)
        self.assertEqual(tasketa.estimate(dict(running, started=1300), 'worker', history, 1200)['state'], 'unknown')

    def test_cli_explains_unknown_queue_time_and_overdue_attempt(self):
        base = {'id': 't1', 'title': 'A task', 'attempt_count': 0, 'status': 'queued'}
        queued = tasketa.estimate({'repo': 'app', 'size': 'S', 'status': 'queued'}, 'worker',
                                  {('app', 'S', 'worker'): [100, 200, 300]}, 1000)
        lines = '\n'.join(autopilot.task_lines('queued', dict(base, eta=queued)))
        self.assertIn('start and finish unknown', lines)
        overdue = dict(queued, state='overdue')
        lines = '\n'.join(autopilot.task_lines('queued', dict(base, eta=overdue)))
        self.assertIn('finish unknown', lines)
