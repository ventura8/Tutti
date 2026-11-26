"""Connect GitHub (docs/GITHUB.md): the device flow, the per-user token store (mode 600, refreshed, removed), the gh-shaped
calls answered by the API, ETags and rate limits, the gh fallback and the dashboard's POST. A fake GitHub API; no network,
no real token or account."""
import io
import json
import os
import stat
import unittest
from types import SimpleNamespace

import tmpdirs

from tutti import config, github, githubcmd, workers


def cfg_of(folder, **raw):
    cfg = config.normalise({'project': {'worktrees': folder, 'state': os.path.join(folder, 'state')},
                            'machines': {'here': {'host': 'local'}}, 'github': {'client_id': 'Iv1.test'}, **raw})
    cfg['_path'] = os.path.join(folder, 'tutti.config.yaml')
    return cfg


class FakeGitHub:
    """Answers GitHub's OAuth and API endpoints from a table; records every request (never prints a token)."""

    def __init__(self):
        self.calls, self.pending, self.etag = [], 1, '"v1"'
        self.limit_left = '4999'

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, dict(headers), body))
        h = {'x-ratelimit-remaining': self.limit_left, 'x-ratelimit-reset': '9999999999'}
        if url == github.DEVICE_CODE_URL:
            return 200, h, json.dumps({'device_code': 'dc', 'user_code': 'ABCD-1234', 'verification_uri':
                                       'https://github.com/login/device', 'expires_in': 900, 'interval': 5}).encode()
        if url == github.TOKEN_URL:
            fields = dict(x.split('=', 1) for x in body.decode().split('&'))
            if fields.get('grant_type') == 'refresh_token':
                return 200, h, json.dumps({'access_token': 'tok-2', 'refresh_token': 'r-2', 'expires_in': 28800}).encode()
            if self.pending:
                self.pending -= 1
                return 200, h, b'{"error": "authorization_pending"}'
            return 200, h, json.dumps({'access_token': 'tok-1', 'refresh_token': 'r-1', 'expires_in': 28800,
                                       'refresh_token_expires_in': 15897600}).encode()
        return self.api(method, url, headers, body, h)

    def api(self, method, url, headers, body, h):
        path = url[len('https://api.github.com'):]
        if path == '/user':
            return 200, h, b'{"login": "octo"}'
        if path == '/repos/o/r/pulls' and method == 'POST':
            return 201, h, b'{"html_url": "https://github.com/o/r/pull/7"}'
        if path == '/repos/o/r/pulls/7/merge':
            sha = json.loads(body)['sha']
            return (200, h, b'{}') if sha == 'abc' else (409, h, b'{"message": "Head branch was modified"}')
        if path == '/repos/o/r/pulls/7':
            return 200, h, b'{"merged": true, "state": "closed", "node_id": "N7"}'
        if path.startswith('/repos/o/r/releases'):
            if headers.get('If-None-Match') == self.etag:
                return 304, h, b''
            return 200, {**h, 'etag': self.etag}, json.dumps([{'tag_name': 'v1.2.0', 'html_url': 'https://github.com/o/r/releases/1',
                                                               'assets': [{}, {}]}]).encode()
        if path == '/repos/o/r/issues':
            if json.loads(body).get('labels'):
                return 422, h, b'{"message": "label does not exist"}'
            return 201, h, b'{"html_url": "https://github.com/o/r/issues/3"}'
        if path == '/graphql':
            return 200, h, b'{"data": {}}'
        return 404, h, b'{"message": "Not Found"}'


def no_gh():
    return ''


