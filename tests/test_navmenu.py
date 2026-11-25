"""Dashboard hamburger menu (t446): markup and pure helpers for view labels."""
import json
import os
import re
import shutil
import subprocess
import unittest

WEB = os.path.join(os.path.dirname(__file__), '..', 'tutti', 'web')


class NavMenuMarkup(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(WEB, 'index.html'), encoding='utf8') as f:
            self.html = f.read()
        with open(os.path.join(WEB, 'navmenu.js'), encoding='utf8') as f:
            self.js = f.read()
        with open(os.path.join(WEB, 'app.css'), encoding='utf8') as f:
            self.css = f.read()

    def test_hamburger_replaces_the_tab_bar_and_keeps_the_hashes(self):
        self.assertIn('id="nav-menu-btn"', self.html)
        self.assertIn('aria-controls="nav-menu-panel"', self.html)
        self.assertNotIn('class="tabs"', self.html)
        self.assertNotIn('tabs__tab', self.html)
        self.assertIn('class="nav-menu" aria-label="Views"', self.html)
        panel = self.html.split('id="nav-menu-panel"', 1)[1].split('</div>', 1)[0]
        views = re.findall(r'href="#([a-z]+)" data-view="([a-z]+)"', panel)
        self.assertEqual(views, [('needs', 'needs'), ('chat', 'chat'), ('dashboard', 'dashboard'),
                                 ('tasks', 'tasks'), ('schedule', 'schedule'),
                                 ('devices', 'devices'), ('hosts', 'machines'), ('discovered', 'discovered'),
                                 ('agents', 'agents'), ('settings', 'settings'), ('users', 'users')])
        self.assertIn('navmenu.js?v=', self.html)

    def test_script_closes_on_escape_and_exports_helpers(self):
        self.assertIn("e.key === 'Escape'", self.js)
        self.assertIn('navMenuSync', self.js)
        self.assertIn('module.exports', self.js)
        self.assertIn('.nav-menu__panel', self.css)
        self.assertNotIn('.tabs__tab', self.css)



@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class NeedsView(unittest.TestCase):
    def node(self, expr):
        script = f'const m = require({json.dumps(os.path.join(WEB, "navmenu.js"))});\nconsole.log(JSON.stringify({expr}));'
        return json.loads(subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True).stdout)

    def test_needs_you_opens_by_default_only_when_it_has_items_and_no_view_is_asked(self):
        self.assertEqual(self.node("[m.tuttiDefaultView(2, ''), m.tuttiDefaultView(1, '#')]"), ['#needs', '#needs'])
        self.assertEqual(self.node("[m.tuttiDefaultView(0, ''), m.tuttiDefaultView(3, '#tasks')]"), ['', ''])

    def test_needs_is_a_view_with_its_own_panel_and_count(self):
        self.assertEqual(self.node("m.tuttiView('#needs')"), 'needs')
        with open(os.path.join(WEB, 'index.html'), encoding='utf8') as f:
            html = f.read()
        self.assertIn('id="needs-view"', html)
        self.assertIn('id="needs-count"', html)


if __name__ == '__main__':
    unittest.main()
