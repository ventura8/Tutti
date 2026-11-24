"""Computer-use tasks (a browser, forms, a desktop UI) try the accounts strong at it first (`strengths: [computer-use]`;
the newest ChatGPT models lead at it); every other task keeps the ladder's order."""
import unittest

from tutti import autopilot

CFG = {'accounts': {'agy-gemini': {}, 'codex': {'strengths': ['computer-use']}, 'claude': {}}}


class Strength(unittest.TestCase):
    def test_kind_or_title_marks_a_computer_use_task(self):
        self.assertTrue(autopilot.computer_use({'title': 'Fill in the signup form on the website'}))
        self.assertTrue(autopilot.computer_use({'kind': 'computer-use', 'title': 'x'}))
        self.assertFalse(autopilot.computer_use({'title': 'Add unit tests for tutti/web/sw.js'}))

    def test_strong_accounts_go_first_only_for_such_tasks(self):
        steps = ['agy-gemini', 'claude', 'codex']
        self.assertEqual(autopilot.by_strength(CFG, {'title': 'Click through the browser checkout'}, steps),
                         ['codex', 'agy-gemini', 'claude'])
        self.assertEqual(autopilot.by_strength(CFG, {'title': 'Refactor the parser'}, steps), steps)
        self.assertEqual(autopilot.by_strength({'accounts': {}}, {'title': 'Use the browser'}, steps), steps)


if __name__ == '__main__':
    unittest.main()
