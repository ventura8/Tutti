import unittest
import os
import tempfile
from unittest import mock
from tutti import config, delegate, cli
from types import SimpleNamespace

class DelegateCrashTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.repo = os.path.join(d, 'repo')
        os.makedirs(self.repo)
        self.cfg = config.normalise({
            'project': {'worktrees': os.path.join(d, 'wt'), 'state': os.path.join(d, 'state')},
            'workers': {'fake': {'kind': 'cli', 'command': ['false']}},
            'delegate': {'checks': []}
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_delegated_crash_on_nonexistent_directory(self):
        a = SimpleNamespace(worker='fake', task='fail', files=[], repo=self.repo, track='test', extra=[], 
                            no_checks=False, discard=False, check_where=None, remote_track=None, bg=False, account=None, check=None,
                            id='1234567890-1234')
        
        # We need to make delegate.delegate return an id and paths that don't exist
        
        with mock.patch('tutti.delegate.delegate', return_value={'id': '1234567890-1234', 'rc': 1, 'report': os.path.join(self.repo, 'nonexistent-dir/report.txt')}):
            # run_delegated opens files before read_report is called
            # Let's see if open() fails first
            with self.assertRaises(FileNotFoundError):
                cli.run_delegated(self.cfg, a, [])

if __name__ == '__main__':
    unittest.main()
