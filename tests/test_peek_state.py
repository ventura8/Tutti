"""The autopilot state read once per change for read-only callers (the state snapshot parsed it once per queued task)."""
import os
import tempfile
import time
import unittest

from tutti import autopilot


class Peek(unittest.TestCase):
    def test_parsed_once_per_change_and_a_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {'project': {'state': d}}
            self.assertEqual(autopilot.peek_state(cfg), {})
            autopilot.write_state(cfg, {'allow': {'a': 1}})
            first = autopilot.peek_state(cfg)
            self.assertIs(autopilot.peek_state(cfg), first)   # unchanged file: the same parse
            autopilot.write_state(cfg, {'allow': {'b': 2}})
            os.utime(autopilot.state_path(cfg), ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
            self.assertEqual(autopilot.peek_state(cfg), {'allow': {'b': 2}})


if __name__ == '__main__':
    unittest.main()
