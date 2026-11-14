"""Fair shares of a household's shared machines (docs/HOUSEHOLD.md), increment 3: water-filling, interactive before
background, no starvation, the grants applied to routing, machines shared and used, spare room, and a 48-hour simulation
of two members with different working hours on two shared machines and one shared project. Pure or temp folders only."""
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from test_household import DirectClient, Household, as_user, make_cfg

from tutti import autopilot, config, coordinator, fairshare, household, householdcmd, remote, routing, sharing, users

UTC = timezone.utc


class WaterFillTest(unittest.TestCase):
    def test_equal_weights_split_evenly_and_never_beyond_a_want(self):
        self.assertEqual(fairshare.water_fill(4, {'a': 9, 'b': 9}, {}), {'a': 2, 'b': 2})
        self.assertEqual(fairshare.water_fill(4, {'a': 1, 'b': 9}, {}), {'a': 1, 'b': 3})   # work-conserving

    def test_weights_tilt_the_split(self):
        self.assertEqual(fairshare.water_fill(6, {'a': 9, 'b': 9}, {'a': 2.0, 'b': 1.0}), {'a': 4, 'b': 2})

    def test_nothing_to_share(self):
        self.assertEqual(fairshare.water_fill(0, {'a': 3}, {}), {'a': 0})


class GrantsTest(unittest.TestCase):
    def test_interactive_demand_is_served_before_anyones_background(self):
        g = fairshare.grants(4, {'ana': {'interactive': 2, 'background': 4}, 'bo': {'background': 4}}, {})
        self.assertEqual(g, {'ana': 3, 'bo': 1})   # ana's 2 interactive first, then the 2 left split 1 : 1

    def test_a_starved_member_takes_one_slot_from_the_richest_background(self):
        g = fairshare.grants(2, {'ana': {'background': 4}, 'bo': {'background': 4}}, {'ana': 9.0, 'bo': 1.0}, starved={'bo'})
        self.assertEqual(g, {'ana': 1, 'bo': 1})

    def test_a_paused_machine_grants_nothing(self):
        self.assertEqual(fairshare.grants(0, {'ana': {'interactive': 1}}, {}), {'ana': 0})

    def test_starving_counts_from_the_first_round_without_a_slot(self):
        prev = {}
        self.assertEqual(fairshare.starving(prev, 'pc', {'bo': {'background': 1}}, {}, 0, 20), set())
        self.assertEqual(fairshare.starving(prev, 'pc', {'bo': {'background': 1}}, {}, 21 * 60, 20), {'bo'})
        self.assertEqual(fairshare.starving(prev, 'pc', {'bo': {'background': 1}}, {'bo': 1}, 22 * 60, 20), set())


class ProfileTest(unittest.TestCase):
    def test_an_active_hour_doubles_the_weight_and_bad_profiles_count_as_none(self):
        rows = [[0] * 24 for _ in range(7)]
        rows[0][9] = 3.0
        p = fairshare.normalise_profile(rows)
        monday_9 = datetime(2026, 11, 16, 9, tzinfo=UTC)
        self.assertEqual(fairshare.hour_weight(1, p, monday_9), 2.0)
        self.assertEqual(fairshare.hour_weight(1, p, monday_9 + timedelta(hours=1)), 1.0)
        self.assertEqual(fairshare.normalise_profile([[1, 2]]), [])
        self.assertEqual(fairshare.hour_weight(2, 'junk', monday_9), 2.0)

    def test_the_household_plan_names_who_is_expected(self):
        rows = [[1.0 if 8 <= h < 16 else 0.0 for h in range(24)] for _ in range(7)]
        plan = fairshare.household_plan({'Ana': rows, 'Bo': []}, datetime(2026, 11, 16, 7, tzinfo=UTC), hours=2)
        self.assertEqual([x[1] for x in plan], [{'Ana': 0.0, 'Bo': 0.0}, {'Ana': 1.0, 'Bo': 0.0}])

    def test_jain(self):
        self.assertEqual(fairshare.jain({'a': 2, 'b': 2}, {'a': 2, 'b': 2}), 1.0)
        self.assertLess(fairshare.jain({'a': 2, 'b': 0}, {'a': 2, 'b': 2}), 1.0)
        self.assertIsNone(fairshare.jain({}, {}))
        self.assertEqual(fairshare.jain({'a': 0, 'b': 0}, {'a': 1, 'b': 1}), 1.0)   # a paused machine: nobody got a slot


