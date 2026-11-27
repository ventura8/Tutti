"""Replay sanitized live findings as synthetic browser faults."""

import json
import os
from pathlib import Path

from tutti import selftest

MOCK_PERMIT = 'mock-dashboard-permit'
FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'selftest'
FAULTS = {
    'page': ('/', {'status': 500, 'body': ''}),
    'viewer': ('/api/auth/me', {'json': {'enabled': False}}),
    'state': ('/api/state', {'status': 500, 'body': ''}),
    'script': ('/qr.js', {'content_type': 'text/javascript', 'body': 'throw new Error("mock failure");'}),
}


def fixtures():
    folders = [FIXTURES]
    if extra := os.environ.get('TUTTI_SELFTEST_FIXTURES'):
        folders.append(Path(extra))
    for folder in folders:
        for path in sorted(folder.glob('*.json')):
            yield path.name, json.loads(path.read_text(encoding='utf8'))


def replay(context, base, fixture):
    path, answer = FAULTS[fixture['check']]
    context.route(lambda url: url.split('?', 1)[0] == base + path, lambda route: route.fulfill(**answer))
    return selftest.probe(context.new_page(), base, timeout=6000)
