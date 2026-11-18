import os
import tempfile
import unittest

from tutti import config, dashboard

class DashboardReloadTest(unittest.TestCase):
    def test_reload_handles_yaml_error(self):
        with tempfile.TemporaryDirectory() as d:
            # Create a valid config first
            cfg_path = os.path.join(d, 'tutti.config.yaml')
            with open(cfg_path, 'w', encoding='utf8') as f:
                f.write('machines: {}\n')

            # Load and create collector
            cfg = config.load(cfg_path)
            collector = dashboard.Collector(cfg)

            # Make the config invalid YAML
            with open(cfg_path, 'w', encoding='utf8') as f:
                f.write('machines: : : :')

            # This currently raises ScannerError which is not caught, causing a crash
            collector.reload()

            # Verify that error was caught and logged
            self.assertIsNotNone(collector.config_error)
            self.assertIn('the config changed and does not load', collector.config_error)
