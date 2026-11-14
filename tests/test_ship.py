"""ship against a throwaway repository with a local bare remote: nothing leaves the temp folder."""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tutti import config, ship


def git(cwd, *args):
    return subprocess.run(['git', '-C', cwd, *args], check=True, capture_output=True, text=True).stdout.strip()


class ShipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.origin, self.tree = os.path.join(d, 'origin.git'), os.path.join(d, 'wt', 'feature')
        git(d, 'init', '-q', '--bare', '-b', 'main', self.origin)
        os.makedirs(self.tree)
        git(self.tree, 'init', '-q', '-b', 'main')
        git(self.tree, 'config', 'user.email', 'tester@example.invalid')
        git(self.tree, 'config', 'user.name', 'Tester')
        git(self.tree, 'remote', 'add', 'origin', self.origin)
        self.write('a.txt', 'one\n')
        git(self.tree, 'add', '-A')
        git(self.tree, 'commit', '-q', '-m', 'chore: start')
        git(self.tree, 'push', '-q', 'origin', 'main')
        git(self.tree, 'checkout', '-q', '-b', 'work')
        self.said = []

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        with open(os.path.join(self.tree, name), 'w', encoding='utf8') as f:
            f.write(text)

    def cfg(self, **over):
        s = {'trailer': 'Co-Authored-By: A Worker <worker@example.invalid>', 'base': 'origin/main',
             'checks': [{'run': 'echo "$TUTTI_CHANGED" > ../changed.txt'}], 'push': {'remote': 'origin', 'branch': 'main'},
             'deploy': 'echo "$TUTTI_COMMIT" > ../deployed.txt', **over}
        return config.normalise({'project': {'worktrees': os.path.dirname(self.tree), 'state': os.path.join(self.tmp.name, 'state')},
                                 'ship': s})

    def ship(self, cfg, **kw):
        ship.ship(cfg, self.tree, 'feat: two', say=self.said.append, **kw)

    def test_happy_path_commits_checks_pushes_and_deploys(self):
        self.write('b.txt', 'two\n')
        self.ship(self.cfg())
        head = git(self.tree, 'rev-parse', 'HEAD')
        self.assertEqual(git(self.origin, 'rev-parse', 'main'), head)
        self.assertIn('Co-Authored-By: A Worker', git(self.tree, 'log', '-1', '--format=%B'))
        with open(os.path.join(self.tree, '..', 'changed.txt'), encoding='utf8') as f:
            self.assertEqual(f.read().strip(), 'b.txt')
        with open(os.path.join(self.tree, '..', 'deployed.txt'), encoding='utf8') as f:
            self.assertEqual(f.read().strip(), head)

    def test_a_failing_check_keeps_the_commit_and_does_not_push(self):
        self.write('b.txt', 'two\n')
        before = git(self.origin, 'rev-parse', 'main')
        with self.assertRaises(ship.ShipError) as ctx:
            self.ship(self.cfg(checks=[{'run': 'exit 3'}]))
        self.assertIn('NOT pushed', str(ctx.exception))
        self.assertEqual(git(self.origin, 'rev-parse', 'main'), before)
        self.assertEqual(git(self.tree, 'log', '-1', '--format=%s'), 'feat: two')

    def test_refuses_the_wrong_worktree_before_changing_anything(self):
        self.write('b.txt', 'two\n')
        with self.assertRaises(ship.ShipError) as ctx:
            self.ship(self.cfg(), track='other')
        self.assertIn('nothing was changed', str(ctx.exception))
        self.assertEqual(git(self.tree, 'log', '-1', '--format=%s'), 'chore: start')
        self.ship(self.cfg(), track='feature')

    def test_refuses_a_protected_branch_and_a_non_repository(self):
        git(self.tree, 'checkout', '-q', 'main')
        with self.assertRaises(ship.ShipError) as ctx:
            self.ship(self.cfg())
        self.assertIn('protected branch "main"', str(ctx.exception))
        with self.assertRaises(ship.ShipError):
            ship.ship(self.cfg(), self.tmp.name, 'feat: x', say=self.said.append)

    def test_refuses_conflict_markers_and_names_the_files(self):
        self.write('b.txt', 'two\n<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> other\n')
        self.write('README.md', 'Title\n=======\n')   # a heading underline alone is not a conflict
        before = git(self.origin, 'rev-parse', 'main')
        with self.assertRaises(ship.ShipError) as ctx:
            self.ship(self.cfg())
        self.assertIn('conflict markers', str(ctx.exception))
        self.assertIn('b.txt', str(ctx.exception))
        self.assertNotIn('README.md', str(ctx.exception))
        self.assertEqual((git(self.tree, 'log', '-1', '--format=%s'), git(self.origin, 'rev-parse', 'main')), ('chore: start', before))
        self.write('b.txt', 'two\n')
        self.ship(self.cfg())

    def test_refuses_a_rebase_or_merge_in_progress(self):
        self.write('a.txt', 'work\n')
        git(self.tree, 'commit', '-qam', 'feat: work')
        git(self.tree, 'checkout', '-q', 'main')
        self.write('a.txt', 'main\n')
        git(self.tree, 'commit', '-qam', 'feat: main')
        git(self.tree, 'checkout', '-q', 'work')
        subprocess.run(['git', '-C', self.tree, 'rebase', 'main'], capture_output=True)
        with self.assertRaises(ship.ShipError) as ctx:
            self.ship(self.cfg())
        self.assertIn('middle of a rebase', str(ctx.exception))
        git(self.tree, 'rebase', '--abort')
        subprocess.run(['git', '-C', self.tree, 'merge', 'main'], capture_output=True)
        with self.assertRaises(ship.ShipError) as ctx:
            self.ship(self.cfg())
        self.assertIn('middle of a merge', str(ctx.exception))

    def test_dry_run_changes_nothing(self):
        self.write('b.txt', 'two\n')
        self.ship(self.cfg(), dry_run=True)
        self.assertEqual(git(self.tree, 'log', '-1', '--format=%s'), 'chore: start')
        self.assertIn('push    origin HEAD:main', self.said[0])

    def test_quiet_check_prints_only_the_verdict(self):
        self.write('b.txt', 'two\n')
        self.ship(self.cfg(checks=[{'run': 'echo noise; echo "[SUCCESS] stage static passed"', 'quiet': True, 'verdict': r'\[SUCCESS\]'}]))
        self.assertTrue(any('[SUCCESS] stage static passed' in line for line in self.said))

    def test_shell_exports_tutti_env_on_the_bash_line(self):
        # WSL bash does not inherit the Windows process environment, so the variables are exported on the line itself;
        # a `K=v cmd` prefix would leave "$K" empty inside cmd (expanded before the assignment).
        with mock.patch('tutti.ship.subprocess.run') as run, mock.patch.object(ship.remote.os, 'name', 'posix'):
            run.return_value = subprocess.CompletedProcess(['bash'], 0)
            ship.shell('/tmp/tree', 'echo ok', {'TUTTI_CHANGED': 'b.txt', 'TUTTI_COMMIT': 'abc'})
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ['bash', '-c'])
        self.assertIn('export TUTTI_CHANGED=b.txt', args[2])
        self.assertIn('export TUTTI_COMMIT=abc', args[2])
        self.assertTrue(args[2].endswith('echo ok'))


if __name__ == '__main__':
    unittest.main()
