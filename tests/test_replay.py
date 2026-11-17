"""A future stable owner can replay a bounded event history without silently skipping gaps."""
import threading
import unittest

from tutti import replay


class ReplayTests(unittest.TestCase):
    def test_reconnect_replays_only_missing_events_and_copies_payloads(self):
        log = replay.ReplayLog(3)
        event = {'text': ['a']}
        self.assertEqual(log.append(event), 1)
        event['text'].append('changed')
        self.assertEqual(log.append({'text': 'b'}), 2)
        events, closed = log.read(log.generation, 0)
        self.assertFalse(closed)
        self.assertEqual(events, [(1, {'text': ['a']}), (2, {'text': 'b'})])
        events[0][1]['text'].append('mutated')
        self.assertEqual(log.read(log.generation, 0)[0][0][1], {'text': ['a']})
        self.assertEqual(log.read(log.generation, 1)[0], [(2, {'text': 'b'})])

    def test_overflow_refuses_silent_gap_and_rejects_invalid_cursor(self):
        log = replay.ReplayLog(2)
        for text in ('a', 'b', 'c'):
            log.append(text)
        with self.assertRaises(replay.ReplayGap):
            log.read(log.generation, 0)
        self.assertEqual(log.read(log.generation, 1)[0], [(2, 'b'), (3, 'c')])
        for cursor in (-1, 4, True, '1'):
            with self.assertRaises(ValueError):
                log.read(log.generation, cursor)
        for limit in (0, -1, True, '2'):
            with self.assertRaises(ValueError):
                replay.ReplayLog(limit)

    def test_followers_wake_on_event_and_close(self):
        log = replay.ReplayLog()
        ready, result = threading.Event(), []
        def follow():
            ready.set()
            result.append(log.read(log.generation, 0, timeout=5))
        thread = threading.Thread(target=follow, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(5))
        log.append('delta')
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [([(1, 'delta')], False)])
        log.close()
        self.assertEqual(log.read(log.generation, 1, timeout=5), ([], True))
        with self.assertRaises(ValueError):
            log.append('too late')

    def test_foreign_generation_and_invalid_timeouts_are_rejected(self):
        first, second = replay.ReplayLog(), replay.ReplayLog()
        first.append('one')
        second.append('other')
        with self.assertRaisesRegex(ValueError, 'owner has changed'):
            second.read(first.generation, 1)
        for timeout in (-1, float('inf'), float('nan'), True, '1', threading.TIMEOUT_MAX * 2):
            with self.assertRaises(ValueError):
                first.read(first.generation, 0, timeout=timeout)

    def test_timeout_is_empty_and_does_not_close(self):
        log = replay.ReplayLog()
        self.assertEqual(log.read(log.generation, 0, timeout=0), ([], False))
        self.assertEqual(log.append('later'), 1)
