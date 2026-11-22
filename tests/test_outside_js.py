"""Unit tests for tutti/web/outside.js (the "Work outside Tutti" card), run under node with a stubbed DOM."""
import json
import pathlib
import shutil
import subprocess
import unittest

SRC = pathlib.Path(__file__).resolve().parent.parent / "tutti" / "web" / "outside.js"

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
const state = JSON.parse(process.argv[2]);
const nodes = {'outside': {rows: null, replaceChildren(...c) { this.rows = c; }}, 'outside-meta': {textContent: ''}};
const el = (tag, attrs, ...kids) => ({tag, attrs: attrs || {}, kids});
const chip = (text, kind) => ({chip: text, kind});
const $ = (id) => nodes[id] || null;
vm.runInNewContext(src + '\nrenderOutside(state);', {el, chip, $, state});
console.log(JSON.stringify({meta: nodes['outside-meta'].textContent, rows: nodes['outside'].rows}));
"""


def render(state):
    out = subprocess.run(["node", "-e", HARNESS, str(SRC), json.dumps(state)],
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


def commit(i=0):
    return {"repo": "r", "sha": f"abc{i}", "author": "bob", "subject": "did a thing"}


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class OutsideJsTests(unittest.TestCase):
    def test_clean_state_says_all_through_tutti(self):
        res = render({"outside": {"hours": 24, "commits": [], "leases": [], "unreadable": []}})
        self.assertEqual(res["meta"], "none in the last 24 h")
        self.assertEqual(len(res["rows"]), 1)
        self.assertEqual(res["rows"][0]["kids"][0], {"chip": "all through Tutti", "kind": "ok"})

    def test_commit_and_lease_rows(self):
        res = render({"outside": {"hours": 24, "commits": [commit()], "unreadable": [],
                                  "leases": [{"kind": "machine", "name": "m1", "why": "no task", "who": "ann"}]}})
        self.assertEqual(res["meta"], "2 in the last 24 h")
        self.assertEqual(len(res["rows"]), 2)
        self.assertEqual(res["rows"][0]["kids"][0]["attrs"]["text"], "r abc0")
        self.assertIn("bob: did a thing", res["rows"][0]["kids"][2]["attrs"]["text"])
        self.assertEqual(res["rows"][1]["kids"][0]["attrs"]["text"], "machine m1")
        self.assertIn("Leased by ann", res["rows"][1]["kids"][2]["attrs"]["text"])

    def test_commits_capped_at_twenty_rows(self):
        res = render({"outside": {"hours": 24, "leases": [], "unreadable": [],
                                  "commits": [commit(i) for i in range(25)]}})
        self.assertEqual(res["meta"], "25 in the last 24 h")
        self.assertEqual(len(res["rows"]), 20)

    def test_unreadable_repo_row(self):
        res = render({"outside": {"hours": 6, "commits": [], "leases": [], "unreadable": ["tutti"]}})
        self.assertEqual(res["meta"], "none in the last 6 h")
        self.assertEqual(res["rows"][0]["kids"][1]["chip"], "not read")
        self.assertIn("autopilot.repos.tutti.path", res["rows"][0]["kids"][2]["attrs"]["text"])

    def test_missing_outside_leaves_card_untouched(self):
        for state in ({}, {"outside": None}):
            res = render(state)
            self.assertEqual(res["meta"], "")
            self.assertIsNone(res["rows"])


if __name__ == "__main__":
    unittest.main()
