"""The Users tab and acting for a member (docs/HOUSEHOLD.md, *The Users tab* and *Your main machine*): the admin changes a
member's schedule, main agent and main machine and the member's own Tutti does it; a member cannot act for another; the
audit says "by admin X for Y"; the admin's own row is there, marked (you), and the last-admin guard holds; one Users tab,
for admins only, with every per-user action. Temp folders and the coordinator's service in this process."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from test_household import DirectClient, Household, as_user

from tutti import config, coordinator, dashboard, household, mainmachine, manage, owner, users

WEB = dashboard.WEB


class Setup(unittest.TestCase):
    def setUp(self):
        self.h = Household(self)
        self.addCleanup(self.h.close)
        with as_user(self.h.bo_home):
            coordinator.join(self.h.bo, self.h.code, 'Bo', 'cursor', clock=self.h.clock)
        self.ana = DirectClient(self.h.service, self.h.membership(self.h.ana_home), self.h.clock)
        self.bo = DirectClient(self.h.service, self.h.membership(self.h.bo_home), self.h.clock)
        self.bo_hash = users.id_hash(self.h.membership(self.h.bo_home)['user_id'])
        self.ana_hash = users.id_hash(self.h.membership(self.h.ana_home)['user_id'])
        self.bo.call('/hh/sync', {'beat': {'setup': {'main_machine': 'laptop', 'targets': ['pc'], 'agents': ['cursor', 'claude']}}})

    def post(self, client, who, body, cfg=None):
        collector = SimpleNamespace(cfg=cfg or self.h.ana, who=lambda headers: who)
        return manage.act_post(collector, {}, json.dumps(body).encode(), client=client)


class AdminActsForAMemberTest(Setup):
    def test_admin_changes_a_members_schedule_main_agent_and_main_machine(self):
        admin = {'sub': 'g-ana', 'email': 'ana@example.com', 'role': 'admin'}
        for act, args in (('schedule', {'mode': 'away'}), ('main-agent', {'kind': 'claude'}), ('main-machine', {'machine': 'pc'})):
            code, answer = self.post(self.ana, admin, {'member': self.bo_hash, 'act': act, 'args': args})
            self.assertEqual(code, 202, answer)
        moved = []
        self.h.bo['workers']['claude'] = {'kind': 'cli', 'command': ['claude']}
        with as_user(self.h.bo_home), mock.patch.object(mainmachine, 'start', lambda cfg, m, popen=None: moved.append(m)), \
                mock.patch.object(users, 'connect_main', lambda cfg, target, fn=None: 'connect: done'):
            done = manage.tick(self.h.bo, client=self.bo)
        self.assertEqual([ok for _i, ok, _n in done], [True, True, True], done)
        folder = config.state_dir(self.h.bo)
        self.assertEqual(owner.read(folder)['mode'], 'away')                 # Bo's schedule, on Bo's computer
        self.assertEqual(users.read(folder)['main_agent'], 'claude')         # Bo's main agent
        self.assertEqual(moved, ['pc'])                                       # Bo's Tutti moves itself
        audit = self.ana.call('/hh/act', {'action': 'list'})['audit']
        self.assertIn('main-machine by admin Ana for Bo: machine=pc', [e['text'] for e in audit])

    def test_an_action_whose_result_was_lost_is_sent_again_and_a_stale_claim_reclaimed(self):
        admin = {'sub': 'g-ana', 'role': 'admin'}
        self.post(self.ana, admin, {'member': self.bo_hash, 'act': 'schedule', 'args': {'mode': 'away'}})
        self.bo.down = True
        with as_user(self.h.bo_home):
            self.assertEqual(manage.tick(self.h.bo, client=self.bo), [])   # offline: nothing claimed
        self.bo.down = False
        acts = self.bo.call('/hh/act', {'action': 'claim'})['acts']        # claimed, then its Tutti stopped
        self.h.clock.t += household.ACT_RECLAIM + 1
        self.assertEqual([a['id'] for a in self.bo.call('/hh/act', {'action': 'claim'})['acts']], [a['id'] for a in acts])

    def test_a_member_cannot_act_for_another_but_for_themself(self):
        viewer = {'sub': 'g-bo', 'role': 'viewer'}
        code, answer = self.post(self.bo, viewer, {'member': self.ana_hash, 'act': 'schedule', 'args': {'mode': 'away'}})
        self.assertEqual(code, 403, answer)
        with self.assertRaisesRegex(household.HouseholdError, 'only the household'):
            self.bo.call('/hh/act', {'action': 'queue', 'member': self.ana_hash, 'act': 'schedule', 'args': {}})
        code, _ = self.post(self.bo, None, {'member': self.bo_hash, 'act': 'schedule', 'args': {'mode': 'away'}}, cfg=self.h.bo)
        self.assertEqual(code, 202)

    def test_the_admins_own_row_is_there_and_the_last_admin_guard_holds(self):
        members = self.ana.call('/hh/act', {'action': 'list'})['members']
        self.assertEqual(sorted((m['name'], m['me']) for m in members), [('Ana', True), ('Bo', False)])
        bo = next(m for m in members if m['name'] == 'Bo')
        self.assertTrue(bo['joined'] and bo['online'] and bo['last_seen'])   # Tutti joined, presence and last seen on the row
        with self.assertRaisesRegex(household.HouseholdError, 'only admin'):
            self.ana.call('/hh/act', {'action': 'remove', 'member': self.ana_hash})
        with self.assertRaisesRegex(household.HouseholdError, 'only the household'):
            self.bo.call('/hh/act', {'action': 'remove', 'member': self.ana_hash})
        self.ana.call('/hh/act', {'action': 'make-admin', 'member': self.bo_hash})   # another admin first, then Ana may go
        self.bo.call('/hh/act', {'action': 'remove', 'member': self.ana_hash})
        self.assertEqual([m['name'] for m in self.bo.call('/hh/act', {'action': 'list'})['members']], ['Bo'])


class HouseholdOfOneTest(unittest.TestCase):
    def test_the_admins_own_row_is_complete_and_their_actions_run_here(self):
        with tempfile.TemporaryDirectory() as d, as_user(d):
            cfg = config.normalise({'project': {'state': os.path.join(d, 'state')}, 'accounts': {'claude': {}},
                                    'workers': {'claude': {'kind': 'cli', 'command': ['claude']}}})
            cfg['_path'] = os.path.join(d, 'c.yaml')
            collector = SimpleNamespace(cfg=cfg, who=lambda h: {'sub': 'g-ana', 'role': 'admin', 'email': 'ana@example.com'})
            code, listing = manage.act_post(collector, {}, b'{"list": true}', client=None)
            self.assertEqual(code, 200)
            row = listing['members'][0]
            self.assertTrue(row['me'] and row['admin'])
            for key in ('main_machine', 'targets', 'agents', 'mode', 'schedule', 'stats'):
                self.assertIn(key, row['setup'])
            code, answer = manage.act_post(collector, {}, json.dumps({'member': row['hash'], 'act': 'schedule',
                                                                     'args': {'mode': 'away'}}).encode(), client=None)
            self.assertEqual(code, 200, answer)
            self.assertEqual(owner.read(config.state_dir(cfg))['mode'], 'away')
            self.assertEqual(manage.act_post(collector, {}, json.dumps({'member': row['hash'], 'act': 'remove'}).encode(),
                                             client=None)[0], 409)


class MainMachineTest(unittest.TestCase):
    def cfg(self, d):
        cfg = config.normalise({'project': {'state': os.path.join(d, 'state')},
                                'machines': {'laptop': {'host': 'local'}, 'pc': {'host': 'pc.lan'}, 'theirs': {'transport': 'household'}}})
        cfg['_path'] = os.path.join(d, 'c.yaml')
        return cfg

    def test_a_member_switches_from_the_laptop_to_the_pc_through_tutti_move(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self.cfg(d)
            calls = []
            with mock.patch.object(mainmachine.move, 'cmd', lambda c, a, out=None: calls.append((a.to, a.dry_run)) or 0):
                self.assertEqual(mainmachine.change(cfg, 'pc'), 0)
            self.assertEqual(calls, [('pc', False)])
            for bad, why in (('theirs', 'compute only'), ('nope', 'no machine'), ('laptop', 'already')):
                with self.assertRaisesRegex(mainmachine.MainMachineError, why):
                    mainmachine.change(cfg, bad)

    def test_the_state_tutti_move_copies_carries_the_identity_and_readings(self):
        with tempfile.TemporaryDirectory() as laptop, tempfile.TemporaryDirectory() as pc:
            before = self.cfg(laptop)
            uid = users.user_id(config.state_dir(before))
            with open(os.path.join(config.state_dir(before), 'usage.jsonl'), 'w') as f:
                f.write('{"account": "claude", "used": 40, "at": "2026-11-14T10:00:00+00:00"}\n')
            shutil.copytree(config.state_dir(before), os.path.join(pc, 'state'))   # what `tutti move`'s copy step does
            after = self.cfg(pc)
            self.assertEqual(users.user_id(config.state_dir(after)), uid)
            with open(os.path.join(config.state_dir(after), 'usage.jsonl')) as f:
                self.assertIn('"used": 40', f.read())

    def test_only_the_user_themself_may_change_it_on_the_server(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self.cfg(d)
            me = users.identity(cfg)['id']
            self.assertTrue(mainmachine.allowed(cfg, None)[0])                       # this computer's own owner
            self.assertTrue(mainmachine.allowed(cfg, {'sub': me, 'role': 'admin'})[0])  # the admin, for themself
            self.assertTrue(mainmachine.allowed(cfg, {'sub': me, 'role': 'viewer'})[0])
            ok, why = mainmachine.allowed(cfg, {'sub': 'someone-else', 'role': 'viewer'})
            self.assertFalse(ok)
            self.assertIn('only your own', why)
            collector = SimpleNamespace(cfg=cfg, who=lambda h: {'sub': 'someone-else', 'role': 'viewer'})
            self.assertEqual(mainmachine.post(collector, {}, b'{"machine": "pc"}')[0], 403)

    def test_a_laptop_that_leaves_makes_the_user_away(self):
        from tutti import bridge
        with tempfile.TemporaryDirectory() as d:
            cfg = self.cfg(d)
            self.assertTrue(mainmachine.present(cfg))   # no client ever connected: the user sits at this Tutti (0.1.64 said away)
            status = os.path.join(bridge.folder(cfg), 'laptop.json')   # the laptop connected once (its status file)
            with open(status, 'w') as f:
                f.write('{}')
            with mock.patch.object(bridge, 'clients', return_value=[]):
                self.assertFalse(mainmachine.present(cfg))   # and has left: away
                old = os.path.getmtime(status) - (mainmachine.KNOWN_DAYS + 1) * 86400
                os.utime(status, (old, old))
                self.assertTrue(mainmachine.present(cfg))    # a retired laptop's old file never makes them away for good
            os.utime(status)
            with mock.patch.object(bridge, 'clients', return_value=[{'machine': 'laptop'}]):
                self.assertTrue(mainmachine.present(cfg))

    def test_the_admins_row_counts_runs_as_a_number(self):
        from tutti import sharing
        with tempfile.TemporaryDirectory() as d:
            cfg = self.cfg(d)
            with mock.patch('tutti.state.runs_today', return_value={'cursor': 2, 'agy': 3, 'odd': None}):
                self.assertEqual(sharing.runs_total(cfg), 5)
                self.assertEqual(sharing.stats_of(cfg)[-1]['used'], 5)
            self.assertIsNone(sharing._pct('n/a'))
            self.assertEqual(sharing._pct(34.075999999999965), 34.1)


class SignedInTest(unittest.TestCase):
    def test_with_sign_in_on_the_users_tab_and_main_machine_posts_reach_their_handlers(self):
        # 0.1.64 answered 404 "not part of the dashboard" to them once sign-in was on: signed_post knew only POSTS
        cfg = config.normalise({})
        collector = SimpleNamespace(cfg=cfg)
        for path in dashboard.SELF_POSTS:
            with mock.patch.object(dashboard, 'allowed_host', return_value=True), \
                    mock.patch.object(dashboard.authweb, 'post', return_value=(202, {'ok': True}, None)) as gate:
                code, _answer, _extra = dashboard.signed_post(object(), collector, {'Host': '127.0.0.1:4777'}, path, 4777, None)
            self.assertEqual(code, 202, path)
            gate.assert_called_once()


class TabTest(unittest.TestCase):
    def page(self):
        with open(os.path.join(WEB, 'index.html'), encoding='utf8') as f:
            return f.read()

    def test_one_users_tab_for_admins_only(self):
        page = self.page()
        tabs = re.findall(r'<a class="tabs__tab"[^>]*>([^<]+)</a>', page)
        # the Chat first: the default view (docs/CHAT.md)
        self.assertEqual(tabs, ['Chat', 'Dashboard', 'Devices', 'Hosts', 'Settings', 'Users'])
        self.assertRegex(page, r'<a class="tabs__tab" href="#users" data-view="users" id="users-tab" data-admin hidden>Users</a>')
        self.assertEqual(page.count('id="users-view"'), 1)   # one place: the sign-in's Users tab, the household drawn into it
        self.assertIn('<div id="users-manage"></div>', page)

    def test_settings_holds_no_users_invites_or_roles_and_the_users_view_really_hides(self):
        page = self.page()
        start = page.index('<div id="settings-view"')
        settings_view = page[start:page.index('\n      </div>', start)]
        for marker in ('users-card', 'invite', 'role', 'id="users"', 'users-manage'):
            self.assertNotIn(marker, settings_view)
        self.assertEqual(page.count('id="users-card"'), 1)
        with open(os.path.join(WEB, 'app.css'), encoding='utf8') as f:
            css = f.read()
        self.assertIn('.users-view[hidden] { display: none; }', css)   # else display: grid shows it on every view

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_every_per_user_action_is_on_the_tab_and_the_admin_row_is_marked_you(self):
        with open(os.path.join(WEB, 'userstab.js'), encoding='utf8') as f:
            src = f.read()
        stats = [{'kind': 'claude', 'used': 40, 'verdict': 'on pace'}]
        data = {'members': [{'hash': 'a', 'name': 'Ana', 'me': True, 'setup': {'stats': stats}}, {'hash': 'b', 'name': 'Bo', 'setup': {}}]}
        out = subprocess.run(['node', '-e', f'{src}\nconsole.log(JSON.stringify(hhUserRows({json.dumps(data)})));'],
                             capture_output=True, text=True, check=True)
        rows = json.loads(out.stdout)
        self.assertEqual(rows[0]['name'], 'Ana (you)')
        self.assertIn('claude: 40 on pace', rows[0]['stats'])
        acts = {a['act'] for a in rows[1]['acts']}
        self.assertTrue(set(household.ACTS) <= acts, set(household.ACTS) - acts)
        self.assertTrue({'join', 'remove', 'make-admin'} <= acts)

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_admins_row_has_every_field_and_control_of_a_member_row_the_guard_only_disables(self):
        with open(os.path.join(WEB, 'userstab.js'), encoding='utf8') as f:
            src = f.read()
        setup = {'main_machine': 'laptop', 'targets': ['pc'], 'agents': ['claude'], 'mode': 'auto', 'schedule': ['days mon'],
                 'stats': [{'kind': 'claude', 'used': 40, 'verdict': 'on pace'}]}
        data = {'admins': 1, 'members': [
            {'hash': 'a', 'name': 'Ana', 'me': True, 'admin': True, 'joined': 1, 'online': True, 'last_seen': 1, 'setup': setup},
            {'hash': 'b', 'name': 'Bo', 'joined': 1, 'online': True, 'last_seen': 1, 'setup': setup}]}
        out = subprocess.run(['node', '-e', f'{src}\nconsole.log(JSON.stringify(hhUserRows({json.dumps(data)}, 2000)));'],
                             capture_output=True, text=True, check=True)
        ana, bo = json.loads(out.stdout)
        self.assertEqual(sorted(ana), sorted(bo))   # the same fields
        for key in ('mainMachine', 'agents', 'targets', 'mode', 'schedule', 'stats', 'seen', 'joined', 'presence'):
            self.assertEqual(ana[key], bo[key], key)
        self.assertEqual([a['act'] for a in ana['acts']], [a['act'] for a in bo['acts']])   # the same controls
        guarded = {a['act']: a for a in ana['acts'] if a['disabled']}
        self.assertEqual(sorted(guarded), ['make-admin', 'remove'])
        self.assertIn('only admin', guarded['remove']['why'])
        self.assertFalse(any(a['disabled'] for a in bo['acts']))


if __name__ == '__main__':
    unittest.main()
