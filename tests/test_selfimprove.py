"""Self-improvement and Sentry triage: the gates (who takes tasks, which base a pull request may target, when a merge and a
release may happen), dedupe and the daily rate, the local analysis, the triage watches per project and the TUTTI-TASK
lines, and a merge round against a fake `gh`. Nothing leaves the temp folder; no real repository or Sentry is used."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from tutti import autopilot, backlog, cli, config, scouts, selfimprove, telemetry, triage

NOW = 1_800_000_000.0
GREEN = [{'conclusion': 'SUCCESS', 'name': 'ci'}, {'conclusion': 'SUCCESS', 'name': 'SonarCloud'}]


def pr(**kw):
    return {'state': 'OPEN', 'isDraft': False, 'reviewDecision': 'APPROVED', 'open_threads': 0, 'mergeable': 'MERGEABLE',
            'statusCheckRollup': GREEN, 'headRefOid': 'abc123', 'url': 'https://github.com/octo/tutti/pull/7',
            'headRefName': 'autopilot/t1-fix-the-crash', 'isCrossRepository': False,
            'body': 'Backlog task t1 (mcp), done by a worker.', **kw}


def finished_review(rc='0'):
    """A spawn that stands for `tutti pr review`: it writes its exit code where the wrapper would."""
    def spawn(argv, **kw):
        if 'RC_FILE' in (kw.get('env') or {}):   # a release starts without one
            with open(kw['env']['RC_FILE'], 'w', encoding='utf8') as f:
                f.write(rc + '\n')
        spawn.calls.append(argv)
    spawn.calls = []
    return spawn


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, 'state')

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **si):
        raw = {'project': {'state': self.state},
               'autopilot': {'repos': {'tutti': {'path': self.tmp.name, 'check': 'true'}, 'app': {'path': self.tmp.name, 'check': 'true'}}},
               'projects': {
                   'Tutti': {'match': 'tutti', 'repo': 'octo/tutti', 'sentry': {'org': 'octo', 'project': 'tutti'},
                             'self_improve': {'tasks': True, 'autopilot_repo': 'tutti', 'merge': True, 'release': True,
                                              'release_every_hours': 24, 'release_command': 'true'}},
                   'App': {'match': 'app', 'repo': 'octo/app', 'sentry': {'org': 'octo', 'project': 'app'},
                           'self_improve': {'tasks': True, 'autopilot_repo': 'app', 'base': 'develop'}},
                   'Radio': {'match': 'radio', 'sentry': {'org': 'octo', 'project': 'radio'}}},
               'self_improve': {'home': 'Tutti', **si},
               'scouts': {'grok': {'kind': 'grok-bot-webhook', 'url_env': 'U', 'key_env': 'K', 'sentry_triage': True,
                                   'reply': {'github_repo': 'octo/tutti'}}}}
        cfg = config.normalise(raw)
        self.assertEqual(config.validate(cfg), [])
        cfg['_path'] = os.path.join(self.tmp.name, 'tutti.config.yaml')
        return cfg


class Gates(Base):
    def test_only_a_project_that_opts_in_takes_tasks(self):
        cfg = self.cfg()
        self.assertTrue(selfimprove.may_add(selfimprove.policy(cfg, 'Tutti'), cfg)[0])
        ok, why = selfimprove.may_add(selfimprove.policy(cfg, 'Radio'), cfg)
        self.assertFalse(ok)
        self.assertIn('issues only', why)
        self.assertFalse(selfimprove.may_add(None, cfg)[0])

    def test_a_pull_request_never_targets_a_protected_branch_without_merge_authority(self):
        cfg = self.cfg()
        task = {'source': 'sentry', 'pr_mode': 'ready', 'repo': 'app'}
        self.assertEqual(selfimprove.pr_plan(cfg, task, {})[:2], ('ready', 'develop'))
        cfg['projects']['App']['self_improve']['base'] = 'master'
        mode, _base, why = selfimprove.pr_plan(cfg, task, {})
        self.assertIsNone(mode)
        self.assertIn('work branch', why)
        cfg['projects']['App']['self_improve']['base'] = ''
        self.assertIsNone(selfimprove.pr_plan(cfg, task, {})[0])   # no base: gh would pick the default branch
        self.assertEqual(selfimprove.pr_plan(cfg, {**task, 'repo': 'tutti'}, {})[:2], ('ready', None))   # merge authority

    def test_an_ordinary_task_keeps_the_repo_setting(self):
        cfg = self.cfg()
        self.assertEqual(selfimprove.pr_plan(cfg, {'source': 'orchestrator'}, {'pr': 'draft'})[0], 'draft')
        self.assertIsNone(selfimprove.pr_plan(cfg, {'source': 'orchestrator'}, {'pr': False})[0])

    def test_merge_needs_authority_green_checks_reviews_and_no_open_thread(self):
        pol = selfimprove.policy(self.cfg(), 'Tutti')
        self.assertEqual(selfimprove.may_merge(pol, pr(), True), (True, ''))
        cases = [({}, False, 'AI reviews'), ({'reviewDecision': 'CHANGES_REQUESTED'}, True, 'changes'),
                 ({'open_threads': 2}, True, 'thread'), ({'isDraft': True}, True, 'ready'),
                 ({'mergeable': 'CONFLICTING'}, True, 'mergeable'),
                 ({'mergeable': 'UNKNOWN'}, True, 'not computed'), ({'reviewDecision': 'REVIEW_REQUIRED'}, True, 'approving'),
                 ({'statusCheckRollup': [{'conclusion': 'FAILURE'}]}, True, 'green'),
                 ({'statusCheckRollup': [{'status': 'IN_PROGRESS', 'conclusion': ''}]}, True, 'green'),
                 ({'statusCheckRollup': []}, True, 'green'),
                 ({'statusCheckRollup': [{'conclusion': 'SKIPPED'}, {'conclusion': 'NEUTRAL'}]}, True, 'green')]
        for change, reviewed, word in cases:
            ok, why = selfimprove.may_merge(pol, pr(**change), reviewed)
            self.assertFalse(ok, change)
            self.assertIn(word, why)
        ok, why = selfimprove.may_merge(selfimprove.policy(self.cfg(), 'App'), pr(), True)
        self.assertFalse(ok)
        self.assertIn('owner merges', why)

    def test_releases_are_batched_and_spaced(self):
        cfg = self.cfg()
        pol = selfimprove.policy(cfg, 'Tutti')
        self.assertTrue(selfimprove.may_release(pol, NOW - 25 * 3600, 2, NOW)[0])
        self.assertIn('in 1.0 h', selfimprove.may_release(pol, NOW - 23 * 3600, 2, NOW)[1])
        self.assertIn('no merged fix', selfimprove.may_release(pol, 0, 0, NOW)[1])
        self.assertIn('owner tags', selfimprove.may_release(selfimprove.policy(cfg, 'App'), 0, 3, NOW)[1])

    def test_the_daily_room(self):
        tasks = [{'source': 'sentry', 'added': NOW - 100}, {'source': 'insight', 'added': NOW - 2 * 86400},
                 {'source': 'test', 'added': NOW}]
        self.assertEqual(selfimprove.room_today(tasks, NOW, 3), 2)
        self.assertEqual(selfimprove.room_today(tasks * 5, NOW, 3), 0)

    def test_config_errors_name_the_key(self):
        cfg = self.cfg()
        cfg['projects']['App']['self_improve'] = {'tasks': True, 'autopilot_repo': 'nope', 'merge_now': True}
        cfg['projects']['Radio']['self_improve'] = {'merge': True}
        cfg['self_improve']['home'] = 'Ghost'
        errs = '\n'.join(config.validate(cfg))
        for word in ('merge_now', 'autopilot_repo must name', 'need projects.Radio.repo', 'self_improve.home names "Ghost"'):
            self.assertIn(word, errs)


class Analysis(Base):
    def summary(self):
        return {'crashes': [{'where': 'KeyError in main', 'count': 2}], 'most_failing': [{'name': 'ship', 'failed': 4, 'runs': 5},
                {'name': 'run', 'failed': 1, 'runs': 9}], 'stalls': [{'cause': 'run', 'count': 3}],
                'wasted': [{'who': 'agy', 'runs': 1, 'minutes': 3}]}

    def test_insights_become_candidates_over_the_thresholds(self):
        sigs = [c['sig'] for c in selfimprove.candidates(self.summary(), 3)]
        self.assertEqual(sigs, ['crash:KeyError in main', 'failing:ship', 'stall:run'])

    def test_tasks_are_deduped_and_rate_limited(self):
        cfg = self.cfg(max_new_per_day=2)
        items = selfimprove.candidates(self.summary(), 3)
        self.assertEqual(selfimprove.add_candidates(cfg, items, 'insight', NOW), 2)
        self.assertEqual(selfimprove.add_candidates(cfg, items, 'insight', NOW + 10), 0)   # the day's room is used
        self.assertEqual(selfimprove.add_candidates(cfg, items[:2], 'insight', NOW + 2 * 86400), 0)   # the same signatures
        tasks = backlog.load(cfg)['tasks']
        self.assertEqual({t['project'] for t in tasks}, {'Tutti'})
        self.assertTrue(all(t['pr_mode'] == 'ready' and 'regression test' in t['notes'][0] for t in tasks))

    def test_analysis_reads_the_local_events_and_waits_its_interval(self):
        cfg = self.cfg()
        telemetry.configure(self.state, cfg, 'x', env={})
        self.addCleanup(telemetry.unconfigure)
        for _ in range(3):
            telemetry.record('command', 'ship', rc=1, outcome='failed')
        self.assertEqual(len(selfimprove.analyse(cfg, telemetry.time.time())), 1)
        self.assertEqual(selfimprove.analyse(cfg, telemetry.time.time()), [])

    def test_the_switch_turns_the_round_off(self):
        cfg = self.cfg()
        selfimprove.set_enabled(cfg, False)
        self.assertFalse(selfimprove.enabled(cfg))
        self.assertEqual(selfimprove.tick(cfg, NOW), [])
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_selfimprove(cfg, SimpleNamespace(action='on'))
        self.assertIn('self-improvement: on', out.getvalue())


class FakeGh:
    """`gh pr view`, the review-thread query and `gh pr merge`, answered from memory."""

    def __init__(self, view, threads=0, merge_rc=0):
        self.view, self.threads, self.merge_rc, self.calls = view, threads, merge_rc, []

    def __call__(self, args, **_kw):
        self.calls.append(args)
        if args[1:3] == ['pr', 'view']:
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.view), stderr='')
        if args[1:3] == ['api', 'graphql']:
            nodes = [{'isResolved': False}] * self.threads
            body = {'data': {'repository': {'pullRequest': {'reviewThreads': {
                'nodes': nodes, 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}}
            return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr='')
        return SimpleNamespace(returncode=self.merge_rc, stdout='', stderr='refused' if self.merge_rc else '')


class Rounds(Base):
    def add_review_task(self, cfg, repo='tutti', number=7):
        with backlog.locked(cfg) as data:
            t = backlog.new_task(repo, 'Fix the crash', source='sentry')
            t.update(status='review', pr=f'https://github.com/octo/{repo}/pull/{number}', pr_mode='ready', project='Tutti')
            added, _ = backlog.add(data, t)
            added['branch'] = f'autopilot/{added["id"]}-{autopilot.slug(added["title"])}'

    def test_review_thread_gate_reads_all_pages_and_fails_closed_on_bad_pagination(self):
        def page(nodes, more, cursor):
            return {'data': {'repository': {'pullRequest': {'reviewThreads': {
                'nodes': nodes, 'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}}}}}
        results = [page([{'isResolved': True}], True, 'next'), page([{'isResolved': False}], False, None)]
        def run(args, **_kw):
            if len(results) == 1:
                self.assertIn('after=next', args)
            return SimpleNamespace(returncode=0, stdout=json.dumps(results.pop(0)), stderr='')
        self.assertEqual(selfimprove.open_threads('octo/tutti', 7, run), 1)
        broken = page([], True, None)
        gh = lambda _args, **_kw: SimpleNamespace(returncode=0, stdout=json.dumps(broken), stderr='')
        self.assertIsNone(selfimprove.open_threads('octo/tutti', 7, gh))
        malformed = {'data': {'repository': {'pullRequest': {'reviewThreads': {'nodes': []}}}}}
        gh = lambda _args, **_kw: SimpleNamespace(returncode=0, stdout=json.dumps(malformed), stderr='')
        self.assertIsNone(selfimprove.open_threads('octo/tutti', 7, gh))

    def test_reviews_are_asked_once_then_a_green_pr_is_merged_and_released(self):
        cfg = self.cfg()
        self.add_review_task(cfg)
        spawn = finished_review()
        gh = FakeGh(pr())
        ev = selfimprove.tick(cfg, NOW, run=gh, spawn=spawn)
        self.assertIn('AI reviews asked', ev[0])
        self.assertIn('review', spawn.calls[0])
        ev = selfimprove.tick(cfg, NOW + 3600, run=gh, spawn=spawn)
        self.assertTrue(any('merged octo/tutti#7' in e for e in ev))
        self.assertTrue(any('release of Tutti started' in e for e in ev))
        merge = [c for c in gh.calls if c[1:3] == ['pr', 'merge']][0]
        self.assertNotIn('--admin', merge)
        self.assertEqual(merge[merge.index('--match-head-commit') + 1], 'abc123')
        self.assertFalse(selfimprove.lane(cfg)['items'][0]['released'])   # started is not released
        self.assertEqual(selfimprove.tick(cfg, NOW + 3700, run=gh, spawn=spawn), ['self-improvement: release of Tutti done'])
        lane = selfimprove.lane(cfg)
        self.assertEqual((lane['items'][0]['merged'], lane['items'][0]['released'], lane['items'][0]['status']), (True, True, 'done'))
        self.assertEqual(selfimprove.tick(cfg, NOW + 7200, run=gh, spawn=spawn), [])   # spaced

    def test_a_commit_pushed_after_the_review_is_never_merged_unreviewed(self):
        # exploit (audit 2026-11, M4): the reviews ran on abc123; a later push merged with them
        cfg = self.cfg()
        self.add_review_task(cfg)
        spawn = finished_review()
        selfimprove.tick(cfg, NOW, run=FakeGh(pr()), spawn=spawn)
        gh = FakeGh(pr(headRefOid='evil999'))
        selfimprove.tick(cfg, NOW + 3600, run=gh, spawn=spawn)
        self.assertFalse([c for c in gh.calls if c[1:3] == ['pr', 'merge']])
        self.assertIn('new commits', backlog.load(cfg)['tasks'][0]['self_improve'])
        self.assertEqual(selfimprove.read_state(cfg)['reviews'], {})
        selfimprove.tick(cfg, NOW + 7200, run=gh, spawn=spawn)   # asked again, on evil999
        self.assertEqual(selfimprove.read_state(cfg)['reviews']['octo/tutti#7']['sha'], 'evil999')

    def test_a_pull_request_without_its_head_commit_is_not_merged_and_nothing_crashes(self):
        cfg = self.cfg()
        self.add_review_task(cfg)
        spawn = finished_review()
        selfimprove.tick(cfg, NOW, run=FakeGh(pr(headRefOid='')), spawn=spawn)
        gh = FakeGh(pr(headRefOid=''))
        selfimprove.tick(cfg, NOW + 3600, run=gh, spawn=spawn)
        self.assertFalse([c for c in gh.calls if c[1:3] == ['pr', 'merge']])

    def test_a_failed_release_keeps_its_fixes_for_the_next_one(self):
        cfg = self.cfg()
        st = {'merged': [{'project': 'Tutti', 'pr': 'octo/tutti#1', 'at': NOW, 'task': 't1'}]}
        selfimprove.release(cfg, st, NOW + 10, finished_review('1'))
        ev = selfimprove.release(cfg, st, NOW + 20, finished_review('1'))
        self.assertIn('failed (exit 1)', ev[0])
        self.assertNotIn('released', st)
        self.assertEqual(selfimprove.release(cfg, st, NOW + 3600, finished_review()), [])   # the retry waits its interval
        ev = selfimprove.release(cfg, st, NOW + 25 * 3600, finished_review())
        self.assertIn('started with 1 fix', ev[0])

    def test_a_review_that_did_not_finish_well_blocks_the_merge(self):
        cfg = self.cfg()
        self.add_review_task(cfg)
        gh = FakeGh(pr())
        for when, spawn in ((NOW, finished_review('1')), (NOW + 3600, None)):
            selfimprove.tick(cfg, when, run=gh, spawn=spawn or (lambda *a, **k: None))
        self.assertFalse([c for c in gh.calls if c[1:3] == ['pr', 'merge']])
        self.assertIn('AI reviews', backlog.load(cfg)['tasks'][0]['self_improve'])

    def test_changes_requested_send_the_task_back_and_nothing_merges(self):
        cfg = self.cfg()
        self.add_review_task(cfg)
        gh = FakeGh(pr(reviewDecision='CHANGES_REQUESTED'))
        spawn = finished_review()
        selfimprove.tick(cfg, NOW, run=gh, spawn=spawn)
        selfimprove.tick(cfg, NOW + 3600, run=gh, spawn=spawn)
        t = backlog.load(cfg)['tasks'][0]
        self.assertEqual(t['status'], 'queued')
        self.assertFalse([c for c in gh.calls if c[1:3] == ['pr', 'merge']])
        self.assertEqual(selfimprove.read_state(cfg)['reviews'], {})   # the pushed fix is reviewed again

    def test_regular_task_review_is_retried_only_for_an_opted_in_project(self):
        cfg = self.cfg()
        self.add_review_task(cfg)
        with backlog.locked(cfg) as data:
            data['tasks'][0].update(source='mcp', pr_mode=None)
        gh, spawn = FakeGh(pr(reviewDecision='CHANGES_REQUESTED')), finished_review()
        selfimprove.tick(cfg, NOW, run=gh, spawn=spawn)
        selfimprove.tick(cfg, NOW + 3600, run=gh, spawn=spawn)
        task = backlog.load(cfg)['tasks'][0]
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(selfimprove.pr_plan(cfg, task, {})[0], 'ready')
        self.assertFalse([c for c in gh.calls if c[1:3] == ['pr', 'merge']])

        cfg = self.cfg()
        cfg['projects']['Tutti']['self_improve']['merge'] = False
        self.assertEqual(selfimprove.pr_plan(cfg, task, {})[0], None)
        with backlog.locked(cfg) as data:
            data['tasks'][0]['status'] = 'review'
        calls_before = len(spawn.calls)
        selfimprove.tick(cfg, NOW + 7200, run=gh, spawn=spawn)
        self.assertEqual(backlog.load(cfg)['tasks'][0]['status'], 'review')
        self.assertEqual(len(spawn.calls), calls_before)

    def test_regular_task_is_reviewed_again_at_the_fixed_head_before_merge(self):
        cfg = self.cfg()
        self.add_review_task(cfg)
        with backlog.locked(cfg) as data:
            data['tasks'][0].update(source='mcp', pr_mode=None)
        spawn = finished_review()
        requested = FakeGh(pr(reviewDecision='CHANGES_REQUESTED'))
        selfimprove.tick(cfg, NOW, run=requested, spawn=spawn)
        selfimprove.tick(cfg, NOW + 1, run=requested, spawn=spawn)
        self.assertEqual(backlog.load(cfg)['tasks'][0]['status'], 'queued')
        with backlog.locked(cfg) as data:
            data['tasks'][0].update(status='review', review_expected_head='fixed123')  # the worker pushed the fix
        fixed = FakeGh(pr(headRefOid='fixed123'))
        selfimprove.tick(cfg, NOW + 2, run=fixed, spawn=spawn)
        self.assertFalse([c for c in fixed.calls if c[1:3] == ['pr', 'merge']])
        self.assertEqual(selfimprove.read_state(cfg)['reviews']['octo/tutti#7']['sha'], 'fixed123')
        selfimprove.tick(cfg, NOW + 3, run=fixed, spawn=spawn)
        [merge] = [c for c in fixed.calls if c[1:3] == ['pr', 'merge']]
        self.assertEqual(merge[merge.index('--match-head-commit') + 1], 'fixed123')

    def test_requested_changes_never_merge_the_old_head_if_push_failed_or_is_unconfirmed(self):
        cfg = self.cfg()
        self.add_review_task(cfg)
        with backlog.locked(cfg) as data:
            data['tasks'][0].update(source='mcp', pr_mode=None)
        spawn = finished_review()
        requested = FakeGh(pr(reviewDecision='CHANGES_REQUESTED'))
        selfimprove.tick(cfg, NOW, run=requested, spawn=spawn)
        selfimprove.tick(cfg, NOW + 1, run=requested, spawn=spawn)
        with backlog.locked(cfg) as data:
            data['tasks'][0]['status'] = 'review'  # the worker finished but its push failed
        old = FakeGh(pr())
        selfimprove.tick(cfg, NOW + 2, run=old, spawn=spawn)
        self.assertFalse([c for c in old.calls if c[1:3] == ['pr', 'merge']])
        self.assertTrue(backlog.load(cfg)['tasks'][0]['review_retry_pending'])
        with backlog.locked(cfg) as data:
            data['tasks'][0]['review_expected_head'] = 'fixed123'
        selfimprove.tick(cfg, NOW + 3, run=old, spawn=spawn)  # pushed, but PR still shows the old head
        self.assertFalse([c for c in old.calls if c[1:3] == ['pr', 'merge']])
        self.assertTrue(backlog.load(cfg)['tasks'][0]['review_retry_pending'])
        fixed = FakeGh(pr(headRefOid='fixed123'))
        selfimprove.tick(cfg, NOW + 4, run=fixed, spawn=spawn)  # PR now shows the fixed head; request a new review
        self.assertFalse([c for c in fixed.calls if c[1:3] == ['pr', 'merge']])
        self.assertNotIn('review_retry_pending', backlog.load(cfg)['tasks'][0])

    def test_regular_task_missing_a_fresh_review_never_merges(self):
        cfg = self.cfg()
        self.add_review_task(cfg)
        with backlog.locked(cfg) as data:
            data['tasks'][0].update(source='mcp', pr_mode=None)
        gh = FakeGh(pr())
        selfimprove.tick(cfg, NOW, run=gh, spawn=finished_review('1'))
        selfimprove.tick(cfg, NOW + 1, run=gh, spawn=finished_review('1'))
        self.assertFalse([c for c in gh.calls if c[1:3] == ['pr', 'merge']])

    def test_regular_task_must_match_its_repository_branch_and_pr_body(self):
        cfg = self.cfg()
        self.add_review_task(cfg)
        with backlog.locked(cfg) as data:
            data['tasks'][0].update(source='mcp', pr_mode=None)
        task = backlog.load(cfg)['tasks'][0]
        self.assertEqual(selfimprove.ordinary_pr_problem(cfg, task, FakeGh(pr())), '')
        bad_tasks = [{**task, 'pr': 'https://github.com/other/project/pull/7'},
                     {**task, 'branch': 'other-branch'}, {**task, 'pr': 'https://[invalid/pull/7'}]
        for bad in bad_tasks:
            self.assertTrue(selfimprove.ordinary_pr_problem(cfg, bad, FakeGh(pr())))
        for bad in (pr(headRefName='other-branch'), pr(isCrossRepository=True), pr(body='unrelated'),
                    pr(url='https://github.com/other/project/pull/7')):
            gh = FakeGh(bad)
            self.assertTrue(selfimprove.ordinary_pr_problem(cfg, task, gh))
            selfimprove.tick(cfg, NOW, run=gh, spawn=finished_review())
            self.assertFalse([c for c in gh.calls if c[1:3] == ['pr', 'merge']])

    def test_retry_does_not_push_when_original_pr_branch_is_different(self):
        cfg = self.cfg()
        self.add_review_task(cfg)
        with backlog.locked(cfg) as data:
            data['tasks'][0].update(source='mcp', pr_mode=None)
        task = backlog.load(cfg)['tasks'][0]
        calls = []
        def run(*args):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout=('a' * 40 if args[1:3] == ('rev-parse', 'HEAD') else ''), stderr='')
        gh = FakeGh(pr(headRefName='another-branch'))
        autopilot.open_pr(cfg, task, {}, '/wt', task['branch'], 'fix', run=run, gh=gh)
        self.assertEqual(calls, [])
        self.assertIn('not pushed', task['publish_note'])

        gh = FakeGh(pr(headRefOid='a' * 40))
        autopilot.open_pr(cfg, task, {}, '/wt', task['branch'], 'fix', run=run, gh=gh)
        self.assertTrue(any(call[1] == 'push' for call in calls))
        self.assertIn('pushed onto the open pull request', task['publish_note'])
        self.assertEqual(task['pr'], 'https://github.com/octo/tutti/pull/7')

    def test_retry_checks_identity_before_changing_the_local_branch(self):
        cfg = self.cfg()
        self.add_review_task(cfg)
        with backlog.locked(cfg) as data:
            data['tasks'][0].update(source='mcp', pr_mode=None)
        task = backlog.load(cfg)['tasks'][0]
        run = mock.Mock()
        with mock.patch('tutti.bridge.tools', return_value={}):
            autopilot.publish(cfg, task, {'worktree': '/wt', 'diff': '/d', 'id': '1-1'}, run=run,
                              gh=FakeGh(pr(headRefName='unrelated')))
        run.assert_not_called()
        self.assertIn('not publishing', task['publish_note'])

    def test_retry_reports_if_pr_closes_after_the_push(self):
        cfg = self.cfg()
        self.add_review_task(cfg)
        with backlog.locked(cfg) as data:
            data['tasks'][0].update(source='mcp', pr_mode=None)
        task = backlog.load(cfg)['tasks'][0]
        gh = FakeGh(pr())
        run = mock.Mock()
        def close(*args):
            if args[1:3] == ('rev-parse', 'HEAD'):
                return SimpleNamespace(returncode=0, stdout='a' * 40, stderr='')
            gh.view = pr(state='CLOSED')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        run.side_effect = close
        autopilot.open_pr(cfg, task, {}, '/wt', task['branch'], 'fix', run=run, gh=gh)
        self.assertIn('could not confirm', task['publish_note'])

    def test_retry_never_claims_pr_was_updated_without_its_new_head(self):
        cfg = self.cfg()
        self.add_review_task(cfg)
        with backlog.locked(cfg) as data:
            data['tasks'][0].update(source='mcp', pr_mode=None)
        task = backlog.load(cfg)['tasks'][0]
        def run(*args):
            return SimpleNamespace(returncode=0, stdout=('a' * 40 if args[1:3] == ('rev-parse', 'HEAD') else ''), stderr='')
        autopilot.open_pr(cfg, task, {}, '/wt', task['branch'], 'fix', run=run, gh=FakeGh(pr(headRefOid='b' * 40)))
        self.assertIn('could not confirm', task['publish_note'])

    def test_open_threads_or_a_project_without_authority_never_merge(self):
        cfg = self.cfg()
        self.add_review_task(cfg, 'app', 3)
        self.add_review_task(cfg, 'tutti', 4)
        gh = FakeGh(pr(), threads=1)
        for t in (NOW, NOW + 3600):
            selfimprove.tick(cfg, t, run=gh, spawn=finished_review())
        self.assertFalse([c for c in gh.calls if c[1:3] == ['pr', 'merge']])
        gates = {t['repo']: t['self_improve'] for t in backlog.load(cfg)['tasks']}
        self.assertIn('owner merges', gates['app'])
        self.assertIn('thread', gates['tutti'])

    def test_all_review_thread_pages_are_checked_before_merge(self):
        def page(nodes, more, cursor):
            return {'data': {'repository': {'pullRequest': {'reviewThreads': {
                'nodes': nodes, 'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}}}}}
        replies = [page([{'isResolved': True}] * 100, True, 'next'),
                   page([{'isResolved': False}], False, None)]
        def gh(args, **_kw):
            if args[1:3] == ['pr', 'view']:
                return SimpleNamespace(returncode=0, stdout=json.dumps(pr()), stderr='')
            return SimpleNamespace(returncode=0, stdout=json.dumps(replies.pop(0)), stderr='')
        result = selfimprove.pr_state('octo/tutti', 7, gh)
        self.assertEqual(result['open_threads'], 1)
        self.assertFalse(selfimprove.may_merge({'merge': True}, result, True)[0])

    def test_unverifiable_review_thread_pages_fail_closed(self):
        def page(nodes, more, cursor):
            return {'data': {'repository': {'pullRequest': {'reviewThreads': {
                'nodes': nodes, 'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}}}}}
        for response in (None, page([{'isResolved': 'false'}], False, None),
                         page([], 'false', None), page([], True, None),
                         {**page([], False, None), 'errors': [{'message': 'partial response'}]}):
            with self.subTest(response=response):
                def gh(args, *, reply=response, **_kw):
                    data = pr() if args[1:3] == ['pr', 'view'] else reply
                    return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr='')
                self.assertEqual(selfimprove.pr_state('octo/tutti', 7, gh)['open_threads'], 1)
        cyclic = page([], True, 'same')
        def gh_cycle(_args, **_kw):
            return SimpleNamespace(returncode=0, stdout=json.dumps(cyclic), stderr='')
        self.assertIsNone(selfimprove.open_threads('octo/tutti', 7, gh_cycle))


class Triage(Base):
    ANSWER = ('Groups...\n'
              '- TUTTI-TASK: Guard None in cli.main || check: python3 -m unittest tests.test_cli || sentry: 123 || severity: high\n'
              'TUTTI-TASK: Retry the probe on timeout || check: `true` || sentry: 124\nTUTTI-TASK:  \nnot a task line')

    def test_every_project_with_sentry_gets_a_daily_watch_in_its_own_repo(self):
        cfg = self.cfg()
        w = scouts.settings(cfg, 'grok')['watches']
        self.assertEqual(sorted(w), ['sentry-app', 'sentry-radio', 'sentry-tutti'])
        self.assertEqual((w['sentry-app']['repo'], w['sentry-radio']['repo']), ('octo/app', 'octo/tutti'))   # Radio: the scout's
        self.assertEqual(w['sentry-tutti']['every'], '1d')
        self.assertIn('TUTTI-TASK', w['sentry-tutti']['task'])
        cfg['scouts']['grok']['sentry_triage'] = False
        self.assertEqual(scouts.settings(cfg, 'grok')['watches'], {})

    def test_task_lines_are_parsed(self):
        items = triage.parse(self.ANSWER)
        self.assertEqual([i['sentry'] for i in items], ['123', '124'])
        self.assertEqual((items[0]['check'], items[0]['severity'], items[1]['check'], items[1]['severity']),
                         ('python3 -m unittest tests.test_cli', 'high', 'true', 'medium'))

    def request(self, project):
        return {'id': 's1', 'kind': 'watch', 'watch': f'sentry-{project.lower()}', 'project': project, 'status': 'answered',
                'answer': self.ANSWER, 'url': 'https://github.com/octo/tutti/issues/1'}

    def test_an_opted_in_project_gets_tasks_once_per_sentry_issue(self):
        cfg = self.cfg()
        data = {'requests': [self.request('Tutti')]}
        triage.feed_answered(cfg, data, True)
        data['requests'].append({**self.request('Tutti'), 'id': 's2'})
        triage.feed_answered(cfg, data, True)
        tasks = backlog.load(cfg)['tasks']
        self.assertEqual(len(tasks), 2)
        self.assertEqual({(t['source'], t['project'], t['status']) for t in tasks}, {('sentry', 'Tutti', 'queued')})
        self.assertEqual({t['check'] for t in tasks}, {''})   # the repo's own check: a command from the answer is never run
        self.assertIn('not passed on', tasks[0]['notes'][-1])
        self.assertFalse(any('unittest' in n for t in tasks for n in t['notes']))   # the command text never reaches it
        self.assertEqual(data['requests'][0]['fed']['added'], 2)

    def test_triage_tasks_count_toward_the_daily_room(self):
        cfg = self.cfg(max_new_per_day=1)
        triage.feed_answered(cfg, {'requests': [self.request('Tutti')]}, True, NOW)
        tasks = backlog.load(cfg)['tasks']
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['added'], NOW)

    def test_a_read_only_project_gets_the_issue_only(self):
        cfg = self.cfg()
        data = {'requests': [self.request('Radio')]}
        events = triage.feed_answered(cfg, data, True)
        self.assertEqual(backlog.load(cfg)['tasks'], [])
        self.assertIn('issues only', events[0])

    def test_with_self_improvement_off_tasks_wait_as_candidates_until_accepted(self):
        cfg = self.cfg()
        triage.feed_answered(cfg, {'requests': [self.request('App')]}, False)
        t = backlog.load(cfg)['tasks'][0]
        self.assertEqual(t['status'], 'candidate')
        with redirect_stdout(io.StringIO()), backlog.locked(cfg) as data:
            cli.task_change(backlog.find(data, t['id']), SimpleNamespace(action='accept', note=None))
        self.assertEqual(backlog.load(cfg)['tasks'][0]['status'], 'queued')
        with backlog.locked(cfg) as data, self.assertRaises(backlog.BacklogError):
            cli.task_change(backlog.find(data, t['id']), SimpleNamespace(action='accept', note=None))

    def test_a_waiting_candidate_is_accepted_by_itself_when_self_improvement_is_on(self):
        cfg = self.cfg()
        triage.feed_answered(cfg, {'requests': [self.request('App')]}, False)
        triage.feed_answered(cfg, {'requests': [self.request('Radio')]}, False)   # read-only: the issue only, no task
        selfimprove.set_enabled(cfg, False)
        self.assertEqual(selfimprove.accept_waiting(cfg), [])
        self.assertEqual({t['status'] for t in backlog.load(cfg)['tasks']}, {'candidate'})
        selfimprove.set_enabled(cfg, True)
        cfg['self_improve']['auto_accept'] = False
        self.assertFalse(selfimprove.auto_accept(cfg))
        self.assertEqual(selfimprove.accept_waiting(cfg), [])
        cfg['self_improve']['auto_accept'] = True
        events = selfimprove.accept_waiting(cfg)
        tasks = backlog.load(cfg)['tasks']
        self.assertEqual(len(events), len(tasks))
        self.assertEqual({(t['project'], t['status']) for t in tasks}, {('App', 'queued')})
        self.assertEqual(selfimprove.accept_waiting(cfg), [])   # nothing twice, nothing added

    def test_insight_tasks_wait_as_candidates_with_auto_accept_off(self):
        cfg = self.cfg(auto_accept=False)
        selfimprove.add_candidates(cfg, selfimprove.candidates(Analysis.summary(self), 3)[:1], 'insight', NOW)
        self.assertEqual(backlog.load(cfg)['tasks'][0]['status'], 'candidate')

    def test_auto_accept_is_a_true_or_false(self):
        cfg = self.cfg()
        cfg['self_improve']['auto_accept'] = 'yes'
        self.assertIn('self_improve.auto_accept', '\n'.join(config.validate(cfg)))

    def test_sentry_config_errors(self):
        cfg = self.cfg()
        cfg['projects']['App']['sentry'] = {'org': 'Bad Org', 'project': 'app', 'reply_repo': 'nope', 'every': 'daily'}
        errs = '\n'.join(config.validate(cfg))
        for word in ('sentry.org', 'reply_repo', 'sentry.every'):
            self.assertIn(word, errs)


if __name__ == '__main__':
    unittest.main()
