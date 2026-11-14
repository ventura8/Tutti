"""The dashboard's layout: its cards in order of importance, and what needs the owner (tutti/web/attention.js, run in node):
most urgent first, nothing when all is clear, and long lists that fold."""
import json
import os
import re
import shutil
import subprocess
import unittest

from tutti import config, dashboard

ATTENTION = os.path.join(dashboard.WEB, 'attention.js')


def js(expr):
    with open(ATTENTION, encoding='utf8') as f:
        src = f.read()
    out = subprocess.run(['node', '-e', f'{src}\nconsole.log(JSON.stringify({expr}));'], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


class Order(unittest.TestCase):
    def test_the_cards_go_by_importance_with_usage_first(self):
        with open(os.path.join(dashboard.WEB, 'index.html'), encoding='utf8') as f:
            page = f.read()
        heads = re.findall(r'<h2 id="([a-z]+)-h">', page)
        self.assertEqual(heads, ['usage', 'attention', 'household',
                                 'running', 'machines', 'autopilot', 'main', 'workers', 'projects', 'scouts',
                                 'controls', 'selfimprove', 'insights', 'users', 'jobs'])

    def test_the_page_and_the_worker_load_the_attention_code(self):
        with open(os.path.join(dashboard.WEB, 'sw.js'), encoding='utf8') as f:
            self.assertIn('`attention.js${V}`', f.read())
        _, page, _, _ = dashboard.route(dashboard.Collector(config.normalise({}), reader=object()), '/', 'tok')
        self.assertIn(b'attention.js?v=', page)


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

    def test_long_lists_fold(self):
        self.assertEqual(js('tuttiMore(10, 4, false)'), {'shown': 4, 'hidden': 6, 'button': 'Show 6 more'})
        self.assertEqual(js('tuttiMore(10, 4, true)'), {'shown': 10, 'hidden': 0, 'button': 'Show fewer'})
        self.assertEqual(js('tuttiMore(3, 4, false)'), {'shown': 3, 'hidden': 0, 'button': ''})


if __name__ == '__main__':
    unittest.main()
