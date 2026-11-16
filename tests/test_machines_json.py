import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import tmpdirs
from tutti import cli, routing


class TestMachinesJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdirs.mkdtemp()
        self.state_dir = os.path.join(self.tmp, 'state')
        self.path = os.path.join(self.tmp, 'tutti.config.json')

    def make_config(self, machines=None):
        if machines is None:
            machines = {
                'box': {
                    'transport': 'local',
                    'slots': 2,
                }
            }
        data = {
            'project': {'state': self.state_dir},
            'machines': machines,
        }
        with open(self.path, 'w', encoding='utf8') as f:
            json.dump(data, f)

    def tutti(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(['--config', self.path, *args])
        return rc, out.getvalue(), err.getvalue()

    def test_machines_plain_default(self):
        self.make_config()
        rc, out, _ = self.tutti('machines')
        self.assertEqual(rc, 0)
        self.assertIn('box', out)
        self.assertIn('local', out)
        self.assertIn('slots', out)

    def test_machines_json_output(self):
        self.make_config()
        rc, out, _ = self.tutti('machines', '--json')
        self.assertEqual(rc, 0)
        items = json.loads(out)
        self.assertIsInstance(items, list)
        self.assertEqual(len(items), 1)
        m = items[0]
        self.assertEqual(m['name'], 'box')
        self.assertEqual(m['transport'], 'local')
        self.assertEqual(m['slots'], 2)
        self.assertTrue(m['reachable'])
        self.assertIn('free', m)
        self.assertIn('load', m)
        self.assertIn('cores', m)
        self.assertIn('status', m)
        self.assertIn('reason', m)

    def test_machines_json_empty_list(self):
        self.make_config(machines={})
        rc, out, _ = self.tutti('machines', '--json')
        self.assertEqual(rc, 0)
        items = json.loads(out)
        self.assertEqual(items, [])

    def test_machines_unreachable_exits_1(self):
        self.make_config()
        unreachable = routing.Probe(reachable=False)
        with mock.patch('tutti.remote.probe', return_value=unreachable):
            rc_plain, out_plain, _ = self.tutti('machines')
            self.assertEqual(rc_plain, 1)
            self.assertIn('unreachable', out_plain)

            rc_json, out_json, _ = self.tutti('machines', '--json')
            self.assertEqual(rc_json, 1)
            items = json.loads(out_json)
            self.assertEqual(len(items), 1)
            self.assertFalse(items[0]['reachable'])
            self.assertEqual(items[0]['status'], 'offline')


if __name__ == '__main__':
    unittest.main()
