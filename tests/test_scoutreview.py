"""A scout's review becomes work: findings file one suggested task on the PR (never twice while open); a clean review and
an old one file nothing; the merge step finds the open task."""
import tempfile
import unittest

from tutti import backlog, scoutreview

ANSWER = 'grok-scout\n- `a.py:3` - a bug\n- `b.py:9` - another\n'


def review(rid, answer=ANSWER, issue=7, answered=1000.0):
    return {'id': rid, 'kind': 'review', 'status': 'answered', 'scout': 'grok', 'repo': 'o/app', 'issue': issue,
            'url': f'https://github.example/o/app/pull/{issue}', 'answer': answer, 'answered': answered}


class Feed(unittest.TestCase):
    def test_findings_file_one_task_per_pr_and_a_clean_review_nothing(self):
        self.assertEqual(scoutreview.findings(ANSWER), 2)
        with tempfile.TemporaryDirectory() as d:
            cfg = {'project': {'state': d}, 'autopilot': {'repos': {'app': {'path': '/repo'}}}}
            remote = {'/repo': 'git@github.example:o/app.git'}.get
            events = scoutreview.feed(cfg, [review('s1'), review('s2'), review('s3', 'grok-scout\nLooks good.', 8)], remote)
            tasks = backlog.load(cfg)['tasks']
            self.assertEqual([t['title'] for t in tasks], ['Address the grok review of #7 (2 findings)'])
            self.assertEqual((tasks[0]['status'], tasks[0]['pr_review']), ('candidate', 'o/app#7'))
            self.assertIn('a bug', tasks[0]['notes'][0])
            self.assertIn('found nothing to fix', events[-1])
            self.assertEqual(scoutreview.open_for(tasks, 'o/app', 7)['id'], tasks[0]['id'])
            self.assertIsNone(scoutreview.open_for(tasks, 'o/app', 8))

    def test_only_fresh_answers_are_fed_and_each_once(self):
        data = {'requests': [review('s1', answered=1000.0), review('s2', answered=1000.0 - 3 * 86400),
                             {**review('s3'), 'kind': 'ask'}]}
        got = scoutreview.answered(data, now=1000.0)
        self.assertEqual([r['id'] for r in got], ['s1'])
        self.assertTrue(data['requests'][1][scoutreview.FED])   # old: marked, no task
        self.assertEqual(scoutreview.answered(data, now=1000.0), [])

    def test_a_repo_not_under_autopilot_files_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {'project': {'state': d}, 'autopilot': {'repos': {}}}
            scoutreview.feed(cfg, [review('s1')], lambda p: '')
            self.assertEqual(backlog.load(cfg)['tasks'], [])


if __name__ == '__main__':
    unittest.main()
