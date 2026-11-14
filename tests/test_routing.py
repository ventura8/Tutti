import unittest

from tutti import routing
from tutti.routing import Probe

MACHINES = {'builder': {}, 'shared': {'max_load_ratio': 0.5}}
ORDER = ['builder', 'shared']


def probes(**by_name):
    asked = []

    def fn(name):
        asked.append(name)
        return by_name[name]
    return fn, asked


class RoutingTest(unittest.TestCase):
    def test_first_machine_with_a_free_slot_wins_and_the_next_is_not_asked(self):
        fn, asked = probes(builder=Probe(True, 2, 1.0, 16), shared=Probe(True, 2, 0.0, 16))
        c = routing.choose(ORDER, MACHINES, fn)
        self.assertEqual((c.machine, c.queued, asked), ('builder', False, ['builder']))

    def test_falls_over_when_the_first_is_full(self):
        fn, _ = probes(builder=Probe(True, 0, 9.0, 16), shared=Probe(True, 1, 2.0, 16))
        c = routing.choose(ORDER, MACHINES, fn)
        self.assertEqual((c.machine, c.queued), ('shared', False))
        self.assertIn('builder: no free slot', c.reason)

    def test_queues_on_the_first_when_the_second_is_too_loaded(self):
        fn, _ = probes(builder=Probe(True, 0, 9.0, 16), shared=Probe(True, 2, 8.0, 16))
        c = routing.choose(ORDER, MACHINES, fn)
        self.assertEqual((c.machine, c.queued), ('builder', True))
        self.assertIn('shared: load 8 is over 0.5 x 16 cores', c.reason)

    def test_a_raised_yield_flag_keeps_work_off_a_machine(self):
        fn, _ = probes(builder=Probe(True, 0, 1.0, 8), shared=Probe(True, 2, 0.0, 16, yielding=True))
        self.assertEqual(routing.choose(ORDER, MACHINES, fn).machine, 'builder')

    def test_queue_avoids_a_yielding_or_unreachable_first_machine(self):
        fn, _ = probes(builder=Probe(True, 3, 0.0, 8, yielding=True), shared=Probe(True, 0, 0.0, 16))
        self.assertEqual(routing.choose(ORDER, MACHINES, fn).machine, 'shared')
        fn, _ = probes(builder=Probe(), shared=Probe(True, 0, 0.0, 16))
        self.assertEqual(routing.choose(ORDER, MACHINES, fn).machine, 'shared')

    def test_everything_down_still_names_the_first_machine(self):
        fn, _ = probes(builder=Probe(), shared=Probe())
        c = routing.choose(ORDER, MACHINES, fn)
        self.assertEqual((c.machine, c.queued), ('builder', True))

    def test_empty_order_is_an_error(self):
        with self.assertRaises(ValueError):
            routing.choose([], MACHINES, lambda n: Probe())

    def test_parse_probe(self):
        self.assertEqual(routing.parse_probe('2 0.53 16 0'), Probe(True, 2, 0.53, 16, False))
        self.assertEqual(routing.parse_probe('2 0.5 8 0 45000 - 7.3').mem_gb, 7.3)   # free memory, GB
        self.assertTrue(routing.parse_probe('0 12 8 1').yielding)
        for junk in ('', 'ssh: connect timed out', '2 x 16 0'):
            self.assertFalse(routing.parse_probe(junk).reachable)


if __name__ == '__main__':
    unittest.main()