class GitHubTest(unittest.TestCase):
    def setUp(self):
        self.folder = tmpdirs.mkdtemp()
        self.cfg = cfg_of(self.folder)
        self.fake = FakeGitHub()
        self.now = [1000.0]
        github._etags.clear()
        github._limits.clear()

    def fail(self, argv, **kw):
        raise AssertionError(f'the real program was run: {argv[:3]}')

    def clock(self):
        return self.now[0]

    def connect(self, sub='u1'):
        flows = github.Flows(self.cfg, http=self.fake, clock=self.clock)
        shown = flows.start(sub)
        self.assertEqual((shown['user_code'], shown['verification_url']), ('ABCD-1234', 'https://github.com/login/device'))
        self.now[0] += 5
        self.assertIsNone(flows.poll(sub))          # the person has not entered the code yet
        self.assertIsNone(flows.poll(sub))          # asked again too soon: GitHub is not asked
        self.now[0] += 5
        self.assertEqual(flows.poll(sub), 'octo')
        return flows

    def test_the_device_flow_keeps_the_token_mode_600_per_user(self):
        self.connect()
        path = github.token_path(self.cfg, 'u1')
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertNotIn('u1', os.path.basename(path))   # the file name is a hash, not the user
        self.assertEqual(github.token_for(self.cfg, 'u1', self.fake, self.clock, no_gh), ('tok-1', 'user'))
        # another person's work falls back to the household's connection (the first one made)
        self.assertEqual(github.token_for(self.cfg, 'u2', self.fake, self.clock, no_gh), ('tok-1', 'household'))
        self.assertEqual(github.status(self.cfg, 'u1', gh=no_gh)['login'], 'octo')

    def test_an_expired_token_is_refreshed_and_disconnect_removes_it(self):
        self.connect()
        self.now[0] += 28800
        self.assertEqual(github.user_token(self.cfg, 'u1', self.fake, self.clock), ('tok-2', 'octo'))
        self.assertTrue(github.forget(self.cfg, 'u1'))
        self.assertFalse(github.forget(self.cfg, 'u1'))
        self.assertEqual(github.token_for(self.cfg, 'u1', self.fake, self.clock, no_gh), ('', ''))

    def test_a_refused_code_slow_down_and_a_transient_refresh_failure(self):
        answers = [b'{"error": "slow_down"}', b'{"error": "access_denied"}']

        def http(method, url, headers, body=None):
            if url == github.TOKEN_URL:
                return 200, {}, answers.pop(0)
            return self.fake(method, url, headers, body)
        flows = github.Flows(self.cfg, http=http, clock=self.clock)
        flows.start('u1')
        self.now[0] += 5
        self.assertIsNone(flows.poll('u1'))   # slow_down: the interval grows by 5 s
        self.now[0] += 5
        self.assertIsNone(flows.poll('u1'))   # too soon after slow_down: GitHub is not asked
        self.now[0] += 5
        with self.assertRaisesRegex(github.GitHubError, 'Cancel'):
            flows.poll('u1')
        self.connect()
        self.now[0] += 28800

        def down(method, url, headers, body=None):
            return 502, {}, b''
        self.assertEqual(github.user_token(self.cfg, 'u1', down, self.clock), ('', ''))
        self.assertTrue(os.path.exists(github.token_path(self.cfg, 'u1')))   # kept for the next call

        def dead(method, url, headers, body=None):
            return 400, {}, b'{"error": "bad_refresh_token"}'
        self.assertEqual(github.user_token(self.cfg, 'u1', dead, self.clock), ('', ''))
        self.assertFalse(os.path.exists(github.token_path(self.cfg, 'u1')))

    def test_flags_and_an_unpinned_merge(self):
        self.assertEqual(github.flags(['--auto', 'https://x', '-R', 'o/r', '--json=state']),
                         ({'--auto': True, '--repo': 'o/r', '--json': 'state'}, ['https://x']))
        # --state open must not become a positional PR number (the old int('open') failure)
        f, pos = github.flags(['-R', 'o/r', '--state', 'open', '--limit', '500', '--json', 'number,title'])
        self.assertEqual(f['--state'], 'open')
        self.assertEqual(f['--limit'], '500')
        self.assertEqual(pos, [])
        self.connect()
        seen = []

        def fallback(argv, **kw):
            seen.append(argv)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        github.run(['gh', 'pr', 'merge', 'https://github.com/o/r/pull/7', '--squash'], cfg=self.cfg, sub='u1', http=self.fake,
                   clock=self.clock, fallback=fallback)
        self.assertEqual(len(seen), 1)   # never merged through the API without the reviewed commit
        argv = ['gh', 'pr', 'list', '-R', 'o/r', '--state', 'open', '--limit', '500', '--json', 'number,title']
        got = github.run(argv, cfg=self.cfg, sub='u1', http=self.fake, clock=self.clock, fallback=fallback)
        self.assertEqual(got.returncode, 0)
        self.assertNotIn('invalid literal for int()', got.stderr)
        self.assertEqual(seen[-1], argv)  # unsupported list goes to gh, even with an App connection
        bad = github.run(['gh', 'pr', 'ready', 'not-a-number'], cfg=self.cfg, sub='u1', http=self.fake, clock=self.clock,
                         fallback=fallback)
        self.assertEqual(bad.returncode, 1)

    def test_a_refused_code_and_an_unset_app_say_what_to_do(self):
        flows = github.Flows(cfg_of(self.folder, github={}), http=self.fake, clock=self.clock)
        with self.assertRaisesRegex(github.GitHubError, 'GitHub App is not set up'):
            flows.start('u1')
        with self.assertRaisesRegex(github.GitHubError, 'expired or was never started'):
            flows.poll('u1')

    def test_without_any_connection_gh_s_token_is_used_and_without_it_the_message_names_the_fix(self):
        self.assertEqual(github.token_for(self.cfg, None, gh=lambda: 'gh-tok'), ('gh-tok', 'gh'))
        self.assertEqual(github.token_for(None, None, gh=lambda: ''), ('', ''))
        with self.assertRaisesRegex(github.GitHubError, 'Connect GitHub'):
            github.client(self.cfg, gh=no_gh)
        self.assertIn('Connect GitHub', github.doctor_line(self.cfg, gh=no_gh))
        self.assertEqual(github.doctor_line(self.cfg, gh=lambda: 'gh-tok'), '')

    def test_gh_shaped_calls_are_answered_by_the_api(self):
        self.connect()
        def run(argv, **kw):
            return github.run(argv, sub='u1', cfg=self.cfg, http=self.fake, clock=self.clock, fallback=self.fail, **kw)
        pr = run(['gh', 'pr', 'create', '--repo', 'o/r', '--base', 'main', '--head', 'feat', '--title', 't', '--body', 'b'])
        self.assertEqual((pr.returncode, pr.stdout), (0, 'https://github.com/o/r/pull/7\n'))
        ok = run(['gh', 'pr', 'merge', 'https://github.com/o/r/pull/7', '--squash', '--match-head-commit', 'abc'])
        self.assertEqual(ok.returncode, 0)
        moved = run(['gh', 'pr', 'merge', 'https://github.com/o/r/pull/7', '--squash', '--match-head-commit', 'zzz'])
        self.assertEqual(moved.returncode, 1)
        self.assertIn('Head branch was modified', moved.stderr)
        self.assertNotIn('tok-1', moved.stderr)
        state = run(['gh', 'pr', 'view', 'https://github.com/o/r/pull/7', '--json', 'state', '--jq', '.state'])
        self.assertEqual(state.stdout, 'MERGED\n')
        issue = run(['gh', 'issue', 'create', '--repo', 'o/r', '--title', 'x', '--body-file', '-', '--label', 'nope'], input='body')
        self.assertEqual(issue.stdout, 'https://github.com/o/r/issues/3\n')   # the missing label is dropped
        self.assertEqual(run(['gh', 'auth', 'status']).returncode, 0)
        # every API call carries the token in a header only, never in the URL
        self.assertTrue(all('tok-1' not in url for _m, url, _h, _b in self.fake.calls))

    def test_an_unknown_gh_call_and_a_non_gh_argv_go_to_the_real_program(self):
        seen = []

        def fallback(argv, **kw):
            seen.append(argv)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        github.run(['git', 'status'], cfg=self.cfg, fallback=fallback)
        github.run(['gh', 'api', 'graphql'], cfg=self.cfg, http=self.fake, fallback=fallback)   # no token: the real gh
        self.assertEqual(seen, [['git', 'status'], ['gh', 'api', 'graphql']])

        def missing(argv, **kw):
            raise FileNotFoundError(argv[0])
        self.assertEqual(github.run(['gh', 'pr', 'list'], cfg=self.cfg, fallback=missing).returncode, 127)

    def test_reads_use_etags_and_a_spent_rate_limit_stops_the_calls(self):
        c = github.Client('t', self.fake, self.clock)
        first = github.latest_releases(c, 'o/r')
        self.assertEqual(github.latest_releases(c, 'o/r'), first)
        self.assertEqual(self.fake.calls[-1][2].get('If-None-Match'), '"v1"')
        self.assertEqual(first[0]['tag'], 'v1.2.0')
        self.fake.limit_left = '0'
        with self.assertRaises(github.GitHubError):
            c.request('GET', '/repos/o/r/nothing')
        before = len(self.fake.calls)
        with self.assertRaisesRegex(github.GitHubError, 'rate limit'):
            c.request('GET', '/user')
        self.assertEqual(len(self.fake.calls), before)   # GitHub was not asked

    def test_open_prs_has_the_shape_the_projects_panel_reads(self):
        data = {'data': {'repository': {'pullRequests': {'nodes': [{
            'number': 4, 'title': 'feat (0.2.0)', 'url': 'https://github.com/o/r/pull/4', 'headRefName': 'f', 'isDraft': False,
            'reviewDecision': 'APPROVED', 'latestReviews': {'nodes': [{'author': {'login': 'a'}, 'state': 'APPROVED'}]},
            'commits': {'nodes': [{'commit': {'statusCheckRollup': {'contexts': {'nodes': [
                {'name': 'ci', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}, {'context': 'sonar', 'state': 'PENDING'}]}}}}]}}]}}}}

        def http(method, url, headers, body=None):
            return 200, {}, json.dumps(data).encode()
        rows = github.open_prs(github.Client('t', http), 'o/r')
        self.assertEqual(rows[0]['checks'][1]['name'], 'sonar')
        self.assertEqual(rows[0]['reviews'], [{'author': 'a', 'state': 'APPROVED'}])
        with self.assertRaisesRegex(github.GitHubError, 'owner/name'):
            github.split_repo('nope')

    def test_urls_remotes_and_worker_environments(self):
        self.assertEqual(github.parse_pr_url('https://github.com/o/r/pull/12'), ('o/r', 12))
        self.assertEqual(github.repo_of_remote('git@github.com:o/r.git'), 'o/r')
        self.assertEqual(github.repo_of_remote('https://example.invalid/x'), '')
        with self.assertRaises(github.GitHubError):
            github.parse_pr_url('https://evil.example/o/r/pull/1')
        env = github.worker_env({'GH_TOKEN': 'x', 'GITHUB_TOKEN': 'y', 'PATH': '/bin'})
        self.assertEqual(env, {'PATH': '/bin'})
        self.assertLessEqual(set(github.TOKEN_VARS), set(workers.NOT_FOR_WORKERS))
        with self.assertRaisesRegex(github.GitHubError, 'outside GitHub'):
            github.urlopen_http('GET', 'https://evil.example/', {})

    def test_the_dashboard_post_is_the_person_s_own(self):
        c = SimpleNamespace(cfg=self.cfg, who=lambda h: {'sub': 'u9'})
        flows = github.Flows(self.cfg, http=self.fake, clock=self.clock)
        self.assertEqual(github.api(c, {}, b'{"action": "start"}', flows)[1]['user_code'], 'ABCD-1234')
        self.assertTrue(github.api(c, {}, b'{"action": "poll"}', flows)[1]['pending'])
        self.assertEqual(github.api(c, {}, b'{"action": "bogus"}', flows)[0], 400)
        self.assertEqual(github.api(c, {}, b'not json', flows)[0], 400)
        self.assertFalse(github.api(c, {}, b'{"action": "disconnect"}', flows)[1]['removed'])
        self.assertFalse(github.api(c, {}, b'{"action": "status"}', flows)[1]['connected'])
        gone = SimpleNamespace(cfg=self.cfg, who=lambda h: None)
        self.assertEqual(github.api(gone, {}, b'{"action": "poll"}', flows)[0], 409)   # the owner never started one

    def test_the_cli_connects_and_prints_the_manifest_page(self):
        out = io.StringIO()
        flows = github.Flows(self.cfg, http=self.fake, clock=self.clock)

        def sleep(s):
            self.now[0] += s
        self.assertEqual(githubcmd.cmd(self.cfg, SimpleNamespace(action='connect'), out, flows, sleep), 0)
        self.assertIn('connected as octo', out.getvalue())
        page = io.StringIO()
        githubcmd.cmd(self.cfg, SimpleNamespace(action='manifest'), page)
        self.assertIn('https://github.com/settings/apps/new', page.getvalue())
        self.assertIn('pull_requests', page.getvalue())
        m = github.manifest()
        self.assertFalse(m['hook_attributes']['active'])   # no webhook: the dashboard stays on the LAN
        self.assertEqual(m['default_permissions']['checks'], 'read')
        self.assertEqual(githubcmd.cmd(self.cfg, SimpleNamespace(action='disconnect'), out), 0)
        self.assertEqual(githubcmd.cmd(cfg_of(self.folder, github={}), SimpleNamespace(action='connect'), out, None, sleep), 1)


if __name__ == '__main__':
    unittest.main()
