"""Automatic reviews use immutable revisions and never inherit unrelated evidence."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from tutti import review


class Binding(unittest.TestCase):
    def test_provider_review_needs_current_commit_and_a_decisive_state(self):
        head, old = 'a' * 40, 'b' * 40
        now = datetime.now(timezone.utc)
        def event(commit, state):
            return {'user': {'login': 'copilot-pull-request-reviewer[bot]'}, 'commit_id': commit,
                    'state': state, 'submitted_at': now.isoformat(), 'body': 'review'}
        def gh(args):
            return 0, json.dumps([event(old, 'APPROVED'), event(head, 'DISMISSED')]) if '/reviews?' in args[-1] else '[]', ''
        self.assertEqual(review.status('copilot', review.events('o/r', 1, gh, head), now, expected_head=head), 'pending')
        def valid(args):
            return 0, json.dumps([event(head, 'APPROVED')]) if '/reviews?' in args[-1] else '[]', ''
        self.assertEqual(review.status('copilot', review.events('o/r', 1, valid, head), now, expected_head=head), 'reviewed')

    def test_immutable_local_diff_even_when_branch_moves_away_and_back(self):
        calls = []
        base, head = 'b' * 40, 'a' * 40
        file = {'filename': 'x.py', 'patch': '@@ -1 +1 @@\n-old\n+new', 'additions': 1, 'deletions': 1}
        def gh(args):
            calls.append(args)
            value = ({'base': {'sha': base}, 'head': {'sha': head}, 'changed_files': 1}
                     if '/pulls/' in args[1] else {'files': [file]})
            return 0, json.dumps(value), ''
        def worker(*_args, **_kwargs):
            return SimpleNamespace(returncode=0, stdout='No findings.', stderr='')
        cfg = {'workers': {'cheap': {'command': ['cheap']}}}
        with mock.patch.object(review.workers, 'command', return_value=['cheap']), \
             mock.patch.object(review.workers, 'worker_env', return_value={}):
            review.local_review(cfg, 'o/r', 1, 'cheap', [], gh, worker, (base, head))
        self.assertEqual(calls[1][1], f'repos/o/r/compare/{base}...{head}?per_page=100')
        for change in ({'patch': None}, {'patch': '@@ -1 +1 @@\n-old'}, {'additions': 2}):
            with self.subTest(change=change):
                with mock.patch.dict(file, change), self.assertRaises(RuntimeError):
                    review.immutable_diff('o/r', 1, (base, head), gh)

    def test_fallback_review_uses_recorded_revisions_and_rechecks_afterward(self):
        head, base = 'a' * 40, 'b' * 40
        cfg = {'review': {'reviewers': [], 'rounds': {}, 'timeout_minutes': 0, 'poll_seconds': 1}}
        def metadata(sha):
            return 0, json.dumps({'base': {'sha': base}, 'head': {'sha': sha}}), ''
        with mock.patch.object(review, 'ask_all', return_value=([], {}, {})), \
             mock.patch.object(review, 'round_left', return_value=''), \
             mock.patch.object(review, 'pick_fallback', return_value=('cheap', [])), \
             mock.patch.object(review, 'count_round'), \
             mock.patch.object(review, 'local_review', return_value='No findings.') as local, \
             mock.patch.object(review, 'comment'):
            gh = mock.Mock(side_effect=[metadata(head), metadata('c' * 40)])
            self.assertEqual(review.review(cfg, 1, 'o/r', gh_fn=gh, say=lambda _: None, expected_head=head, expected_base=base), 1)
            self.assertEqual(local.call_args.kwargs['revisions'], (base, head))
            gh = mock.Mock(side_effect=[metadata(head), metadata(head)])
            self.assertEqual(review.review(cfg, 1, 'o/r', gh_fn=gh, say=lambda _: None, expected_head=head, expected_base=base), 0)

    def test_local_rounds_distinguish_heads_without_reusing_old_success(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {'project': {'state': d}, 'review': {'rounds': {'local': 1}}}
            first, second = 'a' * 40, 'b' * 40
            review.count_round(cfg, 'o/r', 1, 'local', head=first)
            self.assertTrue(review.round_left(cfg, 'o/r', 1, 'local', head=first))
            self.assertEqual(review.round_left(cfg, 'o/r', 1, 'local', head=second), '')
            self.assertNotEqual(review.exhausted_round_result(first, 'rounds are done'), 0)

    def test_stale_or_missing_revisions_and_old_rounds_fail_closed(self):
        head, base = 'a' * 40, 'b' * 40
        def gh(_args):
            return 0, json.dumps({'base': {'sha': 'b' * 40}, 'head': {'sha': 'c' * 40}}), ''
        with tempfile.TemporaryDirectory() as d:
            cfg = {'review': {'reviewers': [], 'rounds': {'local': 1}, 'timeout_minutes': 0, 'poll_seconds': 1},
                   'project': {'state': d}}
            self.assertEqual(review.review(cfg, 1, 'o/r', gh_fn=gh, say=lambda _: None, expected_head=head, expected_base=base), 1)
        self.assertEqual(review.exhausted_round_result(head, 'its 1 review round(s) on this pull request are done'), 1)
        self.assertFalse(review.revisions_match('o/r', 1, head, ('b' * 40, head), gh, lambda _: None))
        self.assertEqual(review.status('scout', [('owner', datetime.now(timezone.utc), 'grok-scout', 'comment')],
                                       datetime(2000, 1, 1, tzinfo=timezone.utc), expected_head=head), 'pending')
