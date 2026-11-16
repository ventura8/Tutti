"""docs/ARCHITECTURE.md stays correct: every `tutti/...` or `scripts/...` path and every linked page it names exists, and
every Mermaid block opens with a known diagram type and has no empty body."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(ROOT, 'docs', 'ARCHITECTURE.md')
KINDS = ('flowchart', 'graph', 'sequenceDiagram', 'stateDiagram-v2', 'classDiagram', 'erDiagram')


class ArchitectureDoc(unittest.TestCase):
    def setUp(self):
        with open(DOC, encoding='utf-8') as f:
            self.text = f.read()

    def test_named_paths_exist(self):
        paths = set(re.findall(r'`((?:tutti|scripts|bin)/[\w./-]+)`', self.text))
        missing = sorted(p for p in paths if not os.path.exists(os.path.join(ROOT, p)))
        self.assertEqual(missing, [], 'ARCHITECTURE.md names paths that do not exist')

    def test_linked_pages_exist(self):
        links = set(re.findall(r'\]\(([\w./-]+\.md)\)', self.text))
        missing = sorted(p for p in links if not os.path.exists(os.path.join(ROOT, 'docs', p)))
        self.assertEqual(missing, [])

    def test_mermaid_blocks_are_well_formed(self):
        blocks = re.findall(r'```mermaid\n(.*?)```', self.text, re.S)
        self.assertGreaterEqual(len(blocks), 1)
        for b in blocks:
            lines = [ln for ln in b.splitlines() if ln.strip()]
            self.assertGreater(len(lines), 1)
            self.assertTrue(lines[0].strip().startswith(KINDS), lines[0])
            self.assertEqual(b.count('['), b.count(']'))
            self.assertEqual(b.count('('), b.count(')'))


if __name__ == '__main__':
    unittest.main()
