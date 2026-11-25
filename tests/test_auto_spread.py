"""`tutti run auto` spreads work: every account with room takes a turn (owner, 2026-11-24: use Codex, save Claude)."""
import os
import unittest
from unittest import mock

import tmpdirs

from tutti import routing, state


def acc(name, verdict='on pace', expires='9999'):
    return {'name': name, 'verdict': verdict, 'used': 10.0, 'panic_at': 95,
            'quota_plan': {'expires': expires, 'tasks': 1, 'left': 50.0} if expires != '9999' else None}


class Spread(unittest.TestCase):
    def test_the_account_picked_longest_ago_goes_first(self):
        gem, codex = acc('agy-gemini', 'behind', '2026-11-24T18:00'), acc('codex')
        self.assertEqual(routing.pick_account([gem, codex])[0]['name'], 'agy-gemini')   # without spreading: soonest expiry
        best, why = routing.pick_account([gem, codex], last_pick={'agy-gemini': 100.0})
        self.assertEqual(best['name'], 'codex')
        self.assertIn('also has room: agy-gemini', why)

    def test_an_account_ahead_of_pace_stays_last_even_when_never_picked(self):
        best, _ = routing.pick_account([acc('a', 'ahead'), acc('b')], last_pick={'b': 100.0})
        self.assertEqual(best['name'], 'b')

    def test_picks_are_remembered_and_a_broken_file_is_ignored(self):
        d = tmpdirs.mkdtemp()
        cfg = {'project': {'state': d}}
        self.assertEqual(state.last_picks(cfg), {})
        state.note_pick(cfg, 'codex', 5.0)
        self.assertEqual(state.last_picks(cfg), {'codex': 5.0})
        with open(os.path.join(d, state.AUTO_PICKS), 'w', encoding='utf8') as f:
            f.write('{broken')
        self.assertEqual(state.last_picks(cfg), {})

    def test_next_worker_with_spread_records_the_pick_under_a_lock_and_without_spread_does_not(self):
        d = tmpdirs.mkdtemp()
        cfg = {'project': {'state': d}, 'accounts': {'codex': {'worker': 'codex'}}, 'workers': {'codex': {}}, 'routing': {}}
        with mock.patch.object(state.routing, 'pick_account', return_value=(acc('codex'), 'ok')), \
                mock.patch('tutti.autopilot.main_in_panic', return_value=False), \
                mock.patch('tutti.tiers.choose', return_value={'args': [], 'label': 'x'}), \
                mock.patch.object(state, 'worker_accounts', return_value=['codex']):
            state.next_worker(cfg, [acc('codex')], now=None, spread=False)
            self.assertEqual(state.last_picks(cfg), {})
            state.next_worker(cfg, [acc('codex')], now=None, spread=True)
        self.assertEqual(list(state.last_picks(cfg)), ['codex'])
        self.assertTrue(os.path.isfile(os.path.join(d, state.AUTO_PICKS + '.lock')))
        self.assertEqual([f for f in os.listdir(d) if f.endswith('.tmp')], [])   # the temp file is gone

    def test_an_unwritable_state_folder_still_picks(self):
        cfg = {'project': {'state': os.path.join(tmpdirs.mkdtemp(), 'file')}}
        with open(cfg['project']['state'], 'w', encoding='utf8') as f:
            f.write('x')   # a file where the folder should be
        with state.picks_locked(cfg):
            state.note_pick(cfg, 'codex', 1.0)
        self.assertEqual(state.last_picks(cfg), {})



class HeldByMachine(unittest.TestCase):
    """An account whose worker's machine may not run now is named in run auto's reason, never dropped silently."""
    CFG = {'workers': {'codex': {'kind': 'cli', 'machine': 'laptop'}, 'agy': {'kind': 'cli'}},
           'accounts': {'codex': {'worker': 'codex'}, 'agy-gemini': {'worker': 'agy'}},
           'routing': {}, 'machines': {'laptop': {'quiet_hours': ['00:00-23:59']}}, 'autopilot': {}, 'project': {'state': '/nonexistent'}}

    def states(self, gem_unfit):
        gem = {**acc('agy-gemini', 'spent' if gem_unfit else 'behind'), 'panic': gem_unfit}
        return [gem, acc('codex', 'behind')]

    def test_no_room_names_the_account_its_machine_holds(self):
        from datetime import datetime, timezone
        from unittest import mock
        with mock.patch('tutti.autopilot.main_in_panic', return_value=False):
            r = state.next_worker(self.CFG, self.states(True), machines=[], now=datetime(2026, 11, 25, 1, tzinfo=timezone.utc))
        self.assertIsNone(r['worker'])
        self.assertIn('held by its machine: codex: codex: quiet hours on laptop', r['reason'])

    def test_a_free_machine_holds_nothing(self):
        from unittest import mock
        with mock.patch('tutti.autopilot.main_in_panic', return_value=False), mock.patch('tutti.tiers.choose',
                                                                                          return_value={'args': [], 'label': ''}):
            r = state.next_worker({**self.CFG, 'machines': {'laptop': {}}}, self.states(True), machines=[])
        self.assertEqual(r['worker'], 'codex')
        self.assertNotIn('held by its machine', r['reason'])


if __name__ == '__main__':
    unittest.main()
