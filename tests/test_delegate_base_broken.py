"""A check that fails on a run is run once on the run's base commit; one that fails there too is not the worker's fault."""
import os
import subprocess
import tempfile
import unittest

from tutti import delegate, worktree


def git(cwd, *a):
    return subprocess.run(['git', '-C', cwd, '-c', 'user.name=t', '-c', 'user.email=t@x.example', *a], check=True,
                          capture_output=True, text=True).stdout.strip()


class Pure(unittest.TestCase):
    def test_failed_on_both_is_marked(self):
        checks = [{'rc': 1}, {'rc': 1}, {'rc': 0}]
        delegate.mark_base_broken(checks, {0: 2, 1: 0})
        self.assertTrue(checks[0]['base_broken'])
        self.assertEqual(checks[0]['base_rc'], 2)
        self.assertNotIn('base_broken', checks[1])   # passes on the base: the change broke it
        self.assertNotIn('base_broken', checks[2])

    def test_no_base_result_marks_nothing(self):
        checks = [{'rc': 1}]
        delegate.mark_base_broken(checks, {})
        self.assertNotIn('base_broken', checks[0])

    def report(self, checks):
        return {'worker_rc': 0, 'denied': [], 'changed': ['a'], 'checks': checks}

    def test_outcome_says_the_base_is_broken(self):
        out = delegate.outcome(self.report([{'rc': 1, 'base_broken': True}]))
        self.assertEqual(out, 'FAILED: 1 check(s) fail on the base too (not this change): fix the base branch')

    def test_outcome_mixed_still_blames_the_change(self):
        out = delegate.outcome(self.report([{'rc': 1, 'base_broken': True}, {'rc': 1}]))
        self.assertEqual(out, 'FAILED: 2 check(s) failed (1 of them fail on the base too)')

    def test_names_new_file_reads_the_verdict(self):
        self.assertTrue(delegate.names_new_file({'verdict': 'src/new.ts:45 error'}, ['src/new.ts']))
        self.assertFalse(delegate.names_new_file({'verdict': 'src/old.ts:1 error'}, ['src/new.ts']))
        self.assertFalse(delegate.names_new_file({'verdict': 'x'}, []))

    def test_outcome_plain_failure(self):
        self.assertEqual(delegate.outcome(self.report([{'rc': 1}])), 'FAILED: 1 check(s) failed')


class OnBase(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.top = os.path.join(d.name, 'repo')
        os.makedirs(self.top)
        git(self.top, 'init', '-q', '-b', 'main')
        with open(os.path.join(self.top, 'flag'), 'w') as f:
            f.write('bad\n')
        git(self.top, 'add', '-A')
        git(self.top, 'commit', '-q', '-m', 'base')
        self.wt = os.path.join(d.name, 'wt')
        git(self.top, 'worktree', 'add', '-q', '--detach', self.wt, 'HEAD')
        self.folder = os.path.join(d.name, 'logs')
        os.makedirs(self.folder)
        self.cfg = {'delegate': {'base_check': True}, 'ship': {'share': []}, 'routing': {'order': []}, 'machines': {}, 'test': {}}
        self.said = []

    def run_base(self, chosen, base_check=True, changed=''):
        self.cfg['delegate']['base_check'] = base_check
        env = {'TUTTI_CHANGED': changed, 'TUTTI_TREE': self.wt, 'TUTTI_GIT_MOUNT': ''}
        results = delegate.run_checks(self.cfg, self.wt, chosen, env, 't', self.said.append, self.folder)
        return delegate.base_checks(self.cfg, self.wt, chosen, results, env, 't', self.said.append, self.folder)

    def worktrees(self):
        return git(self.top, 'worktree', 'list').count('\n') + 1

    def test_a_check_failing_on_the_base_too_is_base_broken(self):
        res = self.run_base([{'run': 'grep -q good flag'}])
        self.assertTrue(res[0]['base_broken'])
        self.assertEqual(self.worktrees(), 2)   # the temporary base worktree is gone

    def test_a_check_the_change_broke_is_not_marked(self):
        with open(os.path.join(self.wt, 'flag'), 'w') as f:
            f.write('worker edit\n')
        res = self.run_base([{'run': 'grep -q bad flag'}])
        self.assertNotEqual(res[0]['rc'], 0)
        self.assertNotIn('base_broken', res[0])
        self.assertEqual(self.worktrees(), 2)

    def test_an_error_in_a_file_the_worker_added_is_never_the_bases(self):
        with open(os.path.join(self.wt, 'geocoder.ts'), 'w') as f:   # a new, untracked file with a lint error
            f.write('bad\n')
        res = self.run_base([{'run': 'f=$(echo "$TUTTI_CHANGED"); grep -q good $f 2>&1 || { echo "error in $f"; exit 1; }'}],
                            changed='geocoder.ts')
        self.assertNotEqual(res[0]['rc'], 0)
        self.assertNotIn('base_broken', res[0])   # on the base the file is missing too, but that is the change's error

    def test_a_check_on_an_old_file_still_compares_with_the_base(self):
        res = self.run_base([{'run': 'grep -q good $TUTTI_CHANGED'}], changed='flag')
        self.assertTrue(res[0]['base_broken'])   # flag is in the base and fails there too

    def test_the_base_is_clean_of_the_workers_edits(self):
        with open(os.path.join(self.wt, 'flag'), 'w') as f:
            f.write('good\n')
        res = self.run_base([{'run': 'grep -q bad flag'}, {'run': 'grep -q good flag'}])
        self.assertEqual([c['rc'] != 0 for c in res], [True, False])
        self.assertNotIn('base_broken', res[0])   # fails on the worker's tree, passes on the base

    def test_a_passing_check_is_never_run_on_the_base(self):
        self.run_base([{'run': 'grep -q bad flag'}])
        self.assertFalse([m for m in self.said if 'on the base' in m])

    def test_switched_off_marks_nothing(self):
        res = self.run_base([{'run': 'grep -q good flag'}], base_check=False)
        self.assertNotIn('base_broken', res[0])

    def test_a_base_that_cannot_be_made_is_reported_not_raised(self):
        res = delegate.base_checks(self.cfg, self.folder, [{'run': 'false'}], [{'rc': 1}], {}, 't', self.said.append, self.folder)
        self.assertNotIn('base_broken', res[0])
        self.assertTrue([m for m in self.said if 'could not check the base' in m])
        self.assertTrue(worktree.git_bin())


if __name__ == '__main__':
    unittest.main()
