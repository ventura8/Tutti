"""Experimental OpenAI Responses transport; not connected to Chat, workers or any live service.

The caller supplies a key provider; it is called only after explicit paid-use opt-in. Keys are never written here.
"""
import json
import math
import urllib.error
import urllib.request

ENDPOINT = 'https://api.openai.com/v1/responses'
MAX_EVENT_BYTES = 1024 * 1024


class OpenAIError(OSError):
    """A safe, credential-free failure from the OpenAI transport."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None   # never send the Authorization header to a redirected endpoint


def _open(request, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _event(lines):
    data = b'\n'.join(lines)
    if not data or data == b'[DONE]':
        return None
    try:
        event = json.loads(data)
    except (ValueError, UnicodeError):
        raise OpenAIError('OpenAI returned an invalid streaming event: retry the request.') from None
    if not isinstance(event, dict):
        raise OpenAIError('OpenAI returned an invalid streaming event: retry the request.')
    return event


def _events(response, cancel):
    """Parse bounded SSE data fields; incomplete or oversized events are errors."""
    data, size = [], 0
    while not cancel.is_set():
        line = response.readline(MAX_EVENT_BYTES + 1)
        if cancel.is_set():
            return
        if not line:
            if data:
                raise OpenAIError('OpenAI ended an incomplete streaming event: retry the request.')
            return
        size += len(line)
        if size > MAX_EVENT_BYTES:
            raise OpenAIError('OpenAI sent an oversized streaming event: retry with a shorter request.')
        line = line.rstrip(b'\r\n')
        if not line:
            event = _event(data)
            data, size = [], 0
            if event is not None:
                yield event
        elif line.startswith(b'data:'):
            value = line[5:]
            data.append(value[1:] if value.startswith(b' ') else value)


def _request(worker, messages, key_provider):
    if worker.get('enabled') is not True or worker.get('costs_money') is not True:
        raise OpenAIError('OpenAI API use is off: explicitly enable its paid API worker before requesting an answer.')
    model = worker.get('model')
    if not isinstance(model, str) or not model.strip():
        raise OpenAIError('OpenAI API model is missing: set the API worker model in configuration.')
    if not isinstance(messages, list) or not all(isinstance(m, dict) and m.get('role') in ('system', 'user', 'assistant')
                                                and isinstance(m.get('content'), str) for m in messages):
        raise OpenAIError('OpenAI API input is invalid: send text messages with supported roles.')
    limit = worker.get('max_output_tokens', 512)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 4096:
        raise OpenAIError('OpenAI max output tokens must be a whole number from 1 to 4096.')
    try:
        key = key_provider()
    except Exception:
        raise OpenAIError('OpenAI API key is unavailable: configure the key in the protected runtime environment.') from None
    if not isinstance(key, str) or not key.strip() or '\n' in key or '\r' in key:
        raise OpenAIError('OpenAI API key is unavailable: configure the key in the protected runtime environment.')
    inputs = [{'role': message['role'], 'content': message['content']} for message in messages]
    body = json.dumps({'model': model, 'input': inputs, 'stream': True, 'store': False, 'max_output_tokens': limit}).encode()
    return urllib.request.Request(ENDPOINT, body, {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                                                  'Accept': 'text/event-stream'})


def _output(events):
    for event in events:
        kind = event.get('type')
        if kind == 'response.output_text.delta':
            if not isinstance(event.get('delta'), str):
                raise OpenAIError('OpenAI returned an invalid text event: retry the request.')
            yield event['delta']
        elif kind in ('response.refusal.delta', 'response.refusal.done'):
            raise OpenAIError('OpenAI refused this request: revise it and try again.')
        elif kind == 'response.completed':
            response = event.get('response')
            if not isinstance(response, dict):
                raise OpenAIError('OpenAI returned an invalid completion event: retry the request.')
            usage = response.get('usage') or {}
            if isinstance(usage, dict):
                yield {'usage': {'in': usage.get('input_tokens') or 0, 'out': usage.get('output_tokens') or 0}}
            return
        elif kind in ('error', 'response.failed', 'response.incomplete'):
            raise OpenAIError('OpenAI could not complete this answer: check account access and retry.')
    raise OpenAIError('OpenAI ended the stream before completion: retry the request.')


def stream(worker, messages, cancel, key_provider, *, open_request=_open, timeout=120):
    """Yield text and usage from Responses SSE. Requires explicit API billing opt-in on every call.

    This is a transport prerequisite, not a coding agent. It deliberately sends no tools or images.
    """
    if cancel.is_set():
        return
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 120
            or not math.isfinite(timeout)):
        raise OpenAIError('OpenAI timeout must be a finite number between zero and 120 seconds.')
    request = _request(worker, messages, key_provider)
    if cancel.is_set():
        return
    try:
        with open_request(request, timeout=timeout) as response:
            yield from _output(_events(response, cancel))
    except OpenAIError:
        if not cancel.is_set():
            raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise OpenAIError('OpenAI could not be reached or returned an invalid response: check connectivity and retry.') from None
