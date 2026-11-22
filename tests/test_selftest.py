"""The live self-test (t241): the running dashboard asks itself for every route its pages GET, with the real data; a route
the pages call that the code does not serve (the 2026-11-22 empty tabs), a crash or a slow answer fails, is shown on the
Self-test card and filed once as a backlog task."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest

from tutti import backlog, dashboard, install, selftest

JS = """fetch('/api/tasks', { cache: 'no-store' });
fetch('/api/queue', { method: 'POST', body: x });
fetch(`/api/task/${id}`);
fetch('/api/auth/me');
fetch('/api/me/schedule', { cache: 'no-store' });
fetch('/api/health');
"""


class Routes(unittest.TestCase):
    def test_only_the_pages_literal_get_routes_the_dashboard_itself_serves(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'a.js'), 'w') as f:
                f.write(JS)
            open(os.path.join(d, 'a.css'), 'w').close()
            self.assertEqual(sorted(selftest.page_routes(d)), ['/api/health', '/api/tasks'])
        self.assertIn('/api/state', selftest.page_routes(dashboard.WEB))   # the real pages

    def test_each_answer_is_judged(self):
        self.assertTrue(selftest.judge('/a', 200, 0.1)['ok'])
        self.assertTrue(selftest.judge('/a', 401, 0.1)['ok'])   # served, behind sign-in
        for args, why in ((('/a', 404, 0.1), 'older code than its pages'), (('/a', 502, 0.1), 'answered 502'),
                          (('/a', 200, 9.0), 'took 9.0 s'), (('/a', 500, 0.1, 'KeyError: x'), 'crashed on the real data')):
            r = selftest.judge(*args)
            self.assertFalse(r['ok'])
            self.assertIn(why, r['why'])

    def test_a_crash_is_a_finding_and_the_real_dashboard_passes(self):
        def get(_c, path):
            if path == '/api/tasks':
                raise KeyError('priority')
            return 200
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'a.js'), 'w') as f:
                f.write(JS)
            out = {c['path']: c for c in selftest.run(None, d, get)}
        self.assertEqual((out['/api/tasks']['ok'], out['/api/health']['ok']), (False, True))
        self.assertIn('KeyError', out['/api/tasks']['why'])


class Record(unittest.TestCase):
    def test_a_failure_is_kept_shown_and_filed_once(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {'project': {'state': d}, 'autopilot': {'repos': {'tutti': {'path': d}}}}
            checks = [selftest.judge('/api/tasks', 404, 0.1), selftest.judge('/api/health', 200, 0.1)]
            rec = selftest.record(cfg, checks, '0.0.1', now=5)
            self.assertEqual((rec['failed'], rec['total'], selftest.load(cfg)['at']), (1, 2, 5))
            selftest.record(cfg, checks, '0.0.1', now=6)   # the same failure again: no second task
            titles = [t['title'] for t in backlog.load(cfg)['tasks']]
        self.assertEqual(titles, ['Live self-test: /api/tasks fails'])

    def test_nothing_is_filed_without_the_tutti_repo_and_a_missing_record_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {'project': {'state': d}}
            self.assertIsNone(selftest.load(cfg))
            selftest.record(cfg, [selftest.judge('/a', 404, 0.1)], '0.0.1')
            self.assertEqual(backlog.load(cfg)['tasks'], [])

    def test_the_schedule_survives_a_broken_check(self):
        stop, calls = threading.Event(), []

        class Inline:
            def __init__(self, target, **_kw):
                self.target = target

            def start(self):
                self.target()

        def check():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('boom')
            stop.set()
        install.start_selftest(stop, check, first=0, every=0, thread=Inline)
        self.assertEqual(len(calls), 2)

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_card_shows_failures_first(self):
        with open(os.path.join(dashboard.WEB, 'discovered.js'), encoding='utf8') as f:
            src = f.read()
        stub = ('function el(tag, attrs, ...kids) { return { tag, attrs: attrs || {}, kids: kids.flat() }; }\n'
                "function text(n) { return typeof n === 'string' ? n : n ? "
                "[(n.attrs || {}).text || '', ...(n.kids || []).map(text)].join(' ') : ''; }\n")
        rec = {'total': 2, 'failed': 1, 'version': '0.0.1',
               'checks': [selftest.judge('/api/tasks', 404, 0.1), selftest.judge('/api/health', 200, 0.1)]}
        js = (f'{stub}\n{src}\nconsole.log(JSON.stringify([selftestNodes({json.dumps(rec)}).map(text).join(" "), '
              'selftestNodes(null).map(text).join(" ")]));')
        shown, none = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
        for part in ('1 of 2 answer', '/api/tasks', 'older code than its pages'):
            self.assertIn(part, shown)
        self.assertIn('Not run yet', none)


if __name__ == '__main__':
    unittest.main()
