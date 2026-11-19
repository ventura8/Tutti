"""Chat token efficiency (tutti/chateffic.py): lazy tools, minimal prompts, desktop benchmark regression, power line."""
import unittest

from tutti import chateffic


class StepTools(unittest.TestCase):
    def test_step_kind_and_tool_sets(self):
        self.assertEqual(chateffic.step_kind('How much quota is left?'), 'status')
        self.assertEqual(chateffic.step_kind("What's running?"), 'read')
        self.assertEqual(chateffic.step_kind('add task in map: fix legend'), 'work')
        self.assertEqual(chateffic.step_kind('write a unit test for the legend'), 'code')
        self.assertEqual(chateffic.step_kind('hello'), 'general')
        self.assertEqual(chateffic.tools_for_step('status'), frozenset({'usage', 'machines', 'job_status'}))
        self.assertTrue(chateffic.tools_for_step('work') >= chateffic.READ_TOOLS)
        self.assertIn('delegate_task', chateffic.tools_for_step('code'))

    def test_lazy_tools_filters_and_skips_missing(self):
        all_tools = {n: {'schema': {'type': 'object'}, 'description': n} for n in ('usage', 'machines', 'backlog_add')}
        got = chateffic.lazy_tools(all_tools, 'status')
        self.assertEqual(set(got), {'usage', 'machines'})
        self.assertEqual(len(chateffic.ollama_tools_lazy(all_tools, 'status')), 2)


class PromptAndBenchmark(unittest.TestCase):
    def test_minimal_system_omits_schemas_when_native(self):
        native = chateffic.minimal_system('status', who='a', role='admin', projects='map', scouts='none',
                                          machines='nuc', devices='none', facts='ok', native_tools=True)
        self.assertIn('Tools: ', native)
        self.assertNotIn('ACTION', native)
        self.assertNotIn('"type": "object"', native)
        text = chateffic.minimal_system('status', who='a', role='admin', projects='map', scouts='none',
                                        machines='nuc', devices='none', facts='ok', native_tools=False,
                                        tools_dict={'usage': {'type': 'object'}})
        self.assertIn('ACTION', text)
        self.assertIn('"usage"', text)

    def test_benchmark_beats_desktop_as_regression(self):
        r = chateffic.run_benchmark()
        self.assertTrue(r['beats_desktop'], r)
        self.assertGreater(len(r['tasks']), 0)
        for row in r['tasks']:
            self.assertTrue(row['win'], row)
            self.assertLess(row['tutti_tokens'], row['desktop']['claude-desktop'])
            self.assertLess(row['tutti_tokens'], row['desktop']['cursor'])
        self.assertLess(r['tutti_total'], r['desktop_total']['claude-desktop'])
        self.assertIn('Tutti', chateffic.benchmark_banner(r))


class PowerHistoryRoute(unittest.TestCase):
    def test_message_power_local_and_cli(self):
        local = chateffic.message_power('ollama', 120, 300)
        self.assertEqual(local['watts'], 300.0)
        self.assertEqual(local['wh'], 10.0)
        cloud = chateffic.message_power('cli', 60, 300)
        self.assertEqual(cloud['watts'], 0.0)
        self.assertEqual(cloud['wh'], 0.0)

    def test_fresh_history_and_route_order(self):
        hist = [{'id': i} for i in range(10)]
        self.assertEqual([m['id'] for m in chateffic.fresh_history(hist, 6)], list(range(4, 10)))
        self.assertEqual(chateffic.step_route_order()[0], 'ollama')
        self.assertLess(chateffic.cli_route_rank('agy-gemini'), chateffic.cli_route_rank('cursor'))
        self.assertLess(chateffic.cli_route_rank('cursor'), chateffic.cli_route_rank('claude'))

    def test_format_message_usage(self):
        line = chateffic.format_message_usage({'in': 120, 'out': 40, 'usd': 0, 'watts': 12, 'wh': 0.002})
        self.assertIn('120 in', line)
        self.assertIn('12 W', line)
        self.assertIn('0.002 Wh', line)
        self.assertIn('local: no cost', line)


if __name__ == '__main__':
    unittest.main()