class LimitTest(unittest.TestCase):
    def test_a_machine_outside_the_household_is_unchanged(self):
        self.assertEqual(fairshare.limit('mine', 3, 0, {}, 0), (3, ''))

    def test_a_grant_cuts_free_slots_and_says_who_is_busy(self):
        cache = {'at': 100, 'machines': {'pc': {'grant': 1, 'slots': 4, 'use': {'Bo': 3}}}}
        free, why = fairshare.limit('pc', 3, 0, cache, 120)
        self.assertEqual(free, 1)
        self.assertIn('busy for Bo 3', why)
        self.assertEqual(fairshare.limit('pc', 3, 1, cache, 120)[0], 0)

    def test_no_fresh_grant_means_anothers_machine_is_not_used_but_ones_own_keeps_its_last(self):
        cache = {'at': 0, 'machines': {'pc': {'grant': 2, 'slots': 4}, 'nuc': {'grant': 1, 'slots': 2, 'mine': True}}}
        self.assertEqual(fairshare.limit('pc', 3, 0, cache, 10_000)[0], 0)
        self.assertEqual(fairshare.limit('nuc', 2, 0, cache, 10_000)[0], 1)
        self.assertIn('paused', fairshare.limit('pc', 3, 0, {'at': 0, 'machines': {'pc': {'paused': True, 'grant': 2}}}, 1)[1])


class OverlayTest(unittest.TestCase):
    def test_shared_and_used_machines_join_the_config_without_editing_it(self):
        raw = {'machines': {'pc': {'host': 'pc'}}, 'routing': {'order': ['pc']}}
        saved = {'machine_overrides': {'pc': {'lock_prefix': '/var/tmp/tutti-household/slot-', 'household_shared': True}},
                 'household_machines': {'nuc': {'host': '10.0.0.5', 'slots': 2}, 'pc': {'host': 'other'}}}
        out = users.apply(raw, saved)
        self.assertEqual(out['machines']['pc']['host'], 'pc')   # one's own machine is never replaced
        self.assertTrue(out['machines']['pc']['household_shared'])
        self.assertEqual(out['routing']['order'], ['pc', 'nuc'])
        cfg = config.normalise(out)
        self.assertIn('SLOT_SHARED=1', remote.slot_prefix(cfg['machines']['pc']))
        self.assertNotIn('SLOT_SHARED', remote.slot_prefix(cfg['machines']['nuc']))


