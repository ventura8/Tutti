import unittest
import os
import tempfile
import subprocess
from unittest import mock
from tutti import config, delegate, cli
from types import SimpleNamespace

class DelegateRegressionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.repo = os.path.join(d, 'repo')
        os.makedirs(self.repo)
        subprocess.run(['git', '-C', self.repo, 'init', '-q', '-b', 'main'], check=True)
        subprocess.run(['git', '-C', self.repo, 'config', 'user.email', 'tester@example.invalid'], check=True)
        subprocess.run(['git', '-C', self.repo, 'config', 'user.name', 'Tester'], check=True)
        subprocess.run(['git', '-C', self.repo, 'commit', '--allow-empty', '-m', 'initial'], check=True)
        self.cfg = config.normalise({
            'project': {'worktrees': os.path.join(d, 'wt'), 'state': os.path.join(d, 'state')},
            'workers': {'fake': {'kind': 'cli', 'command': ['false']}},
            'delegate': {'checks': []}
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_delegated_no_crash_on_missing_file(self):
        a = SimpleNamespace(worker='fake', task='fail', files=[], repo=self.repo, id=None, track='test', extra=[], 
                            no_checks=False, discard=False, check_where=None, remote_track=None, bg=False, account=None, check=None)
        
        # Test that run_delegated uses the new safe helper functions
        with mock.patch('tutti.cli.read_log', return_value=['fake line']) as mock_read_log:
            with mock.patch('tutti.cli.read_report', return_value='fake report') as mock_read_report:
                rc = cli.run_delegated(self.cfg, a, [])
                self.assertEqual(rc, 1) # 'false' command returns 1
                self.assertTrue(mock_read_log.called)
                self.assertTrue(mock_read_report.called)

if __name__ == '__main__':
    unittest.main()
