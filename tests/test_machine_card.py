"""The machine card's facts (tutti/state.py machine_state): slots busy, free and held back add up to the total, the yield flag
sets the share to 0 % and says so, and the slot count says where it comes from."""
import unittest

from tutti import config, routing, state

BASE = {'project': {'state': '/tmp/x'}, 'runner': {'kind': 'shell'}}


class MachineCard(unittest.TestCase):
    def test_a_yielding_machine_has_no_free_slot_and_a_zero_share_that_says_why(self):
        m = {'name': 'pc', 'transport': 'wsl', 'host': 'pc', 'slots': 2, 'min_free_gb': 6}
        row = state.machine_state(m, routing.Probe(True, 2, 0.5, 16, True), 1, idle_s=0)
        self.assertEqual((row['status'], row['free'], row['busy'], row['held_back']), ('paused', 0, 0, 2))
        self.assertEqual(row['use_pct'], 0)
        self.assertEqual(row['use_why'], 'its yield flag is up; also someone is at its keyboard')

    def test_busy_free_and_held_back_add_up_to_the_slots(self):
        m = {'name': 'nuc', 'transport': 'ssh', 'host': 'nuc', 'slots': 4, 'min_free_gb': 6, 'use_pct': 50}
        for free in range(5):
            row = state.machine_state(m, routing.Probe(True, free, 0.1, 16), 1)
            self.assertEqual(row['busy'] + row['free'] + row['held_back'], 4)
        self.assertIsNone(state.machine_state(m, routing.Probe(), 1)['held_back'])

    def test_the_slot_count_says_where_it_comes_from(self):
        laptop = config.machine_of(BASE, 'laptop', {'host': 'local', 'locks': ['/tmp/q.lock']})
        self.assertEqual(laptop['slots'], 1)
        self.assertIn('/tmp/q.lock', state.slots_why(laptop))
        self.assertIn('the default', state.slots_why(config.machine_of(BASE, 'b', {'host': 'b'})))
        self.assertEqual(state.slots_why(config.machine_of(BASE, 'c', {'host': 'c', 'slots': 3})), 'machines.c.slots')


if __name__ == '__main__':
    unittest.main()
