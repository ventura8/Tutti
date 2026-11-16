"""docs/API.md names every CLI subcommand, HTTP route and MCP tool, and its generated parts match the code (tutti/apidoc.py)."""
import os
import unittest

import tmpdirs
from tutti import apidoc, mcpserver


class ApiDocTest(unittest.TestCase):
    def test_the_doc_is_complete_and_current(self):
        self.assertEqual(apidoc.problems(), [], 'run `python3 -m tutti.apidoc --write` and document what is missing')

    def test_the_surfaces_are_found(self):
        self.assertIn('dashboard', apidoc.cli_names())
        routes = apidoc.http_routes()
        for r in ('/api/state', '/api/settings', '/api/chat', '/api/auth/login', '/hh/sync', '/mcp'):
            self.assertIn(r, routes)
        self.assertNotIn('/api/tags', routes)   # Ollama's, not Tutti's

    def test_a_missing_command_route_or_tool_is_named(self):
        with open(apidoc.DOC, encoding='utf8') as f:
            text = f.read()
        tool = mcpserver.TOOLS[0]['name']
        cut = text.replace('`tutti dashboard`', 'tutti dashboard').replace('`/api/chat`', '/api/chat').replace(f'`{tool}`', tool)
        found = apidoc.missing(cut)
        self.assertIn('CLI subcommand `tutti dashboard`', found)
        self.assertIn('HTTP route `/api/chat`', found)
        self.assertIn(f'MCP tool `{tool}`', found)

    def test_a_stale_part_is_found_and_rewritten(self):
        with open(apidoc.DOC, encoding='utf8') as f:
            text = f.read()
        start, _ = apidoc.markers('cli')
        path = os.path.join(tmpdirs.mkdtemp(), 'API.md')
        with open(path, 'w', encoding='utf8') as f:
            f.write(text.replace(start, start + '\nold line'))
        self.assertIn('a generated part is stale: run `python3 -m tutti.apidoc --write`', apidoc.problems(path))
        self.assertEqual(apidoc.main(['--write'], path), 0)
        self.assertEqual(apidoc.problems(path), [])

    def test_lost_markers_are_named(self):
        with self.assertRaises(ValueError) as e:
            apidoc.render('no markers here')
        self.assertIn('cli markers', str(e.exception))


if __name__ == '__main__':
    unittest.main()
