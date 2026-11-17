"""`tutti user` and the user model (docs/HOUSEHOLD.md): a single user is a household of one with no new key, two users on
one computer keep apart ids, names, shares and memberships, the membership file is mode 600, and telemetry names a user
only by its hash. Temp folders only."""
import io
import os
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tutti import config, householdcmd, users


def make_cfg(folder, **raw):
    cfg = config.normalise({'project': {'state': os.path.join(folder, 'state')},
                            'workers': {'agy': {'kind': 'cli', 'command': ['agy']}},
                            'accounts': {'agy-claude': {'worker': 'agy'}, 'api': {'worker': 'agy', 'costs_money': True, 'enabled': True}},
                            **raw})
    cfg['_path'] = os.path.join(folder, 'config.yaml')
    return cfg


class SingleUserMigrationTest(unittest.TestCase):
    def test_the_example_config_is_a_household_of_one_with_no_new_key(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config.load(config.EXAMPLE)
            self.assertEqual(config.validate(cfg), [])
            cfg['project']['state'] = os.path.join(d, 'state')
            env = {'XDG_CONFIG_HOME': os.path.join(d, 'xdg')}
            with mock.patch.object(users, 'login_name', return_value='ana'):
                me = users.identity(cfg, env)
            self.assertEqual((me['name'], me['role'], me['household'], me['shares']), ('ana', 'alone', None, []))
            self.assertEqual(config.state_dir(cfg), os.path.join(d, 'state'))   # the state folder did not move

    def test_household_status_says_household_of_one(self):
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': os.path.join(d, 'xdg')}):
                rc = householdcmd.cmd_household(make_cfg(d), SimpleNamespace(action='status'), out)
            self.assertEqual(rc, 0)
            self.assertIn('household of one', out.getvalue())


class IdentityTest(unittest.TestCase):
    def test_the_id_is_made_once_and_only_its_hash_is_shown(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_cfg(d)
            first = users.identity(cfg, {'XDG_CONFIG_HOME': d})
            again = users.identity(cfg, {'XDG_CONFIG_HOME': d})
            self.assertEqual(first['id'], again['id'])
            self.assertRegex(first['hash'], r'^[0-9a-f]{12}$')
            self.assertNotIn('id', users.view(cfg, {'XDG_CONFIG_HOME': d}))

    def test_two_users_on_one_computer_keep_apart(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            ana, bo = make_cfg(a, user={'name': 'Ana'}), make_cfg(b, user={'name': 'Bo'})
            users.set_share(ana, 'agy-claude', True)
            users.write_household({'household': 'home', 'role': 'member', 'token': 't'}, {'XDG_CONFIG_HOME': a})
            me_a, me_b = users.identity(ana, {'XDG_CONFIG_HOME': a}), users.identity(bo, {'XDG_CONFIG_HOME': b})
            self.assertNotEqual(me_a['id'], me_b['id'])
            self.assertEqual((me_a['name'], me_a['shares'], me_a['role']), ('Ana', ['agy-claude'], 'member'))
            self.assertEqual((me_b['name'], me_b['shares'], me_b['role']), ('Bo', [], 'alone'))

    def test_the_config_name_wins_over_the_saved_one(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_cfg(d)
            users.set_name(cfg, 'Ana B')
            self.assertEqual(users.identity(cfg, {'XDG_CONFIG_HOME': d})['name'], 'Ana B')
            self.assertEqual(users.identity(make_cfg(d, user={'name': 'Ana'}), {'XDG_CONFIG_HOME': d})['name'], 'Ana')
            users.set_name(cfg, 'config')
            with mock.patch.object(users, 'login_name', return_value='ana'):
                self.assertEqual(users.identity(cfg, {'XDG_CONFIG_HOME': d})['name'], 'ana')

    def test_a_bad_name_is_refused_with_the_rule(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(users.UserError, 'letters, digits'):
                users.set_name(make_cfg(d), '../etc')


class ShareTest(unittest.TestCase):
    def test_share_spare_from_the_config_and_its_switch(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_cfg(d)
            cfg['accounts']['agy-claude']['share_spare'] = True
            self.assertEqual(users.identity(cfg, {'XDG_CONFIG_HOME': d})['shares'], ['agy-claude'])
            users.set_share(cfg, 'agy-claude', False)
            self.assertEqual(users.identity(cfg, {'XDG_CONFIG_HOME': d})['shares'], [])

    def test_an_account_that_costs_money_or_is_unknown_is_never_shared(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_cfg(d)
            with self.assertRaisesRegex(users.UserError, 'costs money'):
                users.set_share(cfg, 'api', True)
            with self.assertRaisesRegex(users.UserError, 'no account "nope"'):
                users.set_share(cfg, 'nope', True)
            cfg['accounts']['api']['share_spare'] = True   # written in the config by hand: still never shared, and named
            self.assertEqual(users.identity(cfg, {'XDG_CONFIG_HOME': d})['shares'], [])
            self.assertIn('accounts.api.share_spare is true but it costs money', '\n'.join(users.errors(cfg)))


class MainAgentTest(unittest.TestCase):
    RAW = {'workers': {'claude': {'kind': 'cli', 'command': ['claude']}, 'cursor': {'kind': 'cli', 'command': ['cursor-agent']},
                       'pc': {'kind': 'ollama', 'host': 'localhost:11434', 'model': 'm'}},
           'accounts': {'claude': {'worker': 'claude'}, 'cursor-pro': {'worker': 'cursor'}}}

    def load(self, d):
        """The config as `config.load` builds it: user.json applied before the defaults."""
        import copy
        raw = {**copy.deepcopy(self.RAW), 'project': {'state': os.path.join(d, 'state')}}
        cfg = config.normalise(users.apply(raw, users.read(os.path.join(d, 'state'))))
        cfg['_path'] = os.path.join(d, 'config.yaml')
        return cfg

    def test_a_switch_moves_the_owner_reserve_to_the_new_agents_account(self):
        from tutti import owner
        with tempfile.TemporaryDirectory() as d:
            cfg = self.load(d)
            self.assertEqual(owner.owner_of(cfg, os.path.join(d, 'state'))['accounts'], ['claude'])
            calls = []
            worker, note = users.set_main_agent(cfg, 'cursor', connect_fn=lambda path, names: calls.append(names) or [])
            self.assertEqual((worker, calls), ('cursor', [['cursor']]))
            self.assertIn('tutti connect cursor: done', note)
            cfg = self.load(d)
            self.assertEqual(cfg['main']['agent'], 'cursor')
            self.assertEqual(owner.owner_of(cfg, os.path.join(d, 'state'))['accounts'], ['cursor-pro'])
            self.assertEqual(users.set_main_agent(cfg, 'local', connect_fn=lambda *a, **k: [])[0], 'pc')

    def test_an_unknown_kind_or_one_without_a_worker_says_what_to_do(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self.load(d)
            with self.assertRaisesRegex(users.UserError, 'pick one of'):
                users.set_main_agent(cfg, 'notepad')
            worker, note = users.set_main_agent(cfg, 'gemini', connect_fn=lambda *a, **k: [{'status': 'error', 'note': 'not installed'}])
            self.assertEqual(worker, 'gemini')
            self.assertIn('tutti setup', note)
            self.assertIn('not installed', note)

    def test_kind_of_a_worker(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self.load(d)
            self.assertEqual(users.kind_of(cfg, 'claude'), 'claude')
            self.assertEqual(users.kind_of(cfg, 'cursor'), 'cursor')
            self.assertEqual(users.kind_of(cfg, 'pc'), 'local')
            self.assertIsNone(users.kind_of(cfg, 'nobody'))


class MembershipFileTest(unittest.TestCase):
    def test_the_membership_is_mode_600_and_leaving_removes_it(self):
        with tempfile.TemporaryDirectory() as d:
            env = {'XDG_CONFIG_HOME': d}
            users.write_household({'household': 'home', 'token': 'secret'}, env)
            self.assertEqual(stat.S_IMODE(os.stat(users.household_path(env)).st_mode), 0o600)
            self.assertTrue(users.leave_household(env))
            self.assertFalse(users.leave_household(env))
            self.assertEqual(users.read_household(env), {})


class ConfigErrorsTest(unittest.TestCase):
    def test_bad_user_and_household_keys_name_the_fix(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_cfg(d, user={'weight': 0, 'name': '!'}, household={'port': 0})
            cfg['accounts']['agy-claude']['share_spare'] = 'yes'
            errs = '\n'.join(users.errors(cfg))
            for key in ('user.weight', 'user.name', 'household.port', 'accounts.agy-claude.share_spare'):
                self.assertIn(key, errs)
            self.assertEqual(users.errors(make_cfg(d)), [])


class CommandTest(unittest.TestCase):
    def run_user(self, d, **a):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': d}), mock.patch.object(householdcmd.telemetry, 'record') as rec:
            rc = householdcmd.cmd_user(make_cfg(d), SimpleNamespace(**{'action': 'show', 'arg': None, 'value': None, **a}), out)
        return rc, out.getvalue(), rec

    def test_user_share_and_telemetry_names_the_user_by_hash_only(self):
        with tempfile.TemporaryDirectory() as d:
            rc, text, rec = self.run_user(d, action='share', arg='agy-claude', value='on')
            self.assertEqual(rc, 0)
            self.assertIn('shares spare room of: agy-claude', text)
            fields = rec.call_args.kwargs
            self.assertRegex(fields['user'], r'^[0-9a-f]{12}$')
            self.assertNotIn(users.login_name(), str(rec.call_args))

    def test_user_share_without_on_or_off_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            rc, _text, _rec = self.run_user(d, action='share', arg='agy-claude', value='maybe')
            self.assertEqual(rc, 2)

    def test_user_name_bad_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            rc, _text, _rec = self.run_user(d, action='name', arg='')
            self.assertEqual(rc, 0)   # '' takes it back to the config's
            rc, _text, _rec = self.run_user(d, action='name', arg='/x')
            self.assertEqual(rc, 2)


if __name__ == '__main__':
    unittest.main()
