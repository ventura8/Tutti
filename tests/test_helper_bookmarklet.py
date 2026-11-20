"""The reading helper's bookmarklet (tutti/web/helper-bookmarklet.js), run in node."""

import json
import os
import shutil
import subprocess
import unittest

from tutti import helper

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'cursor-spending.txt')
TARGETS = [
    {'account': 'cursor', 'label': 'Cursor Models'},
    {'account': 'cursor-other', 'label': 'Other Models'},
]
ORIGIN = 'http://127.0.0.1:4777'


def node(js):
    out = subprocess.run(['node', '-e', js], capture_output=True, text=True, env={**os.environ, 'TZ': 'UTC'}, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which('node'), 'node is not installed: this bookmarklet test runs where node is (CI has it)')
class HelperBookmarklet(unittest.TestCase):
    def bookmarklet_script(self):
        # A javascript: URL is decoded before execution, so tests decode %25 before evaluating the script body.
        return helper.bookmarklet(TARGETS, 'KEY', ORIGIN)[len('javascript:'):].replace('%25', '%')

    def test_valid_page_opens_dashboard_with_readings(self):
        with open(FIXTURE, encoding='utf8') as fixture:
            page = fixture.read()
        js = self.bookmarklet_script()
        payload = node(
            'let opened = null; const alerts = [];'
            'const alert = (msg) => { alerts.push(msg); };'
            'const location = {host: "cursor.com", pathname: "/dashboard/spending"};'
            f'const document = {{body: {{innerText: {json.dumps(page)}}}}};'
            'const window = {open: (u) => { opened = u; }};'
            f'{js};'
            'const reading = JSON.parse(decodeURIComponent(opened.split("#reading=")[1]));'
            'console.log(JSON.stringify({opened: opened, alerts: alerts, reading: reading}));'
        )
        self.assertEqual(payload['alerts'], [])
        self.assertTrue(payload['opened'].startswith(ORIGIN + '/#reading='))
        self.assertEqual(payload['reading']['key'], 'KEY')
        self.assertEqual(payload['reading']['account'], 'cursor')
        self.assertEqual(
            [(item['account'], item['used']) for item in payload['reading']['readings']],
            [('cursor', 25), ('cursor-other', 14)],
        )

    def test_loading_page_shows_alert_and_does_not_open_dashboard(self):
        js = self.bookmarklet_script()
        payload = node(
            'let opened = null; const alerts = [];'
            'const alert = (msg) => { alerts.push(msg); };'
            'const location = {host: "cursor.com", pathname: "/dashboard/spending"};'
            'const document = {body: {innerText: "Spending\\nLoading...\\n"}};'
            'const window = {open: (u) => { opened = u; }};'
            f'{js};'
            'console.log(JSON.stringify({opened: opened, alerts: alerts}));'
        )
        self.assertIsNone(payload['opened'])
        self.assertEqual(len(payload['alerts']), 1)
        self.assertIn('still loading', payload['alerts'][0])


if __name__ == '__main__':
    unittest.main()
