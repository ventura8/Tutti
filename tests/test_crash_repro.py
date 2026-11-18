import unittest
import tempfile
import os
from tutti import config

class CrashReproTest(unittest.TestCase):
    def test_load_crashes_on_no_config(self):
        with tempfile.TemporaryDirectory() as d:
            # Set XDG_CONFIG_HOME to an empty dir
            env = {'XDG_CONFIG_HOME': d}
            
            # Now find should return None
            with self.assertRaises(config.ConfigError):
                config.load(start=d, env=env)

if __name__ == '__main__':
    unittest.main()
