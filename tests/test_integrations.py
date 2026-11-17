"""Linear, Slack and Discord (docs/INTEGRATIONS.md): Linear issues with the label become tasks of the mapped repo, a task's
progress goes back (state, comment, pull request), an issue closed in Linear closes its idle task, a refused key says what
to do; Slack and Discord get deduped, rate-limited, scrubbed messages from the shared event hook; the setup writes the
secret with mode 600 and never prints it. Every HTTP call is a fake; nothing leaves the temp folder."""
import hashlib
import hmac
import io
import json
import os
import stat
import tempfile
import unittest
from types import SimpleNamespace

from tutti import backlog, events, integrationscmd, linear, secretenv

T0 = 1_800_000_000.0
KEY = 'lin_api_' + 'x' * 20
SLACK = 'https://hooks.slack.com/services/T000/B000/' + 'y' * 12
DISCORD = 'https://discord.com/api/webhooks/1/' + 'z' * 12


def issue(n, team='ENG', state='unstarted', project=None):
    return {'id': f'id{n}', 'identifier': f'{team}-{n}', 'title': f'Fix thing {n}', 'description': 'details',
            'url': f'https://linear.example/i/{team}-{n}', 'team': {'id': f'team-{team}', 'key': team},
            'project': {'name': project} if project else None, 'state': {'type': state}}


STATES = [{'id': 's-todo', 'name': 'Todo', 'type': 'unstarted', 'position': 1},
          {'id': 's-prog', 'name': 'In Progress', 'type': 'started', 'position': 2},
          {'id': 's-rev', 'name': 'In Review', 'type': 'started', 'position': 3},
          {'id': 's-done', 'name': 'Done', 'type': 'completed', 'position': 4},
          {'id': 's-cxl', 'name': 'Canceled', 'type': 'canceled', 'position': 5}]


class FakeLinear:
    """Answers the GraphQL documents Tutti sends; records every call."""

    def __init__(self, open_issues=(), by_id=(), status=200):
        self.open, self.by_id, self.status, self.calls = list(open_issues), {i['id']: i for i in by_id}, status, []

    def __call__(self, key, body):
        self.calls.append((key, body))
        if self.status != 200:
            return self.status, b'{}'
        q, v = body['query'], body['variables']
        if 'viewer' in q:
            data = {'viewer': {'id': 'u1'}}
        elif 'workflowStates' in q:
            data = {'workflowStates': {'nodes': STATES}}
        elif 'issueUpdate' in q or 'commentCreate' in q or 'attachmentLinkURL' in q:
            data = {'ok': {'success': True}}
        elif '$ids' in q:
            data = {'issues': {'nodes': [self.by_id[i] for i in v['ids'] if i in self.by_id]}}
        else:
            data = {'issues': {'nodes': self.open}}
        return 200, json.dumps({'data': data}).encode()

    def sent(self, word):
        return [b['variables'] for _, b in self.calls if word in b['query']]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {'XDG_CONFIG_HOME': os.path.join(self.tmp.name, 'xdg')}
        self.cfg = {'project': {'state': os.path.join(self.tmp.name, 'state')}, 'autopilot': {'repos': {'app': {}, 'web': {}}},
                    'integrations': {'dashboard_url': 'https://tutti.example',
                                     'linear': {'enabled': True, 'teams': {'ENG': 'app'}, 'projects': {'Site': 'web'}},
                                     'slack': {'enabled': True}, 'discord': {'enabled': False}}}
        os.makedirs(self.cfg['project']['state'])

    def tasks(self):
        return backlog.load(self.cfg)['tasks']


