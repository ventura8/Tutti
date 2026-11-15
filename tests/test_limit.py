"""A machine's limited-usage window (machines.<m>.limit, tutti/share.py): a daily window, past midnight too, when Tutti uses
at most pct % of it: its slots and model threads capped, no new heavy job, editable from the machine card and the CLI, and
carried to the new main machine by `tutti move`. Temp folders and fake runs only."""
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import tmpdirs

from tutti import config, dashboard, local, move, routing, share, state, tz, workq

MON = datetime(2026, 11, 16, tzinfo=timezone.utc)   # a Monday


def at(day, hour, minute=0):
    return MON.replace(day=16 + day, hour=hour, minute=minute)


class Window(unittest.TestCase):
    def setUp(self):
        tz.use({'time': {'zone': 'UTC'}})
        self.addCleanup(tz.use, {})

    def test_a_window_past_midnight_belongs_to_the_day_it_starts(self):
        m = {'limit': {'window': '18-01', 'pct': 60}}
        self.assertEqual(share.limited(m, at(0, 23, 30)), 60)
        self.assertEqual(share.limited(m, at(1, 0, 30)), 60)              # Tuesday 00:30: Monday's window
        self.assertIsNone(share.limited(m, at(1, 1, 30)))
        self.assertIsNone(share.limited(m, at(0, 17, 59)))
        mon_only = {'limit': {'window': '18-01', 'days': ['mon']}}
        self.assertEqual(share.limited(mon_only, at(1, 0, 30)), share.LIMIT_PCT)   # 60 by default
        self.assertIsNone(share.limited(mon_only, at(1, 23)))            # Tuesday evening: not a listed day
        self.assertIsNone(share.limited({}, at(0, 23)))                  # no window: no limit (the default)

    def test_the_cap_lowers_slots_threads_and_keeps_heavy_jobs_off(self):
        m = {'name': 'nuc', 'transport': 'ssh', 'host': 'nuc.example', 'slots': 4, 'min_free_gb': 6,
             'limit': {'window': '00-24', 'pct': 50}}
        row = state.machine_state(m, routing.Probe(True, 4, 0.1, 16), 1, at(0, 12))
        self.assertEqual((row['free'], row['use_pct'], row['limited']), (2, 50, True))
        self.assertIn('limited hours 00-24 at 50 %', row['use_why'])
        busy = state.machine_state(m, routing.Probe(True, 2, 0.1, 16), 1, at(0, 12))   # 2 running: no new run above the cap
        self.assertEqual(busy['free'], 0)
        cfg = config.normalise({'machines': {'nuc': {'host': 'nuc.example', 'limit': {'window': '00-24', 'pct': 50}}},
                                'workers': {'big': {'kind': 'ollama', 'host': '127.0.0.1:1', 'model': 'm', 'machine': 'nuc',
                                                    'threads': 8}}})
        self.assertEqual(local.threads_at_share(cfg, {**cfg['workers']['big'], '_name': 'big'}, at(0, 12)), 4)
        rows = [{**row, 'status': 'free'}, {'name': 'pc', 'status': 'free', 'free': 1, 'cores': 8}]
        cfg['routing']['order'] = ['nuc', 'pc']
        cfg['machines']['pc'] = {'name': 'pc', 'transport': 'ssh', 'slots': 1}
        got = workq.assign(cfg, [{'name': 'static', 'run': 'x', 'heavy': True}], rows, set(), at(0, 12))
        self.assertEqual(got[0][1], 'pc')                                   # the limited NUC takes no new heavy job

    def test_bad_windows_are_named_with_the_fix(self):
        self.assertIn('HH-HH', share.limit_errors('nuc', {'window': '6pm-1am'})[0])
        self.assertIn('from 0 to 100', share.limit_errors('nuc', {'window': '18-01', 'pct': 140})[0])
        self.assertIn('a list of mon', share.limit_errors('nuc', {'window': '18-01', 'days': ['monday']})[0])
        self.assertEqual(share.limit_errors('nuc', None), [])
        self.assertEqual(share.limit_errors('nuc', {'window': '18-01', 'pct': 0}), [])   # 0: not used at all then (blocked hours)
        self.assertIn('the same', share.limit_errors('nuc', {'window': '18-18'})[0])


class Edit(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdirs.mkdtemp()
        self.path = os.path.join(self.dir, 'tutti.config.yaml')
        with open(self.path, 'w', encoding='utf8') as f:
            f.write(f'project:\n  state: {self.dir}/state\nmachines:\n  nuc:\n    host: nuc.example   # the build machine\n')

    def collector(self):
        c = SimpleNamespace(cfg=config.load(self.path))
        c.reload = lambda: setattr(c, 'cfg', config.load(self.path)) or c.cfg
        return c

    def test_the_machine_card_saves_and_removes_the_window_in_the_config(self):
        c = self.collector()
        code, answer = dashboard.save_limit(c, b'{"machine": "nuc", "window": "18-01", "pct": 60}')
        self.assertEqual(code, 200, answer)
        self.assertEqual(c.cfg['machines']['nuc']['limit'], {'window': '18-01', 'pct': 60})
        with open(self.path, encoding='utf8') as f:
            self.assertIn('# the build machine', f.read())                 # the comments stay
        self.assertEqual(dashboard.save_limit(c, b'{"machine": "nuc", "window": "late"}')[0], 400)
        self.assertEqual(dashboard.save_limit(c, b'{"machine": "mars", "window": "18-01"}')[0], 400)
        code, answer = dashboard.save_limit(c, b'{"machine": "nuc", "window": "18-01", "pct": "60"}')
        self.assertEqual(code, 400)
        self.assertIn("pct is '60': machines.nuc.limit.pct", answer['error'])
        self.assertIn('from 0 to 100', answer['error'])
        code, _ = dashboard.save_limit(c, b'{"machine": "nuc", "window": "18-01", "pct": null}')   # an empty box: kept 60
        self.assertEqual((code, c.cfg['machines']['nuc']['limit']['pct']), (200, 60))
        self.assertEqual(dashboard.save_limit(c, b'{"machine": "nuc", "window": "off"}')[0], 200)
        self.assertNotIn('limit', c.cfg['machines']['nuc'])

    def test_the_default_pct_is_60_and_keeps_a_set_one(self):
        cfg = config.load(self.path)
        self.assertEqual(share.limit_change(cfg, 'nuc', '18-01')[0][1]['pct'], 60)
        cfg['machines']['nuc']['limit'] = {'window': '20-22', 'pct': 40}
        self.assertEqual(share.limit_change(cfg, 'nuc', '18-01')[0][1], {'window': '18-01', 'pct': 40})


class Move(unittest.TestCase):
    def test_the_main_machines_window_follows_tutti_to_its_new_main_machine(self):
        cfg = config.normalise({'machines': {'laptop': {'host': 'local', 'limit': {'window': '18-01', 'pct': 60, 'main': True}},
                                             'nuc': {'host': 'nuc.example'}}})
        src = {'name': 'laptop', 'address': 'laptop.example', 'user': 'me'}
        edits = dict((tuple(k), v) for k, v in move.machine_edits(cfg, 'nuc', {}, src))
        self.assertEqual(edits[('machines', 'nuc', 'limit')]['window'], '18-01')
        self.assertIn(('machines', 'laptop', 'limit'), edits)               # taken off the old main machine
        cfg['machines']['laptop']['limit'] = {'window': '18-01'}            # a machine's own window (no main): it stays
        edits = dict((tuple(k), v) for k, v in move.machine_edits(cfg, 'nuc', {}, src))
        self.assertNotIn(('machines', 'nuc', 'limit'), edits)
