"""autopilot.repos.<r>.fix: formatters run on a run's change before its check (map runs failed on prettier alone)."""
import os
import tempfile
import unittest

from tutti import delegate


class RepoFix(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.repo = d.name
        with open(os.path.join(self.repo, 'a.ts'), 'w') as f:
            f.write('x')

    def cfg(self, fix):
        return {'autopilot': {'repos': {'map': {'path': self.repo, 'fix': fix}, 'bad': 'not a dict'}}}

    def test_the_fix_runs_over_the_changed_files(self):
        said = []
        self.assertTrue(delegate.apply_fix(self.cfg('for f in {changed:ts}; do echo fixed > "$f"; done'), self.repo, self.repo,
                                           ['a.ts', 'b.py'], said.append))
        with open(os.path.join(self.repo, 'a.ts')) as f:
            self.assertEqual(f.read().strip(), 'fixed')
        self.assertIn('fix step done', said[0])

    def test_a_failing_fix_is_said_and_no_fix_means_nothing_runs(self):
        said = []
        delegate.apply_fix(self.cfg('exit 3'), self.repo, self.repo, ['a.ts'], said.append)
        self.assertIn('exited 3', said[0])
        self.assertFalse(delegate.apply_fix(self.cfg(''), self.repo, self.repo, ['a.ts'], said.append))
        self.assertFalse(delegate.apply_fix(self.cfg('exit 3'), self.repo, self.repo, [], said.append))
        self.assertEqual(delegate.fix_for({}, self.repo), '')


if __name__ == '__main__':
    unittest.main()
