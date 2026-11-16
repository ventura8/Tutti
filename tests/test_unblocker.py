"""The unblocker (tutti/unblocker.py, backlog t81): each kind of block found and cleared, local first, online only when
local fails, true decisions left for a person, the failover lease and the cooldown respected, red CI rerun once, and
every attempt logged. Models and gh are fakes."""
import json
import unittest

import tmpdirs

from tutti import backlog, needs, unblocker

NOW = 1_800_000_000.0


def fails(*whys):
    return [{'ok': False, 'why': w, 'step': 's', 'tail': f'tail of {w}'} for w in whys]


class Base(unittest.TestCase):
    def setUp(self):
        self.cfg = {'project': {'state': tmpdirs.mkdtemp()}, 'ai_assist': {'enabled': True},
                    'workers': {'pc-local': {'kind': 'ollama'}, 'agy': {'kind': 'cli'}}, 'machines': {}}

    def task(self, title, **kw):
        with backlog.locked(self.cfg) as data:
            t, _ = backlog.add(data, backlog.new_task('tutti', title, check='bash scripts/check.sh'))
            t.update(kw)
            return t['id']

    def get(self, tid):
        return backlog.find(backlog.load(self.cfg), tid)

    def tick(self, answers=(), **kw):
        asked = []

        def ask(cfg, prompt, purpose, online=True, local_first=True):
            asked.append(online)
            a = answers[len(asked) - 1] if len(asked) <= len(answers) else (None, 'no model answered (pc down)')
            return a
        ev = unblocker.tick(self.cfg, NOW, ask=ask, run=kw.pop('run', lambda *a, **k: None), may_write=kw.pop('may_write', lambda c: True))
        return ev, asked


