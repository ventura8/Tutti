"""The dashboard as a browser renders it: no tab shows "null", "undefined", "NaN" or "[object Object]" (a missing value
printed as text: "…(no other machine of yours to move to)null", 2026-11-15). Runs the real dashboard on the example config
and headless Chrome; skipped where no Chrome is installed (CI has one)."""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from html.parser import HTMLParser

import tmpdirs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = next((p for p in (shutil.which(n) for n in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser')) if p),
              None)
BAD = re.compile(r'\b(null|undefined|NaN)\b|\[object Object\]')
VIEWS = ('#chat', '#dashboard', '#settings', '#users')


class Text(HTMLParser):
    """The text a person can read: every text node outside script and style, hidden ones included."""

    def __init__(self):
        super().__init__()
        self.skip, self.parts = 0, []

    def handle_starttag(self, tag, attrs):
        self.skip += tag in ('script', 'style')

    def handle_endtag(self, tag):
        self.skip -= tag in ('script', 'style') and self.skip > 0

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@unittest.skipUnless(CHROME, 'no Chrome here: the rendered-text check runs where Chrome is installed (CI has one)')
class RenderedText(unittest.TestCase):
    """The example config (many machines), then a single computer: its empty lists and missing values are where a "null"
    slipped through."""

    def start(self, config_path=None):
        home = self.home
        self.port = free_port()
        env = {**os.environ, 'XDG_CONFIG_HOME': os.path.join(home, 'xdg'), 'HOME': home, 'TUTTI_TELEMETRY': 'off'}
        cfg = config_path or os.path.join(ROOT, 'tutti.config.example.yaml')
        self.server = subprocess.Popen([sys.executable, os.path.join(ROOT, 'bin', 'tutti'), '--config', cfg, 'dashboard', '--port',
                                        str(self.port), '--no-probe'], env=env, cwd=home,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (self.server.terminate(), self.server.wait(10)))
        for _ in range(100):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/api/health', timeout=1) as r:
                    if json.load(r).get('ok'):
                        return
            except OSError:
                time.sleep(0.1)
        self.fail('the dashboard did not start')

    def chrome_tmp(self):
        """A short temp folder of Chrome's own (its socket path must stay under 108 bytes, so not inside the test's nested
        temp folder), removed when the test ends: Chrome leaves .com.google.Chrome.* folders there."""
        if not getattr(self, '_chrome_tmp', None):
            self._chrome_tmp = tempfile.mkdtemp(prefix='tc', dir='/tmp' if os.path.isdir('/tmp') else None)
            self.addCleanup(shutil.rmtree, self._chrome_tmp, True)
        return self._chrome_tmp

    def rendered(self, view):
        out = subprocess.run([CHROME, '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run', '--disable-dev-shm-usage',
                              f'--user-data-dir={self.chrome_tmp()}/profile',
                              '--virtual-time-budget=8000', '--dump-dom', f'http://127.0.0.1:{self.port}/{view}'],
                             capture_output=True, text=True, timeout=90, env={**os.environ, 'TMPDIR': self.chrome_tmp()})
        t = Text()
        t.feed(out.stdout)
        self.chrome_said = out.stderr[-1500:]
        return t.parts

    def setUp(self):
        self.home = tmpdirs.mkdtemp()

    def check_views(self):
        for view in VIEWS:
            parts = self.rendered(view)
            self.assertTrue(parts, f'{view}: nothing rendered; Chrome said: {self.chrome_said}')
            bad = [p for p in parts if BAD.search(p)]
            self.assertEqual(bad, [], f'{view} shows a missing value as text')
        self.server.terminate()
        self.server.wait(10)

    def test_no_view_prints_a_missing_value_with_many_machines(self):
        self.start()
        self.check_views()

    def test_no_view_prints_a_missing_value_on_a_single_computer(self):
        path = os.path.join(self.home, 'tutti.config.yaml')
        with open(path, 'w', encoding='utf8') as f:
            f.write(f'project: {{state: {self.home}/state, worktrees: {self.home}/wt}}\nmachines: {{here: {{host: local}}}}\n'
                    'workers: {cursor: {kind: cli, command: [cursor-agent, -p, "{prompt}"]}}\n')
        self.start(path)
        self.check_views()


if __name__ == '__main__':
    unittest.main()
