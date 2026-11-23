"""A worktree whose lockfile differs from the main checkout gets its own install, not the main checkout's packages."""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tutti import worktree


def git(cwd, *a):
    subprocess.run(['git', '-C', cwd, *a], check=True, capture_output=True)


class ShareLock(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.main, self.wt = os.path.join(d.name, 'main'), os.path.join(d.name, 'wt')
        os.makedirs(os.path.join(self.main, 'node_modules', 'x'))
        with open(os.path.join(self.main, 'package-lock.json'), 'w') as f:
            f.write('{"a": 1}')
        git(self.main, 'init', '-q')
        git(self.main, 'add', 'package-lock.json')
        git(self.main, '-c', 'user.name=t', '-c', 'user.email=t@x.example', 'commit', '-q', '-m', 'one')
        git(self.main, 'worktree', 'add', '-q', '--detach', self.wt)

    def test_the_same_lockfile_shares_the_packages(self):
        self.assertEqual(worktree.share(self.wt, ['node_modules'], print), ['node_modules'])

    def test_a_changed_lockfile_installs_its_own(self):
        with open(os.path.join(self.wt, 'package-lock.json'), 'w') as f:
            f.write('{"a": 2}')
        said, run = [], mock.Mock(return_value=mock.Mock(returncode=0, stdout='', stderr=''))
        with mock.patch('shutil.which', return_value='/usr/bin/npm'):
            self.assertEqual(worktree.share(self.wt, ['node_modules'], said.append, run), [])
        self.assertEqual(run.call_args[0][0][:2], ['npm', 'ci'])
        self.assertFalse(os.path.exists(os.path.join(self.wt, 'node_modules')))
        self.assertIn('installed its own dependencies', said[-1])

    def test_a_failed_install_says_the_fix(self):
        with open(os.path.join(self.wt, 'package-lock.json'), 'w') as f:
            f.write('{"a": 2}')
        said, run = [], mock.Mock(return_value=mock.Mock(returncode=1, stdout='', stderr='ERESOLVE'))
        with mock.patch('shutil.which', return_value='/usr/bin/npm'):
            worktree.share(self.wt, ['node_modules'], said.append, run)
        self.assertIn('npm ci failed', said[-1])


    def test_a_hung_install_times_out_with_the_fix(self):
        import subprocess
        said = []
        run = mock.Mock(side_effect=subprocess.TimeoutExpired('npm', 1))
        with mock.patch('shutil.which', return_value='/usr/bin/npm'):
            self.assertFalse(worktree.install(self.wt, said.append, run))
        self.assertIn('took over', said[-1])

    def test_a_yarn_lockfile_installs_with_yarn(self):
        os.remove(os.path.join(self.wt, 'package-lock.json'))
        open(os.path.join(self.wt, 'yarn.lock'), 'w').close()
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout='', stderr=''))
        with mock.patch('shutil.which', return_value='/usr/bin/yarn'):
            self.assertTrue(worktree.install(self.wt, print, run))
        self.assertEqual(run.call_args[0][0][0], 'yarn')

if __name__ == '__main__':
    unittest.main()
