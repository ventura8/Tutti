"""Unit tests for `tutti.jobmove`."""
import json
import os
import tempfile
import unittest

from tutti import config, jobmove


class JobMoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = os.path.join(self.tmp.name, 'tutti.config.yaml')
        raw = {
            'project': {'state': os.path.join(self.tmp.name, 'state')},
            'machines': {'alpha': {'host': 'alpha.example'}},
            'devices': {'pixel': {'serial': '1234'}},
        }
        with open(path, 'w', encoding='utf8') as handle:
            json.dump(raw, handle)
        self.cfg = config.load(path)

    def test_check_target_resolves_known_names_and_auto(self):
        self.assertEqual(jobmove.check_target(self.cfg, 'alpha'), 'machine:alpha')
        self.assertEqual(jobmove.check_target(self.cfg, 'pixel'), 'device:pixel')
        self.assertEqual(jobmove.check_target(self.cfg, 'auto'), '')

    def test_move_rejects_invalid_job_id(self):
        with self.assertRaises(jobmove.MoveError) as err:
            jobmove.move(self.cfg, 'invalid:id', to='auto', now=1.0)
        self.assertIn('is not a job id', str(err.exception))


if __name__ == '__main__':
    unittest.main()
