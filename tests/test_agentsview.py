"""The Agents tab (t252): one card per agent, roles only from the config (each with why), limits and live state from the
snapshot, every account shown once (on its worker's card, its weekly bounds included)."""
import json
import os
import shutil
import subprocess
import unittest

from tutti import agentsview, config, dashboard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def example():
    return config.load(os.path.join(ROOT, 'tutti.config.example.yaml'))


class Cards(unittest.TestCase):
    def test_roles_come_from_the_config_with_why(self):
        cards = {c['name']: c for c in agentsview.cards(example(), {})}
        roles = {r['role']: r['why'] for r in cards['cursor']['roles']}
        self.assertEqual(set(roles), {'main', 'worker', 'reviewer', 'chat'})
        self.assertEqual(roles['main'], 'main.fallback, number 3')
        self.assertEqual(cards['claude']['roles'][0]['why'], 'main.preferred')
        self.assertEqual([r['role'] for r in cards['grok']['roles']], ['scout'])

    def test_each_account_shows_once_on_its_workers_card(self):
        cards = agentsview.cards(example(), {})
        names = [c['name'] for c in cards]
        self.assertEqual(len(names), len(set(names)))
        agy = next(c for c in cards if c['name'] == 'agy')
        self.assertEqual(agy['account'], 'agy-claude, agy-gemini, agy-claude-weekly, agy-gemini-weekly')
        self.assertNotIn('agy-claude-weekly', names)
        self.assertNotIn('grok-bot', names)   # the scout's account is on the scout's card

    def test_live_state_limits_and_running_come_from_the_snapshot(self):
        snap = {'workers': {'items': [{'name': 'cursor', 'ready': False, 'note': 'cursor-agent is not installed'}]},
                'usage': {'accounts': [{'name': 'cursor', 'used': 61.4, 'verdict': 'ahead', 'reset': '2026-12-01', 'panic': False}]},
                'jobs': {'items': [{'status': 'running', 'worker': 'cursor'}, {'status': 'done', 'worker': 'cursor'},
                                   {'status': 'running', 'worker': None, 'account': None}]},
                'scouts': {'items': [{'name': 'grok', 'ready': False, 'missing': ['TUTTI_GROK_URL']}]}}
        cards = {c['name']: c for c in agentsview.cards(example(), snap)}
        self.assertEqual((cards['cursor']['state'], cards['cursor']['note']), ('not ready', 'cursor-agent is not installed'))
        self.assertEqual(cards['cursor']['limits'], ['61 % used (ahead)', 'resets 2026-12-01'])
        self.assertEqual(cards['cursor']['running'], 1)
        self.assertEqual((cards['grok']['state'], cards['grok']['note']), ('not ready', 'missing TUTTI_GROK_URL'))
        self.assertEqual(cards['codex']['state'], 'checking')

    def test_a_run_counts_once_and_roles_come_from_every_account(self):
        snap = {'jobs': {'items': [{'status': 'running', 'facts': {'worker': 'agy', 'account': 'agy-claude'}}]},
                'usage': {'accounts': [{'name': 'agy-claude', 'used': 10, 'reset': 'Mon 12:00 (UTC+2)'}]}}
        agy = next(c for c in agentsview.cards(example(), snap) if c['name'] == 'agy')
        self.assertEqual(agy['running'], 1)
        self.assertIn('agy-claude: resets Mon 12:00 (UTC+2)', agy['limits'])
        cfg = {'workers': {'w': {'kind': 'cli'}}, 'accounts': {'a1': {'worker': 'w'}, 'a2': {'worker': 'w'}},
               'autopilot': {'accounts': ['a2']}}
        self.assertIn('worker', [r['role'] for r in agentsview.cards(cfg, {})[0]['roles']])

    def test_cost_comes_from_the_config(self):
        cards = {c['name']: c for c in agentsview.cards(example(), {})}
        self.assertEqual(agentsview.cost({}, {'kind': 'cli', 'costs_money': True}, []), 'pays per use: kept off until enabled: true')
        self.assertTrue(cards['small']['cost'].startswith('electricity only'))
        self.assertEqual(agentsview.cost({}, {'kind': 'cli'}, []), '')   # nothing known: nothing claimed
        paid = {'accounts': {'a': {'costs_money': True, 'enabled': True}}}
        self.assertEqual(agentsview.cost(paid, {'kind': 'cli'}, ['a']), 'pays per use')
        self.assertTrue(agentsview.cost({}, {'kind': 'cli'}, ['a']).startswith('subscription'))

    def test_an_agent_the_config_gives_no_role_gets_none(self):
        cfg = {'workers': {'spare': {'kind': 'unknown'}}, 'accounts': {}}
        self.assertEqual(agentsview.cards(cfg, {})[0]['roles'], [])

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_card_shows_roles_with_why_capabilities_and_limits(self):
        with open(os.path.join(dashboard.WEB, 'agents.js'), encoding='utf8') as f:
            src = f.read()
        stub = ('function el(tag, attrs, ...kids) { return { tag, attrs: attrs || {}, kids: kids.flat().filter((k) => k !== null) }; }\n'
                'function chip(text) { return { tag: "span", attrs: { text }, kids: [] }; }\n'
                "function text(n) { return typeof n === 'string' ? n : n ? "
                "[(n.attrs || {}).text || '', ...(n.kids || []).map(text)].join(' ') : ''; }\n")
        card = agentsview.cards(example(), {})[0]
        out = json.loads(subprocess.run(['node', '-e', f'{stub}\n{src}\nconsole.log(JSON.stringify(text(agentCard({json.dumps(card)}))))'],
                                        capture_output=True, text=True, check=True).stdout)
        for part in ('Cost: subscription',
                     'cursor', 'checking', 'Roles', 'Main agent', 'main.fallback, number 3', 'Can', 'coding agent (CLI)'):
            self.assertIn(part, out)


if __name__ == '__main__':
    unittest.main()
