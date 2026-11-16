"""The dashboard's layout: its cards in order of importance, and what needs the owner (tutti/web/attention.js, run in node):
most urgent first, nothing when all is clear, and long lists that fold."""
import json
import os
import re
import shutil
import subprocess
import unittest

from tutti import config, dashboard, settings_schema

with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tutti', 'web', 'format.js'), encoding='utf8') as _f:
    FORMAT_JS = _f.read()   # the shared number formatter every dashboard script uses (tutti/web/format.js)

ATTENTION = os.path.join(dashboard.WEB, 'attention.js')


def js(expr):
    with open(ATTENTION, encoding='utf8') as f:
        src = f.read()
    out = subprocess.run(['node', '-e', f'{FORMAT_JS}\n{src}\nconsole.log(JSON.stringify({expr}));'], capture_output=True, text=True,
                         check=True)
    return json.loads(out.stdout)


class Order(unittest.TestCase):
    def test_the_cards_go_by_importance_with_usage_first(self):
        with open(os.path.join(dashboard.WEB, 'index.html'), encoding='utf8') as f:
            page = f.read()
        heads = re.findall(r'<h2 id="([a-z]+)-h">', page)
        self.assertEqual(heads, ['usage', 'machines', 'attention', 'household',
                                 'running', 'queued', 'main', 'workers', 'autopilot', 'projects', 'scouts',
                                 'connect', 'where', 'setup', 'controls', 'selfimprove', 'aiassist', 'insights', 'jobs', 'vpn', 'users', 'costs'])

    def test_usage_shows_the_accounts_then_local_models_then_the_owner(self):
        # the served page: online agents' usage cards, then Local models, then "You and the main agent" (#owner)
        _, page, _, _ = dashboard.route(dashboard.Collector(config.normalise({}), reader=object()), '/', 'tok')
        page = page.decode()
        usage = page[page.index('id="usage-h"'):page.index('id="attention-h"')]
        # whole ids: id="usage" is not id="usage-h"
        at = [usage.index(x) for x in ('id="usage" ', 'id="local-h" ', 'id="local-models" ', 'id="owner" ')]
        self.assertEqual(at, sorted(at))
        # Settings follows the same order: accounts, then energy, then the main agent
        names = [s[0] for s in settings_schema.SECTIONS]
        self.assertLess(names.index('accounts'), names.index('energy'))
        self.assertEqual(names.index('energy'), names.index('accounts') + 1)
        self.assertLess(names.index('energy'), names.index('main'))

    def test_the_page_and_the_worker_load_the_attention_code(self):
        with open(os.path.join(dashboard.WEB, 'sw.js'), encoding='utf8') as f:
            self.assertIn('`attention.js${V}`', f.read())
        _, page, _, _ = dashboard.route(dashboard.Collector(config.normalise({}), reader=object()), '/', 'tok')
        self.assertIn(b'attention.js?v=', page)


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class Fit(unittest.TestCase):
    ROWS = 'running: 30, queued: 40, rowRunning: 80, rowQueued: 60, others: 200'

    def test_a_tall_neighbour_column_shows_more_rows_before_show_more(self):
        short = js(f'tuttiFit({{column: 700, {self.ROWS}}})')
        tall = js(f'tuttiFit({{column: 2400, {self.ROWS}}})')
        self.assertGreater(tall['running'], short['running'])
        self.assertEqual(tall['running'], 27)                   # (2400 - 200) / 80
        self.assertEqual(short, {'running': 6, 'queued': 3})    # the rest of a short column, at least 3 queued rows

    def test_the_space_left_by_few_running_jobs_goes_to_the_queue_and_a_phone_keeps_its_cap(self):
        few = js('tuttiFit({column: 1400, others: 200, running: 2, queued: 40, rowRunning: 100, rowQueued: 50})')
        self.assertEqual(few, {'running': 3, 'queued': 20})     # 1200 - 2 x 100 left for the queue
        self.assertEqual(js(f'tuttiFit({{column: 2400, {self.ROWS}, phone: true}})'), {'running': 8, 'queued': 8})

    def test_the_page_measures_both_columns_and_watches_their_size(self):
        with open(os.path.join(dashboard.WEB, 'index.html'), encoding='utf8') as f:
            page = f.read()
        self.assertLess(page.index('id="jobs-stack"'), page.index('id="side-stack"'))   # side by side, jobs first
        with open(os.path.join(dashboard.WEB, 'app.js'), encoding='utf8') as f:
            app = f.read()
        self.assertIn('new ResizeObserver', app)
        self.assertIn("ui.fit?.running", app)


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class Attention(unittest.TestCase):
    STATE = {
        'autopilot': {'parked': [{'id': 't9', 'repo': 'app', 'title': 'stuck', 'parked': 'same failure 3 times'}],
                      'review': [{'id': 't3', 'repo': 'app', 'title': 'done', 'publish_note': 'branch ap/t3', 'branch': 'ap/t3'}]},
        'usage': {'accounts': [{'name': 'cursor', 'panic': False, 'stale': True, 'remind': 'read it'},
                               {'name': 'claude', 'panic': True, 'used': 97, 'action': 'move work'}]},
        'jobs': {'items': [{'id': 'j1', 'status': 'stalled', 'title': 'ship'}, {'id': 'j2', 'status': 'running', 'title': 'ok'}]},
        'summary': {'machines_offline': ['nuc'], 'outside': 0},
        'workers': {'items': [{'name': 'pc', 'ready': False, 'note': 'tunnel down'}, {'name': 'agy', 'ready': True}]},
    }

    def test_most_urgent_first(self):
        items = js(f'tuttiAttention({json.dumps(self.STATE)})')
        self.assertEqual([i['kind'] for i in items], ['needs-person', 'panic', 'stalled', 'offline', 'down', 'reading', 'review'])
        self.assertEqual(items[2]['job'], 'j1')                       # a stalled job's item opens its log
        self.assertEqual(items[0]['task']['id'], 't9')                # a parked task keeps its failure history

    def test_nothing_when_all_is_clear(self):
        self.assertEqual(js('tuttiAttention({})'), [])

    def test_the_planner_rows_show_local_time_with_utc_in_the_tooltip(self):
        plan = {'rows': [{'label': 'Points left', 'value': '86'}, {'label': 'Spend by', 'at': '2026-12-09T21:00:00+00:00'},
                         {'label': 'Why', 'value': ''}]}
        fmt = "(iso) => new Intl.DateTimeFormat('en-GB', {timeZone: 'Europe/Bucharest', weekday: 'short', day: '2-digit', " \
              "month: 'short', hour: '2-digit', minute: '2-digit'}).format(new Date(iso))"
        rows = js(f'tuttiPlanRows({json.dumps(plan)}, {fmt})')
        self.assertEqual(rows[0], {'label': 'Points left', 'text': '86', 'title': ''})
        self.assertEqual(rows[1]['label'], 'Spend by')
        self.assertIn('23:00', rows[1]['text'])                           # the viewer's time (Bucharest: UTC+2)
        self.assertEqual(rows[1]['title'], 'Wed 09 Dec 2026 21:00 UTC')   # UTC in the tooltip
        self.assertEqual(len(rows), 2)                                     # an empty row is left out
        self.assertEqual(js('tuttiPlanRows(null, String)'), [])

    def test_a_hidden_element_stays_hidden_when_its_class_sets_display(self):
        # .users-view { display: grid } beat the hidden attribute: the empty Users card showed on the main dashboard
        with open(os.path.join(dashboard.WEB, 'index.html'), encoding='utf8') as f:
            page = f.read()
        with open(os.path.join(dashboard.WEB, 'app.css'), encoding='utf8') as f:
            css = f.read()
        shown = set(re.findall(r'(?m)^\.([a-z][\w-]*) \{[^}]*\bdisplay:\s*(?!none)', css))
        hidden_rules = set(re.findall(r'[.#]([\w-]+)\[hidden\]', css))
        if re.search(r'(?m)^\[hidden\] \{ display: none !important; \}', css):
            return   # one rule hides every element marked hidden
        for tag in re.findall(r'<[a-z]+\b[^>]*\bhidden\b[^>]*>', page):
            ident = re.search(r'\bid="([^"]+)"', tag)
            for cls in (re.search(r'\bclass="([^"]+)"', tag) or [None, ''])[1].split():
                if cls in shown:
                    self.assertTrue(cls in hidden_rules or (ident and ident.group(1) in hidden_rules), f'{tag}: add .{cls}[hidden]')

    def test_long_lists_fold(self):
        self.assertEqual(js('tuttiMore(10, 4, false)'), {'shown': 4, 'hidden': 6, 'button': 'Show 6 more'})
        self.assertEqual(js('tuttiMore(10, 4, true)'), {'shown': 10, 'hidden': 0, 'button': 'Show fewer'})
        self.assertEqual(js('tuttiMore(3, 4, false)'), {'shown': 3, 'hidden': 0, 'button': ''})


    def test_open_folds_and_filters_survive_a_refresh(self):
        ui = 'const ui = {open: new Set(), expanded: new Set(), collapsed: new Set(), filter: "all", project: ""};'
        # opened by the owner: open after the re-render and after a reload from the kept text
        out = js(f'(() => {{ {ui} tuttiKept.toggled(ui, "history:t16", true); ui.filter = "failed"; ui.open.add("j7");'
                 'const back = tuttiKept.load({open: new Set(), expanded: new Set(), collapsed: new Set(), filter: "all", project: ""},'
                 ' tuttiKept.dump(ui));'
                 'return [tuttiKept.isOpen(ui, "history:t16", false), tuttiKept.isOpen(back, "history:t16", false),'
                 ' back.filter, [...back.open]]; })()')
        self.assertEqual(out, [True, True, 'failed', ['j7']])
        # a fold open by default stays closed once the owner closed it
        closed = f'(() => {{ {ui} tuttiKept.toggled(ui, "scouts:setup", false); return tuttiKept.isOpen(ui, "scouts:setup", true); }})()'
        self.assertFalse(js(closed))

    def test_a_damaged_kept_state_falls_back_to_the_defaults(self):
        ui = '{open: new Set(), expanded: new Set(), collapsed: new Set(), filter: "all", project: ""}'
        self.assertEqual(js(f'tuttiKept.load({ui}, "{{not json").filter'), 'all')
        self.assertEqual(js(f'tuttiKept.load({ui}, null).project'), '')


if __name__ == '__main__':
    unittest.main()
