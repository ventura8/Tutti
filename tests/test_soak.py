import os
import time
import unittest

import psutil


# Mocking the functions to make it run without real network/ssh/etc.
def mock_cfg():
    return {
        'dashboard': {'probe_seconds': 0.01},  # Faster for simulation
        'accounts': [],
        'machines': {},
    }


class TestSoak(unittest.TestCase):
    def test_soak(self):
        if not os.environ.get('TUTTI_SOAK_TEST'):
            self.skipTest('TUTTI_SOAK_TEST not set')

        from tutti import dashboard

        print("Starting soak test (this will take 2 hours)...")

        cfg = mock_cfg()
        c = dashboard.Collector(cfg, probe_fn=lambda *a, **kw: {}, available_fn=lambda *a, **kw: [])

        start_time = time.time()
        process = psutil.Process(os.getpid())

        initial_memory = process.memory_info().rss
        initial_fds = process.num_fds()

        probes = 0
        while time.time() - start_time < 2 * 3600:
            c.refresh()
            probes += 1
            if probes % 1000 == 0:
                current_memory = process.memory_info().rss
                current_fds = process.num_fds()
                print(f"Probe {probes}: RSS={current_memory}, FDs={current_fds}")

            time.sleep(0.01)  # Simulated probe interval

        # Memory growth check: allow some growth but check for massive leaks
        final_memory = process.memory_info().rss
        final_fds = process.num_fds()

        # Leaks check
        self.assertLess(final_fds, initial_fds + 10, "File descriptor leak detected!")
        # Memory growth check: very rough heuristic
        self.assertLess(final_memory, initial_memory * 2, "Memory growth too high!")

        print(f"Soak test completed. Total probes: {probes}")
