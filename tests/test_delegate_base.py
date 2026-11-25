"""`tutti run --base <ref>`: a run starts from the branch the task names; a worker that changes nothing says why."""
import os
import subprocess
import unittest

import tmpdirs

from tutti import delegate, ship


def git(cwd, *args):
    return subprocess.run(['git', '-C', cwd, *args], check=True, capture_output=True, text=True).stdout.strip()


class NamedBase(unittest.TestCase):
    def setUp(self):
        self.origin = tmpdirs.mkdtemp()
        git(self.origin, 'init', '-q', '-b', 'main')
        git(self.origin, '-c', 'user.name=t', '-c', 'user.email=t@x.example', 'commit', '-q', '--allow-empty', '-m', 'one')
        self.top = tmpdirs.mkdtemp()
        git(self.top, 'clone', '-q', self.origin, '.')
        git(self.origin, 'checkout', '-q', '-b', 'feat/x')
        git(self.origin, '-c', 'user.name=t', '-c', 'user.email=t@x.example', 'commit', '-q', '--allow-empty', '-m', 'two')
        self.tip = git(self.origin, 'rev-parse', 'HEAD')

    def test_a_remote_branch_is_fetched_and_used(self):
        said = []
        self.assertEqual(delegate.fresh_base(self.top, said.append, 'origin/feat/x'), self.tip)   # not in the clone until fetched
        self.assertIn('starting from origin/feat/x', said[-1])

    def test_an_unknown_or_unsafe_ref_is_refused(self):
        for bad in ('origin/nope', '--upload-pack=x', 'a..b'):
            with self.assertRaises(ship.ShipError):
                delegate.fresh_base(self.top, lambda m: None, bad)


class LastWords(unittest.TestCase):
    def test_the_workers_final_message_is_kept_without_noise(self):
        lines = ['tutti: run: x', 'I could not start: the worktree is on e54db388.', 'tokens used', '38,243', 'Please rebase it.']
        self.assertEqual(delegate.last_words(lines), ['I could not start: the worktree is on e54db388.', 'Please rebase it.'])

    def test_the_log_is_read_when_no_lines_came_back_and_a_missing_log_is_empty(self):
        d = tmpdirs.mkdtemp()
        log = os.path.join(d, 'worker.log')
        with open(log, 'w', encoding='utf8') as f:
            f.write('step 1\nround 1: changed nothing\n')
        self.assertEqual(delegate.last_words([], log), ['step 1', 'round 1: changed nothing'])
        self.assertEqual(delegate.last_words([], os.path.join(d, 'none.log')), [])

    def test_the_report_quotes_them_only_when_nothing_changed(self):
        r = {'id': '1-2', 'worker': 'codex', 'worker_rc': 0, 'task': 't', 'worktree': '/w', 'diff': '/d', 'changed': [], 'stat': '',
             'denied': [], 'checks': [], 'worker_said': ['wrong commit']}
        self.assertIn('worker said (its last lines):\n  wrong commit', delegate.summary(r))
        self.assertNotIn('worker said', delegate.summary({**r, 'changed': ['a.ts']}))


if __name__ == '__main__':
    unittest.main()
