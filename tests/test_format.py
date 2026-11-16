"""Unit tests for tutti/web/format.js (run in node): number, percentage, share, compact, CSS percentage and text tidying."""
import json
import os
import shutil
import subprocess
import unittest

from tutti import dashboard

FORMAT = os.path.join(dashboard.WEB, 'format.js')


def fmt(expr):
    script = f'const f = require({json.dumps(FORMAT)});\nconsole.log(JSON.stringify({expr}));'
    out = subprocess.run(
        ['node', '-e', script],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, 'LC_ALL': 'en_US.UTF-8'},
    )
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which('node'), 'node is not installed: format tests require node')
class FormatTest(unittest.TestCase):
    def test_tutti_num_formats_numbers(self):
        cases = fmt('[f.tuttiNum(0), f.tuttiNum(1234567), f.tuttiNum(9.876), f.tuttiNum(0.555), f.tuttiNum("42.5")]')
        self.assertEqual(cases, ['0', '1,234,567', '9.9', '0.56', '43'])

    def test_tutti_num_handles_invalid_or_missing_input(self):
        expr = '[f.tuttiNum(null), f.tuttiNum(undefined), f.tuttiNum(""), f.tuttiNum("abc"), f.tuttiNum(NaN), f.tuttiNum(Infinity)]'
        cases = fmt(expr)
        self.assertEqual(cases, ['–', '–', '–', '–', '–', '–'])
        self.assertEqual(fmt('f.tuttiNum(null, {none: "N/A"})'), 'N/A')

    def test_tutti_pct_formats_percentages(self):
        cases = fmt('[f.tuttiPct(50), f.tuttiPct(9.5), f.tuttiPct(0.3), f.tuttiPct(0), f.tuttiPct(12.345, {digits: 2})]')
        self.assertEqual(cases, ['50%', '9.5%', '0.3%', '0%', '12.35%'])

    def test_tutti_pct_handles_invalid_or_missing_input(self):
        cases = fmt('[f.tuttiPct(null), f.tuttiPct(undefined), f.tuttiPct("bad"), f.tuttiPct(NaN)]')
        self.assertEqual(cases, ['–', '–', '–', '–'])
        self.assertEqual(fmt('f.tuttiPct(null, {none: "none"})'), 'none')

    def test_tutti_share_formats_fractions(self):
        cases = fmt('[f.tuttiShare(0.42), f.tuttiShare(0.075), f.tuttiShare(1), f.tuttiShare(0)]')
        self.assertEqual(cases, ['42%', '7.5%', '100%', '0%'])

    def test_tutti_share_handles_invalid_input(self):
        cases = fmt('[f.tuttiShare(null), f.tuttiShare(undefined), f.tuttiShare("invalid")]')
        self.assertEqual(cases, ['–', '–', '–'])
        self.assertEqual(fmt('f.tuttiShare(null, {none: "empty"})'), 'empty')

    def test_tutti_compact_formats_large_counts(self):
        cases = fmt('[f.tuttiCompact(1200000), f.tuttiCompact(500)]')
        self.assertEqual(cases, ['1.2M', '500'])

    def test_tutti_compact_handles_invalid_input(self):
        cases = fmt('[f.tuttiCompact(null), f.tuttiCompact("not-a-number"), f.tuttiCompact(NaN)]')
        self.assertEqual(cases, ['–', '–', '–'])
        self.assertEqual(fmt('f.tuttiCompact(null, {none: "missing"})'), 'missing')

    def test_tutti_css_pct_clamps_and_rounds(self):
        cases = fmt('[f.tuttiCssPct(50.24), f.tuttiCssPct(150), f.tuttiCssPct(-20), f.tuttiCssPct(0)]')
        self.assertEqual(cases, ['50.2%', '100%', '0%', '0%'])

    def test_tutti_css_pct_handles_invalid_input(self):
        cases = fmt('[f.tuttiCssPct(null), f.tuttiCssPct(undefined), f.tuttiCssPct("invalid"), f.tuttiCssPct(NaN)]')
        self.assertEqual(cases, ['0%', '0%', '0%', '0%'])

    def test_tutti_tidy_removes_float_noise(self):
        good = 'heading for 32.122999999999976 % on 0.1.64'
        self.assertEqual(fmt(f'f.tuttiTidy({json.dumps(good)})'), 'heading for 32 % on 0.1.64')

    def test_tutti_tidy_handles_non_strings(self):
        cases = fmt('[f.tuttiTidy(null), f.tuttiTidy(undefined), f.tuttiTidy(42)]')
        self.assertEqual(cases, [None, None, 42])


if __name__ == '__main__':
    unittest.main()
