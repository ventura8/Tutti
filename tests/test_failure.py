
import unittest
import json
import os
import tempfile
from tutti import failure, config

class TestFailure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, 'state')
        os.makedirs(self.state)
        self.cfg = {'project': {'state': self.state}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_log_failure(self):
        failure.log_failure(self.cfg, 'worker1', 'task1', 1, 'flaky', model='m1')
        log_path = os.path.join(self.state, 'failure-log.jsonl')
        self.assertTrue(os.path.exists(log_path))
        with open(log_path, encoding='utf8') as f:
            lines = f.readlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry['worker'], 'worker1')
            self.assertEqual(entry['category'], 'flaky')
            self.assertEqual(entry['model'], 'm1')

    def test_mark_bad(self):
        failure.mark_bad(self.cfg, 'm1')
        path = os.path.join(self.state, 'bad-models.json')
        self.assertTrue(os.path.exists(path))
        with open(path, encoding='utf8') as f:
            self.assertEqual(json.load(f), ['m1'])

        failure.mark_bad(self.cfg, 'm2')
        with open(path, encoding='utf8') as f:
            self.assertEqual(json.load(f), ['m1', 'm2'])
            
        failure.mark_bad(self.cfg, 'm1') # Should not add duplicates
        with open(path, encoding='utf8') as f:
            self.assertEqual(json.load(f), ['m1', 'm2'])

if __name__ == '__main__':
    unittest.main()
