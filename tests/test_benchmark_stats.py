import unittest
import json
import os
import tempfile
from tutti import power

class TestBenchmarkStats(unittest.TestCase):
    def test_save_one_calculates_median_and_spread(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {'project': {'state': d}}
            name = 'pc'
            bench = {'cores': 8, 'threads': 16, 'mem_gb': 16, 'cpu_mbs': 2000, 'disk_mbs': 2000, 'vram_gb': 8, 'npu': False}
            
            # Save 3 times
            for i in range(3):
                power.save_one(cfg, name, {**bench, 'cpu_mbs': 2000 + i * 100}, 1000 + i)
                
            data = power.read(cfg)
            entry = data[name]
            self.assertIn('spread', entry)
            self.assertEqual(entry['cpu_mbs'], 2100) # Median of 2000, 2100, 2200
            self.assertEqual(entry['spread']['cpu_mbs'], 200) # 2200 - 2000

