"""Fix rounds (delegate.fix_rounds): a failing check's errors go back to the same worker before a run is reported FAILED."""
import os
import unittest
from unittest import mock

import tmpdirs

from tutti import delegate


def cfg(rounds=3, kind='agy'):
    return {'delegate': {'fix_rounds': rounds}, 'workers': {'w': {'kind': kind}}}


def check(rc, where='local', verdict='error TS2345'):
    return {'run': 'npm test', 'rc': rc, 'where': where, 'verdict': verdict, 'log': None}


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


class RoundsKeepTheGoodDiff(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdirs.mkdtemp()
        self.diff = os.path.join(self.dir, 'run.diff')
        with open(self.diff, 'w', encoding='utf8') as f:
            f.write('GOOD\n')

    def go(self, first, rc, recaptured, results=(0,)):
        r = {'checks': [first], 'changed': ['a.ts'], 'stat': 'old'}
        said, checks = [], iter(results)

        def work(*a):
            with open(self.diff, 'w', encoding='utf8') as f:
                f.write('BAD\n')
            return rc, []

        def recapture(*a):
            return recaptured
        with mock.patch.object(delegate, 'diff_after_fix', side_effect=recapture):
            delegate.fix_rounds(cfg(), 'w', 'do it', [], '/repo', '/wt', None, (), {'worker_log': '', 'diff': self.diff, 'dir': ''},
                                None, [], {}, 't', r, said.append, work=work, check=lambda *a: [check(next(checks))])
        return r, said

    def read(self):
        with open(self.diff, encoding='utf8') as f:
            return f.read()

    def test_a_round_that_leaves_nothing_restores_the_good_diff_and_checks(self):
        r, said = self.go(check(1), 0, ([], ''))
        self.assertEqual((self.read(), r['changed'], r['checks'][0]['rc']), ('GOOD\n', ['a.ts'], 1))
        self.assertIn('left no change', said[-1])

    def test_a_failed_worker_with_nothing_left_restores_too_and_one_with_changes_reports_the_worktree(self):
        r, _ = self.go(check(1), 7, ([], ''))
        self.assertEqual(self.read(), 'GOOD\n')
        r, _ = self.go(check(1), 7, (['b.ts'], '1 file'))
        self.assertEqual((r['changed'], r['checks'][0]['rc']), (['b.ts'], 1))   # the report matches the worktree; old checks stand

    def test_a_good_round_keeps_its_new_diff(self):
        r, _ = self.go(check(1), 0, (['b.ts'], 'new'), results=[0])
        self.assertEqual((self.read(), r['changed'], r['checks'][0]['rc']), ('BAD\n', ['b.ts'], 0))
        self.assertTrue(os.path.isfile(self.diff + '.round0'))


class NotACodeFailure(unittest.TestCase):
    def test_a_timeout_or_an_unreachable_machine_is_not_a_code_failure(self):
        self.assertTrue(delegate.code_failure(check(1)))
        for c in (check(3, 'none'), check(124), check(255), check(1, 'box.example', 'host unreachable'), check(1, 'x', 'Timed out')):
            self.assertFalse(delegate.code_failure(c), c)
        self.assertFalse(delegate.code_failure(check(0)))

    def test_no_round_runs_for_such_a_failure(self):
        r = {'checks': [check(3, 'none', '')]}
        said = []
        delegate.fix_rounds(cfg(), 'w', 'do it', [], '/repo', '/wt', None, (), {'worker_log': '', 'diff': '', 'dir': ''}, None,
                            [], {}, 't', r, said.append, work=lambda *a: self.fail('no round'), check=lambda *a: self.fail('no check'))
        self.assertEqual(r['fix_rounds'], 0)
        self.assertIn('not a code failure', said[0])


class TrackIsResolvedOnce(unittest.TestCase):
    def test_the_full_worktree_name_is_the_track_everywhere(self):
        c = {'delegate': {'prefix': 'tutti-run-', 'remote_track': 'tutti'}, 'project': {'state': tmpdirs.mkdtemp()}}
        seen = []
        with mock.patch.object(delegate, '_delegate', side_effect=lambda *a: seen.append(a[6]) or {}):
            delegate.delegate(c, 'w', 't', [], '/r', track='tutti-run-123-4', checks=False)
            delegate.delegate(c, 'w', 't', [], '/r', track='search', checks=False)
        self.assertEqual(seen, ['123-4', 'search'])

    def test_a_bad_track_is_still_refused(self):
        with self.assertRaises(delegate.ship.ShipError):
            delegate.delegate({'delegate': {'prefix': 'p-', 'remote_track': 't'}}, 'w', 't', [], '/r', track='a/b', checks=False)


if __name__ == '__main__':
    unittest.main()
