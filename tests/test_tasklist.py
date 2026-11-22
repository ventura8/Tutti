"""The Tasks tab (t307): one subtab per status bucket, P0 first, a blocked task saying why; the route and the page."""
import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

from tutti import dashboard, tasklist


def task(tid, status='queued', **kw):
    return {'id': tid, 'title': f'task {tid}', 'repo': 'app', 'status': status, **kw}


class Buckets(unittest.TestCase):
    def test_each_status_has_its_subtab_p0_first(self):
        tasks = [task('t3'), task('t2', priority=0), task('t10', priority=0), task('t4', 'running', claimed_by='laptop'),
                 task('t5', 'needs-person', parked='pick a name'), task('t6', 'done'), task('t7', 'dropped'), task('t8', 'review')]
        b = {x['key']: x for x in tasklist.buckets(tasks, {'t3': 'blocked until t9 lands'})}
        self.assertEqual([x['key'] for x in tasklist.buckets([])], ['active', 'queued', 'needs-person', 'review', 'candidate', 'done'])
        self.assertEqual([t['id'] for t in b['queued']['items']], ['t2', 't10', 't3'])
        self.assertEqual(b['queued']['items'][2]['why'], 'blocked until t9 lands')
        self.assertEqual((b['active']['items'][0]['who'], b['needs-person']['items'][0]['why']), ('laptop', 'pick a name'))
        self.assertEqual([t['id'] for t in b['done']['items']], ['t7', 't6'])   # newest first
        self.assertEqual(b['review']['count'], 1)

    def test_done_is_bounded_and_odd_tasks_do_not_break_it(self):
        tasks = [task(f't{i}', 'done') for i in range(80)] + [task('x', 'queued', priority='high', title=None)]
        b = {x['key']: x for x in tasklist.buckets(tasks)}
        self.assertEqual((b['done']['count'], len(b['done']['items'])), (80, tasklist.DONE_SHOWN))
        self.assertEqual((b['queued']['items'][0]['priority'], b['queued']['items'][0]['title']), ('P5', ''))

    def test_the_route_answers_every_signed_in_viewer(self):
        c = mock.Mock(cfg={})
        with mock.patch('tutti.backlog.load', return_value={'tasks': [task('t1')]}):
            code, body, *_ = dashboard.get(c, '/api/tasks', 'tok', {})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['buckets'][1]['items'][0]['id'], 't1')

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_page_draws_the_subtabs_and_a_row(self):
        with open(os.path.join(dashboard.WEB, 'tasks.js'), encoding='utf8') as f:
            src = f.read()
        stub = ('function el(tag, attrs, ...kids) { return { tag, attrs: attrs || {}, kids: kids.flat().filter((k) => k !== null) }; }\n'
                "function text(n) { return typeof n === 'string' ? n : n ? "
                "[(n.attrs || {}).text || '', ...(n.kids || []).map(text)].join(' ') : ''; }\n")
        data = tasklist.buckets([task('t1', priority=0)], {'t1': 'blocked until t2 lands'})
        js = (f'{stub}\n{src}\nconst tabs = tasksTabs({json.dumps(data)});\n'
              f'const row = text(taskRow({json.dumps(data[1]["items"][0])}));\n'
              'console.log(JSON.stringify([tabs.map(text).join("|"), tabs[0].attrs["aria-selected"], row]));')
        tabs, selected, row = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
        self.assertIn('Queued (1)', tabs)
        self.assertEqual(selected, 'true')   # Active, the first subtab, is chosen
        for part in ('task t1', 't1 · app · P0', 'Waiting: blocked until t2 lands'):
            self.assertIn(part, row)


if __name__ == '__main__':
    unittest.main()