class Finds(Base):
    def test_usage_limits_only_are_forgiven_without_a_model(self):
        tid = self.task('Add tests', attempts=fails('the worker hit its usage limit', 'the worker hit its usage limit',
                                                     'the worker hit its usage limit'), not_before=NOW + 9000)
        ev, asked = self.tick()
        self.assertEqual(asked, [])
        t = self.get(tid)
        self.assertEqual((t['resume_from'], t['not_before']), (3, NOW))
        self.assertIn('attempts forgiven', ev[0])
        self.assertEqual(unblocker.read_log(self.cfg)[-1]['by'], 'rule')

    def test_a_failing_task_is_diagnosed_locally_and_queued_with_the_fix(self):
        tid = self.task('Add --json', attempts=fails('the worker exited 1', 'check rc=127', 'the worker exited 1'))
        ev, asked = self.tick([('ACTION: retry\nWHY: pytest is not on PATH\nFIX: call python3 -m pytest', 'pc-local')])
        self.assertEqual(asked, [False])   # local only first
        self.assertIn('Fix for this attempt: call python3 -m pytest', self.get(tid)['notes'][-1])
        self.assertEqual(unblocker.read_log(self.cfg)[-1]['by'], 'local')

    def test_local_fails_then_online(self):
        self.task('Add --json', attempts=fails('the worker exited 1', 'the worker exited 1', 'the worker exited 1'))
        _, asked = self.tick([(None, 'pc down'), ('ACTION: retry\nWHY: w\nFIX: f', 'agy')])
        self.assertEqual(asked, [False, True])   # the second call goes online only (local_first=False)
        self.assertEqual(unblocker.read_log(self.cfg)[-1]['by'], 'online')

    def test_a_true_decision_goes_to_the_needs_queue(self):
        tid = self.task('Pick a licence', attempts=fails('the worker exited 1', 'the worker exited 1', 'the worker exited 1'))
        self.tick([('ACTION: person\nWHY: the licence is the owner\'s call\nFIX: -', 'pc-local')])
        self.assertEqual(self.get(tid)['status'], 'needs-person')
        self.assertEqual(len(needs.open_for(self.cfg, 'admin', needs.OWNER)), 1)

    def test_a_parked_question_the_history_decides_is_answered(self):
        tid = self.task('Add tests', attempts=fails('the worker hit its usage limit'))
        with backlog.locked(self.cfg) as data:
            needs.ask(self.cfg, backlog.find(data, tid), 'Retry it?', 'limits', ['retry', 'drop'], 'retry', now=NOW)
        self.tick([('ACTION: retry\nWHY: only usage limits failed it', 'pc-local')])
        self.assertEqual(self.get(tid)['status'], 'queued')

    def test_a_parked_question_the_model_cannot_decide_stays(self):
        tid = self.task('Licence', attempts=fails('x'))
        with backlog.locked(self.cfg) as data:
            needs.ask(self.cfg, backlog.find(data, tid), 'MIT or GPL?', 'a choice', ['mit', 'gpl'], 'mit', now=NOW)
        ev, _ = self.tick([('ACTION: person\nWHY: taste', 'pc-local')])
        self.assertEqual(self.get(tid)['status'], 'needs-person')
        self.assertIn('true decision', ev[0])

    def test_a_rebase_conflict_is_redone_from_scratch(self):
        tid = self.task('X', status='review', publish_note='it does not rebase cleanly onto main')
        self.tick()
        self.assertEqual((self.get(tid)['status'], self.get(tid)['fresh']), ('queued', True))

    def test_red_ci_is_rerun_once_per_commit(self):
        self.cfg['ai_assist']['unblock_repos'] = ['owner/repo']
        calls = []

        class R:
            returncode, stderr = 0, ''

            def __init__(self, out):
                self.stdout = json.dumps(out)
        runs = [{'databaseId': 7, 'headSha': 'abc123', 'event': 'pull_request', 'displayTitle': 'ci'},
                {'databaseId': 8, 'headSha': 'old999', 'event': 'pull_request', 'displayTitle': 'merged PR'}]

        def run(argv, **k):
            calls.append(argv[:3])
            return R([{'headRefOid': 'abc123'}] if argv[1] == 'pr' else runs if argv[2] == 'list' else [])
        self.tick(run=run)
        self.tick(run=run)
        self.assertEqual(calls.count(['gh', 'run', 'rerun']), 1)   # once, and never for the merged PR's old run


    def test_a_429_that_is_not_a_limit_is_a_real_failure(self):
        t = {'id': 't', 'status': 'queued', 'attempts': fails('the worker exited 1 (port 4290)', 'line 429: exited 1', 'exited 1')}
        self.assertEqual(unblocker.classify(t), 'failing')
        t['attempts'] = fails('HTTP 429 Too Many Requests', 'the worker hit its usage limit', 'RESOURCE_EXHAUSTED (code 429)')
        self.assertEqual(unblocker.classify(t), 'limit-only')

    def test_a_task_gone_meanwhile_is_left(self):
        self.assertEqual(unblocker.apply(self.cfg, {'id': 't99', 'kind': 'failing'}, {}, NOW), 'changed meanwhile: left as it is')


    def test_usage_limits_are_forgiven_once_a_day_and_only_after_they_settle(self):
        tid = self.task('Add tests', attempts=[{**a, 'at': NOW - 600} for a in fails('usage limit', 'usage limit', 'usage limit')])
        self.assertEqual(unblocker.find(backlog.load(self.cfg), NOW), [])   # the last limit is 10 minutes old
        t = self.get(tid)
        log = [{'id': tid, 'at': NOW - 3600}]
        self.assertEqual(unblocker.find(backlog.load(self.cfg), NOW + 3600, log), [])   # forgiven an hour ago
        self.assertEqual(len(unblocker.find(backlog.load(self.cfg), NOW + 2 * 86400, log)), 1)
        self.assertEqual(t['status'], 'queued')


class Limits(Base):
    def test_only_the_active_main_acts(self):
        self.task('X', attempts=fails('usage limit', 'usage limit', 'usage limit'))
        self.assertEqual(self.tick(may_write=lambda c: False)[0], [])

    def test_cooldown_and_round_cap(self):
        for i in range(5):
            self.task(f'T{i}', attempts=fails('usage limit', 'usage limit', 'usage limit'), not_before=0)
        self.assertEqual(len(self.tick()[0]), unblocker.MAX_PER_ROUND)
        log = unblocker.read_log(self.cfg)
        left = [i['id'] for i in unblocker.find(backlog.load(self.cfg), NOW + 60, log)]
        self.assertEqual(left, ['t4', 't5'])   # the next round's; the forgiven ones are not blocked any more

    def test_switched_off(self):
        self.cfg['ai_assist']['unblock'] = False
        self.task('X', attempts=fails('usage limit', 'usage limit', 'usage limit'))
        self.assertEqual(self.tick()[0], [])

    def test_nothing_blocked_nothing_done(self):
        self.task('fresh')
        self.assertEqual(self.tick()[0], [])


if __name__ == '__main__':
    unittest.main()
