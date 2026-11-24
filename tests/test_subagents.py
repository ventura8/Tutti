"""`tutti agents count`: which subagent transcripts are live, and the verdict against the limit. Temp folders only."""
import json
import os
import tempfile
import unittest

from tutti import subagents

END = {'message': {'role': 'assistant', 'stop_reason': 'end_turn', 'content': []}}
TOOL = {'message': {'role': 'assistant', 'stop_reason': 'tool_use', 'content': []}}
HANDED = {'message': {'role': 'user', 'content': [{'type': 'tool_result', 'content': 'Report delivered to your caller.'}]}}
NOW = 1_000_000.0


class SubagentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = os.path.join(self.tmp.name, 'proj', 's1')
        os.makedirs(os.path.join(self.session, 'subagents'))

    def agent(self, name, entry, age, desc='task'):
        path = os.path.join(self.session, 'subagents', f'agent-{name}.jsonl')
        with open(path, 'w', encoding='utf8') as f:
            f.write(json.dumps({'message': {'role': 'user'}}) + '\n' + json.dumps(entry) + '\n')
        with open(path[:-6] + '.meta.json', 'w', encoding='utf8') as f:
            json.dump({'description': desc}, f)
        os.utime(path, (NOW - age, NOW - age))

    def test_working_and_waiting_subagents_count_finished_and_dead_ones_do_not(self):
        self.agent('a', TOOL, 5)          # working
        self.agent('b', TOOL, 600)        # waiting on background work, quiet but within the window
        self.agent('c', END, 5)           # gave its final answer
        self.agent('d', HANDED, 5)        # its report was delivered
        self.agent('e', TOOL, 7200)       # quiet for hours: crashed or abandoned
        found = subagents.count(self.session, NOW)
        self.assertEqual([x['id'] for x in found], ['a', 'b'])
        self.assertEqual(found[0]['description'], 'task')

    def test_the_window_is_adjustable_and_a_missing_folder_counts_none(self):
        self.agent('a', TOOL, 600)
        self.assertEqual(subagents.count(self.session, NOW, 300), [])
        self.assertEqual(subagents.count(os.path.join(self.tmp.name, 'none'), NOW), [])

    def test_verdict_against_the_limit(self):
        self.assertEqual(subagents.verdict(2, 3)[0], 'ok')
        self.assertEqual(subagents.verdict(3, 3)[0], 'full')
        self.assertEqual(subagents.verdict(5, 3)[0], 'over')

    def test_session_folder_finds_the_named_or_the_newest_session(self):
        base = self.tmp.name
        self.assertEqual(subagents.session_folder(base, '/x', 's1'), self.session)
        self.assertIsNone(subagents.session_folder(base, '/x', 'nope'))
        cwd = '/home/u/p.q'
        root = os.path.join(base, subagents.project_folder(cwd))
        self.assertEqual(os.path.basename(root), '-home-u-p-q')
        os.makedirs(os.path.join(root, 'old', 'subagents'))
        os.makedirs(os.path.join(root, 'new', 'subagents'))
        os.utime(os.path.join(root, 'old'), (1, 1))
        self.assertEqual(os.path.basename(subagents.session_folder(base, cwd)), 'new')
        self.assertIsNone(subagents.session_folder(base, '/elsewhere'))

    def test_the_command_exits_1_at_the_limit_and_2_without_a_session(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        from types import SimpleNamespace

        from tutti import cli
        cfg = {'agents': {'max_claude_subagents': 1, 'subagent_active_seconds': 900}}
        a = SimpleNamespace(max=None, seconds=None, projects=self.tmp.name, cwd=None, session='s1')
        self.agent('a', TOOL, 5)
        os.utime(os.path.join(self.session, 'subagents', 'agent-a.jsonl'))
        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.cmd_agents(cfg, a), 1)
        self.assertIn('limit 1: full', out.getvalue())
        a.max = 2
        with redirect_stdout(StringIO()):
            self.assertEqual(cli.cmd_agents(cfg, a), 0)
        a.session = 'nope'
        with redirect_stderr(StringIO()) as err:
            self.assertEqual(cli.cmd_agents(cfg, a), 2)
        self.assertIn('--session', err.getvalue())


if __name__ == '__main__':
    unittest.main()