class LinearTests(Base):
    def test_imports_labelled_issues_into_the_mapped_repo_once(self):
        fake = FakeLinear([issue(1), issue(2, project='Site'), issue(3, team='OPS')])
        rep = linear.sync(self.cfg, linear.Client(KEY, fake))
        self.assertEqual(len(rep['imported']), 2)
        repos = {t['title']: t['repo'] for t in self.tasks()}
        self.assertEqual(repos, {'ENG-1 Fix thing 1': 'app', 'ENG-2 Fix thing 2': 'web'})   # OPS has no repo: skipped
        self.assertEqual(fake.sent('issues(first')[0]['label'], 'tutti')
        linear.sync(self.cfg, linear.Client(KEY, fake))
        self.assertEqual(len(self.tasks()), 2)   # not imported twice

    def test_progress_goes_back_with_state_comment_and_pr(self):
        fake = FakeLinear([issue(1)])
        linear.sync(self.cfg, linear.Client(KEY, fake))
        with backlog.locked(self.cfg) as data:
            data['tasks'][0].update(status='review', pr='https://github.example/o/app/pull/7')
        fake.calls.clear()
        rep = linear.sync(self.cfg, linear.Client(KEY, fake))
        self.assertEqual(rep['pushed'], ['t1'])
        self.assertEqual(fake.sent('issueUpdate'), [{'id': 'id1', 'state': 's-prog'}])
        self.assertIn('waiting for review', fake.sent('commentCreate')[0]['body'])
        self.assertEqual(fake.sent('attachmentLinkURL'), [{'id': 'id1', 'url': 'https://github.example/o/app/pull/7'}])
        fake.calls.clear()
        linear.sync(self.cfg, linear.Client(KEY, fake))
        self.assertEqual(fake.sent('issueUpdate'), [])   # nothing changed: nothing sent

    def test_states_by_name_override_the_type(self):
        s = {**linear.DEFAULTS, 'states': {'review': 'In Review'}}
        self.assertEqual(linear.pick_state(STATES, 'review', s), 's-rev')
        self.assertEqual(linear.pick_state(STATES, 'done', s), 's-done')
        self.assertIsNone(linear.pick_state([], 'done', s))

    def test_closed_in_linear_closes_an_idle_task_not_a_running_one(self):
        fake = FakeLinear([issue(1), issue(2)])
        linear.sync(self.cfg, linear.Client(KEY, fake))
        with backlog.locked(self.cfg) as data:
            data['tasks'][1]['status'] = 'running'
        fake.open, fake.by_id = [], {'id1': issue(1, state='canceled'), 'id2': issue(2, state='completed')}
        rep = linear.sync(self.cfg, linear.Client(KEY, fake))
        self.assertEqual(rep['closed'], ['t1'])
        self.assertEqual([t['status'] for t in self.tasks()], ['dropped', 'running'])

    def test_a_refused_key_says_what_to_do_and_never_shows_the_key(self):
        rep = linear.tick(self.cfg, now=T0, client=linear.Client(KEY, FakeLinear(status=401)))
        self.assertIn('setup', rep['error'])
        self.assertNotIn(KEY, json.dumps(linear.read_state(self.cfg)))
        with self.assertRaises(linear.LinearError):
            linear.Client('', FakeLinear())

    def test_tick_waits_for_poll_minutes_unless_woken(self):
        fake = FakeLinear()
        linear.tick(self.cfg, now=T0, client=linear.Client(KEY, fake))
        self.assertIsNone(linear.tick(self.cfg, now=T0 + 60, client=linear.Client(KEY, fake)))
        linear.wake(self.cfg)
        self.assertIsNotNone(linear.tick(self.cfg, now=T0 + 61, client=linear.Client(KEY, fake)))

    def test_config_errors(self):
        self.assertEqual(linear.errors(self.cfg), [])
        bad = {**self.cfg, 'integrations': {'linear': {'enabled': True, 'teams': {}, 'poll_minutes': 0}}}
        self.assertEqual(len(linear.errors(bad)), 2)
        wrong = {**self.cfg, 'integrations': {'linear': {'teams': {'ENG': 'nope'}}}}
        self.assertIn('autopilot.repos', linear.errors(wrong)[0])

    def test_webhook_signature(self):
        body = b'{"type":"Issue"}'
        sig = hmac.new(b'shh', body, hashlib.sha256).hexdigest()
        self.assertTrue(linear.verify_webhook('shh', body, sig))
        self.assertFalse(linear.verify_webhook('shh', body + b' ', sig))
        self.assertFalse(linear.verify_webhook('', body, sig))


