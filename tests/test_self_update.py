"""The dashboard updates itself: the server writes its version into the page and the service worker, and the page's
decision (tutti/web/update.js, run in node) reloads once for a new version and never loops."""
import json
import os
import shutil
import subprocess
import unittest

from tutti import __version__, config, dashboard

UPDATE = os.path.join(dashboard.WEB, 'update.js')


def decide(*args):
    with open(UPDATE, encoding='utf8') as f:
        src = f.read()
    js = f'{src}\nconsole.log(JSON.stringify(tuttiUpdateDecision(...{json.dumps(list(args))})));'
    return json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)


class ServedVersion(unittest.TestCase):
    def setUp(self):
        self.c = dashboard.Collector(config.normalise({}), reader=object())

    def test_the_page_and_the_worker_carry_the_running_version(self):
        _, page, _, _ = dashboard.route(self.c, '/', 'tok')
        _, worker, _, cache = dashboard.route(self.c, '/sw.js')
        self.assertIn(f'<meta name="tutti-version" content="{__version__}">'.encode(), page)
        self.assertIn(f"'tutti-shell-{__version__}'".encode(), worker)
        self.assertNotIn(b'__TUTTI_VERSION__', page + worker)
        self.assertEqual(cache, 'no-cache')   # revalidated: a new release reaches the browser at once
        self.assertIn(f'app.js?v={__version__}'.encode(), page)          # a new page never runs an old worker's cached code
        self.assertIn(f"'?v={__version__}'".encode(), worker)
        self.assertEqual(dashboard.route(self.c, f'/app.js?v={__version__}')[0], 200)


@unittest.skipUnless(shutil.which('node'), 'node is not installed: the decision test runs where node is (CI has it)')
class Decision(unittest.TestCase):
    def test_a_new_version_reloads_once(self):
        self.assertEqual(decide('0.1.13', '0.1.14', None, True), 'reload')

    def test_the_same_version_or_no_answer_does_nothing(self):
        self.assertEqual(decide('0.1.14', '0.1.14', None, True), 'none')
        self.assertEqual(decide('0.1.14', None, None, True), 'none')

    def test_never_a_loop(self):
        self.assertEqual(decide('0.1.13', '0.1.14', '0.1.14', True), 'stuck')   # reloaded once and still old: say so
        self.assertEqual(decide('0.1.13', '0.1.14', None, False), 'stuck')      # no storage to remember a reload
