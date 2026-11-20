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


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class QuotaRoomDisplay(unittest.TestCase):
    def test_quota_room_is_an_estimate_even_when_autopilot_is_stopped(self):
        app = os.path.join(dashboard.WEB, 'app.js')
        script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const code = src.slice(src.indexOf('function roomLine('), src.indexOf('// A scout', src.indexOf('function roomLine(')));
const ui = {autopilot: {enabled: true, fill_room: true, accounts: ['agent'], running: [], queue: [], fill: {}}};
function el(tag, attrs, ...children) { return {tag, attrs, children}; }
function plural(n, unit) { return `${n} ${unit}${n === 1 ? '' : 's'}`; }
function fillRoom() {}
eval(code + `
const active = roomLine({name:'agent',room_tasks:51});
ui.autopilot.enabled = false;
const stopped = roomLine({name:'agent',room_tasks:51});
console.log(JSON.stringify([active, stopped]));`);
"""
        result = subprocess.run(['node', '-e', script, app], capture_output=True, text=True, check=True)
        active, stopped = json.loads(result.stdout)
        self.assertIn('Estimated quota room: 51 bounded tasks', active['attrs']['text'])
        self.assertIn('Starts depend on account eligibility and capacity', active['attrs']['text'])
        self.assertIn('Estimated quota room: 51 bounded tasks', stopped['children'][0])
        self.assertIn('Starts depend on account eligibility and capacity', stopped['children'][0])


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class OffAccountDisplay(unittest.TestCase):
    def test_all_off_renders_status_and_reason_without_actions(self):
        app = os.path.join(dashboard.WEB, 'app.js')
        script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const code = src.slice(src.indexOf('function offAccount('), src.indexOf('// ---- the owner', src.indexOf('function offAccount(')));
const nodes = {};
function $(id) { return nodes[id] ||= {replaceChildren(...children) {this.children = children;}}; }
function el(tag, attrs, ...children) { return {tag, attrs, children}; }
function chip(text) { return el('span', {text}); }
function empty(text) { return el('empty', {text}); }
function codeText() { return ''; }
function plural() { return ''; }
const ui = {editing: new Set(), formPending: false};
const usage = {accounts: [], off_accounts: [{name: 'cursor', status: 'paused', reason: 'rest'}], log: 'log'};
eval(code + '\nrenderUsage({usage}, 0, false);');
console.log(JSON.stringify(nodes.usage.children));
"""
        out = subprocess.run(['node', '-e', script, app], capture_output=True, text=True, check=True)
        cards = json.loads(out.stdout)
        self.assertEqual(len(cards), 1)
        self.assertIn('cursor', json.dumps(cards))
        self.assertIn('paused', json.dumps(cards))
        self.assertIn('rest', json.dumps(cards))
        self.assertNotIn('button', json.dumps(cards))
        self.assertNotIn('No accounts yet', json.dumps(cards))


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class UsageGraphInteraction(unittest.TestCase):
    def test_pointer_touch_and_keyboard_select_actual_readings(self):
        app = os.path.join(dashboard.WEB, 'app.js')
        script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const code = src.slice(src.indexOf('function sparkline('), src.indexOf('\nfunction facts(', src.indexOf('function sparkline(')));
function node(tag, attrs = {}) { return {tag, attrs: {...attrs}, children: [], events: {},
  append(...items) { this.children.push(...items); }, setAttribute(k, v) { this.attrs[k] = String(v); },
  addEventListener(k, f) { this.events[k] = f; }, getBoundingClientRect() { return {left: 10, width: 300}; }}; }
function svg(tag, attrs) { return node(tag, attrs); }
function el(tag, attrs, ...kids) { const n = node(tag, attrs); n.append(...kids); return n; }
function localTime(s) { return s; }
function utcTitle(s) { return s; }
function tuttiPct(n) { return n + '%'; }
const account = {name:'agent', window_start:'2026-01-01T00:00:00Z', reset:'2026-01-01T10:00:00Z',
  target:'2026-01-01T09:00:00Z', panic_at:90, readings:[
    {at:'2026-01-01T02:00:00Z',used:20}, {at:'2026-01-01T08:00:00Z',used:80}]};
