"""Tests for organizations, teams, roles, priorities, budgets and SSO/OIDC auth."""
import io
import os
import stat
import tempfile
import unittest
from types import SimpleNamespace

from tutti import auth, authweb, orgcmd, orgs


class OrgsModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = orgs.Orgs(self.tmp.name, clock=lambda: 1700000000)

    def test_org_team_member_and_file_mode_600(self):
        org = self.store.create_org('acme', 'Acme Corp', owner_sub='u-owner')
        self.assertEqual(org['name'], 'Acme Corp')
        self.assertEqual(org['members']['u-owner']['role'], 'owner')
        mode = stat.S_IMODE(os.stat(self.store.path).st_mode)
        self.assertEqual(mode, 0o600)

        self.store.set_member('acme', 'u-admin', 'admin', teams=['platform'])
        self.store.set_member('acme', 'u-member', 'member', teams=['platform'])
        self.store.set_member('acme', 'u-viewer', 'viewer')
        self.store.upsert_team(
            'acme', 'platform', 'Platform',
            priority=80, budget={'daily_runs': 50, 'monthly_usd': 200.0, 'max_parallel': 4}, projects=['api'],
        )
        got = self.store.get('acme')
        self.assertEqual(got['teams']['platform']['priority'], 80)
        self.assertEqual(orgs.team_priority(self.store.all(), 'api'), 80)
        self.assertEqual(orgs.team_priority(self.store.all(), 'other', fallback=5), 5)

        ok = orgs.budget_status(got['teams']['platform'], used_runs=10, used_usd=25.0)
        self.assertTrue(ok['ok'])
        self.assertEqual(ok['remaining_runs'], 40)
        self.assertEqual(ok['remaining_usd'], 175.0)

        over = orgs.budget_status(got['teams']['platform'], used_runs=50, used_usd=250.0)
        self.assertFalse(over['ok'])
        self.assertIn('daily run budget', over['reason'])
        self.assertIn('monthly USD budget', over['reason'])

    def test_last_owner_guard_and_validation_errors(self):
        self.store.create_org('acme', 'Acme Corp', owner_sub='u-owner')
        with self.assertRaises(orgs.OrgError):
            self.store.set_member('acme', 'u-owner', 'admin')
        with self.assertRaises(orgs.OrgError):
            self.store.remove_member('acme', 'u-owner')
        with self.assertRaises(orgs.OrgError):
            self.store.create_org('BAD ID!', 'Bad')
        with self.assertRaises(orgs.OrgError):
            self.store.upsert_team('acme', 'core', 'Core', priority=999)
        with self.assertRaises(orgs.OrgError):
            self.store.upsert_team('acme', 'core', 'Core', budget={'daily_runs': -1})

    def test_authorization_visibility(self):
        self.store.create_org('acme', 'Acme Corp', owner_sub='u-owner')
        self.store.set_member('acme', 'u-member', 'member')
        self.assertIn('acme', self.store.visible_to({'sub': 'u-member', 'role': 'member'}))
        self.assertNotIn('acme', self.store.visible_to({'sub': 'u-other', 'role': 'viewer'}))
        self.assertIn('acme', self.store.visible_to({'sub': 'u-admin', 'role': 'admin'}))


class SsoOidcTest(unittest.TestCase):
    def test_oidc_config_and_claims(self):
        cfg = {'auth': {'oidc': {
            'provider': 'okta',
            'issuer': 'https://sso.acme.example',
            'jwks_url': 'https://sso.acme.example/oauth2/v1/keys',
            'client_id': 'tutti-sso-client',
            'allowed_domains': ['acme.example'],
            'groups_claim': 'groups',
        }}}
        oc = auth.oidc_config(cfg)
        self.assertEqual(oc['provider'], 'okta')
        self.assertEqual(oc['client_id'], 'tutti-sso-client')
        self.assertIn('https://sso.acme.example', oc['issuers'])

        claims = {
            'iss': 'https://sso.acme.example',
            'aud': ['tutti-sso-client', 'other'],
            'exp': 2000,
            'iat': 1000,
            'sub': 'sso-u1',
            'email': 'alice@acme.example',
            'email_verified': True,
            'name': 'Alice',
            'groups': ['platform', 'admins'],
        }
        got = auth._oidc_claims(claims, 'tutti-sso-client', 1500, oc['issuers'], None, oc['allowed_domains'], 'groups')
        self.assertEqual(got['sub'], 'sso-u1')
        self.assertEqual(got['groups'], ['platform', 'admins'])

        bad_domain = dict(claims, email='bob@other.example')
        with self.assertRaises(auth.TokenError):
            auth._oidc_claims(bad_domain, 'tutti-sso-client', 1500, oc['issuers'], None, oc['allowed_domains'], 'groups')

        with self.assertRaises(auth.TokenError):
            auth.fetch_jwks('http://insecure.example/jwks')


class OrgApiAndCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {'project': {'state': self.tmp.name}, 'dashboard': {'port': 4777}, 'auth': {'client_id': 'c1'}}
        self.rt = authweb.Runtime(self.cfg, 4777, clock=lambda: 1700000000)

    def test_change_orgs_authorization_and_audit(self):
        owner = {'sub': 'u1', 'email': 'owner@acme.example', 'role': 'owner'}
        viewer = {'sub': 'u2', 'email': 'viewer@acme.example', 'role': 'viewer'}
        code, ans = authweb.change_orgs(self.rt, {'action': 'create_org', 'org_id': 'acme', 'name': 'Acme'}, owner)
        self.assertEqual(code, 200)
        self.assertIn('acme', ans['orgs'])

        code, _ = authweb.change_orgs(self.rt, {'action': 'upsert_team', 'org_id': 'acme', 'team_id': 'core', 'name': 'Core'}, viewer)
        self.assertEqual(code, 403)

        audit = auth.audit_log(self.tmp.name)
        self.assertTrue(any(e['action'] == 'org:create_org' for e in audit))

    def test_cli_org_commands(self):
        out = io.StringIO()
        a = SimpleNamespace(action='create', org_id='acme', name='Acme', owner='u1')
        self.assertEqual(orgcmd.cmd_org(self.cfg, a, out=out), 0)
        a_team = SimpleNamespace(action='team', org_id='acme', target='core', name='Core', priority=50,
                                 daily_runs=10, monthly_usd=100.0, max_parallel=2, projects='api')
        self.assertEqual(orgcmd.cmd_org(self.cfg, a_team, out=out), 0)
        a_budget = SimpleNamespace(action='budget', org_id='acme', target='core', used_runs=3, used_usd=20.0)
        self.assertEqual(orgcmd.cmd_org(self.cfg, a_budget, out=out), 0)
        a_over = SimpleNamespace(action='budget', org_id='acme', target='core', used_runs=10, used_usd=20.0)
        self.assertEqual(orgcmd.cmd_org(self.cfg, a_over, out=out), 1)
