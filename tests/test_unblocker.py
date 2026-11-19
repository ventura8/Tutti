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

    def test_invalid_failing_diagnoses_leave_task_unchanged_and_observe_cooldown(self):
        for answer in ('WHY: no action', 'ACTION: retry\nWHY: missing fix', 'ACTION: retry\nFIX: -',
                       'ACTION: person\nWHY: -', 'ACTION: retry\nACTION: person\nWHY: ambiguous\nFIX: try again',
                       'ACTION: drop\nWHY: unknown action'):
            with self.subTest(answer=answer):
                tid = self.task(f'Invalid diagnosis {answer}', attempts=fails('exited 1', 'exited 1', 'exited 1'))
                before = self.get(tid)
                item = {'id': tid, 'kind': 'failing'}
                result = unblocker.handle(self.cfg, item, lambda *_a, **_k: (answer, 'pc-local'), NOW)
                self.assertIn('invalid or incomplete diagnosis', result['outcome'])
                self.assertEqual(self.get(tid), before)
                self.assertFalse(any(q.get('task') == tid for q in needs.load(self.cfg)['questions']))
                log = [{'id': tid, 'at': NOW, 'outcome': result['outcome']}]
                self.assertNotIn(tid, [x['id'] for x in unblocker.find(backlog.load(self.cfg), NOW + 1, log)])

    def test_changed_failing_task_is_not_requeued_or_parked_from_a_stale_diagnosis(self):
        for field, value in (('title', 'Different task'), ('check', 'new check'), ('scope', ['other.py']),
                             ('attempts', fails('exited 2', 'exited 2', 'exited 2')),
                             ('size', 'L'), ('priority', 'P0')):
            for action in ('retry', 'person'):
                with self.subTest(field=field, action=action):
                    tid = self.task(f'Failing {field} {action}', attempts=fails('exited 1', 'exited 1', 'exited 1'),
                                    not_before=NOW + 500)
                    def ask(_cfg, _prompt, _purpose, *, task_id=tid, key=field, new_value=value, selected=action, **_kwargs):
                        with backlog.locked(self.cfg) as data:
                            backlog.find(data, task_id)[key] = new_value
                        return f'ACTION: {selected}\nWHY: old diagnosis\nFIX: old fix', 'pc-local'
                    item = {'id': tid, 'kind': 'failing'}
                    result = unblocker.handle(self.cfg, item, ask, NOW)
                    task = self.get(tid)
                    self.assertIn('changed while diagnosing', result['outcome'])
                    self.assertEqual(task['status'], 'queued')
                    self.assertEqual(task['not_before'], NOW + 500)
                    self.assertFalse(any('old fix' in note for note in task.get('notes', [])))
                    self.assertFalse(any(q.get('task') == tid for q in needs.load(self.cfg)['questions']))

    def test_changed_review_does_not_delay_the_fresh_review_for_six_hours(self):
        tid = self.task('Retry stale review', attempts=fails('exited 1', 'exited 1', 'exited 1'))
        data = backlog.load(self.cfg)
        for outcome in ('task changed while diagnosing: left unchanged for a fresh review',
                        'question or task changed while deciding: left unchanged for a fresh review'):
            with self.subTest(outcome=outcome):
                self.assertEqual([x['id'] for x in unblocker.find(data, NOW + 1, [{'id': tid, 'at': NOW, 'outcome': outcome}])],
                                 [tid])
        self.assertEqual(unblocker.find(data, NOW + 1, [{'id': tid, 'at': NOW, 'outcome': 'a true decision'}]), [])

    def test_deleted_or_reclassified_task_is_left_alone_and_missing_snapshot_fails_closed(self):
        for change in ('deleted', 'reclassified'):
            tid = self.task(f'Failing {change}', attempts=fails('exited 1', 'exited 1', 'exited 1'))
            def ask(_cfg, _prompt, _purpose, *, task_id=tid, mutation=change, **_kwargs):
                with backlog.locked(self.cfg) as data:
                    if mutation == 'deleted':
                        data['tasks'] = [t for t in data['tasks'] if t['id'] != task_id]
                    else:
                        backlog.find(data, task_id)['status'] = 'done'
                return 'ACTION: retry\nWHY: old\nFIX: old', 'pc-local'
            result = unblocker.handle(self.cfg, {'id': tid, 'kind': 'failing'}, ask, NOW)
            self.assertIn('changed meanwhile', result['outcome'])
            self.assertFalse(any(q.get('task') == tid for q in needs.load(self.cfg)['questions']))
        tid = self.task('Failing direct call', attempts=fails('exited 1', 'exited 1', 'exited 1'))
        result = unblocker.apply(self.cfg, {'id': tid, 'kind': 'failing'}, {'action': 'retry'}, NOW)
        self.assertIn('changed while diagnosing', result)
        self.assertEqual(self.get(tid)['status'], 'queued')

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

    def test_exact_multiword_option_is_answered(self):
        tid = self.task('Retry the work', attempts=fails('limit'))
        with backlog.locked(self.cfg) as data:
            needs.ask(self.cfg, backlog.find(data, tid), 'What next?', 'limits', ['retry after reset', 'drop'],
                      'retry after reset', now=NOW)
        self.tick([('ACTION: retry after reset\nWHY: the history says so', 'pc-local')])
        self.assertEqual(self.get(tid)['status'], 'queued')
        self.assertEqual(needs.load(self.cfg)['questions'][-1]['answer'], 'retry after reset')

    def test_extra_words_do_not_turn_into_retry(self):
        tid = self.task('Retry the work', attempts=fails('limit'))
        with backlog.locked(self.cfg) as data:
            needs.ask(self.cfg, backlog.find(data, tid), 'Retry?', 'limits', ['retry', 'drop'], 'retry', now=NOW)
        self.tick([('ACTION: retry later\nWHY: uncertain', 'pc-local')])
        self.assertEqual(self.get(tid)['status'], 'needs-person')
        self.assertEqual(needs.load(self.cfg)['questions'][-1]['status'], 'open')

    def test_person_sentinel_never_answers_an_option_named_person(self):
        tid = self.task('Need a decision', attempts=fails('x'))
        with backlog.locked(self.cfg) as data:
            needs.ask(self.cfg, backlog.find(data, tid), 'Who?', 'a choice', ['person', 'agent'], 'person', now=NOW)
        self.tick([('ACTION: person\nWHY: a person must decide', 'pc-local')])
        self.assertEqual(self.get(tid)['status'], 'needs-person')

    def test_multiple_action_lines_do_not_choose_either(self):
        tid = self.task('Retry the work', attempts=fails('limit'))
        with backlog.locked(self.cfg) as data:
            needs.ask(self.cfg, backlog.find(data, tid), 'Retry?', 'limits', ['retry', 'drop'], 'retry', now=NOW)
        self.tick([('ACTION: retry\nACTION: drop\nWHY: uncertain', 'pc-local')])
        self.assertEqual(self.get(tid)['status'], 'needs-person')

    def test_changed_question_is_not_answered_using_old_model_context(self):
        tid = self.task('Retry task', attempts=fails('limit'))
        with backlog.locked(self.cfg) as data:
            needs.ask(self.cfg, backlog.find(data, tid), 'Retry old work?', 'limits', ['retry', 'drop'], 'retry', now=NOW)
        def ask(cfg, _prompt, _purpose, **_kw):
            with backlog.locked(cfg) as data:
                needs.ask(cfg, backlog.find(data, tid), 'Retry new work?', 'different reason', ['retry', 'drop'], 'retry', now=NOW + 1)
            return 'ACTION: retry\nWHY: old question', 'pc-local'
        events = unblocker.tick(self.cfg, NOW + 2, ask=ask, may_write=lambda _c: True)
        self.assertEqual(self.get(tid)['status'], 'needs-person')
        q = needs.load(self.cfg)['questions'][-1]
        self.assertEqual((q['question'], q['status']), ('Retry new work?', 'open'))
        self.assertIn('changed while deciding', events[0])

    def test_changed_task_history_is_not_answered_using_old_model_context(self):
        tid = self.task('Retry task', attempts=fails('limit'))
        with backlog.locked(self.cfg) as data:
            needs.ask(self.cfg, backlog.find(data, tid), 'Retry?', 'limits', ['retry', 'drop'], 'retry', now=NOW)
        def ask(cfg, _prompt, _purpose, **_kw):
            with backlog.locked(cfg) as data:
                backlog.find(data, tid)['attempts'].append(fails('new failure')[0])
            return 'ACTION: retry\nWHY: old history', 'pc-local'
        unblocker.tick(self.cfg, NOW + 2, ask=ask, may_write=lambda _c: True)
        self.assertEqual(self.get(tid)['status'], 'needs-person')
        self.assertEqual(needs.load(self.cfg)['questions'][-1]['status'], 'open')

    def test_changed_options_or_closed_question_are_not_automatically_answered(self):
        for changed in ('options', 'closed'):
            with self.subTest(changed=changed):
                tid = self.task('Retry ' + changed, attempts=fails('limit'))
                with backlog.locked(self.cfg) as data:
                    needs.ask(self.cfg, backlog.find(data, tid), 'Retry?', 'limits', ['retry', 'drop'], 'retry', now=NOW)
                def ask(cfg, _prompt, _purpose, mode=changed, task_id=tid, **_kw):
                    with needs.locked(cfg) as data:
                        q = next(q for q in data['questions'] if q['task'] == task_id)
                        if mode == 'options':
                            q['options'] = ['retry', 'later']
                        else:
                            q['status'] = 'closed'
                    return 'ACTION: retry\nWHY: old options', 'pc-local'
                unblocker.tick(self.cfg, NOW + 2, ask=ask, may_write=lambda _c: True)
                self.assertEqual(self.get(tid)['status'], 'needs-person')
                q = next(q for q in needs.load(self.cfg)['questions'] if q['task'] == tid)
                self.assertNotIn('answer', q)

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
