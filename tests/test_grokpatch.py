"""Grok as a patch author (t206): the scout drafts one diff, Tutti keeps it only when it stays in scope, applies it at the
base in a worktree of its own and runs the check; a failure goes back once with the output."""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tutti import backlog, grokpatch, scouts

DIFF = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-x = 1
+x = 2
"""


def answer(diff=DIFF, extra=''):
    return f'Done.\n```diff\n{diff}```\n{extra}'


class Pure(unittest.TestCase):
    def test_one_diff_is_taken_from_the_answer(self):
        self.assertEqual(grokpatch.extract(answer()), DIFF)
        two, big = answer() + answer(), answer('+' * 21000 + '\n')
        with self.assertRaisesRegex(grokpatch.PatchError, 'no ```diff block'):
            grokpatch.extract('I could not read the file.')
        with self.assertRaisesRegex(grokpatch.PatchError, '2 diff blocks'):
            grokpatch.extract(two)
        with self.assertRaisesRegex(grokpatch.PatchError, 'over 20 KB'):
            grokpatch.extract(big)

    def test_a_diff_outside_the_scope_is_refused(self):
        self.assertEqual(grokpatch.check_scope(DIFF, ['app.py']), ['app.py'])
        self.assertEqual(grokpatch.check_scope(DIFF.replace('app.py', 'src/app.py'), ['src/']), ['src/app.py'])
        with self.assertRaisesRegex(grokpatch.PatchError, 'changes app.py, outside the scope'):
            grokpatch.check_scope(DIFF, ['other.py'])
        with self.assertRaisesRegex(grokpatch.PatchError, 'names no file'):
            grokpatch.check_scope('nothing here', ['app.py'])

    def test_paths_and_commits_that_could_be_git_options_are_refused(self):
        for bad in ('--output=/tmp/x', '../etc/passwd', '/etc/passwd', 'a/../../b', 'a b'):
            with self.assertRaises(grokpatch.PatchError, msg=bad):
                grokpatch.safe_path(bad)
        self.assertEqual(grokpatch.safe_path('src/app-1.py'), 'src/app-1.py')
        with self.assertRaisesRegex(grokpatch.PatchError, 'full commit id'):
            grokpatch.safe_sha('--upload-pack=x')
        oops = DIFF.replace('app.py', '-oops')
        with self.assertRaisesRegex(grokpatch.PatchError, 'not a plain path'):
            grokpatch.check_scope(oops, ['-oops'])

    def test_the_brief_carries_the_contract_base_files_and_a_failure(self):
        task = {'id': 't9', 'title': 'Make x two', 'scope': ['app.py'], 'check': 'python3 -c 1', 'criteria': ['x is 2'],
                'notes': ['keep it small']}
        text = grokpatch.brief(task, 'abc123def4567', 'https://github.example/o/app', {'app.py': 'x = 1\n'},
                               {'diff': DIFF, 'output': 'AssertionError: x'})
        for part in ('exactly ONE fenced ```diff block', 'Base commit: abc123def4567', 'Scope (the only files you may change): app.py',
                     'Done when: x is 2', 'Note: keep it small', 'FILE app.py at abc123def456', 'x = 1', 'FAILED', 'AssertionError: x'):
            self.assertIn(part, text)


class RoundTrip(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.repo = os.path.join(d.name, 'app')
        os.makedirs(self.repo)
        git = ['git', '-C', self.repo, '-c', 'user.name=t', '-c', 'user.email=t@example.com']
        subprocess.run(['git', 'init', '-q', self.repo], check=True)
        with open(os.path.join(self.repo, 'app.py'), 'w') as f:
            f.write('x = 1\n')
        subprocess.run([*git, 'add', '-A'], check=True)
        subprocess.run([*git, 'commit', '-qm', 'init'], check=True)
        subprocess.run([*git, 'remote', 'add', 'origin', 'https://user:secret@github.example/o/app.git'], check=True)
        self.cfg = {'project': {'state': os.path.join(d.name, 'state')}, 'scouts': {'grok': {}},
                    'autopilot': {'repos': {'app': {'path': self.repo, 'check': 'true'}}}}
        data = backlog.load(self.cfg)
        backlog.add(data, backlog.new_task('app', 'Make x two', scope=['app.py'], check="grep -q 'x = 2' app.py"))
        backlog.save(self.cfg, data)
        sc = {'name': 'grok', 'reply': {'github_repo': 'o/app'}, 'url_env': 'U', 'key_env': 'K'}
        p = [mock.patch.object(scouts, 'settings', return_value=sc), mock.patch.object(scouts, 'secrets', return_value=('u', 'k')),
             mock.patch.object(scouts, 'path_of', return_value=os.path.join(d.name, 'scouts.json'))]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        self.sent = []

    def send(self, sc, r, url, key, gh_fn, post_fn, now):
        self.sent.append(r['task'])
        r.update(status='waiting', issue=7, url='https://github.example/o/app/issues/7', sent=now)
        return r

    def task(self):
        return backlog.find(backlog.load(self.cfg), 't1')

    def ask(self, failure=None):
        with mock.patch.object(scouts, 'send', side_effect=self.send):
            return grokpatch.ask(self.cfg, 't1', 'grok', failure=failure)

    def answer(self, r, text):
        with scouts.locked(self.cfg) as sdata:
            scouts.find(sdata, r['id']).update(status='answered', answer=text)
            got = grokpatch.answered(sdata)
        return grokpatch.feed(self.cfg, got)

    def test_sent_held_kept_applied_and_checked(self):
        r = self.ask()
        self.assertIn('x = 1', self.sent[0])
        self.assertNotIn('secret', self.sent[0])   # never a credential from the remote URL
        self.assertIn('t1', grokpatch.holds(backlog.load(self.cfg)['tasks']))
        events = self.answer(r, answer())
        self.assertIn('diff from', events[0])
        self.assertEqual(grokpatch.holds(backlog.load(self.cfg)['tasks']), {})
        ok, wt, _out = grokpatch.apply(self.cfg, 't1')
        self.assertTrue(ok)
        t = self.task()
        self.assertEqual((t['status'], t['branch']), ('review', 'grok/t1'))
        with open(os.path.join(wt, 'app.py')) as f:
            self.assertEqual(f.read(), 'x = 2\n')
        with open(os.path.join(self.repo, 'app.py')) as f:
            self.assertEqual(f.read(), 'x = 1\n')   # the owner's checkout is untouched
        subprocess.run(['git', '-C', self.repo, 'worktree', 'remove', '--force', wt], check=True)

    def test_an_out_of_scope_diff_is_refused_and_a_failed_check_goes_back_once(self):
        r = self.ask()
        events = self.answer(r, answer(DIFF.replace('app.py', 'evil.py')))
        self.assertIn('outside the scope', events[0])
        self.assertIsNone(self.task()['patch']['request'])
        r = self.ask()
        self.answer(r, answer(DIFF.replace('x = 2', 'x = 3')))
        ok, wt, _out = grokpatch.apply(self.cfg, 't1')
        self.assertFalse(ok)
        failure = self.task()['patch']['failure']
        self.assertIn('x = 3', failure['diff'])
        self.ask(failure)
        self.assertIn('YOUR FIRST DIFF FAILED', self.sent[-1])
        self.assertTrue(self.task()['patch']['second'])
        subprocess.run(['git', '-C', self.repo, 'worktree', 'remove', '--force', wt], check=True)

    def test_a_diff_whose_hunk_header_miscounts_its_lines_still_applies(self):
        r = self.ask()
        self.answer(r, answer(DIFF.replace('@@ -1 +1 @@', '@@ -1,1 +1,4 @@')))   # says 4 new lines, has 1 (t259's diff)
        ok, wt, _out = grokpatch.apply(self.cfg, 't1')
        self.assertTrue(ok)
        subprocess.run(['git', '-C', self.repo, 'worktree', 'remove', '--force', wt], check=True)

    def test_a_task_without_file_scope_or_not_queued_is_refused(self):
        with backlog.locked(self.cfg) as data:
            backlog.find(data, 't1')['scope'] = ['src/']
        with self.assertRaisesRegex(grokpatch.PatchError, 'file paths'):
            self.ask()
        with backlog.locked(self.cfg) as data:
            backlog.find(data, 't1').update(scope=['app.py'], status='running')
        with self.assertRaisesRegex(grokpatch.PatchError, 'only a queued task'):
            self.ask()
        with self.assertRaisesRegex(grokpatch.PatchError, 'no diff yet'):
            grokpatch.apply(self.cfg, 't1')



class Autoland(unittest.TestCase):
    """A kept diff lands without a person: pass -> pushed with a PR (never merged); first failure -> back to the scout once;
    second failure -> left for a person. No real push or PR: run and git are faked."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.cfg = {'project': {'state': d.name}, 'scouts': {'grok': {}}, 'autopilot': {'repos': {'app': {'path': d.name}}}}
        data = backlog.load(self.cfg)
        backlog.add(data, backlog.new_task('app', 'Make x two', scope=['app.py', 'tests/test_x.py'], check='true'))
        backlog.save(self.cfg, data)
        self.diff = os.path.join(d.name, 'p.diff')
        with open(self.diff, 'w') as f:
            f.write(DIFF.replace('app.py', 'tests/test_x.py'))
        with backlog.locked(self.cfg) as data:
            backlog.find(data, 't1')['patch'] = {'diff': self.diff, 'request': 's9', 'failure': {'output': 'boom'}}
        self.gits, self.asked = [], []
        p = mock.patch.object(grokpatch, '_git', side_effect=lambda wt, *a, **k: self.gits.append(a[0]) or '')
        p.start()
        self.addCleanup(p.stop)
        self.wt = d.name

    def run_gh(self, cmd, **_kw):
        self.assertEqual(cmd[:3], ['gh', 'pr', 'create'])
        return mock.Mock(returncode=0, stdout='https://github.example/o/app/pull/7\n', stderr='')

    def test_a_pass_is_pushed_with_a_pr_and_never_merged(self):
        line = grokpatch.autoland(self.cfg, 't1', 'grok', run=self.run_gh, apply_fn=lambda c, t, r: (True, self.wt, 'ok'))
        self.assertIn('pull request https://github.example/o/app/pull/7', line)
        self.assertIn('push', self.gits)
        self.assertNotIn('merge', self.gits)
        self.assertEqual(backlog.find(backlog.load(self.cfg), 't1')['pr'], 'https://github.example/o/app/pull/7')

    def test_a_first_failure_goes_back_once_and_a_second_stays_for_a_person(self):
        def fail(c, t, r):
            return False, self.wt, 'boom'
        line = grokpatch.autoland(self.cfg, 't1', 'grok', apply_fn=fail, ask_fn=lambda *a, **k: self.asked.append(k))
        self.assertIn('sent back to grok', line)
        self.assertEqual(self.asked[0]['failure'], {'output': 'boom'})
        with backlog.locked(self.cfg) as data:
            backlog.find(data, 't1')['patch']['second'] = True
        line = grokpatch.autoland(self.cfg, 't1', 'grok', apply_fn=fail, ask_fn=lambda *a, **k: self.asked.append(k))
        self.assertIn('left for a person', line)
        self.assertEqual(len(self.asked), 1)

    def test_new_test_files_join_the_allowlist_when_the_repo_has_one(self):
        self.assertEqual(grokpatch.allowlist_new(self.wt, ['tests/test_x.py']), [])   # no SDIST_ALLOWLIST: left alone


if __name__ == '__main__':
    unittest.main()
