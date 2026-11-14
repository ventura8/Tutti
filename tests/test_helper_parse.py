"""The reading helper's parser (tutti/web/helper-parse.js) on the text of the real Cursor spending page (2026-11-14, name
removed), run in node: the bookmarklet and the orchestrator's page script share it."""
import json
import os
import shutil
import subprocess
import unittest

from tutti import helper

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'cursor-spending.txt')
NOW = '2026-11-14T12:00:00Z'
CURSOR = [{'account': 'cursor', 'label': 'Cursor Models'}, {'account': 'cursor-other', 'label': 'Other Models'}]


def node(js):
    r = subprocess.run(['node', '-e', js], capture_output=True, text=True, env={**os.environ, 'TZ': 'UTC'}, check=True)
    return json.loads(r.stdout)


@unittest.skipUnless(shutil.which('node'), 'node is not installed: the parser test runs where node is (CI has it)')
class HelperParse(unittest.TestCase):
    def parse(self, targets, text=None):
        with open(FIXTURE, encoding='utf8') as f:
            page = f.read() if text is None else text
        return node(f'console.log(JSON.stringify(({helper.joined(helper.PARSE_FILE)})({json.dumps(page)}, {json.dumps(targets)}, '
                    f'Date.parse({json.dumps(NOW)}))))')

    def test_the_real_cursor_page_gives_both_meters_and_the_plan_reset(self):
        r = self.parse(CURSOR)
        self.assertEqual(r['missing'], [])
        got = {x['account']: (x['used'], x['resets']) for x in r['readings']}
        # "Other Models" must not take Grok's weekly "Resets Nov 15" that follows it on the page
        self.assertEqual(got, {'cursor': (25, '2026-12-10T00:00:00.000Z'), 'cursor-other': (14, '2026-12-10T00:00:00.000Z')})

    def test_a_meter_with_its_own_reset_line_keeps_it(self):
        r = self.parse([{'account': 'grok', 'label': 'Weekly usage'}])
        self.assertEqual((r['readings'][0]['used'], r['readings'][0]['resets']), (0, '2026-11-15T00:00:00.000Z'))

    def test_a_meter_stops_at_the_next_label(self):
        r = self.parse(CURSOR, text='Cursor Models\n25%\nOther Models\n14% used\n')
        self.assertEqual([x['used'] for x in r['readings']], [25, 14])

    def test_no_label_searches_the_whole_page(self):
        page = '\n'.join([f'Menu {i}' for i in range(12)] + ['Plan', '37% used'])
        self.assertEqual(self.parse([{'account': 'x', 'label': None}], text=page)['readings'][0]['used'], 37)

    def test_a_label_not_on_the_page_is_missing(self):
        r = self.parse([{'account': 'x', 'label': 'Premium Models'}])
        self.assertEqual((r['readings'], r['missing']), ([], ['Premium Models']))

    def test_a_page_without_any_amount_reads_nothing(self):
        r = self.parse([{'account': 'x', 'label': None}], text='Sign in to continue\nEmail\n')
        self.assertEqual(r['missing'], ['x'])

    def test_the_bookmarklet_opens_the_dashboard_with_the_readings(self):
        with open(FIXTURE, encoding='utf8') as f:
            page = f.read()
        js = helper.bookmarklet(CURSOR, 'KEY', 'http://127.0.0.1:4777')[len('javascript:'):].replace('%25', '%')
        out = node('let opened = null; const alert = () => {}; const location = {host: "cursor.com", pathname: "/dashboard/spending"};'
                   f'const document = {{body: {{innerText: {json.dumps(page)}}}}}; const window = {{open: (u) => {{ opened = u; }}}};'
                   f'{js}; console.log(JSON.stringify(decodeURIComponent(opened.split("#reading=")[1])));')
        sent = json.loads(out)
        self.assertEqual(sent['key'], 'KEY')
        self.assertEqual([(x['account'], x['used']) for x in sent['readings']], [('cursor', 25), ('cursor-other', 14)])

    def test_the_page_script_evaluates_to_json(self):
        js = helper.page_script(CURSOR)
        with open(FIXTURE, encoding='utf8') as f:
            page = f.read()
        out = node(f'const document = {{body: {{innerText: {json.dumps(page)}}}}}; console.log(JSON.stringify({js}));')
        self.assertEqual([x['used'] for x in json.loads(out)['readings']], [25, 14])


if __name__ == '__main__':
    unittest.main()