eval(code + `
const box = sparkline(account, Date.parse('2026-01-01T05:00:00Z'));
const g = box.children[0], detail = box.children[1], values = [];
values.push([detail.textContent, g.attrs['aria-valuetext']]);
g.events.pointermove({pointerType:'mouse',clientX:70}); values.push([detail.textContent,g.attrs['aria-valuenow']]);
g.events.pointerdown({pointerType:'touch',clientX:250}); values.push([detail.textContent,g.attrs['aria-valuenow']]);
let prevented = false; g.events.keydown({key:'ArrowLeft',preventDefault(){prevented=true;}});
values.push([detail.textContent,g.attrs['aria-valuenow'],prevented]);
const empty = sparkline({...account,readings:[]}, Date.now());
values.push([empty.children[0].attrs.role, empty.children.length]);
const bad = sparkline({...account,readings:[null,{}, {at:'bad',used:4}, {at:account.window_start,used:'x'},
  {at:{toString:null},used:5}]}, Date.now());
values.push([bad.children[0].attrs.role, bad.children[0].attrs['aria-label']]);
const absent = sparkline({...account,readings:null}, Date.now());
values.push([absent.children[0].attrs.role, absent.children.length]);
const one = sparkline({...account,readings:[account.readings[0], {at:account.reset,used:101}]}, Date.now());
values.push([one.children[0].attrs.role, one.children[1].textContent]);
console.log(JSON.stringify(values));`);
"""
        out = subprocess.run(['node', '-e', script, app], capture_output=True, text=True, check=True)
        values = json.loads(out.stdout)
        self.assertIn('80%', values[0][0])
        self.assertEqual(values[0][0], values[0][1])
        self.assertIn('20%', values[1][0])
        self.assertEqual(values[1][1], '1')
        self.assertIn('80%', values[2][0])
        self.assertEqual(values[2][1], '2')
        self.assertIn('20%', values[3][0])
        self.assertEqual(values[3][1:], ['1', True])
        self.assertEqual(values[4], ['img', 1])
        self.assertEqual(values[5][0], 'img')
        self.assertIn('0 readings', values[5][1])
        self.assertEqual(values[6], ['img', 1])
        self.assertEqual(values[7][0], 'img')
        self.assertIn('20%', values[7][1])


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

    def test_wsl_and_desktop_environments_render_inside_one_physical_machine_card(self):
        with open(os.path.join(dashboard.WEB, 'app.js'), encoding='utf8') as f:
            app = f.read()
        self.assertIn('const key = m.physical || m.name', app)
        self.assertIn("class: 'machine-environments'", app)
        self.assertIn('items.find((m) => m.name === physical)', app)

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


def needstab(expr):
    src = os.path.join(dashboard.WEB, 'needstab.js')
    script = f'const n = require({json.dumps(src)});\nconsole.log(JSON.stringify({expr}));'
    return json.loads(subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True).stdout)


class NeedsTab(unittest.TestCase):
    """The Needs you tab (t442): a tab of its own every view switcher knows, its script cached, its pure helpers."""

    def read(self, name):
        with open(os.path.join(dashboard.WEB, name), encoding='utf8') as f:
            return f.read()

    def test_the_tab_its_view_and_every_switcher_know_it(self):
        page = self.read('index.html')
        self.assertIn('href="#needs" data-view="needs"', page)
        self.assertIn('<div id="needs-view" class="needs-view view-ok" hidden>', page)   # viewers answer their own
        view = page[page.index('id="needs-view"'):page.index('id="settings-view"')]
        for part in ('id="needs-questions"', 'id="needs-other"', 'id="needs-note"'):
            self.assertIn(part, view)
        self.assertIn('needstab.js', self.read('sw.js'))
        self.assertIn("'#needs') ? 'needs'", self.read('settings.js'))
        self.assertIn("h.startsWith('#needs')", self.read('chat.js'))
        self.assertIn("$('needs-view').hidden = true", self.read('users.js'))
        self.assertIn('function showTask(id)', self.read('app.js'))

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_heading_count_and_links(self):
        v = {'questions': [{}], 'items': [{}, {}], 'resolving': 2}
        self.assertEqual(needstab(f'n.needsTabCount({json.dumps(v)})'), ' (3)')
        self.assertIn('3 things only you can do', needstab(f'n.needsTabMeta({json.dumps(v)})'))
        self.assertIn('still trying 2 more', needstab(f'n.needsTabMeta({json.dumps(v)})'))
        empty = {'questions': [], 'items': [], 'resolving': 0}
        self.assertEqual(needstab(f'n.needsTabCount({json.dumps(empty)})'), '')
        self.assertTrue(needstab(f'n.needsTabMeta({json.dumps(empty)})').startswith('Nothing needs you'))
        self.assertEqual(needstab('n.needsTabHref("/#settings/ai_assist")'), '#settings/ai_assist')
        for bad in ('"/#usage"', '"https://evil.example/"', '"javascript:alert(1)"', 'null'):
            self.assertEqual(needstab(f'n.needsTabHref({bad})'), '#dashboard', bad)


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
