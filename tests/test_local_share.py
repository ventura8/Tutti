import unittest
from unittest import mock

from tutti import autopilot, parallel


class LocalShareTest(unittest.TestCase):
    """Tutti's share of a computer someone uses: cores and load at the same share, so workers still get room."""

    def share(self, here, use_pct):
        with mock.patch('tutti.fleet.machine_of', return_value='laptop'):
            return autopilot.local_share({}, here, [{'name': 'laptop', 'use_pct': use_pct}])

    def test_a_busy_owner_machine_still_leaves_room_for_a_worker(self):
        here = self.share({'mem_gb': 20.0, 'cores': 16, 'load': 5.5}, 10)
        self.assertEqual(here['cores'], 1)
        self.assertAlmostEqual(here['load'], 5.5 / 16)
        lim = parallel.limit({}, 0, 5, here, None)
        self.assertGreaterEqual(lim['bounds']['cpu'], 1)

    def test_a_fully_loaded_machine_still_gives_no_room(self):
        here = self.share({'mem_gb': 20.0, 'cores': 16, 'load': 16.0}, 10)
        self.assertEqual(parallel.limit({}, 0, 5, here, None)['bounds']['cpu'], 0)

    def test_no_share_reading_keeps_the_machine_as_measured(self):
        here = {'mem_gb': 20.0, 'cores': 16, 'load': 5.5}
        with mock.patch('tutti.fleet.machine_of', return_value='laptop'):
            self.assertEqual(autopilot.local_share({}, here, []), here)


if __name__ == '__main__':
    unittest.main()
