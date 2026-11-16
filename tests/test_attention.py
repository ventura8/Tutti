"""Unit tests for tutti/web/attention.js."""
import json
import os
import shutil
import subprocess
import unittest

from tutti import dashboard

with open(os.path.join(dashboard.WEB, 'format.js'), encoding='utf8') as _f:
    FORMAT_JS = _f.read()
with open(os.path.join(dashboard.WEB, 'attention.js'), encoding='utf8') as _f:
    ATTENTION_JS = _f.read()


def js(expr):
    out = subprocess.run(
        ['node', '-e', f'{FORMAT_JS}\n{ATTENTION_JS}\nconsole.log(JSON.stringify({expr}));'],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class Attention(unittest.TestCase):
    def test_tutti_attention_orders_items_by_urgency(self):
        state = {
            'autopilot': {
                'parked': [{'id': 't9', 'repo': 'app', 'title': 'stuck'}],
                'review': [{'id': 't3', 'repo': 'app', 'title': 'done', 'branch': 'ap/t3'}],
            },
            'usage': {'accounts': [{'name': 'claude', 'panic': True, 'used': 97, 'action': 'move work'}]},
            'jobs': {'items': [{'id': 'j1', 'status': 'stalled', 'title': 'ship'}]},
            'summary': {'machines_offline': ['nuc']},
            'workers': {'items': [{'name': 'pc', 'ready': False}]},
        }
        items = js(f'tuttiAttention({json.dumps(state)})')
        self.assertEqual([item['kind'] for item in items], ['needs-person', 'panic', 'stalled', 'offline', 'down', 'review'])
        self.assertEqual(items[0]['task']['id'], 't9')
        self.assertEqual(items[2]['job'], 'j1')

    def test_tutti_attention_ignores_non_object_input(self):
        self.assertEqual(js('tuttiAttention(null)'), [])
        self.assertEqual(js('tuttiAttention(7)'), [])


if __name__ == '__main__':
    unittest.main()
