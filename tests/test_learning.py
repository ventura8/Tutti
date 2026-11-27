"""Learning from outcomes: the ladder steps ranked by measured success, with the rest as the backup chain."""
import unittest
from unittest import mock

from tutti import learning, routing


class RankTest(unittest.TestCase):
    def test_the_best_measured_step_leads_and_the_rest_follow(self):
        history = {'a': {'successes': 1, 'failures': 3}, 'b': {'successes': 3, 'failures': 1}}
        ranked = routing.rank_agents(history, ['a', 'b', 'c'])
        self.assertEqual([r['agent'] for r in ranked], ['b', 'c', 'a'])
        self.assertIn('75%', ranked[0]['rationale'])

    def test_no_history_keeps_the_ladder_order(self):
        self.assertEqual([r['agent'] for r in routing.rank_agents({}, ['x', 'y'])], ['x', 'y'])

    def test_invalid_old_counters_are_treated_as_unknown(self):
        ranked = routing.rank_agents({'x': {'successes': 'invalid'}, 'y': {'successes': 1}}, ['x', 'y'])
        self.assertEqual([r['agent'] for r in ranked], ['y', 'x'])
        self.assertEqual(ranked[1]['rationale'], 'No recorded outcomes')

    def test_similar_tasks_predict_the_better_agent_in_two_offline_scenarios(self):
        history = {
            'a': {'successes': 6, 'failures': 4, 'categories': {'docs:S': {'successes': 4}, 'fix:M': {'failures': 3}}},
            'b': {'successes': 4, 'failures': 6, 'categories': {'docs:S': {'failures': 4}, 'fix:M': {'successes': 3}}},
        }
        for category, winner in [('docs:S', 'a'), ('fix:M', 'b'), ('test:L', 'a')]:
            with self.subTest(category=category):
                ranked = routing.rank_agents(history, ['a', 'b'], category)
                self.assertEqual(ranked[0]['agent'], winner)


class RecordTest(unittest.TestCase):
    def test_outcomes_are_counted_and_an_empty_step_is_ignored(self):
        store = {}
        with mock.patch.object(learning, 'read_history', side_effect=lambda cfg: store), mock.patch.object(learning, 'path_of'), \
                mock.patch.object(learning.telemetry, 'write_json', side_effect=lambda path, data, **kw: store.update(data)):
            learning.record_outcome({}, 's', True, 'fix:S')
            learning.record_outcome({}, 's', False, 'docs:M')
            learning.record_outcome({}, '', False)
        self.assertEqual(store, {'s': {'successes': 1, 'failures': 1, 'categories': {
            'fix:S': {'successes': 1, 'failures': 0},
            'docs:M': {'successes': 0, 'failures': 1},
        }}})
