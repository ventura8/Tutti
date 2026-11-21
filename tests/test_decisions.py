"""Household decisions (t293): a person's answer is kept and the same question is never asked again."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from tutti import backlog, cli, config, decisions, needs

T0 = 1_800_000_000.0


def task(tid='t1'):
    return {'id': tid, 'repo': 'app', 'title': f'Task {tid}', 'scope': [], 'check': 'true', 'size': 'S', 'source': 'test',
            'status': 'queued', 'attempts': [], 'notes': [], 'added': T0}


class Decisions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.normalise({'project': {'state': os.path.join(self.tmp.name, 'state')},
                                     'autopilot': {'repos': {'app': {'path': self.tmp.name, 'check': 'true'}}}})

    def add(self, t):
        with backlog.locked(self.cfg) as data:
            data['tasks'].append(t)

    def ask(self, tid, question, options=('Codex CLI', 'API key')):
        with backlog.locked(self.cfg) as data:
            t = next(x for x in data['tasks'] if x['id'] == tid)
            return needs.ask(self.cfg, t, question, 'needs the owner', list(options), options[0], now=T0), dict(t)

    def test_an_answered_question_is_never_asked_again(self):
        self.add(task('t1'))
        self.add(task('t2'))
        q, _ = self.ask('t1', 't1: How should Tutti sign in to ChatGPT Pro?')
        self.assertEqual(q['status'], 'open')
        self.assertTrue(needs.answer(self.cfg, q['id'], 'Codex CLI', now=T0 + 1)[0])
        q2, t2 = self.ask('t2', 't2: how should Tutti sign in to ChatGPT Pro')   # another task, same question
        self.assertEqual((q2['status'], q2['answer'], q2['by']), ('answered', 'Codex CLI', 'decision:d1'))
        self.assertEqual(t2['status'], 'queued')   # resumed at once, never parked
        self.assertEqual(needs.open_for(self.cfg, 'admin', needs.OWNER), [])

    def test_task_steering_answers_are_not_decisions(self):
        self.add(task('t1'))
        self.add(task('t2'))
        q, _ = self.ask('t1', 'Task keeps failing: retry?', needs.PARK_OPTIONS)
        needs.answer(self.cfg, q['id'], 'retry', now=T0 + 1)
        self.assertEqual(decisions.load(self.cfg), [])
        q2, t2 = self.ask('t2', 'Task keeps failing: retry?', needs.PARK_OPTIONS)
        self.assertEqual((q2['status'], t2['status']), ('open', 'needs-person'))

    def test_a_hand_written_decision_matches_by_words_and_reaches_briefs(self):
        decisions.add(self.cfg, ['chatgpt', 'sign'], 'Codex CLI only', note='owner 2026-11-17')
        self.assertTrue(decisions.find(self.cfg, 'Which way do we sign in to ChatGPT Pro?'))
        self.assertIsNone(decisions.find(self.cfg, 'Which way do we sign in to Cursor?'))
        lines = '\n'.join(decisions.brief_lines(self.cfg))
        self.assertIn('never ask the owner these again', lines)
        self.assertIn('chatgpt + sign -> Codex CLI only (owner 2026-11-17)', lines)

    def test_cli_add_list_rm_and_bad_input(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.cmd_decisions(self.cfg, SimpleNamespace(action='add', what='chatgpt sign', answer='Codex CLI',
                                                                         note=None)), 0)
            self.assertEqual(cli.cmd_decisions(self.cfg, SimpleNamespace(action='list', what=None, answer=None, note=None)), 0)
        self.assertIn('d1', out.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_decisions(self.cfg, SimpleNamespace(action='rm', what='d9', answer=None, note=None)), 1)
            self.assertEqual(cli.cmd_decisions(self.cfg, SimpleNamespace(action='add', what='x', answer=None, note=None)), 2)
            self.assertEqual(cli.cmd_decisions(self.cfg, SimpleNamespace(action='rm', what='d1', answer=None, note=None)), 0)
        self.assertEqual(decisions.load(self.cfg), [])

    def test_a_broken_file_reads_as_no_decisions(self):
        os.makedirs(os.path.dirname(decisions.path_of(self.cfg)), exist_ok=True)
        with open(decisions.path_of(self.cfg), 'w', encoding='utf8') as f:
            f.write('{not json')
        self.assertEqual(decisions.load(self.cfg), [])
        self.assertEqual(decisions.brief_lines(self.cfg), [])


if __name__ == '__main__':
    unittest.main()
