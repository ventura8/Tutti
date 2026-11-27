"""Unit and snapshot tests for Tutti's TUI (tutti/tui.py): layout, navigation, color modes, and accessibility."""

import contextlib
import io
import unittest
from unittest.mock import patch

from tutti import cli, tui


class TestTui(unittest.TestCase):
    def test_all_screens_80x24_snapshot(self):
        for screen in tui.SCREENS:
            state = tui.TuiState(screen=screen, color_mode='none')
            lines = tui.render_screen(state, width=80, height=24, color_mode='none')
            self.assertEqual(len(lines), 24, f'screen {screen} must render exactly 24 lines')
            for idx, line in enumerate(lines):
                self.assertEqual(tui.visible_len(line), 80, f'line {idx} of screen {screen} must be exactly 80 chars')
            self.assertIn('Tutti TUI', lines[0])
            self.assertIn(screen.capitalize()[:4], lines[0])
            self.assertIn('[?] Help', lines[0])
            self.assertIn('[q] Quit', lines[-1])
            self.assertIn('[Tab/1-5]', lines[-1])

    def test_focus_snapshot_at_each_size(self):
        for idx, screen in enumerate(tui.SCREENS, 1):
            state = tui.TuiState(screen=screen)
            state._cache[screen] = [{'id': 'one', 'title': 'Example', 'status': 'queued'}]
            for width, height in ((40, 8), (80, 24), (120, 40)):
                with self.subTest(screen=screen, width=width, height=height):
                    rows = tui.render_screen(state, width, height)
                    tab = f'[{idx}]' if width == 40 else f'[{idx}:{screen.capitalize()}]'
                    self.assertIn(tab, rows[0])
                    self.assertEqual(rows[2].rstrip(), '> [+] [⏳ Queued] Example')
                    self.assertIn('[?] Help', rows[-1])
                    self.assertEqual(len(rows), height)
                    self.assertTrue(all(tui.visible_len(row) == width for row in rows))

    def test_compact_overlay_scrolls_and_restores_focus(self):
        state = tui.TuiState(cfg={'machines': {'first': {}, 'second': {}}})
        state.on_key('j')
        state.on_key('enter')
        rows = tui.render_screen(state, 40, 8)
        self.assertIn('Host: second', rows[2])
        state.on_key('escape')
        self.assertEqual(state.selected, 1)
        state.on_key('?')
        for _ in range(2 * len(tui.HELP_LINES)):
            state.on_key('down')
            rows = tui.render_screen(state, 40, 8)
        self.assertTrue(any('Status badges' in row for row in rows))
        state.on_key('?')
        self.assertEqual(state.selected, 1)

    def test_curses_falls_back_to_basic_colors(self):
        with patch.object(tui, 'curses') as fake:
            fake.COLORS = 8
            fake.color_pair.return_value = 42
            styles = tui.curses_styles('truecolor')
            self.assertEqual(styles[tui.ANSI_TRUECOLOR['green']], 42)
            fake.init_pair.assert_any_call(1, 2, -1)
            self.assertEqual(tui.curses_styles('none'), {})

    def test_setup_uses_saved_status_and_reports_bad_data(self):
        report = {'items': [{'id': 'service', 'title': 'Dashboard at login', 'state': 'todo', 'detail': 'Enable login service'}]}
        with patch('tutti.setupcmd.read_report', return_value=report):
            rows = tui.render_screen(tui.TuiState(cfg={'project': {}}, screen='setup'))
        self.assertIn('[! Needs You] Dashboard at login', rows[2])

        with patch('tutti.setupcmd.read_report', return_value={'items': [{}]}):
            rows = tui.render_screen(tui.TuiState(cfg={'project': {}}, screen='setup'))
        self.assertIn('Could not load this screen', rows[2])

    def test_screen_resizing(self):
        state = tui.TuiState(screen='machines', color_mode='none')
        # 120x40 (large)
        lines40 = tui.render_screen(state, width=120, height=40, color_mode='none')
        self.assertEqual(len(lines40), 40)
        self.assertTrue(all(tui.visible_len(row) == 120 for row in lines40))
        # 60x15 (compact)
        lines15 = tui.render_screen(state, width=60, height=15, color_mode='none')
        self.assertEqual(len(lines15), 15)
        self.assertTrue(all(tui.visible_len(row) == 60 for row in lines15))
        # 40x8 (very narrow/short)
        lines8 = tui.render_screen(state, width=40, height=8, color_mode='none')
        self.assertEqual(len(lines8), 8)
        self.assertTrue(all(tui.visible_len(row) == 40 for row in lines8))

    def test_navigation_arrow_and_vim_keys(self):
        state = tui.TuiState(cfg={'machines': {'first': {}, 'second': {}}}, screen='machines')
        items = state.get_items()
        self.assertEqual(state.selected, 0)
        # down / j
        state.on_key('j')
        self.assertEqual(state.selected, min(1, len(items) - 1))
        state.on_key('k')
        self.assertEqual(state.selected, 0)
        state.on_key('down')
        self.assertEqual(state.selected, min(1, len(items) - 1))
        state.on_key('up')
        self.assertEqual(state.selected, 0)

    def test_screen_switching_and_digit_keys(self):
        state = tui.TuiState(screen='machines')
        state.on_key('tab')
        self.assertEqual(state.screen, 'needs')
        state.on_key('l')
        self.assertEqual(state.screen, 'dispatch')
        state.on_key('h')
        self.assertEqual(state.screen, 'needs')
        state.on_key('btab')
        self.assertEqual(state.screen, 'machines')
        # direct digits 1-5
        state.on_key('4')
        self.assertEqual(state.screen, 'chat')
        state.on_key('5')
        self.assertEqual(state.screen, 'setup')
        state.on_key('1')
        self.assertEqual(state.screen, 'machines')

    def test_help_and_item_details_toggle(self):
        state = tui.TuiState(screen='machines', color_mode='none')
        state.cfg = {'machines': {'example': {}}}
        self.assertFalse(state.help_open)
        state.on_key('?')
        self.assertTrue(state.help_open)
        lines = tui.render_screen(state, width=80, height=24, color_mode='none')
        self.assertTrue(any('Help & Navigation' in row for row in lines))
        # Close help
        state.on_key('?')
        self.assertFalse(state.help_open)
        # Enter toggles item expansion
        state.on_key('enter')
        items = state.get_items()
        self.assertIn(str(items[0]['id']), state.expanded)
        state.on_key('enter')
        self.assertNotIn(str(items[0]['id']), state.expanded)

    def test_color_modes_and_fallbacks(self):
        # none (NO_COLOR)
        state_none = tui.TuiState(cfg={'machines': {'example': {}}}, screen='machines')
        lines_none = tui.render_screen(state_none, width=80, height=24, color_mode='none')
        self.assertFalse(any('\033[' in row for row in lines_none), 'NO_COLOR must produce zero ANSI escape codes')

        # 16 colors
        lines_16 = tui.render_screen(state_none, width=80, height=24, color_mode='16')
        self.assertTrue(any('\033[' in row for row in lines_16))

        # 256 colors
        lines_256 = tui.render_screen(state_none, width=80, height=24, color_mode='256')
        self.assertTrue(any('\033[38;5;' in row for row in lines_256))

        # truecolor
        lines_tc = tui.render_screen(state_none, width=80, height=24, color_mode='truecolor')
        self.assertTrue(any('\033[38;2;' in row for row in lines_tc))

    def test_color_mode_detection(self):
        self.assertEqual(tui.detect_color_mode({'NO_COLOR': '1'}), 'none')
        self.assertEqual(tui.detect_color_mode({'TERM': 'dumb'}), 'none')
        self.assertEqual(tui.detect_color_mode({'COLORTERM': 'truecolor'}), 'truecolor')
        self.assertEqual(tui.detect_color_mode({'TERM': 'xterm-256color'}), '256')
        self.assertEqual(tui.detect_color_mode({'TERM': 'xterm'}), '16')

    def test_colorblind_safe_status_indicators(self):
        for key in tui.STATUS_BADGES:
            badge = tui.format_status(key, color_mode='none')
            self.assertTrue(badge.startswith('['), f'badge for {key} must start with [')
            self.assertTrue(badge.endswith(']'), f'badge for {key} must end with ]')
            # Contains both non-ascii icon and word
            parts = badge[1:-1].split(' ')
            self.assertGreaterEqual(len(parts), 2, f'badge for {key} must have icon and text label')

    def test_plain_screen_reader_mode(self):
        cfg = {'machines': {'first': {}, 'second': {}}}
        state = tui.TuiState(cfg=cfg, screen='machines')
        plain_lines = tui.render_plain(state)
        self.assertTrue(any('=== Tutti TUI' in row for row in plain_lines))
        self.assertTrue(any('Item 1 of' in row for row in plain_lines))
        self.assertTrue(any('[FOCUSED]' in row for row in plain_lines))

        # Test interactive plain runner with simulated input
        inputs = iter(['down', 'enter', 'escape', '?', 'q'])
        out = io.StringIO()
        rc = tui.run_plain(cfg=cfg, screen='machines', read=lambda _: next(inputs), out=out)
        self.assertEqual(rc, 0)
        transcript = out.getvalue()
        self.assertIn('Host: second (local) [FOCUSED]', transcript)
        self.assertIn('ID: second', transcript)
        self.assertIn('Help & Navigation', transcript)
        self.assertNotIn('\033', transcript)

    def test_loading_error_and_empty_states(self):
        state = tui.TuiState(screen='machines')
        state.loading = True
        lines_load = tui.render_screen(state, 80, 24, color_mode='none')
        self.assertTrue(any('Loading...' in row for row in lines_load))

        state.loading = False
        state.error = 'Network timeout'
        lines_err = tui.render_screen(state, 80, 24, color_mode='none')
        self.assertTrue(any('Network timeout' in row for row in lines_err))

    def test_cli_ui_once_smoke(self):
        for sc in ('machines', 'needs', 'dispatch', 'chat', 'setup'):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = cli.main(['ui', sc, '--once'])
            self.assertEqual(rc, 0)
        with contextlib.redirect_stdout(io.StringIO()):
            rc_plain = cli.main(['ui', '--plain', '--once'])
        self.assertEqual(rc_plain, 0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(['ui', 'needs', '--plain', '--once']), 0)
        self.assertIn('=== Tutti TUI — Needs Attention ===', buf.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            rc_setup = cli.main(['setup', '--tui', '--once'])
        self.assertEqual(rc_setup, 0)

    def test_bad_inputs_and_edge_cases(self):
        # Unknown screen falls back to machines
        state = tui.TuiState(screen='nonexistent')
        self.assertEqual(state.screen, 'machines')
        # Escape closes help when open, quits when closed
        state.on_key('?')
        self.assertTrue(state.help_open)
        self.assertIsNone(state.on_key('escape'))
        self.assertFalse(state.help_open)
        self.assertEqual(state.on_key('escape'), 'quit')
        # Unknown status still produces colorblind-safe badge
        self.assertEqual(tui.format_status('mystery', color_mode='none'), '[? mystery]')
        # Unknown color mode returns unstyled text
        self.assertEqual(tui.colorize('hello', 'green', mode='invalid'), 'hello')
        # Tiny terminal height renders footer cleanly
        tiny = tui.render_screen(state, width=40, height=3, color_mode='none')
        self.assertEqual(len(tiny), 1)
        self.assertEqual(len(tiny[0]), 40)
        # Empty item list renders helpful empty state
        empty = tui.render_item_list([], 0, set(), 80, 5, color_mode='none')
        self.assertEqual(len(empty), 5)
        self.assertIn('No items on this screen', empty[0])


if __name__ == '__main__':
    unittest.main()
