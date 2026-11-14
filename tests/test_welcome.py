"""Welcome back: an away -> back transition makes one summary (the owner's signals and a dashboard visit), the needs-you list
holds only what only the person can do, a viewer sees only their own and the household's items while an admin sees all,
the per-user switch, the template when AI is off and the model's words when it is on. Nothing leaves the temp folder."""
import json
import os
import tempfile
import unittest
from unittest import mock

from tutti import backlog, config, welcome

H = 3600
T0 = 1_800_000_000.0


def task(tid, status, title, at, **kw):
    return {'id': tid, 'repo': 'app', 'title': title, 'scope': [], 'check': '', 'size': 'S', 'source': 'test', 'status': status,
            'attempts': [{'at': at, 'ok': status == 'done', 'why': 'tests failed', 'step': 'cursor'}], 'notes': [], 'added': at, **kw}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, 'state')

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **w):
        raw = {'project': {'state': self.state}, 'autopilot': {'repos': {'app': {'path': self.tmp.name}}},
               'projects': {'App': {'match': 'app', 'repo': 'octo/app'}}}
        if w:
            raw['welcome'] = w
        cfg = config.normalise(raw)
        self.assertEqual(config.validate(cfg), [])
        cfg['_path'] = os.path.join(self.tmp.name, 'tutti.config.yaml')
        return cfg


class Return(Base):
    def test_away_then_back_makes_one_summary(self):
        cfg = self.cfg()
        with mock.patch.object(welcome, 'accounts_now', return_value=[]), \
                mock.patch.object(welcome, 'rewrite') as rewrite, \
                mock.patch.object(welcome, 'owner_here', side_effect=[(True, 'owner-mode'), (False, 'presence'),
                                                                      (False, 'presence'), (True, 'owner-mode'),
                                                                      (True, 'owner-mode')]):
            self.assertEqual(welcome.tick(cfg, T0), [])
            self.assertEqual(welcome.tick(cfg, T0 + H), [])
            self.assertEqual(welcome.tick(cfg, T0 + 2 * H), [])
            self.assertEqual(welcome.tick(cfg, T0 + 6 * H), ['welcome: a summary is waiting for the owner'])
            self.assertEqual(welcome.tick(cfg, T0 + 7 * H), [])   # one per return
        u = welcome.read(cfg)['users'][welcome.OWNER]
        self.assertEqual(len(u['summaries']), 1)
        self.assertEqual(u['summaries'][0]['since'], T0 + H)   # the period since they left
        self.assertTrue(u['unread'])
        rewrite.assert_called_once()

    def test_a_short_absence_is_not_a_return(self):
        cfg = self.cfg(away_hours=3)
        data = {'users': {}}
        self.assertIsNone(welcome.observe(cfg, data, 'x', False, 'presence', T0))
        self.assertIsNone(welcome.observe(cfg, data, 'x', True, 'presence', T0 + H))

    def test_a_first_dashboard_visit_after_a_gap_is_a_return(self):
        cfg = self.cfg()
        data = {'users': {}}
        self.assertIsNone(welcome.visit(cfg, data, 'sub1', T0))
        self.assertIsNone(welcome.visit(cfg, data, 'sub1', T0 + H))
        self.assertEqual(welcome.visit(cfg, data, 'sub1', T0 + 5 * H), {'since': T0 + H, 'signal': 'dashboard'})
        self.assertIsNone(welcome.visit(cfg, data, 'sub1', T0 + 5 * H + 60))

    def test_turned_off_for_a_user_makes_none(self):
        cfg = self.cfg()
        data = {'users': {welcome.OWNER: {'enabled': False}}}
        welcome.write(cfg, data)
        with mock.patch.object(welcome, 'owner_here', return_value=(True, 'x')):
            self.assertEqual(welcome.tick(cfg, T0), [])
        self.assertTrue(welcome.enabled_for(self.cfg(enabled=False), {'users': {'a': {'enabled': True}}}, 'a'))
        self.assertFalse(welcome.enabled_for(self.cfg(enabled=False), {'users': {}}, 'b'))


