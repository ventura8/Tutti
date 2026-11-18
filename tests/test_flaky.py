"""The flaky marker: one re-run on AssertionError; a second failure, or another exception, still fails."""
import unittest

from tests.flaky import flaky


class FlakyMarkerTest(unittest.TestCase):
    def test_a_single_assertion_error_is_retried_and_passes(self):
        hits = []

        @flaky
        def once(self):
            hits.append(1)
            if len(hits) == 1:
                raise AssertionError('transient')

        once(None)
        self.assertEqual(hits, [1, 1])
        self.assertTrue(once.flaky)

    def test_a_second_assertion_error_still_fails(self):
        @flaky
        def always(_self):
            raise AssertionError('still broken')

        with self.assertRaises(AssertionError):
            always(None)

    def test_a_non_assertion_error_is_not_retried(self):
        hits = []

        @flaky
        def boom(_self):
            hits.append(1)
            raise ValueError('not an assertion')

        with self.assertRaises(ValueError):
            boom(None)
        self.assertEqual(hits, [1])
