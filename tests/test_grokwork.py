"""Grok Bot as a coding worker (kind grok): payload shape, webhook send, diff apply; URL/key never printed."""
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from test_scouts import ENV, KEY, REPO, FakeGh, FakeHook, cfg_of

from tutti import config, grokwork, workers


def coding_cfg(folder):
    raw = {'project': {'state': os.path.join(folder, 'state')},
           'accounts': {'grok-bot': {'window': '7d', 'resets': '2026-01-04T00:00Z', 'worker': 'grok'}},
           'workers': {'grok': {'kind': 'grok', 'scout': 'grok'}},
           'scouts': {'grok': {'kind': 'grok-bot-webhook', 'url_env': 'TUTTI_GROK_URL', 'key_env': 'TUTTI_GROK_KEY',
                               'github_login': 'scout-bot',
                               'account': 'grok-bot', 'reply': {'github_repo': REPO}, 'poll_seconds': 0.01,
                               'timeout_minutes': 0.05}}}
    cfg = config.normalise(raw)
    errs = config.validate(cfg)
    if errs:
        raise AssertionError(errs)
    cfg['_path'] = os.path.join(folder, 'tutti.config.yaml')
    return cfg


def git_repo(path):
    subprocess.run(['git', 'init', '-q', path], check=True)
    subprocess.run(['git', '-C', path, 'config', 'user.email', 't@example'], check=True)
    subprocess.run(['git', '-C', path, 'config', 'user.name', 't'], check=True)
    with open(os.path.join(path, 'a.txt'), 'w', encoding='utf8') as f:
        f.write('one\n')
    subprocess.run(['git', '-C', path, 'add', 'a.txt'], check=True)
    subprocess.run(['git', '-C', path, 'commit', '-q', '-m', 'init'], check=True)
    subprocess.run(['git', '-C', path, 'remote', 'add', 'origin', 'https://github.com/octo/app.git'], check=True)
    branch = subprocess.check_output(['git', '-C', path, 'branch', '--show-current'], text=True).strip()
    return branch or 'master'


class PayloadTest(unittest.TestCase):
    def test_payload_carries_repo_branch_title_scope_and_check(self):
        body = grokwork.payload('octo/app', 'main', 'Fix typo', ['a.py'], 'pytest -q', 'do it', {'repo': 'octo/app'}, 's1',
                                base_sha='abc123')
        self.assertEqual(body['kind'], 'code')
        self.assertEqual((body['repo'], body['branch'], body['base_sha'], body['title']),
                         ('octo/app', 'main', 'abc123', 'Fix typo'))
        self.assertEqual((body['scope'], body['check'], body['id']), (['a.py'], 'pytest -q', 's1'))
        self.assertNotIn('key', json.dumps(body).lower())
        self.assertNotIn(KEY, json.dumps(body))

    def test_code_instructions_use_patch_author_contract(self):
        text = grokwork.code_instructions('octo/app', 'main', 'Fix', ['a.py'], 'pytest -q', 'deadbeef')
        self.assertIn('patch author', text.lower())
        self.assertIn('deadbeef', text)
        self.assertIn('do NOT run builds/tests', text)
        self.assertIn('do NOT push', text)

    def test_origin_must_be_a_remote_url_without_embedded_credentials(self):
        self.assertTrue(grokwork.clone_url_allowed('git@github.example:octo/app.git'))
        self.assertTrue(grokwork.clone_url_allowed('https://github.example/octo/app.git'))
        self.assertFalse(grokwork.clone_url_allowed('https://user:example@github.example/octo/app.git'))
        self.assertFalse(grokwork.clone_url_allowed('/path/to/local/repo'))

    def test_extract_diff_takes_the_fenced_block(self):
        answer = 'done\n```diff\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n```\n'
        self.assertIn('+two', grokwork.extract_diff(answer))
        self.assertEqual(grokwork.extract_diff('no fence'), '')
        self.assertEqual(grokwork.extract_diff('```diff\n+line with ``` inline\n```\n'), '+line with ``` inline\n')