class Needs(Base):
    def test_the_needs_you_list_holds_only_actionable_items(self):
        cfg = self.cfg()
        with open(os.path.join(config.state_dir(cfg), 'setup.json'), 'w', encoding='utf8') as f:
            json.dump({'steps': [{'kind': 'sign-in', 'title': 'Sign in to gh', 'command': 'gh auth login', 'why': 'pull requests'},
                                 {'kind': 'other', 'title': 'Admire the view', 'command': '', 'why': ''}]}, f)
        tasks = [task('t1', 'needs-person', 'Fix the flaky probe', T0 + H, parked='no progress in 3 attempts'),
                 task('t2', 'queued', 'Speed up the report', T0 + H),
                 task('t3', 'done', 'Cache the readings', T0 + H, pr='https://github.example/octo/app/pull/7'),
                 task('t4', 'running', 'Rework pacing', T0 + H)]
        accounts = [{'name': 'cursor', 'state': 'no reading', 'used': None, 'action': 'tutti usage log cursor <percent>'},
                    {'name': 'agy', 'state': 'no reading', 'used': None, 'auto': True},
                    {'name': 'claude', 'state': 'ok', 'used': 40.0, 'projected_end': 92.5, 'verdict': 'behind',
                     'reset': '2026-11-20T08:00:00+00:00'}]
        f = welcome.facts(cfg, T0, accounts, tasks)
        self.assertEqual([n['title'] for n in f['needs']], ['Decide on task t1: Fix the flaky probe', "Read cursor's usage",
                                                            'Sign in to gh'])
        self.assertTrue(all(welcome.actionable(n) and n['link'] for n in f['needs']))
        self.assertIn('Cache the readings (pull request https://github.example/octo/app/pull/7)', f['shipped'][0]['text'])
        self.assertIn('Rework pacing', f['running'][0]['text'])
        self.assertEqual(len(f['failed']), 2)   # the parked one and the failed attempt; only the parked one needs a person
        self.assertIn('on track to end at 92.5 %', f['usage'][0]['text'])
        text = welcome.render(welcome.template(f, T0), f)
        self.assertIn('Needs you (3): only you can do these.\n1. Decide on task t1', text)
        self.assertIn('gh auth login', text)

    def test_before_the_period_is_left_out(self):
        cfg = self.cfg()
        f = welcome.facts(cfg, T0, [], [task('t3', 'done', 'Old work', T0 - H)])
        self.assertEqual(f['shipped'], [])


