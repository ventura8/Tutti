"""Slack API client (docs/INTEGRATIONS.md)."""
import json
import urllib.error
import urllib.request
from . import secretenv

class SlackError(Exception):
    """A Slack call failed."""

def urlopen_post(url, body, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'tutti'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

class Client:
    def __init__(self, webhook_url, post=None):
        self._url = webhook_url
        self._post = post or urlopen_post

    def post(self, body):
        status, raw = self._post(self._url, body)
        if status != 200:
            raise SlackError(f'Slack answered HTTP {status}: {raw}')
        return json.loads(raw or b'{}')