class ConfigTest(unittest.TestCase):
    def test_kind_grok_needs_a_scout_name(self):
        cfg = config.normalise({'workers': {'g': {'kind': 'grok'}}})
        self.assertTrue(any('scout is missing' in e for e in config.validate(cfg)))

    def test_kind_grok_scout_must_exist(self):
        cfg = config.normalise({'workers': {'g': {'kind': 'grok', 'scout': 'missing'}}})
        self.assertTrue(any('not under scouts' in e for e in config.validate(cfg)))

    def test_kind_grok_is_valid_with_a_scout(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = coding_cfg(folder)
            self.assertEqual(config.validate(cfg), [])


class AvailableTest(unittest.TestCase):
    def test_ready_when_env_is_set_and_down_when_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = coding_cfg(folder)
            w = cfg['workers']['grok']
            # XDG in env: secretenv must not read the owner's ~/.config/tutti/grok.env
            bare = {'XDG_CONFIG_HOME': folder}
            ok, note = workers.available(w, cfg=cfg, env=bare)
            self.assertFalse(ok)
            self.assertIn('TUTTI_GROK', note)
            ok, note = workers.available(w, cfg=cfg, env={**bare, 'TUTTI_GROK_URL': 'https://hooks.example/r',
                                                           'TUTTI_GROK_KEY': KEY})
            self.assertTrue(ok)
            self.assertIn('webhook ready', note)
            self.assertNotIn(KEY, note)

    def test_statuses_passes_cfg_so_kind_grok_resolves_the_scout(self):
        # Without cfg, kind grok cannot look up scouts; statuses must inject it (dashboard default available_fn=None).
        with tempfile.TemporaryDirectory() as folder:
            cfg = coding_cfg(folder)
            with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': folder}):
                for fn in (None, workers.available):
                    got = workers.statuses(cfg, fn)
                    self.assertFalse(got['grok'][0], got)
                    self.assertIn('TUTTI_GROK', got['grok'][1])   # scout found; env still missing
                    self.assertNotIn(KEY, got['grok'][1])


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tree = os.path.join(self.tmp.name, 'tree')
        os.makedirs(self.tree)
        self.branch = git_repo(self.tree)
        self.gh = FakeGh()
        self.diff = ('```diff\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n```\n')
        self.hook = FakeHook(self.gh, answer=f'Done.\n{self.diff}')
        self.env = {**ENV, 'XDG_CONFIG_HOME': self.tmp.name, 'TUTTI_GROK_URL': self.hook.url}
        self.cfg = coding_cfg(self.tmp.name)

    def tearDown(self):
        self.hook.close()
        self.tmp.cleanup()

    def test_run_posts_coding_payload_and_applies_the_diff(self):
        self.gh.comment(1, 'Early comment from a repository writer', login='another-writer')
        out = io.StringIO()
        rc, lines = grokwork.run(self.cfg, 'grok', 'Fix a.txt', ['a.txt'], cwd=self.tree, out=out,
                                 check='true', env=self.env, post_fn=self.hook_post, gh_fn=self.gh)
        self.assertEqual(rc, 0, lines)
        self.assertEqual(len(self.hook.calls), 1)
        body = self.hook.calls[0]['body']
        self.assertEqual(body['kind'], 'code')
        self.assertEqual(body['branch'], self.branch)
        self.assertEqual(len(body['base_sha']), 40)
        self.assertIn('github.com/octo/app', body['repo'])
        self.assertEqual(body['title'], 'Fix a.txt')
        self.assertEqual(body['scope'], ['a.txt'])
        self.assertEqual(body['check'], 'true')
        issue_body = self.gh.issues[1][self.gh.issues[1].index('--body') + 1]
        self.assertNotIn('Fix a.txt', issue_body)
        self.assertEqual(self.hook.calls[0]['auth'], f'Bearer {KEY}')
        dump = out.getvalue() + json.dumps(body)
        self.assertNotIn(KEY, dump.replace(f'Bearer {KEY}', 'Bearer ****'))
        with open(os.path.join(self.tree, 'a.txt'), encoding='utf8') as f:
            self.assertEqual(f.read(), 'two\n')

    def hook_post(self, url, key, body):
        # FakeHook's Handler posts via HTTP; scouts.post would hit the server. Drive the same path:
        from tutti import scouts
        return scouts.post(url, key, body)

    def test_missing_diff_fails_without_changing_the_tree(self):
        self.hook.close()
        self.hook = FakeHook(self.gh, answer='I could not do it')
        self.env = {**ENV, 'XDG_CONFIG_HOME': self.tmp.name, 'TUTTI_GROK_URL': self.hook.url}
        out = io.StringIO()
        rc, lines = grokwork.run(self.cfg, 'grok', 'Fix', ['a.txt'], cwd=self.tree, out=out, check='true',
                                 env=self.env, post_fn=self.hook_post, gh_fn=self.gh)
        self.assertEqual(rc, 1)
        self.assertTrue(any('no ```diff' in ln or 'diff block' in ln for ln in lines))
        with open(os.path.join(self.tree, 'a.txt'), encoding='utf8') as f:
            self.assertEqual(f.read(), 'one\n')

    def test_out_of_scope_diff_leaves_the_tree_unchanged(self):
        rc, lines = grokwork.run(self.cfg, 'grok', 'Fix', ['other.txt'], cwd=self.tree, out=io.StringIO(),
                                 check='true', env=self.env, gh_fn=self.gh)
        self.assertEqual(rc, 1)
        self.assertIn('outside scope', lines[-1])
        with open(os.path.join(self.tree, 'a.txt'), encoding='utf8') as f:
            self.assertEqual(f.read(), 'one\n')

    def test_refused_webhook_names_the_variable_not_the_key(self):
        self.hook.close()
        self.hook = FakeHook(self.gh, status=401)
        out = io.StringIO()
        rc, lines = grokwork.run(self.cfg, 'grok', 'Fix', ['a.txt'], cwd=self.tree, out=out, check='true',
                                 env={**self.env, 'TUTTI_GROK_URL': self.hook.url}, post_fn=self.hook_post, gh_fn=self.gh)
        self.assertEqual(rc, 1)
        text = '\n'.join(lines)
        self.assertIn('TUTTI_GROK_KEY', text)
        self.assertNotIn(KEY, text)
        self.assertNotIn(self.hook.url, text)

    def test_only_the_configured_login_can_return_a_patch(self):
        self.hook.answer = None
        self.gh.comment(1, self.diff, login='another-writer')
        rc, lines = grokwork.run(self.cfg, 'grok', 'Fix', ['a.txt'], cwd=self.tree, out=io.StringIO(),
                                 check='true', env=self.env, gh_fn=self.gh)
        self.assertEqual(rc, 1)
        self.assertIn('no reply in time', lines[-1])
        with open(os.path.join(self.tree, 'a.txt'), encoding='utf8') as f:
            self.assertEqual(f.read(), 'one\n')

    def test_missing_check_does_not_send_a_task(self):
        rc, lines = grokwork.run(self.cfg, 'grok', 'Fix', ['a.txt'], cwd=self.tree, out=io.StringIO(),
                                 env=self.env, gh_fn=self.gh)
        self.assertEqual(rc, 1)
        self.assertIn('acceptance check is missing', lines[-1])
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(self.hook.calls, [])


class CodingAccountTest(unittest.TestCase):
    def test_is_coding_when_the_account_worker_is_kind_grok(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = coding_cfg(folder)
            self.assertTrue(grokwork.is_coding(cfg, 'grok-bot'))
            cfg2 = cfg_of(folder)
            self.assertFalse(grokwork.is_coding(cfg2, 'grok-bot'))

    def test_coding_scout_account_keeps_room_tasks(self):
        from datetime import datetime, timedelta, timezone

        from tutti import pacing, state
        with tempfile.TemporaryDirectory() as folder:
            cfg = coding_cfg(folder)
            now = datetime(2026, 1, 3, 20, tzinfo=timezone.utc)
            pacing.append_log(state.usage_log_path(cfg), 'grok-bot', 13, now - timedelta(hours=1))
            a = next(x for x in state.usage_state(cfg, now)['accounts'] if x['name'] == 'grok-bot')
            self.assertEqual(a.get('scout'), 'grok')
            self.assertIsNotNone(a.get('room_tasks'))


class LadderStepTest(unittest.TestCase):
    def test_ladder_step_kind_is_grok(self):
        from tutti import autopilot
        with tempfile.TemporaryDirectory() as folder:
            cfg = coding_cfg(folder)
            step = autopilot.step_of(cfg, 'grok-bot')
            self.assertIsNotNone(step)
            self.assertEqual(step['kind'], 'grok')
            self.assertEqual(step['worker'], 'grok')

    def test_step_room_for_grok_worker(self):
        from tutti import autopilot
        with tempfile.TemporaryDirectory() as folder:
            raw = {'project': {'state': os.path.join(folder, 'state')},
                   'workers': {'grok': {'kind': 'grok', 'scout': 'grok'}},
                   'scouts': {'grok': {'kind': 'grok-bot-webhook', 'url_env': 'TUTTI_GROK_URL', 'key_env': 'TUTTI_GROK_KEY'}}}
            cfg = config.normalise(raw)
            room, _ = autopilot.step_room(cfg, 'grok', {}, {})
            self.assertEqual(room, 1)
            room, why = autopilot.step_room(cfg, 'grok', {}, {'grok': 1})
            self.assertEqual(room, 0)
            self.assertIn('busy', why)

    def test_dispatch_passes_scoped_files_for_grok_step(self):
        from tutti import autopilot
        with tempfile.TemporaryDirectory() as folder:
            cfg = coding_cfg(folder)
            repo_dir = os.path.join(folder, 'repo')
            os.makedirs(repo_dir)
            cfg['autopilot'] = {'repos': {'my-app': {'path': repo_dir, 'check': 'true'}}}
            with open(os.path.join(repo_dir, 'scoped.py'), 'w') as f:
                f.write('pass\n')
            task = {'id': 't1', 'repo': 'my-app', 'title': 'Do task', 'scope': ['scoped.py'],
                    'attempts': [], 'criteria': []}
            spawned = []

            def fake_spawn(argv, **kw):
                spawned.append(argv)
                m = mock.MagicMock()
                m.pid = 999
                return m

            autopilot.dispatch(cfg, task, 'grok-bot', 12345.0, spawn=fake_spawn)
            self.assertEqual(len(spawned), 1)
            argv = spawned[0]
            self.assertIn('scoped.py', argv)
            self.assertIn('grok', argv)

    def test_cli_run_worker_grok_invokes_grokwork(self):
        from tutti import cli
        with tempfile.TemporaryDirectory() as folder:
            cfg = coding_cfg(folder)
            args = mock.MagicMock()
            args.account = None
            args.worker = 'grok'
            args.task = 'Task'
            args.files = ['a.txt']
            args.dir = folder
            args.check = ['true']
            with mock.patch('tutti.grokwork.run', return_value=(0, ['ok'])) as mock_run:
                rc = cli.run_worker(cfg, args, ())
            self.assertEqual(rc, 0)
            mock_run.assert_called_once_with(cfg, 'grok', 'Task', ['a.txt'], cwd=folder, out=mock.ANY, check='true')


if __name__ == '__main__':
    unittest.main()
