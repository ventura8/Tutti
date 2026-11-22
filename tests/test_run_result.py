"""A run report opens with its result: a run that changed nothing is FAILED, not exit 0 (map session, 2026-11-22)."""
import unittest

from tutti import delegate


def run(**kw):
    r = {'id': '1-1', 'worker': 'agy', 'task': 't', 'worker_rc': 0, 'worktree': '/w', 'diff': '/d', 'changed': ['a.ts'],
         'stat': '', 'denied': [], 'checks': [{'where': 'local', 'rc': 0, 'verdict': '', 'log': 'l', 'coverage': ''}]}
    return {**r, **kw}


class RunResult(unittest.TestCase):
    def test_a_run_that_changed_nothing_is_failed(self):
        text = delegate.summary(run(changed=[], checks=[]))
        self.assertTrue(text.startswith('result    FAILED: the worker changed nothing'))
        self.assertEqual(delegate.verdict(run(changed=[], checks=[])), 1)

    def test_passed_failed_and_untested(self):
        self.assertEqual(delegate.outcome(run()), 'PASSED')
        self.assertTrue(delegate.outcome(run(checks=[{'rc': 2}])).startswith('FAILED: 1 check'))
        self.assertTrue(delegate.outcome(run(checks=[])).startswith('CHANGED, NOT TESTED'))
        self.assertTrue(delegate.outcome(run(worker_rc=3)).startswith('FAILED: the worker exited 3'))
        self.assertTrue(delegate.outcome(run(denied=['x'])).startswith('FAILED: the worker\'s CLI refused'))


if __name__ == '__main__':
    unittest.main()
