"""review.coderabbit: wait (default), fallback, skip and skip-when-unavailable; the re-request queue."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from test_orchestrator import NOW, FakeGh

from tutti import config, review

LIMIT = 'Review limit reached. Next included review available in 42 minutes.'


def cfg_of(d, policy=None, repos=None):
    r = {'reviewers': ['copilot', 'coderabbit'], 'timeout_minutes': 1, 'fallback': ['cheap']}
    if policy:
        r['coderabbit'] = policy
    if repos:
        r['repos'] = repos
    return config.normalise({'project': {'state': d}, 'workers': {'cheap': {'kind': 'cli', 'command': ['cheap', '{prompt}']}},
                             'review': r})


def limited_gh(copilot_review=False):
    later = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    evs = [{'user': {'login': 'coderabbitai[bot]'}, 'created_at': later, 'body': LIMIT}]
    if copilot_review:
        evs.append({'user': {'login': 'copilot-pull-request-reviewer[bot]'}, 'submitted_at': later, 'body': 'fine'})
    return FakeGh(copilot_ok=True, events=evs)


def run(cfg, fake):
    said = []
    done = SimpleNamespace(returncode=0, stdout='No findings.', stderr='')
    with mock.patch.object(review.workers, 'available', return_value=(True, 'cheap')), \
            mock.patch.object(review.subprocess, 'run', return_value=done):
        rc = review.review(cfg, 7, 'o/r', gh_fn=fake, say=said.append, sleep=lambda s: None)
    return rc, said


class PolicyTest(unittest.TestCase):
    def test_the_wait_time_of_the_limit_message_is_read(self):
        self.assertEqual(review.limit_reset(LIMIT, NOW), NOW + timedelta(minutes=43))

    def test_policy_defaults_to_wait_and_a_repo_overrides_it(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(review.coderabbit_policy(cfg_of(d), 'o/r'), 'wait')
            cfg = cfg_of(d, 'fallback', {'o/x': {'coderabbit': 'skip'}})
            self.assertEqual(review.coderabbit_policy(cfg, 'o/r'), 'fallback')
            self.assertEqual(review.coderabbit_policy(cfg, 'o/x'), 'skip')

    def test_bad_policies_are_named(self):
        cfg = config.normalise({'review': {'coderabbit': 'later', 'repos': {'o/x': {'coderabbit': 'never'}, 'o/y': 'skip'}}})
        errs = '\n'.join(config.validate(cfg))
        self.assertIn("review.coderabbit is 'later'", errs)
        self.assertIn("review.repos.o/x.coderabbit is 'never'", errs)
        self.assertIn('review.repos.o/y is', errs)

    def test_wait_keeps_the_pull_request_unmerged_and_queues_a_rerun(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d)
            rc, said = run(cfg, limited_gh(copilot_review=True))
            self.assertEqual(rc, 4)   # Copilot reviewed, but CodeRabbit is required
            self.assertTrue(any('stays unmerged' in s for s in said))
            q = review.read_queue(cfg)
            self.assertEqual([(i['repo'], i['number'], i['policy']) for i in q], [('o/r', 7, 'wait')])
            reran = []
            soon = review.rerequest_due(cfg, datetime.now(timezone.utc), FakeGh(), rerun=lambda c, r, n: reran.append((r, n)))
            self.assertEqual(soon, [])
            out = review.rerequest_due(cfg, datetime.now(timezone.utc) + timedelta(hours=1), FakeGh(),
                                       rerun=lambda c, r, n: reran.append((r, n)))
            self.assertEqual(reran, [('o/r', 7)])
            self.assertIn('started again', out[0])
            self.assertEqual(review.read_queue(cfg), [])

    def test_fallback_reviews_now_and_asks_coderabbit_later(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d, 'fallback')
            rc, said = run(cfg, limited_gh())
            self.assertEqual(rc, 0)   # the local review had no findings
            self.assertTrue(any('local review by cheap' in s for s in said))
            fake = FakeGh()
            out = review.rerequest_due(cfg, datetime.now(timezone.utc) + timedelta(hours=1), fake)
            self.assertIn(['pr', 'comment', '7', '--repo', 'o/r', '--body', '@coderabbitai review'], fake.calls)
            self.assertIn('asked again', out[0])

    def test_a_failed_rerequest_stays_queued(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d, 'fallback')
            review.schedule_rerequest(cfg, 'o/r', 7, NOW, 'fallback')
            fail = mock.Mock(return_value=(1, '', 'no network'))
            out = review.rerequest_due(cfg, NOW + timedelta(minutes=1), fail)
            self.assertIn('could not be asked again', out[0])
            self.assertEqual(len(review.read_queue(cfg)), 1)

    def test_skip_never_asks_coderabbit(self):
        with tempfile.TemporaryDirectory() as d:
            fake = limited_gh(copilot_review=True)
            rc, said = run(cfg_of(d, 'skip'), fake)
            self.assertEqual(rc, 0)
            self.assertFalse(any('@coderabbitai review' in c for c in fake.calls))
            self.assertTrue(any('not asked' in s for s in said))

    def test_skip_when_unavailable_merges_on_the_other_reviews(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d, 'skip-when-unavailable')
            rc, _said = run(cfg, limited_gh(copilot_review=True))
            self.assertEqual(rc, 0)
            self.assertEqual(review.read_queue(cfg), [])   # no re-request, no follow-up


class GreptileTest(unittest.TestCase):
    def test_greptile_is_asked_read_and_held_like_coderabbit(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d)
            cfg['review']['reviewers'] = ['greptile']
            later = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
            fake = FakeGh(events=[{'user': {'login': 'greptile-apps[bot]'}, 'created_at': later,
                                   'body': 'Rate limit exceeded. Please wait 10 minutes.'}])
            rc, _said = run(cfg, fake)
            self.assertIn(['pr', 'comment', '7', '--repo', 'o/r', '--body', '@greptileai review'], fake.calls)
            self.assertEqual(rc, 4)
            self.assertEqual([i['who'] for i in review.read_queue(cfg)], ['greptile'])
            ok = [('greptile-apps[bot]', NOW, 'summary', 'review')]
            self.assertEqual(review.status('greptile', ok, NOW - timedelta(minutes=1)), 'reviewed')


class RoundsTest(unittest.TestCase):
    def test_one_round_each_by_default_then_done(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d, 'skip')
            later = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
            fake = FakeGh(copilot_ok=True, events=[{'user': {'login': 'copilot-pull-request-reviewer[bot]'}, 'submitted_at': later,
                                                    'body': 'fine'}])
            self.assertEqual(run(cfg, fake)[0], 0)
            rc, said = run(cfg, FakeGh(copilot_ok=True))   # the second round: Copilot and the local worker used theirs
            self.assertEqual(rc, 0)
            self.assertTrue(any('copilot is not asked: its 1 review round' in s for s in said))

    def test_zero_rounds_never_calls_the_reviewer(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d, 'skip')
            cfg['review']['rounds'] = {'copilot': 0, 'local': 0}
            fake = FakeGh(copilot_ok=True)
            rc, said = run(cfg, fake)
            self.assertEqual(rc, 1)   # no one reviews: a person does
            self.assertFalse(any(c[:2] == ['api', '-X'] for c in fake.calls))
            self.assertTrue(any('not called' in s for s in said))

    def test_a_limit_answer_is_no_round(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d)
            run(cfg, limited_gh())
            self.assertEqual(review.round_left(cfg, 'o/r', 7, 'coderabbit'), '')

    def test_bad_rounds_are_named(self):
        cfg = config.normalise({'review': {'rounds': {'copilot': -1, 'grok': 'two'}}})
        errs = '\n'.join(config.validate(cfg))
        self.assertIn('review.rounds.copilot is -1', errs)
        self.assertIn("review.rounds.grok is 'two'", errs)


class SecurityRoundTest(unittest.TestCase):
    def test_is_tutti_project_identifies_tutti_repos(self):
        cfg = config.normalise({'projects': {'Tutti': {'match': 'tutti', 'repo': 'octo/tutti'}}})
        self.assertTrue(review.is_tutti_project(cfg, 'octo/tutti'))
        self.assertTrue(review.is_tutti_project(cfg, 'tutti'))
        self.assertTrue(review.is_tutti_project(cfg, 'my/tutti'))
        self.assertFalse(review.is_tutti_project(cfg, 'octo/other'))
        self.assertFalse(review.is_tutti_project(cfg, ''))

    def test_security_rounds_defaults_to_one_for_tutti_and_zero_for_others(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d)
            self.assertEqual(review.security_rounds(cfg, 'octo/tutti'), 1)
            self.assertEqual(review.security_rounds(cfg, 'o/r'), 0)

    def test_security_rounds_admin_configurable_globally_and_per_repo(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d)
            cfg['review']['rounds'] = {'security': 2}
            self.assertEqual(review.security_rounds(cfg, 'o/r'), 2)
            cfg['review']['rounds']['security'] = 0
            self.assertEqual(review.security_rounds(cfg, 'octo/tutti'), 0)
            cfg['review']['security_rounds'] = 3
            self.assertEqual(review.security_rounds(cfg, 'o/r'), 3)
            cfg['review']['repos'] = {'o/r': {'security_rounds': 4}}
            self.assertEqual(review.security_rounds(cfg, 'o/r'), 4)

    def test_security_round_requests_bugbot_greptile_coderabbit_and_grok(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d)
            fake = FakeGh()
            said = []
            with mock.patch.object(review, 'ask_scout', return_value='') as mock_scout:
                review.request_security_round(cfg, 'octo/tutti', 7, gh_fn=fake, say=said.append)
                mock_scout.assert_called_once_with(cfg, 'octo/tutti', 7, 'grok', fake, security=True)
            self.assertIn(['pr', 'comment', '7', '--repo', 'octo/tutti', '--body', 'bugbot run'], fake.calls)
            self.assertIn(['pr', 'comment', '7', '--repo', 'octo/tutti', '--body', '@greptileai review'], fake.calls)
            self.assertIn(['pr', 'comment', '7', '--repo', 'octo/tutti', '--body', '@coderabbitai review'], fake.calls)
            self.assertTrue(any('asked Cursor Security Review/Bugbot' in s for s in said))
            self.assertTrue(any('asked Greptile' in s for s in said))
            self.assertTrue(any('asked CodeRabbit' in s for s in said))
            self.assertTrue(any('asked grok scout (security focus)' in s for s in said))

    def test_security_round_runs_on_tutti_pr_during_review_and_counts_round(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d, 'skip')
            cfg['review']['timeout_minutes'] = 0
            fake = FakeGh(copilot_ok=True)
            said = []
            with mock.patch.object(review, 'ask_scout', return_value=''):
                rc, said = run(cfg, fake)
            # o/r is not tutti: no security round run
            self.assertFalse(any('starting security review round' in s for s in said))

            said_tutti = []
            done = SimpleNamespace(returncode=0, stdout='No findings.', stderr='')
            with mock.patch.object(review, 'ask_scout', return_value=''), \
                    mock.patch.object(review.workers, 'available', return_value=(True, 'cheap')), \
                    mock.patch.object(review.subprocess, 'run', return_value=done):
                rc = review.review(cfg, 7, 'octo/tutti', gh_fn=fake, say=said_tutti.append, sleep=lambda s: None)
            self.assertEqual(rc, 0)
            self.assertTrue(any('starting security review round' in s for s in said_tutti))
            left = review.round_left(cfg, 'octo/tutti', 7, 'security')
            self.assertIn('done', left)

            # second run: security round already done
            said_again = []
            with mock.patch.object(review, 'ask_scout', return_value=''), \
                    mock.patch.object(review.workers, 'available', return_value=(True, 'cheap')), \
                    mock.patch.object(review.subprocess, 'run', return_value=done):
                review.review(cfg, 7, 'octo/tutti', gh_fn=fake, say=said_again.append, sleep=lambda s: None)
            self.assertTrue(any('security round not run' in s for s in said_again))

    def test_bad_security_rounds_are_named_in_config_validation(self):
        cfg = config.normalise({'review': {'security_rounds': -1, 'repos': {'o/r': {'security_rounds': 'three'}}}})
        errs = '\n'.join(config.validate(cfg))
        self.assertIn('review.security_rounds is -1', errs)
        self.assertIn("review.repos.o/r.security_rounds is 'three'", errs)

    def test_failed_bot_request_does_not_consume_security_round(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d)
            with mock.patch.object(review, 'request', return_value='could not comment'), \
                    mock.patch.object(review, 'ask_scout', return_value=''):
                self.assertFalse(review.run_security_round(cfg, 'octo/tutti', 7, FakeGh(), lambda _: None))
            self.assertEqual(review.round_left(cfg, 'octo/tutti', 7, 'security'), '')

    def test_scout_failure_still_counts_when_bots_succeed(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d)
            with mock.patch.object(review, 'ask_scout', return_value='not configured'):
                self.assertTrue(review.run_security_round(cfg, 'octo/tutti', 7, FakeGh(), lambda _: None))
            self.assertIn('done', review.round_left(cfg, 'octo/tutti', 7, 'security'))

    def test_security_can_be_enabled_for_another_repo(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d)
            cfg['review']['rounds'] = {'security': 1}
            with mock.patch.object(review, 'ask_scout', return_value=''):
                self.assertTrue(review.run_security_round(cfg, 'o/r', 7, FakeGh(), lambda _: None))

    def test_grok_scout_security_focus_task(self):
        from tutti import trial
        with tempfile.TemporaryDirectory() as d:
            cfg = config.normalise({'project': {'state': d}, 'scouts': {'grok': {'kind': 'grok-bot-webhook', 'url_env': 'U',
                                                                                  'key_env': 'K', 'skills': ['review']}}})
            sent = []
            with mock.patch.object(trial.scouts, 'guard'), \
                    mock.patch.object(trial.scouts, 'secrets', return_value=('http://example.com', 'key')), \
                    mock.patch.object(trial.scouts, 'send', side_effect=lambda sc, r, t, k, gh, post: sent.append(r)):
                trial.ask_review(cfg, 'grok', 'octo/tutti', 7, 'https://github.com/octo/tutti/pull/7', security=True)
            self.assertEqual(len(sent), 1)
            self.assertIn('focus on security', sent[0]['task'])
            self.assertIn('Report only real security problems', sent[0]['task'])


if __name__ == '__main__':
    unittest.main()
