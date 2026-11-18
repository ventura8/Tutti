import unittest
from tutti import config

class TestConfigError(unittest.TestCase):
    def test_load_raises_config_error_on_missing_file(self):
        # The regression test: ensures that load raises ConfigError for a missing file
        # Currently, it raises FileNotFoundError, which crashes tutti.
        with self.assertRaises(config.ConfigError):
            config.load('non-existent.yaml')

if __name__ == '__main__':
    unittest.main()
