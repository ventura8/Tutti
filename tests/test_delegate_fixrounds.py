"""Fix rounds (delegate.fix_rounds): a failing check's errors go back to the same worker before a run is reported FAILED."""
import unittest
from unittest import mock

from tutti import delegate


def cfg(rounds=3, kind='agy'):
    return {'delegate': {'fix_rounds': rounds}, 'workers': {'w': {'kind': kind}}}


def check(rc):
    return {'run': 'npm test', 'rc': rc, 'where': 'local', 'verdict': 'error TS2345', 'log': None}


class FixRounds(unittest.TestCase):
    def go(self, c, results):
        r = {'checks': [check(1)]}
        tasks = []
        checks = iter(results)

        def work(*a):
            tasks.append(a[2])
            return 0, []
        with mock.patch.object(delegate, 'diff_after_fix', return_value=(['a.ts'], '1 file')):
            delegate.fix_rounds(c, 'w', 'do it', [], '/repo', '/wt', None, (), {'worker_log': '', 'diff': '', 'dir': ''}, None,
                                [], {}, 't', r, lambda m: None, work=work, check=lambda *a: [check(next(checks))])
        return r, tasks

    def test_a_check_error_goes_back_and_the_fixed_run_passes(self):
        r, tasks = self.go(cfg(), [0])
        self.assertEqual(r['fix_rounds'], 1)
        self.assertEqual(r['checks'][0]['rc'], 0)
        self.assertEqual(delegate.outcome({**r, 'worker_rc': 0, 'denied': [], 'changed': ['a.ts']}), 'PASSED')
        self.assertIn('error TS2345', tasks[0])
        self.assertIn('Fix round 1 of 3', tasks[0])
        self.assertTrue(tasks[0].startswith('do it'))

    def test_a_check_that_keeps_failing_stops_after_the_rounds(self):
        r, tasks = self.go(cfg(2), [1, 1, 1])
        self.assertEqual((r['fix_rounds'], len(tasks)), (2, 2))
        self.assertIn('FAILED', delegate.outcome({**r, 'worker_rc': 0, 'denied': [], 'changed': ['a.ts']}))

    def test_no_rounds_for_zero_or_a_local_model(self):
        for c in (cfg(0), cfg(3, 'ollama')):
            r, tasks = self.go(c, [])
            self.assertEqual((r['fix_rounds'], tasks), (0, []))

    def base_go(self, results):
        r = {'checks': results}
        tasks = []

        def work(*a):
            tasks.append(a[2])
            return 0, []
        with mock.patch.object(delegate, 'diff_after_fix', return_value=(['a.ts'], '1 file')):
            delegate.fix_rounds(cfg(), 'w', 'do it', [], '/repo', '/wt', None, (), {'worker_log': '', 'diff': '', 'dir': ''}, None,
                                [], {}, 't', r, lambda m: None, work=work,
                                check=lambda *a: [check(0), {**check(1), 'run': 'make base'}])
        return r, tasks

    def test_only_the_changes_own_failure_goes_back(self):
        own = {**check(1), 'run': 'npm test'}
        base = {**check(1), 'run': 'make base', 'base_broken': True, 'base_rc': 1}
        r, tasks = self.base_go([own, base])
        self.assertEqual(r['fix_rounds'], 1)
        self.assertIn('npm test', tasks[0])
        self.assertNotIn('make base', tasks[0])
        self.assertTrue(r['checks'][1]['base_broken'])   # still marked after the re-run
        self.assertEqual(r['checks'][0]['rc'], 0)

    def test_all_base_broken_runs_no_round(self):
        base = {**check(1), 'base_broken': True, 'base_rc': 1}
        r, tasks = self.base_go([base])
        self.assertEqual((r['fix_rounds'], tasks), (0, []))


if __name__ == '__main__':
    unittest.main()
