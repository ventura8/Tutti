"""Linked worktrees: the git binary, shared dependency folders, the docker mount, the stage rewrite and the stall wait."""
import os
import subprocess
import tempfile
import unittest

from tutti import config, remote, waiting, worktree


def git(cwd, *args):
    return subprocess.run([worktree.git_bin(), '-C', cwd, *args], check=True, capture_output=True, text=True).stdout.strip()


class WorktreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.main, self.wt = os.path.join(self.tmp.name, 'main'), os.path.join(self.tmp.name, 'wt')
        os.makedirs(os.path.join(self.main, 'node_modules', 'x'))
        os.makedirs(os.path.join(self.main, 'pkg', 'a', 'node_modules'))
        with open(os.path.join(self.main, 'node_modules', 'x', 'f.js'), 'w') as f:
            f.write('1')
        git(self.main, 'init', '-q', '-b', 'main')
        os.makedirs(os.path.join(self.main, 'pkg', 'a', 'src'), exist_ok=True)
        with open(os.path.join(self.main, 'pkg', 'a', 'src', 'i.js'), 'w') as f:
            f.write('2')
        git(self.main, 'add', 'pkg')
        git(self.main, '-c', 'user.email=t@example.invalid', '-c', 'user.name=T', 'commit', '-q', '-m', 'c')
        git(self.main, 'worktree', 'add', '-q', self.wt)

    def tearDown(self):
        self.tmp.cleanup()

    def test_git_bin_honours_the_override(self):
        self.assertEqual(worktree.git_bin({'TUTTI_GIT': '/opt/g'}), '/opt/g')
        self.assertTrue(worktree.git_bin({}))

    def test_share_hard_links_missing_folders_only(self):
        done = worktree.share(self.wt, ['node_modules', 'pkg/*/node_modules'], say=lambda m: None)
        self.assertEqual(done, ['node_modules', 'pkg/a/node_modules'])
        a, b = (os.stat(os.path.join(d, 'node_modules', 'x', 'f.js')).st_ino for d in (self.main, self.wt))
        self.assertEqual(a, b)
        self.assertEqual(worktree.share(self.wt, ['node_modules'], say=lambda m: None), [])
        self.assertEqual(worktree.share(self.main, ['node_modules'], say=lambda m: None), [])

    def test_docker_mount_names_the_common_git_dir(self):
        c = os.path.realpath(os.path.join(self.main, '.git'))
        self.assertEqual(os.path.realpath(worktree.docker_mount(self.wt).split(':')[-1]), c)
        self.assertEqual(worktree.docker_mount(self.main), '')

    def test_runner_rewrites_and_mounts(self):
        cfg = config.normalise({'machines': {'a': {'host': 'h', 'runner': {
            'kind': 'docker', 'image': 'img:1', 'rewrite': {'run-all-checks.sh': 'in-container.sh'}}}}})
        line = remote.runner_command(cfg['machines']['a'], 't', 'bash run-all-checks.sh', mount='-v /g:/g')
        self.assertIn('in-container.sh', line)
        self.assertNotIn('run-all-checks', line)
        self.assertIn('-v /g:/g img:1', line)


class StallTest(unittest.TestCase):
    def test_stalled_after_quiet_seconds(self):
        with tempfile.NamedTemporaryFile() as f:
            m = os.path.getmtime(f.name)
            self.assertFalse(waiting.stalled(f.name, 10, clock=lambda: m + 5)())
            self.assertTrue(waiting.stalled(f.name, 10, clock=lambda: m + 11)())
        self.assertFalse(waiting.stalled('/nonexistent/x', 1)())
