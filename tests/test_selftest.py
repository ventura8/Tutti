"""tutti/selftest.py: the live self-test's identity, redaction, dedupe, fixtures and tasks (docs/TESTING.md)."""
import json
import os
import tempfile
import unittest

from tutti import auth, backlog, selftest


def cfg_in(folder):
    return {'project': {'state': folder}, 'autopilot': {'repos': {}}, 'dashboard': {'port': 4777}}


FINDING = {'view': 'Dashboard', 'width': 'phone', 'theme': 'dark', 'check': 'copy',
           'detail': 'a plural verb after 1: "1 account need a fresh reading" at p#usage-meta, owner@example.com'}


class Selftest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_in(self.tmp.name)
        self.fixtures = os.path.join(self.tmp.name, 'fixtures')

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_session_is_minted_before_a_person_is_the_admin(self):
        with self.assertRaisesRegex(selftest.SelftestError, 'sign in once as the owner'):
            selftest.mint(self.cfg)
        self.assertEqual(auth.Users(self.tmp.name).all(), {})

    def test_the_minted_identity_is_a_viewer_and_its_session_is_dropped(self):
        users = auth.Users(self.tmp.name)
        users.sign_in({'sub': 'owner', 'email': 'o@example.com', 'name': 'Owner'})
        sid = selftest.mint(self.cfg)
        self.assertEqual(users.get(selftest.SUB)['role'], 'viewer')
        sessions = auth.Sessions(self.tmp.name, 1)
        self.assertEqual(sessions.get(sid)['sub'], selftest.SUB)
        selftest.revoke(self.cfg, sid)
        self.assertIsNone(auth.Sessions(self.tmp.name, 1).get(sid))

    def test_emails_and_tokens_are_redacted(self):
        out = selftest.redact({'a': ['me@example.org', 'key ' + 'x' * 40], 'n': 3})
        self.assertEqual(out, {'a': ['someone@example.com', 'key REDACTED'], 'n': 3})

    def test_a_finding_whose_numbers_move_is_the_same_finding(self):
        other = {**FINDING, 'detail': FINDING['detail'].replace('1 account', '7 account')}
        self.assertEqual(selftest.signature(FINDING), selftest.signature(other))
        self.assertNotEqual(selftest.signature(FINDING), selftest.signature({**FINDING, 'view': 'Settings'}))

    def test_a_new_finding_gets_a_fixture_and_one_task_and_a_repeat_neither(self):
        result = {'checked': 9, 'findings': [FINDING], 'state': {'user': 'owner@example.com'}}
        first = selftest.learn(self.cfg, result, self.fixtures)
        sig = selftest.signature(FINDING)
        self.assertEqual((first['new'], first['findings'], first['ok']), ([sig], 1, False))
        with open(os.path.join(self.fixtures, f'live-{sig}.json'), encoding='utf8') as f:
            fx = json.load(f)
        self.assertNotIn('owner@example.com', json.dumps(fx))
        tasks = backlog.load(self.cfg)['tasks']
        self.assertEqual(len(tasks), 1)
        self.assertIn(sig, tasks[0]['check'])
        again = selftest.learn(self.cfg, result, self.fixtures)
        self.assertEqual((again['new'], again['repeat']), ([], [sig]))
        self.assertEqual(len(backlog.load(self.cfg)['tasks']), 1)
        self.assertEqual(selftest.last(self.cfg)['repeat'], [sig])

    def test_a_clean_run_is_ok(self):
        self.assertTrue(selftest.learn(self.cfg, {'checked': 9, 'findings': [], 'state': {}}, self.fixtures)['ok'])


if __name__ == '__main__':
    unittest.main()
