"""A delegated run starts from fresh upstream when the local checkout is behind (map session, 2026-11-22)."""
import os
import subprocess
import tempfile
import unittest

from tutti import delegate


def git(cwd, *a):
    return subprocess.run(['git', '-C', cwd, *a], check=True, capture_output=True, text=True).stdout.strip()


class FreshBase(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.up, self.local = os.path.join(d.name, 'up'), os.path.join(d.name, 'local')
        os.makedirs(self.up)
        git(self.up, 'init', '-q', '-b', 'main')
        git(self.up, '-c', 'user.name=t', '-c', 'user.email=t@x.example', 'commit', '-q', '--allow-empty', '-m', 'one')
        subprocess.run(['git', 'clone', '-q', self.up, self.local], check=True)

    def commit(self, where, msg):
        git(where, '-c', 'user.name=t', '-c', 'user.email=t@x.example', 'commit', '-q', '--allow-empty', '-m', msg)

    def test_a_checkout_behind_its_upstream_starts_from_the_upstream(self):
        self.commit(self.up, 'two')
        said = []
        self.assertEqual(delegate.fresh_base(self.local, said.append), 'origin/main')
        self.assertIn('behind origin/main', said[0])

    def test_local_commits_are_kept(self):
        self.commit(self.local, 'mine')
        self.assertEqual(delegate.fresh_base(self.local, print), 'HEAD')

    def test_no_upstream_or_remote_means_head(self):
        self.assertEqual(delegate.fresh_base(self.up, print), 'HEAD')
        git(self.local, 'checkout', '-q', '--detach')
        self.assertEqual(delegate.fresh_base(self.local, print), 'HEAD')


if __name__ == '__main__':
    unittest.main()
