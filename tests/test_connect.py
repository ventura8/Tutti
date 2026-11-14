"""`tutti connect` against fake home folders: merge, idempotency, undo, other servers kept; the MCP server's tools and roles."""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from tutti import config, connect, dashboard, mcpserver

CFG = '/cfg/tutti.config.yaml'


def no_bins(_b):
    return None


def all_bins(b):
    return f'/usr/bin/{b}'


class ConnectorTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def p(self, rel):
        return os.path.join(self.home, rel)

    def put(self, rel, text):
        os.makedirs(os.path.dirname(self.p(rel)), exist_ok=True)
        with open(self.p(rel), 'w', encoding='utf8') as f:
            f.write(text)

    def read(self, rel):
        with open(self.p(rel), encoding='utf8') as f:
            return f.read()

    def apply(self, name, on=True):
        return connect.apply(name, CFG, on, self.home, which=all_bins, command='/bin/tutti')

    def test_json_agents_merge_keep_other_servers_and_undo(self):
        for name in ('claude-desktop', 'cursor', 'copilot-cli', 'antigravity', 'gemini', 'vscode', 'claude-code'):
            with self.subTest(name):
                _kind, rel, key, _style = connect.AGENTS[name]['mcp']
                self.put(rel, json.dumps({'theme': 'dark', key: {'other': {'command': 'x'}}}))
                r = self.apply(name)
                self.assertEqual(r['status'], 'needs-restart')
                data = json.loads(self.read(rel))
                self.assertEqual(data['theme'], 'dark')
                self.assertEqual(data[key]['other'], {'command': 'x'})
                self.assertEqual(data[key]['tutti']['args'], ['--config', CFG, 'mcp'])
                self.assertTrue(all(c['backup'] and os.path.exists(c['backup']) for c in r['changed'] if c['file'].endswith(rel)))
                again = self.apply(name)
                self.assertEqual((again['status'], again['changed']), ('connected', []))
                self.apply(name, on=False)
                data = json.loads(self.read(rel))
                self.assertEqual(data[key], {'other': {'command': 'x'}})
                self.assertEqual(data['theme'], 'dark')

    def test_entry_shapes_follow_each_tool(self):
        self.assertEqual(connect.entry('copilot', CFG, 't')['type'], 'local')
        self.assertEqual(connect.entry('copilot', CFG, 't')['tools'], ['*'])
        self.assertEqual(connect.entry('vscode', CFG, 't')['type'], 'stdio')
        self.assertIs(connect.entry('agy', CFG, 't')['disabled'], False)

    def test_codex_toml_block_merges_idempotent_and_undoes(self):
        self.put('.codex/config.toml', 'model = "o5"\n\n[mcp_servers.other]\ncommand = "x"\n')
        self.apply('codex')
        first = self.read('.codex/config.toml')
        data = connect.toml().loads(first)
        self.assertEqual(data['model'], 'o5')
        self.assertEqual(data['mcp_servers']['other'], {'command': 'x'})
        self.assertEqual(data['mcp_servers']['tutti']['args'], ['--config', CFG, 'mcp'])
        self.assertIn(connect.BEGIN, self.read('.codex/AGENTS.md'))
        self.assertEqual(self.apply('codex')['changed'], [])
        self.assertEqual(self.read('.codex/config.toml'), first)
        self.apply('codex', on=False)
        self.assertEqual(self.read('.codex/config.toml'), 'model = "o5"\n\n[mcp_servers.other]\ncommand = "x"\n')
        self.assertNotIn(connect.BEGIN, self.read('.codex/AGENTS.md'))

    def test_codex_keeps_the_users_own_tutti_entry(self):
        own = '[mcp_servers.tutti]\ncommand = "mine"\n'
        self.put('.codex/config.toml', own)
        self.apply('codex')
        self.assertEqual(self.read('.codex/config.toml'), own)

    def test_instructions_block_keeps_the_users_text(self):
        self.put('.copilot/copilot-instructions.md', '# Mine\nBe brief.\n')
        self.apply('copilot-cli')
        self.assertTrue(self.read('.copilot/copilot-instructions.md').startswith('# Mine\nBe brief.\n\n' + connect.BEGIN))
        self.apply('copilot-cli')
        self.assertEqual(self.read('.copilot/copilot-instructions.md').count(connect.BEGIN), 1)
        self.apply('copilot-cli', on=False)
        self.assertEqual(self.read('.copilot/copilot-instructions.md'), '# Mine\nBe brief.\n')

    def test_skill_file_written_and_removed(self):
        self.apply('claude-code')
        self.assertIn('name: tutti', self.read('.claude/skills/tutti/SKILL.md'))
        self.apply('claude-code', on=False)
        self.assertFalse(os.path.exists(self.p('.claude/skills/tutti')))

    def test_shared_gemini_md_stays_while_another_agent_uses_it(self):
        self.apply('antigravity')
        self.apply('gemini')
        self.assertEqual(self.read('.gemini/GEMINI.md').count(connect.BEGIN), 1)
        self.apply('gemini', on=False)
        self.assertIn(connect.BEGIN, self.read('.gemini/GEMINI.md'))
        self.apply('antigravity', on=False)
        self.assertNotIn(connect.BEGIN, self.read('.gemini/GEMINI.md'))

    def test_servers_that_are_not_an_object_are_refused(self):
        self.put('.cursor/mcp.json', '{"mcpServers": []}')
        with self.assertRaises(connect.ConnectError):
            self.apply('cursor')
        self.assertEqual(connect.status('cursor', self.home, which=all_bins)['status'], 'error')

    def test_empty_config_file_is_an_empty_object(self):
        self.put('.gemini/config/mcp_config.json', '')
        self.apply('antigravity')
        self.assertIn('tutti', json.loads(self.read('.gemini/config/mcp_config.json'))['mcpServers'])

    def test_broken_json_is_refused_and_left_alone(self):
        self.put('.cursor/mcp.json', '{broken')
        with self.assertRaises(connect.ConnectError) as e:
            self.apply('cursor')
        self.assertIn('not valid JSON', str(e.exception))
        self.assertEqual(self.read('.cursor/mcp.json'), '{broken')
        out = connect.connect_all(CFG, self.home, names=['cursor'], which=all_bins)
        self.assertEqual(out[0]['status'], 'error')

    def test_not_installed_and_chatgpt_are_reported_not_written(self):
        r = connect.apply('cursor', CFG, True, self.home, which=no_bins)
        self.assertEqual(r['status'], 'not-installed')
        self.assertFalse(os.path.exists(self.p('.cursor/mcp.json')))
        self.assertEqual(connect.apply('chatgpt', CFG, True, self.home)['status'], 'cannot')
        self.assertIn('HTTPS', connect.status('chatgpt', self.home)['note'])

    def test_unknown_agent_is_refused(self):
        with self.assertRaises(connect.ConnectError):
            connect.resolve(['nope'])

    def test_status_connected_needs_restart_and_not_connected(self):
        os.makedirs(self.p('.cursor'))
        self.assertEqual(connect.status('cursor', self.home, which=no_bins)['status'], 'not-connected')
        self.apply('cursor')
        # a process started at the epoch, long before the connect
        self.assertEqual(connect.status('cursor', self.home, which=no_bins, started_fn=lambda n: [0.0])['status'], 'needs-restart')
        self.assertEqual(connect.status('cursor', self.home, which=no_bins, started_fn=lambda n: [])['status'], 'connected')

    def test_claude_code_uses_its_own_cli_on_the_real_home(self):
        calls = []

        def run(argv, **_kw):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout='', stderr='')
        with mock.patch.object(connect, 'home_of', lambda h=None: h or self.home):
            connect.apply('claude-code', CFG, True, None, which=all_bins, run=run, command='/bin/tutti')
        self.assertEqual(calls[0][:3], ['claude', 'mcp', 'remove'])
        self.assertEqual(calls[1][:6], ['claude', 'mcp', 'add-json', '--scope', 'user', 'tutti'])

    def test_roundtrip_lists_the_tools_through_the_configured_command(self):
        server = os.path.join(self.home, 'fake-server.py')
        with open(server, 'w', encoding='utf8') as f:
            f.write('import sys, json\nfrom tutti import mcpserver\n'
                    'for l in sys.stdin:\n'
                    '    o = mcpserver.answer_line({"_path": "x"}, l, "admin")\n'
                    '    o and print(json.dumps(o), flush=True)\n')
        e = {'command': sys.executable, 'args': [server]}
        env = {**os.environ, 'PYTHONPATH': os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}
        with mock.patch.dict(os.environ, env):
            ok, text = connect.roundtrip(e)
        self.assertTrue(ok, text)
        self.assertIn('delegate_task', text)
        self.assertEqual(connect.roundtrip({}), (False, 'not connected: run `tutti connect` first'))


class McpServerTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.cfg = config.normalise({'project': {'worktrees': d, 'state': os.path.join(d, 'state')}})
        self.cfg['_path'] = os.path.join(d, 'tutti.config.yaml')
        self.calls = []

    def runner(self, path, args):
        self.calls.append(args)
        return 0, 'ok'

    def test_tool_schemas_are_valid_and_described(self):
        names = [t['name'] for t in mcpserver.TOOLS]
        self.assertEqual(len(names), len(set(names)))
        for t in mcpserver.TOOLS:
            s = t['inputSchema']
            self.assertEqual(s['type'], 'object')
            self.assertGreater(len(t['description']), 40, t['name'])
            self.assertTrue(set(s.get('required', [])) <= set(s['properties']), t['name'])
            for p in s['properties'].values():
                self.assertIn('description', p)
        for want in ('delegate_task', 'job_status', 'backlog_list', 'backlog_add', 'usage', 'handoff_advice', 'machines', 'devices',
                     'test_on', 'scout_ask', 'away_mode'):
            self.assertIn(want, names)

    def test_initialize_and_list_over_stdio(self):
        lines = [json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}}),
                 json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}),
                 json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}), 'not json',
                 json.dumps({'jsonrpc': '2.0', 'id': 3, 'method': 'nope'})]
        out = io.StringIO()
        mcpserver.serve_stdio(self.cfg, io.StringIO('\n'.join(lines) + '\n'), out, env={})
        answers = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(answers[0]['result']['protocolVersion'], '2025-03-26')
        self.assertEqual(answers[0]['result']['serverInfo']['name'], 'tutti')
        self.assertEqual(len(answers[1]['result']['tools']), len(mcpserver.TOOLS))
        self.assertEqual(answers[2]['error']['code'], -32700)
        self.assertEqual(answers[3]['error']['code'], -32601)

    def test_delegate_maps_to_run_auto_with_project_and_size(self):
        err, _ = mcpserver.call(self.cfg, 'delegate_task', {'task': 'fix x', 'project': '/r', 'size': 'S'}, 'admin', runner=self.runner)
        self.assertFalse(err)
        self.assertEqual(self.calls[-1], ['run', 'auto', '[size S] fix x', '--repo', '/r', '--bg'])
        mcpserver.call(self.cfg, 'backlog_add', {'repo': 'r', 'title': 't', 'scope': ['a', 'b']}, 'admin', runner=self.runner)
        self.assertEqual(self.calls[-1], ['backlog', 'add', 'r', 't', '--source', 'mcp', '--scope', 'a', '--scope', 'b'])

    def test_viewer_may_read_but_not_change(self):
        for name, args in (('delegate_task', {'task': 't', 'project': 'p'}), ('backlog_add', {'repo': 'r', 'title': 't'}),
                           ('test_on', {'track': 't', 'command': 'c'}), ('scout_ask', {'scout': 's', 'question': 'q'}),
                           ('away_mode', {'mode': 'away'})):
            for role in ('viewer', 'pending'):
                err, text = mcpserver.call(self.cfg, name, args, role, runner=self.runner, popen=self.fail)
                self.assertTrue(err)
                self.assertIn('needs the admin role', text)
        self.assertEqual(self.calls, [])
        err, _ = mcpserver.call(self.cfg, 'usage', {}, 'viewer', runner=self.runner)
        self.assertFalse(err)
        self.assertEqual(self.calls, [['usage']])
        mcpserver.call(self.cfg, 'backlog_add', {'repo': 'r', 'title': 't', 'scope': 'src/a.py'}, 'admin', runner=self.runner)
        self.assertEqual(self.calls[-1][-2:], ['--scope', 'src/a.py'])

    def fail(self, *a, **k):
        raise AssertionError('a refused call must not start anything')

    def test_missing_arguments_and_unknown_tool(self):
        self.assertIn('needs task', mcpserver.call(self.cfg, 'delegate_task', {'project': 'p'}, 'admin')[1])
        self.assertIn('no tool', mcpserver.call(self.cfg, 'nope', {}, 'admin')[1])

    def test_role_from_env_then_config_else_admin(self):
        self.assertEqual(mcpserver.role_of({}, {}), 'admin')
        self.assertEqual(mcpserver.role_of({'mcp': {'role': 'viewer'}}, {}), 'viewer')
        self.assertEqual(mcpserver.role_of({'mcp': {'role': 'admin'}}, {'TUTTI_ROLE': 'viewer'}), 'viewer')
        self.assertEqual(mcpserver.role_of({}, {'TUTTI_ROLE': 'boss'}), 'pending')

    def test_test_on_starts_detached_and_status_reads_its_log(self):
        started = []
        err, text = mcpserver.call(self.cfg, 'test_on', {'track': 't1', 'command': 'make test', 'machine': 'm1'}, 'admin',
                                   popen=lambda argv, **kw: started.append(argv))
        self.assertFalse(err)
        jid = text.split()[1].rstrip(':')
        self.assertIn('--on', started[0])
        self.assertEqual(started[0][-2:], ['-c', 'make test'])
        self.assertIn('running', mcpserver.call(self.cfg, 'job_status', {'id': jid}, 'viewer')[1])
        with open(os.path.join(mcpserver.jobs_dir(self.cfg), jid + '.rc'), 'w', encoding='utf8') as f:
            f.write('0\n')
        self.assertIn('exit code 0', mcpserver.call(self.cfg, 'job_status', {'id': jid}, 'viewer')[1])
        self.assertTrue(mcpserver.call(self.cfg, 'job_status', {'id': 'test-nope'}, 'viewer')[0])

    def test_http_token_is_private(self):
        tok = mcpserver.http_token(self.cfg)
        self.assertEqual(mcpserver.http_token(self.cfg), tok)
        self.assertEqual(os.stat(mcpserver.token_path(self.cfg)).st_mode & 0o777, 0o600)


class DashboardConnectTest(unittest.TestCase):
    def test_status_test_and_bad_requests(self):
        home = tempfile.mkdtemp()
        col = mock.Mock(cfg={'_path': CFG})
        code, body = dashboard.save_connect(col, b'{"action": "status"}', home)
        self.assertEqual(code, 200)
        self.assertEqual({r['agent'] for r in body['agents']}, set(connect.AGENTS))
        self.assertEqual(dashboard.save_connect(col, b'{"action": "connect", "agent": "nope"}', home)[0], 400)
        self.assertEqual(dashboard.save_connect(col, b'not json', home)[0], 400)
        code, body = dashboard.save_connect(col, b'{"action": "test", "agent": "cursor"}', home)
        self.assertEqual((code, body['ok']), (200, False))


if __name__ == '__main__':
    unittest.main()
