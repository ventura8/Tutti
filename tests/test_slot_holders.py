"""slot.sh names who holds each slot and flags a stuck holder; a waiting run says its place in the queue (map, 2026-11-23)."""
import os
import subprocess
import tempfile
import time
import unittest

SLOT = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'slot.sh')


class Holders(unittest.TestCase):
    def test_a_waiter_reports_its_place_and_a_stale_holder(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, 'SLOTS': '1', 'SLOT_LOCK_PREFIX': os.path.join(d, 's-'), 'MIN_FREE_GB': '0', 'SLOT_POLL': '0.2',
                   'THERMAL_GLOB': os.path.join(d, 'none'), 'NVIDIA_SMI': 'none', 'SLOT_REPORT_S': '0', 'SLOT_STALE_H': '0'}
            holder = subprocess.Popen(['bash', SLOT, 'sleep', '3'], env=env, stderr=subprocess.PIPE, text=True)
            for _ in range(50):
                if os.path.exists(os.path.join(d, 's-1.lock.who')):
                    break
                time.sleep(0.1)
            waiter = subprocess.run(['bash', SLOT, 'true'], env=env, capture_output=True, text=True, timeout=30)
            holder.wait(timeout=30)
            holder.stderr.close()
            self.assertEqual(waiter.returncode, 0)
            self.assertIn('place 1 in the queue', waiter.stderr)
            self.assertIn('past 0 h, likely stuck', waiter.stderr)
            self.assertIn('sleep 3', waiter.stderr)
            self.assertFalse([f for f in os.listdir(d) if f.startswith('s-wait.')])   # the waiter's file is gone


if __name__ == '__main__':
    unittest.main()