class ChannelTests(Base):
    def setUp(self):
        super().setUp()
        self.posted = []
        self.env_secret = {**self.env, 'TUTTI_SLACK_WEBHOOK_URL': SLACK}

    def post(self, url, body):
        self.posted.append((url, body))
        return 200

    def test_events_are_sent_once_scrubbed_with_a_dashboard_link(self):
        events.emit(self.cfg, 'needs-person', 't4 Deploy <x> with token ghp_' + 'a' * 36, key='t4')
        events.emit(self.cfg, 'needs-person', 't4 again', key='t4')
        res = events.deliver(self.cfg, now=T0, post=self.post, env=self.env_secret)
        self.assertEqual(res, {'slack': []})
        self.assertEqual(len(self.posted), 1)
        text = self.posted[0][1]['text']
        self.assertNotIn('ghp_', text)
        self.assertIn('&lt;x&gt;', text)
        self.assertIn('<https://tutti.example/#needs|open>', text)
        events.emit(self.cfg, 'needs-person', 't4 later', key='t4')
        events.deliver(self.cfg, now=T0 + 60, post=self.post, env=self.env_secret)
        self.assertEqual(len(self.posted), 1)   # deduped within dedupe_hours

    def test_the_hourly_cap_counts_the_rest(self):
        self.cfg['integrations']['slack']['max_per_hour'] = 2
        for n in range(4):
            events.emit(self.cfg, 'run-finished', f't{n} done', key=str(n))
        events.deliver(self.cfg, now=T0, post=self.post, env=self.env_secret)
        self.assertEqual(len(self.posted), 2)
        events.emit(self.cfg, 'release', 'v1', key='v1')
        events.deliver(self.cfg, now=T0 + 3700, post=self.post, env=self.env_secret)
        self.assertIn('+2 more', self.posted[-1][1]['text'])

    def test_discord_pings_nobody_and_filters_kinds(self):
        self.cfg['integrations']['discord'] = {'enabled': True, 'events': ['release']}
        env = {**self.env, 'TUTTI_DISCORD_WEBHOOK_URL': DISCORD}
        events.emit(self.cfg, 'release', '@everyone v1.2.0', key='v')
        events.emit(self.cfg, 'run-failed', 't9', key='9')
        events.deliver(self.cfg, now=T0, post=self.post, env=env)
        discord = [b for u, b in self.posted if u == DISCORD]
        self.assertEqual(len(discord), 1)
        self.assertEqual(discord[0]['allowed_mentions'], {'parse': []})

    def test_a_revoked_webhook_is_reported_not_raised(self):
        events.emit(self.cfg, 'release', 'v1', key='v')
        res = events.deliver(self.cfg, now=T0, post=lambda u, b: 404, env=self.env_secret)
        self.assertIn('setup', res['slack'][0])
        self.assertNotIn(SLACK, json.dumps(events.load(self.cfg)))
        self.assertFalse(events.emit(self.cfg, 'nonsense', 'x'))

    def test_backlog_changes_become_events(self):
        prev = {'t1': 'running', 't2': 'running', 't3': 'queued'}
        tasks = [{'id': 't1', 'title': 'a', 'status': 'review'},
                 {'id': 't2', 'title': 'b', 'status': 'queued', 'attempts': [{'ok': False}]},
                 {'id': 't3', 'title': 'c', 'status': 'needs-person'},
                 {'id': 't4', 'title': 'd', 'status': 'queued', 'source': 'depbots'},
                 {'id': 't5', 'title': 'e', 'status': 'queued'}]
        kinds = [k for k, _, _ in events.backlog_events(prev, tasks)]
        self.assertEqual(kinds, ['run-finished', 'run-failed', 'needs-person', 'security'])

    def test_the_first_scan_only_remembers(self):
        with backlog.locked(self.cfg) as data:
            backlog.add(data, {**backlog.new_task('app', 'x'), 'status': 'needs-person'})
        self.assertEqual(events.scan_backlog(self.cfg), [])
        self.assertEqual(events.load(self.cfg)['outbox'], [])

    def test_only_webhook_addresses_are_accepted(self):
        self.assertIsNone(events.check_url('slack', SLACK))
        self.assertIsNotNone(events.check_url('slack', 'https://evil.example/services/x'))
        self.assertIsNotNone(events.check_url('discord', 'http://discord.com/api/webhooks/1/x'))
        bad = {'integrations': {'slack': {'events': ['nope']}, 'dashboard_url': 'tutti.example'}}
        self.assertEqual(len(events.errors(bad)), 2)


class SetupTests(Base):
    def run_cmd(self, name, action, stdin='', post=None):
        out = io.StringIO()
        rc = integrationscmd.run(self.cfg, SimpleNamespace(name=name, action=action), out=out, stdin=io.StringIO(stdin),
                                 env=self.env, post=post)
        return rc, out.getvalue()

    def test_setup_saves_the_secret_mode_600_without_printing_it(self):
        rc, text = self.run_cmd('slack', 'setup', SLACK + '\n')
        self.assertEqual(rc, 0)
        self.assertNotIn(SLACK, text)
        path = os.path.join(self.env['XDG_CONFIG_HOME'], 'tutti', secretenv.INTEGRATIONS_FILE)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(secretenv.get('TUTTI_SLACK_WEBHOOK_URL', self.env), SLACK)
        rc, _ = self.run_cmd('linear', 'setup', KEY + '\n', post=FakeLinear())
        self.assertEqual(rc, 0)
        self.assertEqual(secretenv.get('TUTTI_SLACK_WEBHOOK_URL', self.env), SLACK)   # the other line kept
        rc, text = self.run_cmd(None, 'status')
        self.assertIn('linear: on; TUTTI_LINEAR_KEY set', text)
        self.assertNotIn(KEY, text)

    def test_setup_refuses_a_bad_secret(self):
        rc, text = self.run_cmd('discord', 'setup', 'https://evil.example/x\n')
        self.assertEqual(rc, 1)
        self.assertIn('not saved', text)
        rc, text = self.run_cmd('linear', 'setup', KEY + '\n', post=FakeLinear(status=401))
        self.assertEqual(rc, 1)
        self.assertNotIn(KEY, text)
        rc, _ = self.run_cmd('jira', 'setup')
        self.assertEqual(rc, 2)

    def test_test_sends_one_message_or_says_what_is_missing(self):
        rc, text = self.run_cmd('slack', 'test', post=lambda u, b: 200)
        self.assertEqual(rc, 1)
        self.assertIn('not set', text)
        self.run_cmd('slack', 'setup', SLACK + '\n')
        rc, text = self.run_cmd('slack', 'test', post=lambda u, b: 200)
        self.assertEqual(rc, 0)


if __name__ == '__main__':
    unittest.main()
