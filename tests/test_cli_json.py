import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tutti import cli, workers


class WorkersCliJsonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, 'tutti.config.json')
        env = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != 'TUTTI_CONFIG'}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, raw):
        cfg = {'project': {'worktrees': self.tmp.name, 'state': os.path.join(self.tmp.name, 'state')}, **raw}
        with open(self.config_path, 'w', encoding='utf8') as f:
            json.dump(cfg, f)

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(['--config', self.config_path, *args])
        return rc, out.getvalue()

    def test_workers_plain_output_is_default(self):
        self.write_config({
            'workers': {
                'cursor': {'kind': 'cli', 'command': ['cursor-agent', '{prompt}']},
                'small': {'kind': 'ollama', 'host': '127.0.0.1:11434', 'model': 'llama3'},
            }
        })
        probes = {
            'cursor': (True, '/usr/local/bin/cursor-agent'),
            'small': (False, '127.0.0.1:11434 does not answer'),
        }

        def fake_available(w):
            return probes['cursor'] if w['kind'] == 'cli' else probes['small']

        with mock.patch('tutti.workers.available', side_effect=fake_available):
            rc, text = self.run_cli('workers')
        self.assertEqual(rc, 0)
        self.assertIn('cursor', text)
        self.assertIn('cli', text)
        self.assertIn('ready', text)
        self.assertIn('/usr/local/bin/cursor-agent', text)
        self.assertIn('small', text)
        self.assertIn('ollama', text)
        self.assertIn('DOWN', text)
        self.assertIn('127.0.0.1:11434 does not answer', text)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(text)

    def test_workers_json_output(self):
        self.write_config({
            'workers': {
                'cursor': {'kind': 'cli', 'command': ['cursor-agent', '{prompt}']},
                'small': {'kind': 'ollama', 'host': '127.0.0.1:11434', 'model': 'llama3'},
            }
        })
        probes = {
            'cursor': (True, '/usr/local/bin/cursor-agent'),
            'small': (False, '127.0.0.1:11434 does not answer'),
        }

        def fake_available(w):
            return probes['cursor'] if w['kind'] == 'cli' else probes['small']

        with mock.patch('tutti.workers.available', side_effect=fake_available):
            rc, text = self.run_cli('workers', '--json')
        self.assertEqual(rc, 0)
        rows = json.loads(text)
        self.assertIsInstance(rows, list)
        self.assertEqual(len(rows), 2)
        by_name = {r['name']: r for r in rows}
        self.assertEqual(
            by_name['cursor'],
            {
                'name': 'cursor',
                'kind': 'cli',
                'ready': True,
                'note': '/usr/local/bin/cursor-agent',
            },
        )
        self.assertEqual(
            by_name['small'],
            {
                'name': 'small',
                'kind': 'ollama',
                'ready': False,
                'note': '127.0.0.1:11434 does not answer',
            },
        )

    def test_empty_workers_plain_and_json(self):
        self.write_config({'workers': {}})
        rc_plain, text_plain = self.run_cli('workers')
        self.assertEqual(rc_plain, 0)
        self.assertIn('no workers in the config', text_plain)

        rc_json, text_json = self.run_cli('workers', '--json')
        self.assertEqual(rc_json, 0)
        self.assertEqual(json.loads(text_json), [])

    def test_workers_listing_and_rows_helpers(self):
        cfg = {'workers': {'test-w': {'kind': 'cli', 'command': ['test']}}}
        with mock.patch('tutti.workers.available', return_value=(True, 'found')):
            self.assertIn('test-w', workers.listing(cfg, as_json=False))
            rows = json.loads(workers.listing(cfg, as_json=True))
            self.assertEqual(rows, [{'name': 'test-w', 'kind': 'cli', 'ready': True, 'note': 'found'}])
            self.assertEqual(workers.rows(cfg), [{'name': 'test-w', 'kind': 'cli', 'ready': True, 'note': 'found'}])


if __name__ == '__main__':
    unittest.main()
