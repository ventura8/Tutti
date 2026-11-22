"""A queued task's expanded row (t237): everything the task has, bounded and scrubbed, read when the row opens."""
import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

from tutti import dashboard, taskdetail

TASK = {'id': 't7', 'repo': 'app', 'title': 'Fix the legend', 'size': 'M', 'source': 'mcp', 'status': 'queued', 'priority': 0,
        'added': 1794856046, 'scope': ['web/', 'docs/'], 'check': 'bash scripts/check.sh', 'worker': 'cursor', 'step': 'cursor',
        'pr': 'https://github.example/o/app/pull/4', 'branch': 'autopilot/t7',
        'criteria': ['The legend fits a phone.'],
        'notes': [f'note {i}' for i in range(12)] + ['token=ghp_' + 'a' * 36],
        'attempts': [{'at': 1794856100 + i, 'step': 'cursor', 'ok': i == 14, 'why': f'try {i}'} for i in range(15)]}


class Detail(unittest.TestCase):
    def test_everything_the_task_has_newest_attempts_first_bounded_and_scrubbed(self):
        d = taskdetail.detail(TASK)
        facts = dict(d['facts'])
        self.assertEqual(facts['Id'], 't7')
        self.assertEqual(facts['Priority'], '0')
        self.assertEqual(facts['Scope'], 'web/, docs/')
        from datetime import datetime, timezone

        from tutti import tz
        self.assertEqual(facts['Added'], tz.show(datetime.fromtimestamp(1794856046, timezone.utc)))   # local, with its offset
        self.assertIn('(UTC', facts['Added'])
        self.assertEqual(d['pr'], 'https://github.example/o/app/pull/4')
        self.assertEqual(d['criteria'], ['The legend fits a phone.'])
        self.assertEqual((len(d['notes']), d['notes_total']), (taskdetail.NOTES, 13))
        self.assertNotIn('a' * 36, ' '.join(d['notes']))
        self.assertEqual((len(d['attempts']), d['attempts_total']), (taskdetail.ATTEMPTS, 15))
        self.assertEqual((d['attempts'][0]['why'], d['attempts'][0]['ok']), ('try 14', True))

    def test_a_bare_task_shows_only_what_is_set_and_no_unsafe_link(self):
        self.assertIsNone(taskdetail.detail({'id': 't1', 'pr': 'https://user:ghp_' + 'a' * 36 + '@github.example/o/a/pull/1'})['pr'])
        d = taskdetail.detail({'id': 't1', 'title': 'x', 'pr': 'javascript:alert(1)', 'attempts': ['junk']})
        self.assertEqual(d['facts'], [['Id', 't1']])
        self.assertIsNone(d['pr'])
        self.assertEqual(d['attempts'], [])

    def test_the_route_answers_a_known_task_and_says_why_for_an_unknown_one(self):
        c = mock.Mock(cfg={})
        with mock.patch('tutti.backlog.load', return_value={'tasks': [TASK]}):
            code, body, *_ = dashboard.get(c, '/api/task/t7', 'tok', {})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)['id'], 't7')
            self.assertEqual(json.loads(body)['links'], ['Ready: nothing it depends on is open'])
            code, body, *_ = dashboard.get(c, '/api/board', 'tok', {})
            self.assertEqual([lane['name'] for lane in json.loads(body)['lanes']], ['Blocked', 'Ready', 'Grouped'])
            code, body, *_ = dashboard.get(c, '/api/task/t99', 'tok', {})
        self.assertEqual(code, 404)
        self.assertIn('no task "t99"', json.loads(body)['error'])

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_page_draws_facts_criteria_attempts_and_notes(self):
        with open(os.path.join(dashboard.WEB, 'queue.js'), encoding='utf8') as f:
            src = f.read()
        stub = ('function el(tag, attrs, ...kids) { return { tag, attrs: attrs || {}, kids: kids.flat() }; }\n'
                "function text(n) { return typeof n === 'string' ? n : n ? "
                "[(n.attrs || {}).text || '', ...(n.kids || []).map(text)].join(' ') : ''; }\n")
        d = {**taskdetail.detail(TASK), 'links': ['Depends on: t2 (queued)']}
        js = f'{stub}\n{src}\nconsole.log(JSON.stringify(taskDetailNodes({json.dumps(d)}).map(text).join(" ")));'
        out = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
        for part in ('Id t7', 'Pull request', 'Done when', 'The legend fits a phone.', 'Attempts (15, newest first)', 'try 14',
                     'Notes (8 of 13, newest last)', 'Dependencies', 'Depends on: t2'):
            self.assertIn(part, out)


if __name__ == '__main__':
    unittest.main()