class MachinesTest(unittest.TestCase):
    """Ana shares pc; Bo uses it; only the owner or the admin pauses it."""

    def setUp(self):
        self.h = Household(self)
        self.addCleanup(self.h.close)
        with as_user(self.h.bo_home):
            coordinator.join(self.h.bo, self.h.code, 'Bo', 'cursor', clock=self.h.clock)
        self.ana = DirectClient(self.h.service, self.h.membership(self.h.ana_home), self.h.clock)
        self.bo = DirectClient(self.h.service, self.h.membership(self.h.bo_home), self.h.clock)
        self.h.ana['machines']['pc'] = config.machine_of(self.h.ana, 'pc', {'host': 'pc.lan', 'slots': 4})

    def cmd(self, cfg, client, *arg, **kw):
        out = io.StringIO()
        probe = kw.pop('probe', routing.Probe(True, 4, 0.1, 8, gpu_temp=50.0))
        a = SimpleNamespace(action='machine', arg=list(arg), host=None, user=None, **kw)
        with mock.patch.object(householdcmd, 'machine', wraps=householdcmd.machine):
            rc = householdcmd.machine(cfg, a, out, lambda c: client, probe_fn=lambda m: probe,
                                      sh_fn=lambda m, script: SimpleNamespace(returncode=0))
        return rc, out.getvalue()

    def test_share_then_use_then_pause_rules(self):
        with as_user(self.h.ana_home):
            rc, text = self.cmd(self.h.ana, self.ana, 'share', 'pc')
        self.assertEqual(rc, 0, text)
        self.assertTrue(users.read(config.state_dir(self.h.ana))['machine_overrides']['pc']['household_shared'])
        with as_user(self.h.bo_home):
            rc, text = self.cmd(self.h.bo, self.bo, 'use', 'pc')
        self.assertEqual(rc, 0, text)
        self.assertIn("Ana's Tutti runs them there", text)   # no login on pc for Bo, nothing to set up there
        entry = users.read(config.state_dir(self.h.bo))['household_machines']['pc']
        self.assertEqual((entry['transport'], entry['household_owner']), ('household', 'Ana'))
        self.assertNotIn('host', entry)
        with self.assertRaisesRegex(household.HouseholdError, 'only Ana'):
            self.bo.call('/hh/machine', {'action': 'pause', 'name': 'pc'})
        self.ana.call('/hh/machine', {'action': 'pause', 'name': 'pc'})
        view = self.bo.call('/hh/sync', {'beat': {'machines': {'pc': {'interactive': 1}}}})['machines']['pc']
        self.assertEqual((view['paused'], view['grant'], view['paused_by']), (True, 0, 'Ana'))

    def test_another_members_machine_of_the_same_name_is_refused(self):
        self.ana.call('/hh/machine', {'action': 'share', 'name': 'pc', 'info': {'slots': 2}})
        with self.assertRaisesRegex(household.HouseholdError, 'already has a machine'):
            self.bo.call('/hh/machine', {'action': 'share', 'name': 'pc', 'info': {'slots': 2}})

    def test_grants_follow_demand_and_reach_the_members_routing(self):
        self.ana.call('/hh/machine', {'action': 'share', 'name': 'pc', 'info': {'slots': 4}})
        self.bo.call('/hh/sync', {'beat': {'machines': {'pc': {'background': 4}}}})
        answer = self.ana.call('/hh/sync', {'beat': {'machines': {'pc': {'interactive': 2, 'background': 4}}}})
        self.assertEqual(answer['machines']['pc']['grant'], 3)
        with as_user(self.h.bo_home):
            fairshare.save_cache(config.state_dir(self.h.bo), self.bo.call('/hh/sync', {'beat': {
                'machines': {'pc': {'background': 4}}}})['machines'], self.h.clock())
            probe = sharing.limited_probe(self.h.bo, 'pc', routing.Probe(True, 4, 0.1, 8), self.h.clock())
        self.assertEqual(probe.free, 1)


class DemandTest(unittest.TestCase):
    def test_queued_work_and_the_prompting_slot_are_counted_once(self):
        with tempfile.TemporaryDirectory() as home, as_user(home):
            cfg = make_cfg(home, 'Ana')
            for n in ('pc', 'nuc'):
                cfg['machines'][n] = config.machine_of(cfg, n, {'host': n, 'slots': 2})
            cfg['routing']['order'] = ['nuc', 'pc']
            fairshare.save_cache(config.state_dir(cfg), {'pc': {'slots': 2, 'mine': True}, 'nuc': {'slots': 2}}, 0)
            from tutti import backlog
            with backlog.locked(cfg) as data:
                for i in range(3):
                    backlog.add(data, backlog.new_task('app', f'q{i}', (), 'true'))
            d = sharing.demand(cfg, True, [{'name': 'pc', 'status': 'loaded', 'reason': 'hot: CPU 90 C'}])
        self.assertEqual(sum(x['background'] for x in d.values()), 3)
        self.assertEqual((d['nuc']['interactive'], d['pc']['interactive']), (1, 0))
        self.assertEqual(d['pc']['capacity'], 1)   # hot: heat.hot_slots

    def test_a_status_look_keeps_the_last_demand(self):
        s = household.new_state('home', 0)
        s['members'] = {'ana': {'name': 'Ana'}}
        household.share_machine(s, 'ana', 'pc', {'slots': 2}, 0)
        household.heartbeat(s, 'ana', {'machines': {'pc': {'background': 2}}}, 1)
        household.heartbeat(s, 'ana', {}, 2)
        self.assertEqual(s['members']['ana']['demand']['pc']['background'], 2)


