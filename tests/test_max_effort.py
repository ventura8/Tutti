"""The highest effort a plan should use (accounts.<n>.max_effort): whatever the pace asks (the strong tier when behind),
a run never goes above it; a model's -xhigh suffix and an --effort flag are both lowered; a wrong value names the fix."""
import unittest

from tutti import tiers

CFG = {'accounts': {'agy-gemini': {'args': ['--model', 'gemini-4-neon-high'], 'subscription': 'AI Pro', 'max_effort': 'low',
                                   'tiers': {'strong': {'args': ['--model', 'gemini-4-neon-xhigh'], 'sizes': ['L']}}},
                    'claude': {'args': ['--effort', 'max']}}}


class Cap(unittest.TestCase):
    def test_a_behind_account_still_stays_under_its_plans_cap(self):
        c = tiers.choose(CFG, 'agy-gemini', {'name': 'agy-gemini', 'verdict': 'behind'})
        self.assertEqual((c['model'], c['effort'], c['tier']), ('gemini-4-neon-low', 'low', 'strong'))
        self.assertIn('effort capped at low by the AI Pro plan', c['reason'])
        self.assertEqual(c['plan'], 'AI Pro')

    def test_no_cap_leaves_the_effort_and_a_flag_is_capped_too(self):
        c = tiers.choose(CFG, 'claude', {'name': 'claude', 'verdict': 'on pace'})
        self.assertEqual(c['effort'], 'max')   # no max_effort: unchanged
        self.assertEqual(tiers.cap_effort(['--effort', 'xhigh'], 'medium')[0], ['--effort', 'medium'])
        self.assertEqual(tiers.cap_effort(['--model', 'm-low'], 'high'), (['--model', 'm-low'], 'low', False))

    def test_a_wrong_cap_names_the_fix(self):
        errs = tiers.errors({'accounts': {'a': {'max_effort': 'turbo'}}})
        self.assertTrue(any('accounts.a.max_effort' in e and 'low, medium' in e for e in errs))
        self.assertEqual(tiers.errors({'accounts': {'a': {'max_effort': 'low'}}}), [])


if __name__ == '__main__':
    unittest.main()
