"""Tests for per-user schedule changes in `tutti/myschedule.py`."""
import unittest

import tmpdirs
from tutti import myschedule, owner

SUB = '100000000000000000001'


class MyScheduleTest(unittest.TestCase):
    def setUp(self):
        self.folder = tmpdirs.mkdtemp()

    def test_change_valid_schedule_persists_for_user(self):
        payload = {'mode': 'active', 'days': ['mon', 'tue'], 'window': '02-10', 'hours': 6}
        status, answer = myschedule.change(self.folder, SUB, payload)
        self.assertEqual(status, 200)
        self.assertTrue(answer.get('ok'))
        self.assertEqual(answer.get('schedule'), payload)
        self.assertEqual(myschedule.read(self.folder, SUB), payload)

    def test_change_rejects_invalid_mode(self):
        status, answer = myschedule.change(self.folder, SUB, {'mode': 'invalid'})
        self.assertEqual(status, 400)
        self.assertIn('my mode is', answer.get('error', ''))

    def test_change_rejects_empty_payload(self):
        status, answer = myschedule.change(self.folder, SUB, {})
        self.assertEqual(status, 400)
        self.assertIn('send', answer.get('error', ''))

    def test_change_none_resets_to_default(self):
        myschedule.change(self.folder, SUB, {'hours': 6})
        status, answer = myschedule.change(self.folder, SUB, {'hours': None})
        self.assertEqual(status, 200)
        self.assertEqual(answer['schedule']['hours'], owner.DEFAULT['hours'])