class SpareRoomTest(unittest.TestCase):
    def test_only_shared_accounts_near_their_target_with_room_are_spare(self):
        with tempfile.TemporaryDirectory() as home, as_user(home):
            cfg = make_cfg(home, 'Ana')
            users.set_share(cfg, 'agy-claude', True)
            now = datetime(2026, 11, 16, 12, tzinfo=UTC)
            soon = {'agy-claude': {'quota_plan': {'tasks': 3, 'expires': (now + timedelta(hours=2)).isoformat()}}}
            later = {'agy-claude': {'quota_plan': {'tasks': 3, 'expires': (now + timedelta(days=2)).isoformat()}}}
            self.assertEqual(sharing.spare_accounts(cfg, soon, now.timestamp()), ['agy-claude'])
            self.assertEqual(sharing.spare_accounts(cfg, later, now.timestamp()), [])
            panic = {'agy-claude': {**soon['agy-claude'], 'panic': True}}
            self.assertEqual(sharing.spare_accounts(cfg, panic, now.timestamp()), [])

    def test_an_offered_project_is_cloned_once_and_only_spare_accounts_take_it(self):
        with tempfile.TemporaryDirectory() as home, as_user(home):
            cfg = make_cfg(home, 'Ana')
            calls = []

            def clone(argv, **kw):
                calls.append(argv)
                os.makedirs(argv[-1])
                return SimpleNamespace(returncode=0, stderr='')
            result = {'answer': {'offers': [{'key': 'github.com/bo/site', 'check': 'npm test', 'queued': 3}]}}
            events = sharing.adopt_offers(cfg, result, 1000.0, run=clone)
            self.assertIn('adopted as hh-bo-site', events[0])
            self.assertEqual(sharing.adopt_offers(cfg, result, 1001.0, run=clone), [])
            self.assertEqual(len(calls), 1)
            task = {'id': 't1', 'repo': 'hh-bo-site', 'status': 'queued', 'attempts': []}
            cfg['_spare_accounts'] = []
            self.assertIn('share_spare', sharing.spare_hold(cfg, {'tasks': [task]})['t1'])
            self.assertEqual(autopilot.candidates(cfg, task), [])
            cfg['_spare_accounts'] = ['agy-claude']
            self.assertEqual(autopilot.candidates(cfg, task), ['agy-claude'])
            self.assertEqual(sharing.spare_hold(cfg, {'tasks': [task]}), {})

    def test_a_clone_that_fails_is_said_and_not_tried_every_round(self):
        with tempfile.TemporaryDirectory() as home, as_user(home):
            cfg = make_cfg(home, 'Ana')
            fail = mock.Mock(return_value=SimpleNamespace(returncode=128, stderr='Repository not found'))
            result = {'answer': {'offers': [{'key': 'github.com/bo/private', 'check': 'true', 'queued': 1}]}}
            self.assertIn('could not clone', sharing.adopt_offers(cfg, result, 1000.0, run=fail)[0])
            self.assertEqual(sharing.adopt_offers(cfg, result, 2000.0, run=fail), [])
            self.assertEqual(fail.call_count, 1)

    def test_the_coordinator_offers_projects_only_to_a_member_with_spare_room(self):
        s = household.new_state('home', 0)
        s['members'] = {'bo': {'name': 'Bo'}}
        household.sync_project(s, 'github.com/bo/site', {'publish': [{'title': 'x'}], 'meta': {'check': 'npm test'}}, 'bo', 0, 30)
        self.assertEqual(household.offers(s, set(), 1), [{'key': 'github.com/bo/site', 'check': 'npm test', 'queued': 1}])
        self.assertEqual(household.offers(s, {'github.com/bo/site'}, 1), [])


