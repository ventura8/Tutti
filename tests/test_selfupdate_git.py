"""`tutti self-update` on a source (git) checkout: a fast-forward pull when it is clean, on its default branch and tracking
origin; a plain refusal otherwise. Real git, in temp folders only: never the live install."""
import os
import subprocess
import unittest
import unittest.mock

import tmpdirs

from tutti import config, install

ENV = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.test', 'GIT_COMMITTER_NAME': 't',
       'GIT_COMMITTER_EMAIL': 't@example.test', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name):
    with open(os.path.join(repo, name), 'w', encoding='utf8') as f:
        f.write(name)
    git(repo, 'add', name)
    git(repo, 'commit', '-qm', name)


class Fixture(unittest.TestCase):
    def setUp(self):
        base = tmpdirs.mkdtemp()
        self.origin, self.work, self.clone = (os.path.join(base, n) for n in ('origin.git', 'work', 'clone'))
        git(base, 'init', '-q', '--bare', '-b', 'main', self.origin)
        git(base, 'clone', '-q', self.origin, self.work)
        git(self.work, 'checkout', '-q', '-B', 'main')
        commit(self.work, 'a.txt')
        git(self.work, 'push', '-q', 'origin', 'main')
        git(base, 'clone', '-q', self.origin, self.clone)
        patcher = unittest.mock.patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ahead(self):
        commit(self.work, 'b.txt')
        git(self.work, 'push', '-q', 'origin', 'main')


class GitUpdateTest(Fixture):
    def test_pulls_a_clean_default_branch_fast_forward(self):
        self.ahead()
        code, msg = install.git_update(self.clone)
        self.assertEqual(code, 0, msg)
        self.assertIn('pulled 1 commit', msg)
        self.assertTrue(os.path.exists(os.path.join(self.clone, 'b.txt')))

    def test_up_to_date_and_untracked_files_do_not_block(self):
        with open(os.path.join(self.clone, 'scratch'), 'w', encoding='utf8') as f:
            f.write('x')
        self.assertEqual(install.git_update(self.clone)[0], 0)
        self.assertIn('up to date', install.git_update(self.clone)[1])

    def test_check_only_reports_and_changes_nothing(self):
        self.ahead()
        code, msg = install.git_update(self.clone, check_only=True)
        self.assertEqual(code, 0)
        self.assertIn('1 commit(s) ahead', msg)
        self.assertFalse(os.path.exists(os.path.join(self.clone, 'b.txt')))

    def test_refuses_local_changes(self):
        self.ahead()
        with open(os.path.join(self.clone, 'a.txt'), 'w', encoding='utf8') as f:
            f.write('mine')
        code, msg = install.git_update(self.clone)
        self.assertEqual(code, 1)
        self.assertIn('local changes', msg)
        self.assertFalse(os.path.exists(os.path.join(self.clone, 'b.txt')))

    def test_refuses_a_branch_that_is_not_the_default(self):
        self.ahead()
        git(self.clone, 'checkout', '-q', '-b', 'feature')
        git(self.clone, 'push', '-q', '-u', 'origin', 'feature')
        code, msg = install.git_update(self.clone)
        self.assertEqual(code, 1)
        self.assertIn('not its default branch (main)', msg)
        self.assertIn('git switch main', msg)

    def test_refuses_a_checkout_that_does_not_track_origin(self):
        git(self.clone, 'branch', '--unset-upstream')
        code, msg = install.git_update(self.clone)
        self.assertEqual(code, 1)
        self.assertIn('does not track a branch of origin', msg)

    def test_refuses_a_detached_head(self):
        git(self.clone, 'checkout', '-q', '--detach')
        self.assertIn('detached HEAD', install.git_update(self.clone)[1])

    def test_a_diverged_checkout_fails_with_the_fix_and_changes_nothing(self):
        self.ahead()
        commit(self.clone, 'mine.txt')
        before = git(self.clone, 'rev-parse', 'HEAD')
        code, msg = install.git_update(self.clone)
        self.assertEqual(code, 1)
        self.assertIn('rebase or reset', msg)
        self.assertEqual(git(self.clone, 'rev-parse', 'HEAD'), before)

    def test_an_unreachable_origin_is_a_retry_not_a_change(self):
        git(self.clone, 'remote', 'set-url', 'origin', os.path.join(self.origin, 'gone'))
        code, msg = install.git_update(self.clone)
        self.assertEqual(code, 1)
        self.assertIn('git fetch failed', msg)

    def test_is_git_checkout(self):
        self.assertTrue(install.is_git_checkout(self.clone))
        self.assertFalse(install.is_git_checkout(tmpdirs.mkdtemp()))


class CommandTest(Fixture):
    def run_cmd(self, root, **flags):
        from types import SimpleNamespace

        from tutti import cli
        a = SimpleNamespace(check=False, auto=flags.pop('auto', False), config=None, insecure_skip_verify=False, stage_only=False, **flags)
        said = []
        with unittest.mock.patch.object(cli.remote, 'ROOT', root), \
                unittest.mock.patch.object(install, 'home', return_value=tmpdirs.mkdtemp()), \
                unittest.mock.patch.object(config, 'load', return_value={}), \
                unittest.mock.patch.object(cli, 'say', said.append):
            return cli.cmd_self_update({}, a), said

    def test_a_git_checkout_is_updated_not_refused(self):
        self.ahead()
        (code, said) = self.run_cmd(self.clone)
        self.assertEqual(code, 0)
        self.assertIn('pulled 1 commit', said[0])

    def test_a_dirty_checkout_exits_1_but_at_login_it_never_fails_the_service(self):
        with open(os.path.join(self.clone, 'a.txt'), 'w', encoding='utf8') as f:
            f.write('mine')
        self.assertEqual(self.run_cmd(self.clone)[0], 1)
        self.assertEqual(self.run_cmd(self.clone, auto=True)[0], 0)

    def test_a_folder_that_is_neither_release_nor_git_is_still_refused_with_the_way_to_update(self):
        code, said = self.run_cmd(tmpdirs.mkdtemp())
        self.assertEqual(code, 1)
        self.assertIn('pipx upgrade', said[0])


if __name__ == '__main__':
    unittest.main()
