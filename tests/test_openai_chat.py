"""Paid OpenAI Chat routing is explicitly selected, owner-only and gated by API billing quota."""
import copy
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.test_chat import ADMIN, VIEWER, ApiBase, snapshot
from tutti import chat, chatctl, chatrun, openai_api, pacing


def billing():
    return {'name': 'api-billing', 'used': 10.0, 'verdict': 'behind', 'panic': False, 'stale': False,
            'reading_at': datetime.now(timezone.utc).isoformat(), 'selected_source': 'openai-api-billing', 'limits': []}


class OpenAIChat(ApiBase):
    def setUp(self):
        super().setUp()
        ready = mock.patch.object(chat, 'OPENAI_PAID_ROUTE_READY', True)
        ready.start()
        self.addCleanup(ready.stop)
        self.cfg['chat'] = {'openai_api': {'enabled': True, 'costs_money': True, 'model': 'example-model',
                                          'account': 'api-billing', 'max_output_tokens': 64}}
        self.cfg['accounts']['api-billing'] = {'enabled': True, 'costs_money': True, 'billing_source': 'openai_api', 'window': '7d'}
        self.snap = snapshot(usage={'accounts': [billing()]})

    def test_paid_route_is_unavailable_in_production_even_with_opt_in(self):
        with mock.patch.object(chat, 'OPENAI_PAID_ROUTE_READY', False):
            choices, reasons = chat.candidates(self.cfg, self.snap, ADMIN, pick='openai-api')
            self.assertEqual(choices, [])
            self.assertIn('reservations', reasons[0])
            engine = self.engine({})
            engine.streams = mock.Mock(side_effect=AssertionError('paid transport must not run'))
            self.send(engine, ADMIN, 'hello', model='openai-api')
            engine.streams.assert_not_called()

    def test_synthetic_and_unverified_billing_sources_fail_closed(self):
        for update in ({'after_reset': True}, {'selected_source': 'estimate'}, {'selected_source': None},
                       {'selected_source': 'reader'}, {'selected_source': 'codex-app-server'}):
            with self.subTest(update=update):
                item = {**billing(), **update}
                self.assertEqual(chat.candidates(self.cfg, snapshot(usage={'accounts': [item]}), ADMIN, pick='openai-api')[0], [])

    def test_owner_can_select_but_auto_and_other_users_cannot_use_paid_api(self):
        explicit, _ = chat.candidates(self.cfg, self.snap, ADMIN, pick='openai-api')
        self.assertEqual([c['kind'] for c in explicit], ['openai_api'])
        for pick in ('auto', 'main', 'local', 'online'):
            choices, _ = chat.candidates(self.cfg, self.snap, ADMIN, pick=pick)
            self.assertNotIn('openai_api', [c['kind'] for c in choices])
        denied, reasons = chat.candidates(self.cfg, self.snap, VIEWER, owner_sub='owner', pick='openai-api')
        self.assertEqual(denied, [])
        self.assertIn('private', reasons[0])

    def test_invalid_or_missing_billing_reading_fails_closed(self):
        for update in ({'stale': True}, {'used': None}, {'panic': True}, {'verdict': 'ahead'},
                       {'reading_at': None}, {'reading_at': (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()},
                       {'used': 99.0}):
            item = {**billing(), **update}
            with self.subTest(update=update):
                candidates, _ = chat.candidates(self.cfg, snapshot(usage={'accounts': [item]}), ADMIN, pick='openai-api')
                self.assertEqual(candidates, [])
        for account_change in ({'enabled': False}, {'costs_money': False}, {'billing_source': 'codex'}):
            cfg = copy.deepcopy(self.cfg)
            cfg['accounts']['api-billing'].update(account_change)
            self.assertEqual(chat.candidates(cfg, self.snap, ADMIN, pick='openai-api')[0], [])
        paused = snapshot(usage={'accounts': [billing()]}, controls={'paused': [{'kind': 'account', 'name': 'api-billing'}]})
        self.assertEqual(chat.candidates(self.cfg, paused, ADMIN, pick='openai-api')[0], [])

    def test_real_pacing_reset_and_estimate_states_do_not_count_as_billing_measurement(self):
        now = datetime.now(timezone.utc)
        configured = {'window': '5h', 'resets': (now + timedelta(hours=1)).isoformat(), 'reader': {'format': 'test'}}
        prior = {'account': 'api-billing', 'used': 50, 'at': (now - timedelta(hours=6)).isoformat()}
        reset_state = pacing.account_state('api-billing', configured, [prior], now)
        self.assertTrue(reset_state.get('after_reset'))
        self.assertIsNotNone(chat._openai_quota_problem(self.cfg, 'api-billing', reset_state))
        estimate = {'account': 'api-billing', 'used': 10, 'at': now.isoformat(), 'source': 'estimate'}
        estimate_state = pacing.account_state('api-billing', configured, [estimate], now)
        self.assertEqual(estimate_state.get('selected_source'), 'estimate')
        self.assertIsNotNone(chat._openai_quota_problem(self.cfg, 'api-billing', estimate_state))

    def test_linked_quota_must_be_present_and_fresh(self):
        self.cfg['accounts']['api-billing']['limits'] = ['monthly']
        self.assertEqual(chat.candidates(self.cfg, self.snap, ADMIN, pick='openai-api')[0], [])
        primary = {**billing(), 'limits': [{**billing(), 'name': 'monthly', 'stale': True}]}
        self.assertEqual(chat.candidates(self.cfg, snapshot(usage={'accounts': [primary]}), ADMIN, pick='openai-api')[0], [])

    def test_chat_never_executes_api_action_lines_and_marks_cost_unknown(self):
        action = 'ACTION {"tool":"backlog_add","args":{"repo":"map","title":"bad","size":"S"}}'
        engine = self.engine({'openai-api': [action, {'usage': {'in': 3, 'out': 2}}]})
        events = self.send(engine, ADMIN, 'hello', model='openai-api')
        done = events[-1]['message']
        self.assertEqual(done['model'], 'OpenAI API (paid)')
        self.assertIn(action, done['text'])
        self.assertTrue(done['usage']['cost_unknown'])
        self.assertFalse(self.calls)
        self.assertTrue(chatctl.add_usage({}, done['usage'])['cost_unknown'])
        # Cleared conversations keep usage=None; totals must still accumulate (t294).
        cleared = chatctl.add_usage({'usage': None}, done['usage'])
        self.assertEqual((cleared['in'], cleared['out']), (3, 2))
        self.assertTrue(cleared['cost_unknown'])

    def test_real_chat_dispatch_reaches_fake_api_transport_only_when_selected(self):
        engine = self.engine({})
        engine.streams = chatrun.stream_of
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'fake-key'}, clear=True):
            with mock.patch.object(openai_api, 'stream', return_value=iter(['fake API answer'])) as transport:
                events = self.send(engine, ADMIN, 'hello', model='openai-api')
        self.assertEqual(events[-1]['message']['text'], 'fake API answer')
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.call_args.args[0]['account'], 'api-billing')

    def test_budget_rechecked_before_starting_model(self):
        engine = self.engine({'openai-api': ['should never run']})
        reads = [self.snap, snapshot(usage={'accounts': [{**billing(), 'panic': True}]})]
        engine.snap_fn = lambda: reads.pop(0) if reads else snapshot(usage={'accounts': [{**billing(), 'panic': True}]})
        events = self.send(engine, ADMIN, 'hello', model='openai-api')
        self.assertNotEqual(events[-1]['message']['model'], 'OpenAI API (paid)')

    def test_transport_reads_only_standard_key_at_call_time(self):
        options = self.cfg['chat']['openai_api']
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'fake-key'}, clear=True):
            with mock.patch.object(openai_api, 'stream', return_value=iter(['fake'])) as transport:
                out = list(chatrun.stream_of({'worker': 'openai-api', 'kind': 'openai_api'}, self.cfg,
                                             [{'role': 'user', 'content': 'hello'}], mock.Mock()))
                self.assertEqual(out, ['fake'])
                args = transport.call_args.args
                self.assertEqual(args[0], options)
                self.assertEqual(args[3](), 'fake-key')

    def test_config_values_do_not_leak_via_chat_limits(self):
        self.cfg['chat']['openai_api']['api_key'] = 'fake-secret'
        self.assertNotIn('fake-secret', repr(chat.settings(self.cfg)))
        self.assertTrue(any('unsupported keys' in e for e in chat.errors(self.cfg)))
        self.assertNotIn('fake-secret', repr(chat.errors(self.cfg)))


if __name__ == '__main__':
    unittest.main()
