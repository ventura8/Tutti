"""Chat's Terminal panel (t304): a listed job's log tail, admin only, credentials scrubbed."""
import json
import os
import tempfile
import unittest
from unittest import mock

from tutti import dashboard, joblog


class Snap:
    def __init__(self, items):
        self.items = items

    def snapshot(self):
        return {'jobs': {'items': self.items}}


class JobLog(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.log = os.path.join(d.name, 'run.log')
        with open(self.log, 'w', encoding='utf8') as f:
            f.write(''.join(f'line {i}\n' for i in range(300)) + 'token=ghp_' + 'a' * 36 + '\n')

    def test_tail_reads_only_a_listed_jobs_log_bounded_and_scrubbed(self):
        code, out = joblog.tail({'jobs': {'items': [{'id': 'run:7.log', 'log': self.log, 'status': 'running'}]}}, 'run:7.log', 5)
        self.assertEqual(code, 200)
        lines = out['text'].splitlines()
        self.assertEqual(lines[:4], ['line 296', 'line 297', 'line 298', 'line 299'])
        self.assertNotIn('a' * 36, out['text'])
        whole = joblog.tail({'jobs': {'items': [{'id': 'j', 'log': self.log}]}}, 'j')[1]['text']
        self.assertEqual(len(whole.splitlines()), joblog.MAX_LINES)

    def test_a_key_crossing_the_cut_is_never_shown(self):
        key = '-----BEGIN PRIVATE KEY-----\n' + '\n'.join('A' * 60 for _ in range(10)) + '\n-----END PRIVATE KEY-----'
        # the BEGIN line far above the last lines: scrubbed before the cut
        out = joblog.tail_text((key + '\n' + 'after\n' * 5).encode(), 3)
        self.assertNotIn('A' * 60, out)
        # a byte window that starts inside the key (no BEGIN in it): masked up to its END
        inside = ('A' * 60 + '\n') * 3 + '-----END PRIVATE KEY-----\nafter 1\nafter 2\n'
        out = joblog.tail_text(inside.encode(), 10)
        self.assertNotIn('A' * 60, out)
        self.assertIn('after 2', out)
        self.assertEqual(joblog.tail_text(b'plain 1\nplain 2\n', 1), 'plain 2')

    def test_viewers_are_refused_before_a_log_is_read(self):
        from tutti import authweb
        self.assertTrue('/api/job-log/run%3A7.log'.startswith(authweb.ADMIN_GET))
        self.assertFalse('/api/state'.startswith(authweb.ADMIN_GET))

    def test_unknown_job_or_missing_log_says_why_and_what_next(self):
        code, out = joblog.tail({'jobs': {'items': []}}, '/etc/passwd')
        self.assertEqual(code, 404)
        self.assertIn('no job "/etc/passwd"', out['error'])
        code, out = joblog.tail({'jobs': {'items': [{'id': 'j', 'log': None}]}}, 'j')
        self.assertEqual(code, 404)
        self.assertIn('no log file on this host', out['error'])

    def test_the_route_decodes_the_id_and_sits_behind_the_admin_check(self):
        c = Snap([{'id': 'run:7.log', 'log': self.log}])
        code, body, *_ = dashboard.get(c, '/api/job-log/run%3A7.log', 'tok', {})
        self.assertEqual(code, 200)
        self.assertIn('line 299', json.loads(body)['text'])
        with mock.patch.object(dashboard, 'authorize', return_value=(403, 'admins only')):
            self.assertEqual(dashboard.get(c, '/api/job-log/run%3A7.log', 'tok', {})[0], 403)


if __name__ == '__main__':
    unittest.main()
