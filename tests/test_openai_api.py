"""The unconnected OpenAI transport uses fake HTTPS responses and never reads real credentials."""
import io
import json
import threading
import unittest
from unittest import mock

from tutti import openai_api


class ResponsesTransport(unittest.TestCase):
    def setUp(self):
        self.worker = {'enabled': True, 'costs_money': True, 'model': 'example-model'}
        self.messages = [{'role': 'user', 'content': 'hello'}]
        self.cancel = threading.Event()
        self.key = 'fake-secret-for-tests'

    @staticmethod
    def event(data):
        return b'data: ' + json.dumps(data).encode() + b'\n\n'

    def test_explicit_paid_opt_in_is_required_before_key_or_network(self):
        key, opener = mock.Mock(return_value=self.key), mock.Mock()
        for worker in ({}, {'enabled': True, 'model': 'x'}, {'costs_money': True, 'model': 'x'},
                       {'enabled': False, 'costs_money': True, 'model': 'x'}):
            with self.assertRaises(openai_api.OpenAIError):
                list(openai_api.stream(worker, self.messages, self.cancel, key, open_request=opener))
        key.assert_not_called()
        opener.assert_not_called()

    def test_streams_text_and_usage_to_fixed_endpoint_without_storing_response(self):
        response = (b': keepalive\n\n' + self.event({'type': 'response.output_text.delta', 'delta': 'hello'}) +
                    self.event({'type': 'response.completed', 'response': {'usage': {'input_tokens': 3, 'output_tokens': 2}}}))
        calls = []
        def opener(request, timeout):
            calls.append((request, timeout))
            return io.BytesIO(response)
        events = list(openai_api.stream(self.worker, self.messages, self.cancel, lambda: self.key, open_request=opener))
        self.assertEqual(events, ['hello', {'usage': {'in': 3, 'out': 2}}])
        request, timeout = calls[0]
        self.assertEqual((request.full_url, timeout), (openai_api.ENDPOINT, 120))
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + self.key)
        body = json.loads(request.data)
        self.assertEqual((body['input'], body['stream'], body['store']), (self.messages, True, False))
        self.assertNotIn(self.key, request.data.decode())

    def test_cancelled_request_never_reads_key_or_opens_network(self):
        key, opener = mock.Mock(), mock.Mock()
        self.cancel.set()
        self.assertEqual(list(openai_api.stream(self.worker, self.messages, self.cancel, key, open_request=opener)), [])
        key.assert_not_called()
        opener.assert_not_called()

    def test_failed_malformed_truncated_and_oversized_streams_fail_without_body_leak(self):
        errors = [self.event({'type': 'response.failed', 'response': {'error': self.key}}),
                  b'data: {bad json\n\n', b'data: {"type":"response.output_text.delta"}',
                  b'data: ' + b'x' * openai_api.MAX_EVENT_BYTES + b'\n\n', b'']
        for data in errors:
            with self.subTest(size=len(data)), self.assertRaises(openai_api.OpenAIError) as failure:
                list(openai_api.stream(self.worker, self.messages, self.cancel, lambda: self.key,
                                       open_request=lambda request, timeout, data=data: io.BytesIO(data)))
            self.assertNotIn(self.key, str(failure.exception))

    def test_cancellation_during_key_lookup_does_not_open_network(self):
        opener = mock.Mock()
        def key_provider():
            self.cancel.set()
            return self.key
        self.assertEqual(list(openai_api.stream(self.worker, self.messages, self.cancel, key_provider, open_request=opener)), [])
        opener.assert_not_called()

    def test_refusal_and_malformed_text_are_not_silently_accepted(self):
        for event in ({'type': 'response.refusal.delta', 'delta': 'no'},
                      {'type': 'response.output_text.delta', 'delta': 123}):
            data = self.event(event) + self.event({'type': 'response.completed', 'response': {}})
            with self.subTest(event=event), self.assertRaises(openai_api.OpenAIError):
                list(openai_api.stream(self.worker, self.messages, self.cancel, lambda: self.key,
                                       open_request=lambda request, timeout, data=data: io.BytesIO(data)))

    def test_only_role_and_content_are_sent_and_timeout_is_bounded(self):
        sent = []
        def opener(request, timeout):
            sent.append(json.loads(request.data))
            return io.BytesIO(self.event({'type': 'response.completed', 'response': {}}))
        messages = [{'role': 'user', 'content': 'hello', 'private_metadata': 'do not send'}]
        list(openai_api.stream(self.worker, messages, self.cancel, lambda: self.key, open_request=opener))
        self.assertEqual(sent[0]['input'], [{'role': 'user', 'content': 'hello'}])
        for invalid in (0, -1, 121, float('inf'), float('nan'), True, '1'):
            with self.assertRaises(openai_api.OpenAIError):
                list(openai_api.stream(self.worker, messages, self.cancel, lambda: self.key, timeout=invalid))

    def test_stop_during_read_does_not_emit_the_line_after_it_returns(self):
        cancel = self.cancel
        class StoppedResponse(io.BytesIO):
            def readline(self, size=-1):
                line = super().readline(size)
                cancel.set()
                return line
        data = self.event({'type': 'response.output_text.delta', 'delta': 'too late'})
        self.assertEqual(list(openai_api.stream(self.worker, self.messages, cancel, lambda: self.key,
                                               open_request=lambda request, timeout: StoppedResponse(data))), [])

    def test_malformed_completion_is_not_treated_as_success(self):
        for response in (None, 'invalid', []):
            data = self.event({'type': 'response.completed', 'response': response})
            with self.subTest(response=response), self.assertRaises(openai_api.OpenAIError):
                list(openai_api.stream(self.worker, self.messages, self.cancel, lambda: self.key,
                                       open_request=lambda request, timeout, data=data: io.BytesIO(data)))

    def test_provider_and_transport_errors_do_not_reveal_secrets(self):
        def fail_key():
            raise ValueError(self.key)
        with self.assertRaises(openai_api.OpenAIError) as failure:
            list(openai_api.stream(self.worker, self.messages, self.cancel, fail_key))
        self.assertNotIn(self.key, str(failure.exception))
        def fail_request(request, timeout):
            raise OSError(self.key)
        with self.assertRaises(openai_api.OpenAIError) as failure:
            list(openai_api.stream(self.worker, self.messages, self.cancel, lambda: self.key, open_request=fail_request))
        self.assertNotIn(self.key, str(failure.exception))

    def test_redirects_are_refused(self):
        self.assertIsNone(openai_api._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid/'))
