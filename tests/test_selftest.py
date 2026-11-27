import json
import os
import unittest
from unittest.mock import patch

import tmpdirs

from tutti import backlog, selftest


class SelfTest(unittest.TestCase):
    def test_runs_after_each_start_and_daily(self):
        first = {'started': 100, 'timestamp': 180}
        self.assertFalse(selftest.due(first, 200, 259))
        self.assertTrue(selftest.due(first, 200, 260))
        self.assertFalse(selftest.due(first, 100, 180 + selftest.EVERY - 1))
        self.assertTrue(selftest.due(first, 100, 180 + selftest.EVERY))

    def test_failure_creates_a_replay_case_and_one_backlog_task(self):
        folder = tmpdirs.mkdtemp()
        cfg = {'project': {'state': folder}}
        checks = {'page': True, 'state': False}
        with patch.object(selftest, 'run_browser', return_value=checks):
            result = selftest.Runner.measure(cfg, 'http://127.0.0.1:1', '')
            selftest.Runner.measure(cfg, 'http://127.0.0.1:1', '')
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(result['missed'], [selftest.CHECKS['state']])
        with open(os.path.join(folder, 'selftest-fixtures', 'state.json'), encoding='utf8') as f:
            self.assertEqual(json.load(f), selftest.fixture_for('state'))
        self.assertEqual(len(backlog.load(cfg)['tasks']), 1)

    def test_pass_has_no_findings(self):
        cfg = {'project': {'state': tmpdirs.mkdtemp()}}
        with patch.object(selftest, 'run_browser', return_value=dict.fromkeys(selftest.CHECKS, True)):
            result = selftest.Runner.measure(cfg, 'http://127.0.0.1:1', '')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['missed'], [])
        self.assertEqual(backlog.load(cfg)['tasks'], [])

    def test_browser_setup_errors_are_not_persisted_verbatim(self):
        with patch.object(selftest, 'run_browser', side_effect=RuntimeError('private diagnostic')):
            result = selftest.Runner.measure({}, 'http://127.0.0.1:1', '')
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['message'], selftest.UNAVAILABLE)
