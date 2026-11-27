"""Per-machine read-only GitHub deploy keys (docs/GITHUB.md): repo/machine planning, SSH config merging, idempotent
provisioning, strictly read-only registration, error handling, `tutti github deploy-keys` and selfheal integration."""
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import tmpdirs

from tutti import config, deploykeys, github, githubcmd, selfheal


def make_cfg(folder, machines=None, projects=None):
    raw = {
        'project': {'worktrees': folder, 'state': os.path.join(folder, 'state')},
        'machines': machines or {'box-a': {'host': 'box-a.example', 'transport': 'ssh'},
                                 'box-b': {'host': 'box-b.example', 'transport': 'ssh'}},
        'projects': projects or {},
    }
    return config.normalise(raw)


class FakeGitHub:
    """In-memory GitHub deploy-key API: records every POST and enforces read_only."""

    def __init__(self, initial=None, fail_repo=None):
        self.keys = {k: list(v) for k, v in (initial or {}).items()}
        self.posted = []
        self.fail_repo = fail_repo

    def __call__(self, method, url, headers, body=None):
        path = url[len('https://api.github.com'):]
        h = {'x-ratelimit-remaining': '4999', 'x-ratelimit-reset': '9999999999'}
        if path.startswith('/repos/') and path.endswith('/keys'):
            repo = path[len('/repos/'):-len('/keys')]
            if repo == self.fail_repo:
                return 403, h, b'{"message": "Forbidden"}'
            if method == 'GET':
                return 200, h, json.dumps(self.keys.get(repo, [])).encode()
            if method == 'POST':
                data = json.loads(body)
                self.posted.append((repo, data))
                self.keys.setdefault(repo, []).append(data)
                return 201, h, json.dumps(data).encode()
        return 404, h, b'{"message": "Not Found"}'


