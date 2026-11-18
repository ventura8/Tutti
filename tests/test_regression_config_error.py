import unittest
import tempfile
import os
from tutti import config

class TestConfigError(unittest.TestCase):
    def test_load_raises_config_error_on_invalid_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'tutti.config.yaml')
            with open(path, 'w', encoding='utf8') as f:
                # Invalid configuration: machines.a.slots must be > 0
                f.write('machines:\n  a:\n    transport: local\n    slots: 0\n')
            
            # The regression test: ensures that load raises ConfigError for an invalid config
            with self.assertRaises(config.ConfigError):
                config.load(path)

if __name__ == '__main__':
    unittest.main()
