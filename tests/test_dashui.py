"""The dashboard's one number formatter (tutti/web/format.js, run in node), the card order the owner agreed, and the server
bugs the browser tests found (tests/e2e/test_ui.py)."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from tutti import config, dashboard

FORMAT = os.path.join(dashboard.WEB, 'format.js')


def fmt(expr):
    script = f'const f = require({json.dumps(FORMAT)});\nconsole.log(JSON.stringify({expr}));'
    out = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True, env={**os.environ, 'LC_ALL': 'en_US.UTF-8'})
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class Formatter(unittest.TestCase):
    def test_float_noise_is_rounded_by_size(self):
        self.assertEqual(fmt('[f.tuttiPct(32.122999999999976), f.tuttiPct(7.3500000000000005), f.tuttiPct(0.30000000000000004), '
                             'f.tuttiPct(99.99999999999999), f.tuttiPct(0)]'), ['32%', '7.4%', '0.3%', '100%', '0%'])
        self.assertEqual(fmt('[f.tuttiNum(1234567.891), f.tuttiNum(0.333333333), f.tuttiNum(6.66666666667), '
                             'f.tuttiNum(48.2, {digits: 1})]'),
                         ['1,234,568', '0.33', '6.7', '48.2'])
        self.assertEqual(fmt('[f.tuttiShare(0.42), f.tuttiCompact(1234567), f.tuttiCssPct(132.4), f.tuttiCssPct(-3)]'),
                         ['42%', '1.2M', '100%', '0%'])

    def test_missing_or_bad_values_show_a_dash_never_nan(self):
        self.assertEqual(fmt('[f.tuttiPct(null), f.tuttiPct(undefined), f.tuttiNum("x"), f.tuttiPct(NaN), f.tuttiNum(null, {none: "?"})]'),
                         ['–', '–', '–', '–', '?'])

    def test_the_locale_decides_the_form(self):
        script = (f'const f = require({json.dumps(FORMAT)});\n'
                  'console.log(JSON.stringify([f.tuttiPct(32.1229), f.tuttiNum(1234.5, {digits: 1}), '
                  'new Intl.NumberFormat("de-DE").format(1234.5)]));')
        out = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True,
                             env={**os.environ, 'LC_ALL': 'de_DE.UTF-8', 'LANG': 'de_DE.UTF-8'})
        pct, num, german = json.loads(out.stdout)
        if german != '1.234,5':
            self.skipTest('this node has no German locale data (a small-icu build): the locale form cannot be checked here')
        if num == '1,234.5':
            self.skipTest('this node ignores LC_ALL for its default locale: the locale form cannot be checked here')
        self.assertRegex(pct, r'^32\s%$')
        self.assertEqual(num, '1.234,5')

    def test_the_tidy_leaves_logs_and_titles_alone(self):
        with open(os.path.join(dashboard.WEB, 'app.js'), encoding='utf8') as f:
            app = f.read()
        self.assertNotIn('node.textContent = tuttiTidy(v)', app)   # el() writes data as it is; only plan text is tidied
        self.assertIn('tuttiTidy(a.outlook.text)', app)

    def test_server_text_loses_float_noise_but_keeps_versions(self):
        self.assertEqual(fmt('f.tuttiTidy("heading for 32.122999999999976 % by 21:00 on 0.1.64 at 3.5 h")'),
                         'heading for 32 % by 21:00 on 0.1.64 at 3.5 h')

    def test_the_tidy_needs_no_lookbehind_and_keeps_the_text_around_a_number(self):
        with open(FORMAT, encoding='utf8') as f:
            self.assertNotIn('(?<', f.read())   # a lookbehind is a SyntaxError on Safari before 16.4
        self.assertEqual(fmt('f.tuttiTidy("1.23456789 and (2.000001) x12.34567 v1.2.34567 9.87654")'),
                         '1.2 and (2) x12 v1.2.34567 9.9')


class Order(unittest.TestCase):
    def page(self):
        with open(os.path.join(dashboard.WEB, 'index.html'), encoding='utf8') as f:
            return f.read()

    def test_machines_sits_directly_below_usage_and_pacing(self):
        heads = re.findall(r'<h2 id="([a-z]+)-h">', self.page())
        self.assertEqual(heads[:2], ['usage', 'machines'])

    def test_usage_keeps_online_agents_then_local_models_then_you_and_the_main_agent(self):
        page = self.page()
        usage = page[page.index('aria-labelledby="usage-h"'):page.index('aria-labelledby="machines-h"')]
        self.assertLess(usage.index('id="usage"'), usage.index('id="local-models"'))
        self.assertLess(usage.index('id="local-models"'), usage.index('id="owner"'))

    def test_every_script_loads_after_the_formatter(self):
        scripts = re.findall(r'<script src="([a-z0-9-]+)\.js', self.page())
        self.assertEqual(scripts[0], 'format')

    def test_chat_header_has_the_main_agent_selector(self):
        page = self.page()
        chat = page[page.index('id="chat-view"'):page.index('id="dashboard-view"')]
        self.assertIn('id="chat-main-pick"', chat)
        self.assertIn('id="chat-main-wrap"', chat)
        with open(os.path.join(dashboard.WEB, 'app.js'), encoding='utf8') as f:
            app = f.read()
        self.assertIn("value: 'auto'", app)
        self.assertIn('Auto (now:', app)
        self.assertIn('/api/main', dashboard.ADMIN_PATHS)


class VpnPost(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raw = {'project': {'state': os.path.join(self.tmp.name, 'state'), 'worktrees': self.tmp.name},
               'machines': {'here': {'host': 'local'}}}
        self.c = dashboard.Collector(config.normalise(raw))

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_vpn_card_gets_its_status(self):
        code, answer = dashboard.dispatch(self.c, '/api/vpn', b'{"action": "status"}')
        self.assertEqual(code, 200)
        self.assertIn('checklist', answer)

    def test_a_body_that_is_not_a_json_object_is_a_400_not_a_crash(self):
        for body in (b'', b'[1]', b'not json'):
            code, answer = dashboard.dispatch(self.c, '/api/vpn', body)
            self.assertEqual(code, 400, body)
            self.assertIn('JSON object', answer['error'])


if __name__ == '__main__':
    unittest.main()
