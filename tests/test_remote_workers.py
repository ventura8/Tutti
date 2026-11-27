import unittest
from unittest.mock import patch

from tutti import workers


class TestRemoteWorkers(unittest.TestCase):
    def test_run_offload_when_quiet(self):
        cfg = {
            'project': {'state': '~/.local/state/tutti'},
            'workers': {'agy': {'command': ['agy'], 'remote': 'nuc', 'kind': 'cli'}},
            'machines': {
                'nuc': {
                    'transport': 'ssh',
                    'host': 'nuc-host',
                    'user': 'user',
                    'root': 'tutti-wt',
                    'slots': 1,
                    'runner': {'kind': 'shell', 'memory_gb': 12, 'cpus': 8, 'env': {}, 'args': [], 'prelude': ''}
                }
            }
        }
        with patch('tutti.quietmode.status', return_value={'quiet': True}), \
             patch('tutti.remote.ensure_master'), \
             patch('tutti.remote.argv', return_value=['ssh', 'nuc-host', 'agy']), \
             patch('subprocess.Popen') as mock_popen, \
             patch('tutti.inuse.using'), \
             patch('tutti.workers.grant_tree', return_value=['agy']), \
             patch('tutti.workers.with_args', return_value=['agy']), \
             patch('tutti.workers.command', return_value=['agy']), \
             patch('tutti.workers.brief', return_value='task'), \
             patch('tutti.workers.worker_env', return_value={}), \
             patch('tutti.autocompact.env_for', return_value={}), \
             patch('tutti.autocompact.Watch'):
            mock_popen.return_value.__enter__.return_value.returncode = 0
            workers.run(cfg, 'agy', 'task')
            mock_popen.assert_called()
            args, _ = mock_popen.call_args
            self.assertEqual(args[0], ['ssh', 'nuc-host', 'agy'])

    def test_run_no_offload_when_not_quiet(self):
        cfg = {
            'project': {'state': '~/.local/state/tutti'},
            'workers': {'agy': {'command': ['agy'], 'remote': 'nuc', 'kind': 'cli'}},
            'machines': {
                'nuc': {
                    'transport': 'ssh',
                    'host': 'nuc-host',
                    'user': 'user',
                    'root': 'tutti-wt',
                    'slots': 1,
                    'runner': {'kind': 'shell', 'memory_gb': 12, 'cpus': 8, 'env': {}, 'args': [], 'prelude': ''}
                }
            }
        }
        with patch('tutti.quietmode.status', return_value={'quiet': False}), \
             patch('tutti.remote.ensure_master'), \
             patch('tutti.remote.argv', return_value=['ssh', 'nuc-host', 'agy']), \
             patch('subprocess.Popen') as mock_popen, \
             patch('tutti.inuse.using'), \
             patch('tutti.workers.grant_tree', return_value=['agy']), \
             patch('tutti.workers.with_args', return_value=['agy']), \
             patch('tutti.workers.command', return_value=['agy']), \
             patch('tutti.workers.brief', return_value='task'), \
             patch('tutti.workers.worker_env', return_value={}), \
             patch('tutti.autocompact.env_for', return_value={}), \
             patch('tutti.autocompact.Watch'):
            mock_popen.return_value.__enter__.return_value.returncode = 0
            workers.run(cfg, 'agy', 'task')
            mock_popen.assert_called()
            args, _ = mock_popen.call_args
            self.assertEqual(args[0], ['agy'])
