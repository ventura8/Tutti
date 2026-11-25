"""Use the cheap workers while the main agent is overspent (owner, 2026-11-25): a cloud CLI worker is not held by its machine's
heat or load, and an overspent main agent's share moves to the other accounts' room (tutti/planner.py `shift`)."""
import unittest
from datetime import datetime, timezone
from unittest import mock

from tests.test_limits import ACCOUNTS, NOW, state
from tutti import autopilot, fleet, routing

HOT = [{'name': 'laptop', 'status': 'loaded', 'reason': 'too hot: CPU 99 C'}]
BUSY = [{'name': 'laptop', 'status': 'busy', 'reason': 'no free slot'}]
LOAD = [{'name': 'laptop', 'status': 'loaded', 'reason': 'load 9.0 is over 2.0 x 4 cores'}]


def cfg_of(kind):
    return {'workers': {'w': {'kind': kind, 'machine': 'laptop'}}, 'machines': {'laptop': {'quiet_hours': ['00:00-23:59']}}, 'fleet': {}}


class CloudWorkerIsNotHeld(unittest.TestCase):
    def test_heat_load_busy_quiet_hours_and_memory_do_not_hold_a_cli_worker(self):
        cfg, at = cfg_of('cli'), datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
        cfg['fleet'] = {'memory_floor_gb': 4}
        for rows in (HOT, LOAD, BUSY):
            self.assertEqual(fleet.governor(cfg, 'w', rows, at, mem_gb=0.5), '')

    def test_pause_off_offline_and_a_yield_flag_still_hold_it(self):
        for status, words in (('held', 'paused by the owner'), ('off', 'turned off'), ('offline', 'offline'), ('paused', 'yield flag')):
            why = fleet.governor(cfg_of('cli'), 'w', [{'name': 'laptop', 'status': status, 'reason': 'x'}])
            self.assertIn(words, why)

    def test_a_local_model_keeps_the_full_governor(self):
        cfg, at = cfg_of('ollama'), datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
        self.assertIn('too hot', fleet.governor(cfg, 'w', HOT))
        self.assertIn('CPU', fleet.governor(cfg, 'w', BUSY))
        self.assertIn('quiet hours', fleet.governor(cfg, 'w', [], at))
        self.assertIn('memory', fleet.governor({**cfg, 'fleet': {'memory_floor_gb': 4}}, 'w', [], mem_gb=1.0))

    def test_next_worker_does_not_hold_codex_on_a_hot_laptop(self):
        cfg = {'workers': {'codex': {'kind': 'cli', 'machine': 'laptop'}}, 'accounts': {'codex': {'worker': 'codex'}},
               'routing': {}, 'machines': {'laptop': {}}, 'autopilot': {}, 'project': {'state': '/nonexistent'}}
        from tutti import state as st
        s = {'name': 'codex', 'verdict': 'behind', 'used': 10.0, 'panic_at': 95, 'quota_plan': None}
        with mock.patch('tutti.autopilot.main_in_panic', return_value=False), mock.patch('tutti.tiers.choose',
                                                                                         return_value={'args': [], 'label': ''}):
            r = st.next_worker(cfg, [s], machines=HOT)
            self.assertEqual(r['worker'], 'codex')
            self.assertNotIn('held by its machine', r['reason'])
            cfg['workers']['codex']['kind'] = 'ollama'
            self.assertIn('held by its machine', st.next_worker(cfg, [s], machines=HOT)['reason'])

    def test_the_autopilot_gate_follows_the_same_rule(self):
        step = {'worker': 'w', 'kind': 'cli'}
        self.assertEqual(autopilot.cli_host_out(cfg_of('cli'), step, HOT), '')
        self.assertIn('offline', autopilot.cli_host_out(cfg_of('cli'), step, [{'name': 'laptop', 'status': 'offline', 'reason': ''}]))


OVER = [('2026-01-05T23:00Z', 80), ('2026-01-06T02:00Z', 84)]   # this window used 4 points of its 2-point share


