"""A future supervisor can quiesce registered work without stopping it."""
import threading
import unittest

from tutti import drain


class GateTests(unittest.TestCase):
    def test_active_work_drains_and_new_work_is_refused(self):
        gate = drain.Gate()
        entered, release = threading.Event(), threading.Event()
        def work():
            with gate.admit():
                entered.set()
                release.wait(5)
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            token = gate.begin()
            self.assertFalse(gate.wait(token, 0))
            with self.assertRaises(drain.Draining), gate.admit():
                self.fail('draining gate admitted new work')
            release.set()
            self.assertTrue(gate.wait(token, 5))
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(gate.cancel(token))
        with gate.admit():
            pass

    def test_cancel_wakes_waiter_and_old_token_cannot_cancel_new_drain(self):
        gate, ready = drain.Gate(), threading.Event()
        results = []
        with gate.admit():
            first = gate.begin()
            def wait():
                ready.set()
                results.append(gate.wait(first, 5))
            waiter = threading.Thread(target=wait, daemon=True)
            waiter.start()
            self.assertTrue(ready.wait(5))
            self.assertTrue(gate.cancel(first))
            second = gate.begin()
            self.assertFalse(gate.cancel(first))
            waiter.join(5)
            self.assertEqual(results, [False])
            self.assertFalse(gate.wait(second, 0))
        self.assertTrue(gate.wait(second, 0))
        self.assertTrue(gate.cancel(second))

    def test_token_from_another_gate_cannot_cancel_or_wait_for_this_gate(self):
        first, second = drain.Gate(), drain.Gate()
        token1, token2 = first.begin(), second.begin()
        self.assertFalse(second.wait(token1, 0))
        self.assertFalse(second.cancel(token1))
        self.assertTrue(first.cancel(token1))
        self.assertTrue(second.cancel(token2))

    def test_wait_timeout_does_not_reopen_admission_or_stop_work(self):
        gate = drain.Gate()
        with gate.admit():
            token = gate.begin()
            self.assertFalse(gate.wait(token, 0.001))
            with self.assertRaises(drain.Draining), gate.admit():
                self.fail('timed-out drain reopened admission')
        self.assertTrue(gate.wait(token, 0))
        self.assertTrue(gate.cancel(token))

    def test_exception_releases_work_and_duplicate_begin_is_rejected(self):
        gate = drain.Gate()
        with self.assertRaises(ValueError), gate.admit():
            raise ValueError('work failed')
        token = gate.begin()
        self.assertTrue(gate.wait(token, 0))
        with self.assertRaises(drain.Draining):
            gate.begin()
        self.assertTrue(gate.cancel(token))