class Roles(Base):
    TASKS = [task('a1', 'done', 'Owner work', T0 + H), task('h1', 'done', 'Household shard', T0 + 2 * H, household=True),
             task('u1', 'done', 'Ana\'s task', T0 + 3 * H, user='ana'), task('u2', 'done', 'Bo\'s task', T0 + 3 * H, user='bo'),
             task('p1', 'needs-person', 'Owner decision', T0 + H)]

    def test_a_viewer_sees_their_own_and_the_household_an_admin_sees_all(self):
        cfg = self.cfg()
        f = welcome.facts(cfg, T0, [{'name': 'claude', 'state': 'ok', 'used': 10.0}], self.TASKS)
        viewer = welcome.for_viewer(cfg, f, 'viewer', 'ana')
        self.assertEqual([i['text'].split(' (')[0] for i in viewer['shipped']], ["Ana's task", 'Household shard'])   # newest first
        self.assertEqual((viewer['needs'], viewer['usage'], viewer['failed']), ([], [], []))
        admin = welcome.for_viewer(cfg, f, 'admin', welcome.OWNER)
        self.assertEqual(len(admin['shipped']), 4)
        self.assertEqual(len(admin['needs']), 1)
        self.assertEqual(len(admin['usage']), 1)

    def test_the_page_answers_each_viewer_their_own(self):
        cfg = self.cfg()
        with backlog.locked(cfg) as data:
            data['tasks'].extend(self.TASKS)
        who = {'h-ana': {'sub': 'ana', 'role': 'viewer'}, 'h-admin': {'sub': 'adm', 'role': 'admin'}}
        collector = type('C', (), {'cfg': cfg, 'who': staticmethod(lambda h: who.get(h.get('X')))})()
        with mock.patch.object(welcome, 'rewrite'), mock.patch.object(welcome, 'accounts_now', return_value=[]):
            welcome.get(collector, {'X': 'h-ana'}, T0)
            code, w = welcome.get(collector, {'X': 'h-ana'}, T0 + 10 * H)   # back after a long gap
            self.assertEqual(code, 200)
            self.assertTrue(w['unread'])
            self.assertNotIn('Owner work', w['summary']['text'])
            self.assertNotIn('Bo\'s task', w['summary']['text'])
            code, a = welcome.post(collector, {'X': 'h-admin'}, b'{"action": "miss"}')
            self.assertIn('Owner work', a['summary']['text'])
            self.assertIn('Decide on task p1', a['summary']['text'])
            self.assertEqual(welcome.post(collector, {'X': 'h-ana'}, b'{"action": "off"}')[1]['enabled'], False)
            self.assertEqual(welcome.post(collector, {'X': 'h-ana'}, b'{"action": "nope"}')[0], 400)
        self.assertTrue(welcome.enabled_for(cfg, welcome.read(cfg), 'adm'))


class Chat(Base):
    def test_what_did_i_miss_in_the_chat_answers_the_summary_for_that_user(self):
        cfg = self.cfg()
        with backlog.locked(cfg) as data:
            data['tasks'].extend(Roles.TASKS)
        self.assertTrue(welcome.asks_miss('What did I miss?'))
        self.assertFalse(welcome.asks_miss('what did I miss in the planner code'))
        with mock.patch.object(welcome, 'accounts_now', return_value=[]), mock.patch.object(welcome, 'rewrite'):
            viewer = welcome.chat_answer(cfg, {'sub': 'ana', 'role': 'viewer'}, T0 + 10 * H)
            admin = welcome.chat_answer(cfg, {'sub': 'local', 'role': 'admin'}, T0 + 10 * H)
        self.assertNotIn('Owner decision', viewer)
        self.assertIn('Decide on task p1', admin)
        self.assertIn('owner', welcome.read(cfg)['users'])   # sign-in off: the chat's local user is the owner


class Words(Base):
    def test_the_template_when_ai_is_off_and_the_model_when_on(self):
        cfg = self.cfg()
        f = {'shipped': [{'text': 'x'}], 'running': [], 'failed': [], 'usage': [], 'needs': []}
        self.assertIsNone(welcome.ai_intro(cfg, f, T0, ask=lambda c, p: ('hi', 'pc')))   # AI assist is off
        self.assertEqual(welcome.template(f, T0)[:13], 'Welcome back.')
        on = config.normalise({**cfg, 'ai_assist': {'enabled': True}})
        on['_path'] = cfg['_path']
        self.assertEqual(welcome.ai_intro(on, f, T0, ask=lambda c, p: (' While you were away, x shipped. ', 'pc')),
                         'While you were away, x shipped.')
        self.assertIsNone(welcome.ai_intro(on, f, T0, ask=lambda c, p: (None, 'down')))   # model down: the template

    def test_a_bad_setting_is_named(self):
        errs = ' '.join(config.validate(config.normalise({'welcome': {'enabled': 'yes', 'away_hours': 0, 'x': 1}})))
        for want in ('welcome.enabled', 'welcome.away_hours', 'welcome has x'):
            self.assertIn(want, errs)


if __name__ == '__main__':
    unittest.main()