class ShiftWhenMainAhead(unittest.TestCase):
    def test_without_the_shift_the_window_share_blocks(self):
        self.assertIn('used its share', routing.account_unfit(state(OVER)))

    def test_with_the_shift_the_whole_room_before_the_target_is_open_and_the_line_says_why(self):
        s = state(OVER)
        entries_state = _shifted(OVER)
        p = entries_state['quota_plan']
        self.assertEqual(routing.account_unfit(entries_state), '')
        self.assertGreater(p['tasks'], 0)
        self.assertEqual(p['shifted'], 'claude')
        self.assertIn('main agent claude is ahead of pace: agy may spend ahead', p['why'])
        self.assertEqual(s['quota_plan']['tasks'], 0)

    def test_the_panic_line_still_holds_a_shifted_account(self):
        s = _shifted(OVER, five_used=94, panic_at=95)
        self.assertEqual(s['quota_plan']['tasks'], 0)   # 1 point under its panic line cannot fit a task

    def test_a_limit_with_no_room_left_stays_closed(self):
        s = _shifted([('2026-01-05T23:00Z', 99), ('2026-01-06T02:00Z', 100)])
        self.assertTrue(routing.account_unfit(s))

    def test_ahead_main_account_is_found_and_the_switch_turns_it_off(self):
        cfg = {'routing': {}, 'autopilot': {}}
        ahead = [{'name': 'claude', 'state': 'ok', 'verdict': 'ahead', 'used': 89.0, 'panic_at': 95}]
        self.assertEqual(autopilot.main_overspent(cfg, ahead), 'claude')
        self.assertEqual(autopilot.main_overspent(cfg, {'claude': ahead[0]}), 'claude')
        self.assertEqual(autopilot.main_overspent({'routing': {'shift_when_main_ahead': False}}, ahead), '')
        self.assertEqual(autopilot.main_overspent(cfg, [{**ahead[0], 'verdict': 'on pace'}]), '')
        self.assertEqual(autopilot.main_overspent(cfg, [{**ahead[0], 'panic': True}]), '')   # panic is the takeover's job
        self.assertEqual(autopilot.main_overspent(cfg, []), '')
        other = {'routing': {}, 'autopilot': {'main_accounts': ['gpt']}}
        self.assertEqual(autopilot.main_overspent(other, ahead), '')

    def test_a_stale_reading_already_above_the_pace_still_counts_as_ahead(self):
        cfg = {'routing': {}, 'autopilot': {}}
        stale = {'name': 'claude', 'state': 'ok', 'verdict': 'stale', 'used': 80.0, 'expected': 66.0, 'tolerance': 5.0,
                 'panic_at': 95}
        self.assertEqual(autopilot.main_overspent(cfg, [stale]), 'claude')   # use only grows: 80 then is at least 80 now
        self.assertEqual(autopilot.main_overspent(cfg, [{**stale, 'used': 68.0}]), '')   # within the tolerance: unknown
        self.assertEqual(autopilot.main_overspent(cfg, [{**stale, 'used': None}]), '')

    def test_an_ahead_account_gets_room_only_when_shifted(self):
        s = {'name': 'x', 'verdict': 'ahead', 'used': 20.0, 'panic_at': 95, 'limits': [], 'quota_plan': {'shifted': 'claude'}}
        cfg = {'accounts': {'x': {}}, 'autopilot': {}}
        with mock.patch('tutti.autopilot.forecast_room', return_value=2), mock.patch('tutti.autopilot.trigger_accounts', return_value=[]):
            self.assertEqual(autopilot.ordinary_account_room(cfg, 'x', s, 0), (1, ''))
            s['quota_plan'] = {}
            self.assertEqual(autopilot.ordinary_account_room(cfg, 'x', s, 0)[0], 0)


def _shifted(weekly, five_used=30, panic_at=None):
    from tutti import pacing
    accounts = {**ACCOUNTS}
    if panic_at:
        accounts['agy'] = {**accounts['agy'], 'panic_at': panic_at}
    entries = [{'at': at, 'account': 'agy-weekly', 'used': u, 'note': ''} for at, u in weekly]
    entries.append({'at': NOW.isoformat(), 'account': 'agy', 'used': five_used, 'note': ''})
    return pacing.account_state('agy', accounts['agy'], entries, NOW, accounts, None, shift='claude')



class RunAutoShift(unittest.TestCase):
    """`tutti run auto` plans the cheap accounts with the shift too, not only the dashboard (2026-11-25: gemini stayed held)."""
    CFG = {'routing': {}, 'autopilot': {}, 'accounts': {'claude': {}, 'agy-gemini': {'worker': 'agy'}}}

    def run_shifted(self, main_verdict):
        from unittest import mock

        from tutti import cli
        calls = []

        def state(name, a, entries, now, *args, shift=None):
            calls.append((name, shift))
            return {'name': name, 'state': 'ok', 'verdict': 'shifted' if shift else 'held'}
        accounts = [{'name': 'claude', 'state': 'ok', 'verdict': main_verdict, 'used': 91.0, 'panic_at': 95},
                    {'name': 'agy-gemini', 'state': 'ok', 'verdict': 'held'}]
        with mock.patch.object(cli.pacing, 'account_state', side_effect=state), \
                mock.patch.object(cli.pacing, 'plan_args', return_value=()):
            return cli.shifted(self.CFG, accounts, [], None), calls

    def test_an_ahead_main_replans_the_others_with_the_shift(self):
        out, calls = self.run_shifted('ahead')
        self.assertEqual(calls, [('agy-gemini', 'claude')])
        self.assertEqual([s['verdict'] for s in out], ['ahead', 'shifted'])

    def test_a_main_on_pace_changes_nothing(self):
        out, calls = self.run_shifted('on pace')
        self.assertEqual(calls, [])
        self.assertEqual(out[1]['verdict'], 'held')


if __name__ == '__main__':
    unittest.main()
