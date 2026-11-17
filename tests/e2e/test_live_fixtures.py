"""The learning loop's mock half (tutti/selftest.py, docs/TESTING.md, *Live self-test and learning loop*): every fixture the
live self-test wrote (tests/e2e/fixtures/live-<id>.json: a finding and the redacted /api/state it was drawn from) replayed in
the mock dashboard, whose /api/state answers that JSON. The check that failed live must pass here: until the bug is fixed
this test fails, so the backlog task the self-test filed has a failing test to make pass.

Run one: PYTHONPATH=tests python3 -m unittest discover -s tests/e2e -p test_live_fixtures.py -k <id>"""
import glob
import json
import os
import unittest

import test_ui as base

FIXTURES = sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'live-*.json')))
CHECK = {'layout': '[...tuttiChecks.layout(), ...tuttiChecks.headlinesFit()]', 'copy': 'tuttiChecks.copyLint()'}


def setUpModule():
    if FIXTURES:
        base.setUpModule()


def tearDownModule():
    if FIXTURES:
        base.tearDownModule()


class LiveFixtures(unittest.TestCase):
    maxDiff = None

    def replay(self, path):
        with open(path, encoding='utf8') as f:
            fx = json.load(f)
        finding, state = fx['finding'], fx['state']
        ctx = base.new_context(base.VIEWER, finding['width'] if finding['width'] in base.WIDTHS else 'laptop', finding['theme'])
        ctx.route('**/api/state', lambda r: r.fulfill(status=200, content_type='application/json', body=json.dumps(state)))
        page = base.open_view(ctx)
        console = []
        page.on('console', lambda m: console.append(m.text) if m.type == 'error' else None)
        target = next((h for n, h in base.tabs(page) if n == finding['view']), None)
        if finding['view'] == 'Household':
            base.go(page, '#dashboard')
            page.click('[data-view-pick="household"]')
        elif target is not None:
            base.go(page, target)
        kind = finding['check']
        if kind == 'axe':
            found = base.axe(page)
        elif kind == 'console':
            found = page.errors + console
        else:
            found = page.evaluate(CHECK[kind])
        ctx.close()
        self.assertEqual(found, [], f'the live finding still reproduces: {finding["detail"]}')


def _make(path):
    return lambda self: self.replay(path)


for _p in FIXTURES:
    setattr(LiveFixtures, f'test_{os.path.basename(_p)[5:-5]}', _make(_p))
