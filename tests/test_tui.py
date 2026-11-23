"""The terminal UI (t201): every view renders at 80x24 and at a small size, lines never wrap, the footer always lists the
keys, help is one key away, arrow and vim keys move, status is an icon and a word (never colour alone), plain mode has no
icons or lines, NO_COLOR turns colour off."""
import unittest
from unittest import mock

from tutti import tui, tuicmd

STATE = {'version': '0.31.12', 'summary': {'running': 3, 'stalled': 1, 'free_slots': 2, 'panic': ['cursor'], 'behind': [],
                                            'machines_offline': ['rpi'], 'next_worker': {'account': 'codex'}},
         'queue': {'header': '12 queued'},
         'tasks': [{'id': f't{i}', 'title': f'task {i} ' + 'long words ' * 12, 'status': 'queued'} for i in range(40)]
         + [{'id': 't99', 'title': 'pick a name', 'status': 'needs-person'}],
         'autopilot': {'parked': [{'id': 't99', 'title': 'pick a name', 'parked': 'two names fit'}]},
         'machines': {'items': [{'name': 'nuc', 'status': 'free', 'reason': 'fit'}, {'name': 'rpi', 'status': 'offline',
                                                                                      'reason': 'no answer'}]},
         'agents': [{'name': 'codex', 'state': 'ready', 'record': '268 runs, 2 passed'}]}


class Render(unittest.TestCase):
    def test_every_view_fits_80x24_and_a_small_terminal(self):
        for view in tui.VIEWS:
            for w, h in ((80, 24), (40, 12), (132, 50)):
                lines = tui.render(view, STATE, w, h)
                self.assertEqual(len(lines), h, (view, w, h))
                self.assertTrue(all(len(line) <= w for line in lines), (view, w, h))
                self.assertIn('? help', lines[-1])   # the footer always says how to get help
                self.assertIn(tui.TITLES[view], lines[0])

    def test_status_is_an_icon_and_a_word(self):
        hosts = '\n'.join(tui.render('hosts', STATE, 80, 24))
        self.assertIn('+ free', hosts)
        self.assertIn('x offline', hosts)
        self.assertEqual(tui.role_of('> + free nuc'), 'ok')
        self.assertIsNone(tui.role_of('plain words'))

    def test_the_cursor_scrolls_a_long_list_and_tasks_have_status_tabs(self):
        lines = tui.render('tasks', STATE, 80, 12, {'tab': 1, 'cursor': 30})
        self.assertIn('<Queued>', lines[3])
        self.assertTrue(any(line.startswith('> ') and 't30' in line for line in lines))
        needs = '\n'.join(tui.render('tasks', STATE, 80, 24, {'tab': 2}))
        self.assertIn('t99', needs)
        empty = '\n'.join(tui.render('tasks', STATE, 80, 24, {'tab': 3}))
        self.assertIn('No task is to review.', empty)

    def test_help_and_plain_mode(self):
        self.assertIn('Keys', tui.render('overview', STATE, 80, 24, {'help': True})[3])
        plain = tui.render('hosts', STATE, 80, 24, plain=True)
        self.assertFalse(any('─' in line or line.startswith('>') for line in plain))
        self.assertIn('offline rpi no answer', '\n'.join(plain))
        self.assertIn('Accounts in panic: cursor', '\n'.join(tui.render('overview', STATE, 80, 24)))


class Keys(unittest.TestCase):
    def test_views_moves_status_tabs_help_and_quit(self):
        v, ui, q = tui.key('overview', {}, 'tab')
        self.assertEqual(v, 'tasks')
        v, ui, q = tui.key(v, ui, 'btab')
        self.assertEqual(v, 'overview')
        v, ui, q = tui.key(v, ui, '4')
        self.assertEqual(v, 'hosts')
        v, ui, q = tui.key(v, ui, 'j')
        v, ui, q = tui.key(v, ui, 'down')
        self.assertEqual(ui['cursor'], 2)
        v, ui, q = tui.key(v, ui, 'k')
        v, ui, q = tui.key(v, ui, 'g')
        self.assertEqual(ui['cursor'], 0)
        v, ui, q = tui.key('tasks', {'tab': 1}, 'l')
        self.assertEqual(ui['tab'], 2)
        v, ui, q = tui.key(v, ui, 'h')
        v, ui, q = tui.key(v, ui, 'h')
        self.assertEqual(ui['tab'], 0)
        v, ui, q = tui.key(v, ui, '?')
        self.assertTrue(ui['help'])
        v, ui, q = tui.key(v, ui, 'x')   # any key closes help
        self.assertFalse(ui['help'] or q)
        self.assertTrue(tui.key(v, ui, 'q')[2])


