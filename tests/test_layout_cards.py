"""Dashboard card layout (t447): clamp, normalize, reset; temp folder for per-user save."""
import os
import unittest

import tmpdirs

from tutti import layout_cards as lc


class Clamp(unittest.TestCase):
    def test_cols_and_height_are_clamped(self):
        self.assertEqual(lc.clamp_cols(99), 12)
        self.assertEqual(lc.clamp_cols(1), 4)
        self.assertEqual(lc.clamp_cols('x'), 12)
        self.assertEqual(lc.clamp_height(0), 0)
        self.assertEqual(lc.clamp_height(-3), 0)
        self.assertEqual(lc.clamp_height(50), 140)
        self.assertEqual(lc.clamp_height(9999), 1200)
        self.assertEqual(lc.clamp_size({'cols': 3, 'height': 200}), {'cols': 4, 'height': 200})


class Order(unittest.TestCase):
    def test_unknown_ids_are_dropped_and_missing_ones_appended(self):
        got = lc.normalize_order(['jobs', 'ghost', 'usage'])
        self.assertEqual(got[0], 'jobs')
        self.assertEqual(got[1], 'usage')
        self.assertNotIn('ghost', got)
        self.assertEqual(set(got), set(lc.DEFAULT_ORDER))
        self.assertEqual(len(got), len(lc.DEFAULT_ORDER))

    def test_empty_saved_layout_is_the_default(self):
        d = lc.reset_layout()
        self.assertEqual(d['order'], list(lc.DEFAULT_ORDER))
        self.assertEqual(d['sizes']['usage']['cols'], 12)
        self.assertEqual(lc.apply_layout(None), d)
        self.assertEqual(lc.apply_layout({'order': [], 'sizes': {}})['order'], list(lc.DEFAULT_ORDER))


class Persist(unittest.TestCase):
    def test_per_user_save_and_read_round_trip(self):
        folder = tmpdirs.mkdtemp()
        layout = {'order': ['machines', 'usage'], 'sizes': {'usage': {'cols': 8, 'height': 300}}}
        saved = lc.save_user(folder, 'alice', layout)
        self.assertEqual(saved['order'][0], 'machines')
        self.assertEqual(saved['sizes']['usage'], {'cols': 8, 'height': 300})
        self.assertEqual(lc.for_user(folder, 'alice')['sizes']['usage']['cols'], 8)
        self.assertEqual(lc.for_user(folder, 'bob')['order'], list(lc.DEFAULT_ORDER))
        with self.assertRaisesRegex(ValueError, 'user_key is empty'):
            lc.save_user(folder, '', layout)
        self.assertTrue(os.path.isfile(lc.path(folder)))


class Api(unittest.TestCase):
    def test_dashboard_layout_get_and_post(self):
        from types import SimpleNamespace
        from tutti import dashboard
        folder = tmpdirs.mkdtemp()
        cfg = {'_path': os.path.join(folder, 'tutti.config.yaml'), 'project': {'state': folder},
               'dashboard': {'port': 4777}}
        collector = SimpleNamespace(cfg=cfg, who=lambda _h: {'sub': 'u1', 'email': 'a@example', 'role': 'admin'})
        code, answer = dashboard.layout_get(collector, {})
        self.assertEqual(code, 200)
        self.assertEqual(answer['layout']['order'][0], 'usage')
        code, answer = dashboard.layout_post(collector, {}, b'{"order":["machines","usage"],"sizes":{"usage":{"cols":8,"height":200}}}')
        self.assertEqual(code, 200)
        self.assertEqual(answer['layout']['order'][0], 'machines')
        code, answer = dashboard.layout_post(collector, {}, b'{"reset": true}')
        self.assertEqual(code, 200)
        self.assertEqual(answer['layout']['order'][0], 'usage')


if __name__ == '__main__':
    unittest.main()