class FakeRunner:
    """Captures remote argv calls and returns canned SSH public keys or failures."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        joined = ' '.join(argv)
        for needle, result in self.responses.items():
            if needle in joined:
                return result
        return SimpleNamespace(returncode=0, stdout='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey tutti-deploy\n', stderr='')


class DeployKeysPureTest(unittest.TestCase):
    def test_required_repos_respects_project_and_suite_machines(self):
        folder = tmpdirs.mkdtemp()
        cfg = make_cfg(folder, projects={
            'app-all': {'match': 'app-all', 'repo': 'octo/app-all'},
            'app-a': {'match': 'app-a', 'repo': 'octo/app-a', 'machines': ['box-a']},
            'app-suite': {'match': 'app-suite', 'repo': 'octo/app-suite',
                          'tests': {'e2e': {'machines': ['box-b'], 'run': 'echo ok'}}},
            'no-repo': {'match': 'no-repo'},
        })
        plan = deploykeys.required_repos(cfg)
        self.assertEqual(plan['box-a'], ['octo/app-a', 'octo/app-all'])
        self.assertEqual(plan['box-b'], ['octo/app-all', 'octo/app-suite'])

    def test_merge_ssh_config_is_idempotent_and_preserves_other_hosts(self):
        existing = 'Host other.example\n    User alice\n'
        once = deploykeys.merge_ssh_config(existing, 'octo/app', '~/.ssh/tutti-deploy/octo--app')
        twice = deploykeys.merge_ssh_config(once, 'octo/app', '~/.ssh/tutti-deploy/octo--app')
        self.assertEqual(once, twice)
        self.assertIn('Host other.example', once)
        self.assertIn('Host tutti-gh-octo--app', once)
        self.assertIn('IdentitiesOnly yes', once)
        self.assertIn('StrictHostKeyChecking accept-new', once)

    def test_provision_script_replaces_git_insteadof_idempotently(self):
        script = deploykeys.provision_script('octo/app')
        self.assertIn('git config --global --replace-all', script)
        self.assertIn('StrictHostKeyChecking accept-new', script)

    def test_key_matches_ignores_comment_and_rejects_empty(self):
        existing = [{'key': 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey old-comment'}]
        self.assertTrue(deploykeys.key_matches(existing, 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey new-comment'))
        self.assertFalse(deploykeys.key_matches(existing, 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherKey'))
        self.assertFalse(deploykeys.key_matches(existing, ''))


class DeployKeysEdgeTest(unittest.TestCase):
    def setUp(self):
        self.folder = tmpdirs.mkdtemp()
        github._etags.clear()
        github._limits.clear()

    def test_ensure_machine_repo_registers_read_only_and_is_idempotent(self):
        cfg = make_cfg(self.folder, projects={'app': {'match': 'app', 'repo': 'octo/app'}})
        fake_gh = FakeGitHub()
        client = github.Client('tok-test', http=fake_gh)
        runner = FakeRunner()

        first = deploykeys.ensure_machine_repo(cfg, 'box-a', 'octo/app', client=client, run=runner)
        second = deploykeys.ensure_machine_repo(cfg, 'box-a', 'octo/app', client=client, run=runner)
        self.assertEqual(first, 'added')
        self.assertEqual(second, 'present')
        self.assertEqual(len(fake_gh.posted), 1)
        repo, payload = fake_gh.posted[0]
        self.assertEqual(repo, 'octo/app')
        self.assertTrue(payload['read_only'])
        self.assertEqual(payload['title'], 'tutti:box-a:octo/app')

    def test_ensure_machine_repo_rejects_missing_machine_and_bad_pubkey(self):
        cfg = make_cfg(self.folder)
        client = github.Client('tok-test', http=FakeGitHub())
        with self.assertRaises(ValueError):
            deploykeys.ensure_machine_repo(cfg, 'unknown-box', 'octo/app', client=client, run=FakeRunner())
        bad_runner = FakeRunner({'box-a.example': SimpleNamespace(returncode=0, stdout='not-a-key\n', stderr='')})
        with self.assertRaises(ValueError):
            deploykeys.ensure_machine_repo(cfg, 'box-a', 'octo/app', client=client, run=bad_runner)
        fail_runner = FakeRunner({'box-a.example': SimpleNamespace(returncode=255, stdout='', stderr='Connection refused')})
        with self.assertRaises(RuntimeError):
            deploykeys.ensure_machine_repo(cfg, 'box-a', 'octo/app', client=client, run=fail_runner)

    def test_sync_handles_unreachable_machine_and_github_error_without_stopping(self):
        cfg = make_cfg(self.folder, projects={
            'ok': {'match': 'ok', 'repo': 'octo/ok'},
            'denied': {'match': 'denied', 'repo': 'octo/denied'},
        })
        fake_gh = FakeGitHub(fail_repo='octo/denied')
        client = github.Client('tok-test', http=fake_gh)
        runner = FakeRunner({'box-a.example': SimpleNamespace(returncode=255, stdout='', stderr='unreachable')})

        added = deploykeys.sync(cfg, client=client, run=runner)
        self.assertEqual(added, ['added read-only deploy key for box-b on octo/ok'])
        self.assertTrue(all(payload['read_only'] for _, payload in fake_gh.posted))

    def test_githubcmd_deploy_keys_and_selfheal_skip_when_offline(self):
        cfg = make_cfg(self.folder, projects={'app': {'match': 'app', 'repo': 'octo/app'}})
        out = io.StringIO()
        with mock.patch.object(github, 'token_for', return_value=('', '')), \
                mock.patch.dict(os.environ, {'TUTTI_OFFLINE': '1'}):
            code = githubcmd.cmd(cfg, SimpleNamespace(action='deploy-keys'), out=out)
            self.assertEqual(code, 0)
            self.assertIn('read-only deploy keys', out.getvalue())
            ctx = selfheal.Ctx(cfg)
            self.assertEqual(selfheal.check_deploy_keys(ctx), [])


if __name__ == '__main__':
    unittest.main()
