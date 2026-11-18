import unittest

from tutti import dedupe


class DedupeTest(unittest.TestCase):
    def test_dedupe_exact_duplicates(self):
        data = {'tasks': [
            {'id': 't1', 'repo': 'app', 'title': 'Fix bug', 'status': 'queued', 'attempts': [], 'notes': ['n1'], 'source': 's1'},
            {'id': 't2', 'repo': 'app', 'title': 'Fix bug', 'status': 'queued', 'attempts': [], 'notes': ['n2'], 'source': 's2'}
        ], 'next': 3}

        # This function should be implemented
        dedupe.process(data)

        tasks = {t['id']: t for t in data['tasks']}
        self.assertIn('t1', tasks)
        self.assertIn('t2', tasks) # Or maybe t2 is removed?
        # "merged, retaining notes and sources from both, with a link to the closed task"
        # Let's say t1 is the master, t2 is closed/linked.
        self.assertIn('Merged t2', tasks['t1']['notes'])
        self.assertEqual(tasks['t2']['status'], 'dropped')
        self.assertIn('Merged into t1', tasks['t2']['notes'])
        self.assertIn('s1', tasks['t1']['sources'])
        self.assertIn('s2', tasks['t1']['sources'])

    def test_group_overlapping_tasks(self):
        data = {'tasks': [
            {'id': 't1', 'repo': 'app', 'title': 'Task 1', 'status': 'queued', 'scope': ['a.py'], 'notes': []},
            {'id': 't2', 'repo': 'app', 'title': 'Task 2', 'status': 'queued', 'scope': ['a.py'], 'notes': []}
        ], 'next': 3}

        dedupe.process(data)

        tasks = {t['id']: t for t in data['tasks']}
        self.assertIn('t2', tasks['t1']['group'])
        self.assertIn('t1', tasks['t2']['group'])


if __name__ == '__main__':
    unittest.main()
