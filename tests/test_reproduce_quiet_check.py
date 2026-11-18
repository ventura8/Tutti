import os
import tempfile
import unittest

from tutti import config, ship


class ReproduceFileNotFound(unittest.TestCase):
    def test_quiet_check_with_non_existent_folder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a minimal config
            cfg = config.normalise({'project': {'state': os.path.join(tmpdir, 'state')}})
            top = tmpdir
            check = {'run': 'echo "hello"', 'verdict': 'hello'}
            env = {}
            track = 'test'
            # Non-existent folder
            non_existent_folder = os.path.join(tmpdir, 'does_not_exist')
            rc, where, log, verdict = ship.quiet_check(cfg, top, check, env, track, folder=non_existent_folder)
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(log))


if __name__ == '__main__':
    unittest.main()
