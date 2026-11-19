import unittest
from unittest import mock

from tutti import autopilot, parallel


class LocalShareTest(unittest.TestCase):
    """Tutti's share of a computer someone uses: its cores, and the load left on them after the owner's cores."""

    def share(self, here, use_pct):
        with mock.patch('tutti.fleet.machine_of', return_value='laptop'):
            return autopilot.local_share({}, here, [{'name': 'laptop', 'use_pct': use_pct}])

    def test_an_owner_within_their_cores_leaves_tuttis_core_free(self):
        here = self.share({'mem_gb': 20.0, 'cores': 16, 'load': 5.5}, 10)
        self.assertEqual(here['cores'], 1)
        self.assertEqual(here['load'], 0.0)
        # one free core at run_cpu 0.25: four runs
        self.assertEqual(parallel.limit({}, 0, 50, here, None)['bounds']['cpu'], 4)

    def test_an_owner_using_the_whole_machine_leaves_no_room(self):
        here = self.share({'mem_gb': 20.0, 'cores': 16, 'load': 16.0}, 10)
        self.assertEqual(here['load'], 1.0)
        self.assertEqual(parallel.limit({}, 0, 50, here, None)['bounds']['cpu'], 0)

    def test_no_load_reading_adds_no_load_key(self):
        here = self.share({'mem_gb': 20.0, 'cores': 16}, 10)
        self.assertNotIn('load', here)

    def test_no_share_reading_keeps_the_machine_as_measured(self):
        here = {'mem_gb': 20.0, 'cores': 16, 'load': 5.5}
        with mock.patch('tutti.fleet.machine_of', return_value='laptop'):
            self.assertEqual(autopilot.local_share({}, here, []), here)


if __name__ == '__main__':
    unittest.main()
