"""The Users tab rows and audit filters (tutti/web/userstab.js), run in node."""
import json
import os
import shutil
import subprocess
import unittest

from tutti import dashboard

WEB = dashboard.WEB


def node_eval(src, expr):
    out = subprocess.run(
        ['node', '-e', f'{src}\nconsole.log(JSON.stringify({expr}));'],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class UsersTabJsTest(unittest.TestCase):
    def source(self):
        with open(os.path.join(WEB, 'userstab.js'), encoding='utf8') as f:
            return f.read()

    def test_hh_user_rows_good_case_formats_member_row(self):
        src = self.source()
        data = {
            'admins': 2,
            'members': [{
                'hash': 'a',
                'name': 'Ana',
                'me': True,
                'joined': True,
                'online': True,
                'last_seen': 1700000000,
                'setup': {
                    'main_machine': 'laptop',
                    'agents': ['claude'],
                    'targets': ['pc'],
                    'mode': 'active',
                    'schedule': ['days mon-fri', '09-17'],
                    'stats': [{'kind': 'claude', 'used': 40, 'verdict': 'on pace'}],
                },
            }],
        }
        row = node_eval(src, f'hhUserRows({json.dumps(data)}, {1700000120 * 1000})[0]')
        self.assertEqual(row['name'], 'Ana (you)')
        self.assertEqual(row['mainMachine'], 'laptop')
        self.assertEqual(row['presence'], 'online')
        self.assertEqual(row['seen'], '2 min ago')
        self.assertEqual(row['joined'], 'Tutti joined')
        self.assertEqual(row['schedule'], 'days mon-fri · 09-17')
        self.assertIn('claude: 40 on pace', row['stats'])
        self.assertFalse(any(a['disabled'] for a in row['acts']))

    def test_hh_user_rows_bad_case_applies_last_admin_guard_and_defaults(self):
        src = self.source()
        data = {
            'admins': 1,
            'members': [{'hash': 'a', 'name': 'Ana', 'admin': True, 'setup': {}}],
            'audit': [
                {'by': 'Ana', 'for': 'Bo', 'text': 'make-admin by admin Ana for Bo: asked'},
                {'by': 'Bo', 'for': 'Bo', 'text': 'schedule by Bo for Bo: mode=away'},
            ],
        }
        row = node_eval(src, f'hhUserRows({json.dumps(data)}, {1700000120 * 1000})[0]')
        guarded = {a['act']: a for a in row['acts'] if a['disabled']}
        self.assertEqual(sorted(guarded), ['make-admin', 'remove'])
        self.assertIn('only admin', guarded['remove']['why'])
        self.assertEqual(row['mainMachine'], 'unknown')
        self.assertEqual(row['stats'], 'no readings yet')
        self.assertEqual(row['seen'], 'never')
        self.assertEqual(row['joined'], 'Tutti not joined')
        self.assertEqual(row['presence'], 'offline')
        self.assertEqual(node_eval(src, f'hhAuditRows({json.dumps(data)}, "Bo")'), ['make-admin by admin Ana for Bo: asked',
                                                                                      'schedule by Bo for Bo: mode=away'])


if __name__ == '__main__':
    unittest.main()
