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


    def test_runs_show_exits_1_for_a_failed_run_even_without_a_stored_rc(self):
        import types
        from unittest import mock

        from tutti import cli
        for stored, want in (({'rc': 1}, 1), ({'rc': 0}, 0), ({'changed': []}, 1)):
            r = {k: v for k, v in run(**({'changed': []} if 'changed' in stored else {})).items()}
            r.update({k: v for k, v in stored.items() if k == 'rc'})
            a = types.SimpleNamespace(action='show', id='1795000000-1')
            with mock.patch.object(delegate, 'load_report', return_value=r), \
                    mock.patch.object(delegate, 'paths', return_value={'report': __file__}), \
                    mock.patch('builtins.print'):
                self.assertEqual(cli.cmd_runs({}, a), want, stored)

if __name__ == '__main__':
    unittest.main()
