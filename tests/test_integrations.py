import contextlib
import json
import os
import tempfile
import unittest
from unittest import mock

from tutti import backlog, config, integrations, needs


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _cfg(state_dir, linear=True, slack=True, discord=True):
    cfg = config.merge(config.DEFAULTS, {'project': {'state': state_dir}})
    cfg['integrations'] = {
        'linear': {'enabled': linear, 'team_id': 'team-example'},
        'slack': {'enabled': slack, 'channel': '#tutti-example'},
        'discord': {'enabled': discord},
    }
    return cfg


@contextlib.contextmanager
def _mock_http(xdg_dir):
    calls = []

    def fake_open(req, timeout=5):
        calls.append((req.full_url, json.loads(req.data.decode('utf-8'))))
        return _FakeResponse()

    env = {
        'XDG_CONFIG_HOME': xdg_dir,
        'LINEAR_API_KEY': 'lin-example',
        'SLACK_TOKEN': 'xoxb-example',
        'DISCORD_WEBHOOK_URL': 'https://discord.example/webhook',
    }
    with mock.patch.dict(os.environ, env), mock.patch('urllib.request.urlopen', side_effect=fake_open):
        yield calls


class IntegrationsTest(unittest.TestCase):
    def test_task_created_notifies_all_enabled(self):
        with tempfile.TemporaryDirectory() as tmp, _mock_http(tmp) as calls:
            cfg = _cfg(tmp)
            data = backlog.load(cfg)
            task, added = backlog.add(data, backlog.new_task('app', 'Add health check'), cfg=cfg)
            self.assertTrue(added)
            urls = [url for url, _ in calls]
            self.assertIn('https://slack.com/api/chat.postMessage', urls)
            self.assertIn('https://discord.example/webhook', urls)
            self.assertIn('https://api.linear.app/graphql', urls)
            linear_body = next(body for url, body in calls if 'linear' in url)
            self.assertEqual(linear_body['variables']['title'], task['title'])

    def test_disabled_integrations_send_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, _mock_http(tmp) as calls:
            cfg = _cfg(tmp, linear=False, slack=False, discord=False)
            integrations.notify(cfg, 'Update')
            integrations.needs_attention(cfg, ['q1: Pick one'])
            self.assertEqual(calls, [])

    def test_needs_attention_posts_to_slack_and_discord(self):
        with tempfile.TemporaryDirectory() as tmp, _mock_http(tmp) as calls:
            cfg = _cfg(tmp, linear=False)
            integrations.needs_attention(cfg, ['q1: Deploy now?'])
            self.assertEqual(len(calls), 2)
            self.assertIn('Needs attention:', calls[0][1]['text'])
            self.assertIn('q1: Deploy now?', calls[1][1]['content'])

    def test_ask_owner_command_answers_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp, _mock_http(tmp) as calls:
            cfg = _cfg(tmp, linear=False)
            with backlog.locked(cfg) as data:
                task, _ = backlog.add(data, backlog.new_task('app', 'Ship release'))
                task['status'] = 'needs-person'
            q = needs.ask(cfg, task['id'], 'Proceed?', 'needs sign-off', ['yes', 'drop'], 'yes')
            with backlog.locked(cfg) as data:
                backlog.find(data, task['id'])['question'] = q['id']
            reply = integrations.handle_command(cfg, f'!ask_owner {q["id"]} yes')
            self.assertIn('answered: yes', reply)
            self.assertEqual(len(calls), 2)
            self.assertIn('Answer: yes', calls[0][1]['text'])
            self.assertIn('Answer: yes', calls[1][1]['content'])

    def test_ask_owner_command_usage_and_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp, linear=False, slack=False, discord=False)
            self.assertIn('Usage:', integrations.handle_command(cfg, '!ask_owner'))
            self.assertEqual(integrations.handle_command(cfg, '!nope'), 'Unknown command')
