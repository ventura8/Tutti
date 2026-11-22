"""Dashboard hamburger menu (t446): markup and pure helpers for view labels."""
import os
import re
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
        self.assertEqual(views, [('chat', 'chat'), ('dashboard', 'dashboard'), ('tasks', 'tasks'), ('schedule', 'schedule'),
                                 ('devices', 'devices'), ('machines', 'machines'), ('discovered', 'discovered'),
                                 ('agents', 'agents'), ('settings', 'settings'), ('users', 'users')])
        self.assertIn('navmenu.js?v=', self.html)

    def test_script_closes_on_escape_and_exports_helpers(self):
        self.assertIn("e.key === 'Escape'", self.js)
        self.assertIn('navMenuSync', self.js)
        self.assertIn('module.exports', self.js)
        self.assertIn('.nav-menu__panel', self.css)
        self.assertNotIn('.tabs__tab', self.css)


if __name__ == '__main__':
    unittest.main()