class TwoMembersFortyEightHoursTest(unittest.TestCase):
    """Ana works 08-16 UTC, Bo 18-02 UTC. pc (Ana's, 4 slots) and nuc (Bo's, 2 slots) are shared; both run background
    autopilots all day and prompt in their own hours; one shared project has 30 tasks. Every 15 minutes each member sends
    its heartbeat (demand), the coordinator grants, and each member's autopilot takes the shared tasks it holds."""

    def profile(self, hours):
        return [[1.0 if h in hours else 0.0 for h in range(24)] for _ in range(7)]

    PKEY = 'github.com/home/app'
    HOURS = {'ana': set(range(8, 16)), 'bo': set(range(18, 24)) | {0, 1}}

    def setUp(self):
        s = household.new_state('home', 0)
        s['members'] = {u: {'name': n, 'joined': 0, 'last_seen': 0} for u, n in (('ana', 'Ana'), ('bo', 'Bo'))}
        s['admin'] = 'ana'
        household.share_machine(s, 'ana', 'pc', {'slots': 4}, 0)
        household.share_machine(s, 'bo', 'nuc', {'slots': 2}, 0)
        tasks = [{'title': f'task {i}', 'check': 'true'} for i in range(30)]
        household.sync_project(s, self.PKEY, {'publish': tasks[:15]}, 'ana', 0, 30)
        household.sync_project(s, self.PKEY, {'publish': tasks[10:]}, 'bo', 0, 30)   # 5 overlap: deduplicated
        self.s, self.running, self.done_by, self.no_slot_since = s, {'ana': {}, 'bo': {}}, {}, {}

    def beats(self, at):
        now = at.timestamp()
        for u in ('ana', 'bo'):
            active = at.hour in self.HOURS[u]
            beat = {'prompting': active, 'profile': self.profile(self.HOURS[u]), 'utc_offset': 0,
                    'machines': {m: {'interactive': 2 if active else 0, 'background': 4, 'running': 0} for m in ('pc', 'nuc')}}
            household.heartbeat(self.s, u, beat, now)
        household.machine_round(self.s, now, 20, lambda m: fairshare.hour_weight(m.get('weight', 1), m.get('profile'), at))

    def check_grants(self, at):
        for name, g in self.s['grants'].items():
            cap, got, want = g['capacity'], g['grants'], g['demand']
            self.assertLessEqual(sum(got.values()), cap)
            self.assertEqual(sum(got.values()), min(cap, sum(want.values())), f'{name} idle while wanted at {at}')
            for u in ('ana', 'bo'):
                if at.hour in self.HOURS[u]:   # in their own hours a member's prompting is never left waiting
                    self.assertGreaterEqual(got[u], 2 if cap >= 4 else 1, f'{u} interactive on {name} at {at}')

    def check_starvation(self, at):
        now = at.timestamp()
        for u in ('ana', 'bo'):
            if sum(g['grants'][u] for g in self.s['grants'].values()):
                self.no_slot_since.pop(u, None)
                continue
            self.no_slot_since.setdefault(u, now)
            self.assertLessEqual(now - self.no_slot_since[u], 35 * 60, f'{u} starved at {at}')

    def work(self, u, now):
        """One member's autopilot round on the shared project: finish, renew, reserve and start (an hour per task)."""
        run = self.running[u]
        for key in [k for k, left in run.items() if left <= 1]:
            household.sync_project(self.s, self.PKEY, {'closed': [{'key': key, 'status': 'done'}]}, u, now, 30)
            self.assertNotIn(key, self.done_by)
            self.done_by[key] = u
            del run[key]
        for k in run:
            run[k] -= 1
        tasks = self.s['projects'][self.PKEY]['tasks']
        queued = [k for k, t in tasks.items() if t['status'] == 'queued' and k not in run]
        answer = household.sync_project(self.s, self.PKEY, {'held': list(run), 'want': queued, 'room': 2 - len(run)}, u, now, 30)
        for key in answer['mine']:
            if key not in run and key not in self.done_by and len(run) < 2:
                run[key] = 4   # started: its lease at full length at once (sharing.renew)
                household.claim(self.s, household.lease_key(self.PKEY, key), u, now, 30)

    def test_fair_work_conserving_no_starvation_and_every_task_once(self):
        self.assertEqual(len(self.s['projects'][self.PKEY]['tasks']), 30)
        start = datetime(2026, 11, 16, 0, tzinfo=UTC)
        for step in range(48 * 4):
            at = start + timedelta(minutes=15 * step)
            self.beats(at)
            self.check_grants(at)
            self.check_starvation(at)
            for u in (('ana', 'bo') if step % 2 else ('bo', 'ana')):   # who asks first alternates, as rounds do
                self.work(u, at.timestamp())
        self.assertEqual(len(self.done_by), 30, 'every shared task done')
        self.assertEqual(set(self.done_by.values()), {'ana', 'bo'}, 'both members took shared tasks')


if __name__ == '__main__':
    unittest.main()
