"""The dashboard's main script, tutti/web/app.js, run in node: it loads without a page, and its link guard and time
helpers give the right text for good input and refuse or blank the bad.

app.js is a browser script with no exports, so it runs as it is in a node `vm` context with a small stand-in for
`document`; its DOMContentLoaded handler (the one that polls and starts the timer) is recorded and never called, so the
process ends on its own. Every run has a timeout all the same.
"""
import json
import os
import shutil
import subprocess
import unittest

from tutti import dashboard

APP = os.path.join(dashboard.WEB, 'app.js')
FORMAT = os.path.join(dashboard.WEB, 'format.js')

RUNNER = '''
const fs = require('fs');
const vm = require('vm');
const listeners = [];
const document = { querySelector: () => null, getElementById: () => null, addEventListener: (type) => listeners.push(type) };
const ctx = vm.createContext({ document, console });
vm.runInContext(fs.readFileSync(FORMAT_PATH, 'utf8'), ctx, { filename: 'format.js' });
vm.runInContext(fs.readFileSync(APP_PATH, 'utf8'), ctx, { filename: 'app.js' });
ctx.listeners = listeners;
console.log(JSON.stringify(vm.runInContext(EXPR, ctx)));
'''


def run(expr):
    script = RUNNER.replace('FORMAT_PATH', json.dumps(FORMAT)).replace('APP_PATH', json.dumps(APP)).replace('EXPR', json.dumps(expr))
    out = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True, timeout=60,
                         env={**os.environ, 'LC_ALL': 'en_US.UTF-8'})
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class AppScript(unittest.TestCase):
    def test_it_loads_without_a_page_and_only_waits_for_the_page_to_be_ready(self):
        self.assertEqual(run('listeners'), ['DOMContentLoaded'])
        self.assertEqual(run('[TOKEN, PAGE_VERSION, ui.filter]'), ['', '', 'all'])   # no meta tags: empty, not a crash

    def test_safe_links_are_kept_as_they_are(self):
        links = ['https://github.example/o/r/pull/1', 'http://host.example:4777/', 'mailto:owner@example.com', '#jobs',
                 '/api/state', 'relative/page']
        self.assertEqual(run(f'{json.dumps(links)}.map(safeHref)'), links)

    def test_script_and_data_links_become_a_dead_hash(self):
        bad = ['javascript:alert(1)', 'JavaScript:alert(1)', 'java\tscript:alert(1)', ' javascript:alert(1)',
               'data:text/html,<b>x</b>', 'vbscript:msgbox(1)', 'file:///etc/hosts']
        self.assertEqual(run(f'{json.dumps(bad)}.map(safeHref)'), ['#'] * len(bad))

    def test_duration_reads_in_the_unit_that_fits(self):
        self.assertEqual(run('[duration(30), duration(600), duration(5400), duration(7200), duration(3 * 86400 + 3600)]'),
                         ['30 s', '10 min', '1 h 30 min', '2 h 0 min', '3 d 1 h'])

    def test_duration_of_nothing_is_blank_not_nan(self):
        self.assertEqual(run('[duration(null), duration(undefined)]'), ['', ''])

    def test_relative_time_picks_the_unit_by_distance(self):
        expr = ('(() => { const now = Date.parse("2026-11-18T12:00:00Z"); '
                'const f = new Intl.RelativeTimeFormat(undefined, { numeric: "auto", style: "short" }); '
                'const at = (s) => new Date(now + s * 1000).toISOString(); '
                'return [[relative(at(-10), now), f.format(-10, "second")], [relative(at(-120), now), f.format(-2, "minute")], '
                '[relative(at(3 * 3600), now), f.format(3, "hour")], [relative(at(-2 * 86400), now), f.format(-2, "day")]]; })()')
        for got, want in run(expr):
            self.assertEqual(got, want)

    def test_relative_time_of_no_date_is_blank(self):
        self.assertEqual(run('[relative(""), relative(null), relative(undefined)]'), ['', '', ''])


if __name__ == '__main__':
    unittest.main()