class Load(unittest.TestCase):
    def test_the_dashboards_fresh_round_is_read_at_once_and_a_stale_one_is_not(self):
        import json
        import os
        import tempfile
        import time

        from tutti import dashboard
        with tempfile.TemporaryDirectory() as d:
            cfg = {'project': {'state': d}}
            path = os.path.join(d, dashboard.Collector.SNAPSHOT_FILE)
            with open(path, 'w') as f:
                json.dump({'version': 'from the dashboard', 'machines': {'items': []}}, f)
            with mock.patch('tutti.backlog.load', return_value={'tasks': [{'id': 't1', 'title': 'x', 'status': 'queued'}]}):
                got = tuicmd.load(cfg)
                self.assertEqual((got['version'], got['tasks'][0]['id']), ('from the dashboard', 't1'))
                built = mock.Mock(snapshot=lambda: {'version': 'built here'})
                with mock.patch.object(dashboard, 'Collector', return_value=built) as made:
                    made.SNAPSHOT_FILE = dashboard.Collector.SNAPSHOT_FILE
                    got = tuicmd.load(cfg, now=time.time() + tuicmd.FRESH_S + 60)
                self.assertEqual(got['version'], 'built here')

    def test_loading_draws_at_once(self):
        self.assertIn('Loading', '\n'.join(tui.render('hosts', None, 80, 24)))


class Review(unittest.TestCase):
    """Grok's review of the first version: a clamped cursor and a load that fails without freezing."""

    def test_the_cursor_is_clamped_when_stored(self):
        v, ui, _q = tui.key('hosts', {'rows': 3}, 'G')
        self.assertEqual(ui['cursor'], 2)
        v, ui, _q = tui.key(v, ui, 'k')
        self.assertEqual(ui['cursor'], 1)   # one press back, not a million
        v, ui, _q = tui.key(v, {**ui, 'cursor': 0}, 'k')
        self.assertEqual(ui['cursor'], 0)

    def test_a_failed_load_says_so_and_keeps_the_last_good_data(self):
        first = '\n'.join(tui.render('hosts', {'load_error': 'OSError: boom'}, 80, 24))
        self.assertIn('Could not load: OSError: boom', first)
        later = tui.render('hosts', {**STATE, 'load_error': 'OSError: boom'}, 80, 24)
        self.assertIn('nuc', '\n'.join(later))
        self.assertIn('Last load failed', later[-1])
        loader = tuicmd.Loader({}, lambda cfg: (_ for _ in ()).throw(OSError('boom')))

        class Now:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()
        loader.threading = mock.Mock(Thread=Now)
        loader.poke()
        self.assertEqual((loader.error, loader.state, loader.busy), ('OSError: boom', {'load_error': 'OSError: boom'}, False))
        loader.poke()   # within REFRESH_S: not tried again at once
        self.assertGreater(loader.at, 0)


class Command(unittest.TestCase):
    def test_plain_output_and_no_color(self):
        lines = []
        a = mock.Mock(view='hosts', plain=True, width=60, status=None)
        self.assertEqual(tuicmd.cmd_tui({}, a, lines.append, load_fn=lambda cfg: STATE), 0)
        self.assertIn('free nuc fit', '\n'.join(lines))
        self.assertTrue(all(len(line) <= 60 for line in lines))
        self.assertFalse(tuicmd.colours_on({'NO_COLOR': '1', 'TERM': 'xterm-256color'}))
        self.assertFalse(tuicmd.colours_on({'TERM': 'dumb'}))
        self.assertTrue(tuicmd.colours_on({'TERM': 'xterm-256color'}))


if __name__ == '__main__':
    unittest.main()
