"""`tutti run auto` spreads work: every account with room takes a turn (owner, 2026-11-24: use Codex, save Claude)."""
import os
import unittest

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


if __name__ == '__main__':
    unittest.main()
