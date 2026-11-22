"""Two-sided link repair (owner, 2026-11-22). This side: an unreachable host is diagnosed (name, port, key, nothing),
repaired where Tutti can, retried with backoff, and its card says so; a paused host is left alone. The member side: the
client falls back to the coordinator's other addresses and keeps the one that answered. No real machine or network."""
import tempfile
import unittest
from unittest import mock

from tutti import coordinator, linkheal, sharing


class Diagnosis(unittest.TestCase):
    def test_each_cause_and_a_working_link(self):
        self.assertEqual(linkheal.cause(False, None, 255, ''), 'dns')
        self.assertEqual(linkheal.cause(True, False, 255, ''), 'port')
        self.assertEqual(linkheal.cause(True, None, 255, ''), 'timeout')
        self.assertEqual(linkheal.cause(True, True, 255, 'Permission denied (publickey).'), 'auth')
        self.assertIsNone(linkheal.cause(True, True, 0, ''))

    def test_diagnose_walks_the_link_step_by_step(self):
        def refused(addr, timeout):
            raise ConnectionRefusedError()
        c, why = linkheal.diagnose({'host': 'pc.example'}, resolve=lambda h, p: [], connect=refused,
                                   target=lambda h: ('pc.example', 22))
        self.assertEqual((c, why), ('port', 'its ssh port is closed'))

        def nodns(h, p):
            raise OSError('no name')
        self.assertEqual(linkheal.diagnose({'host': 'x'}, resolve=nodns, target=lambda h: ('x', 22))[0], 'dns')

    def test_wake_on_lan_packet(self):
        sent = []
        linkheal.wake('02:00:00:00:00:10', send=sent.append)
        self.assertEqual(sent[0][:6], b'\xff' * 6)
        self.assertEqual(len(sent[0]), 102)
        with self.assertRaises(ValueError):
            linkheal.wake('02:00', send=sent.append)


class Rounds(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.cfg = {'project': {'state': d.name}, 'machines': {'pc': {'host': 'pc.example', 'mac': '02:00:00:00:00:10'},
                                                               'nas': {'host': 'nas.example'}, 'here': {'host': 'local'}}}
        self.tried = []

    def round(self, up, now, paused=()):
        state = [{'name': 'pc', 'reachable': 'pc' in up, 'status': 'paused' if 'pc' in paused else 'free'},
                 {'name': 'nas', 'reachable': True, 'status': 'free'}, {'name': 'here', 'reachable': True}]
        return linkheal.round_(self.cfg, state, now, diagnose_fn=lambda m: ('timeout', 'nothing answers in time'),
                               repair_fn=lambda m, c: self.tried.append(m['name']) or ['sent Wake-on-LAN'])

    def test_retried_with_backoff_shown_and_said_when_back(self):
        records, events = self.round(set(), 1000)
        self.assertEqual((records['pc']['tries'], records['pc']['next']), (1, 1060))
        self.assertIn('pc: not reachable (nothing answers in time); try 1: sent Wake-on-LAN', events[0])
        self.assertIn('reconnecting: try 1', linkheal.note(records['pc']))
        self.round(set(), 1030)   # before its next try: not tried again
        self.assertEqual(self.tried, ['pc'])
        records, _ = self.round(set(), 1061)
        self.assertEqual((records['pc']['tries'], records['pc']['next']), (2, 1061 + 120))
        records, events = self.round({'pc'}, 1200)
        self.assertEqual(events, ['pc: reconnected after 2 tries'])
        self.assertEqual(linkheal.note(records['pc']), '')
        self.assertEqual(self.tried, ['pc', 'pc'])   # nas answered all along; this computer is never dialled

    def test_a_paused_host_is_never_repaired(self):
        self.round(set(), 1000, paused={'pc'})
        self.assertEqual(self.tried, [])

    def test_backoff_steps(self):
        self.assertEqual([linkheal.next_wait(n) for n in (1, 2, 3, 4, 5, 9)], [60, 120, 300, 600, 1800, 1800])


class MemberSide(unittest.TestCase):
    def test_the_client_falls_back_to_another_address_and_keeps_it(self):
        c = coordinator.Client({'coordinator': '192.0.2.1:4778', 'alternates': ['192.0.2.1:4778', '[fd00::1]:4778'],
                                'token': 't'})
        seen = []

        def post(path, body, key, user=''):
            seen.append(c.m['coordinator'])
            if c.m['coordinator'] == '192.0.2.1:4778':
                raise coordinator.Unreachable('down')
            return {'ok': True, 'addresses': ['[fd00::1]:4778', '192.0.2.9:4778']}
        with mock.patch.object(c, '_post', side_effect=post):
            answer = c.post('/hh/sync', {}, 't')
        self.assertEqual((seen, c.switched), (['192.0.2.1:4778', '[fd00::1]:4778'], '[fd00::1]:4778'))
        saved = []
        self.assertTrue(sharing.keep_addresses(c, answer, read=lambda: dict(c.m, coordinator='192.0.2.1:4778'),
                                               write=saved.append))
        self.assertEqual(saved[0]['coordinator'], '[fd00::1]:4778')
        self.assertEqual(saved[0]['alternates'], ['[fd00::1]:4778', '192.0.2.9:4778'])

    def test_when_no_address_answers_the_first_error_is_said(self):
        c = coordinator.Client({'coordinator': 'a:1', 'alternates': ['b:1'], 'token': 't'})
        with mock.patch.object(c, '_post', side_effect=coordinator.Unreachable('a did not answer')):
            with self.assertRaisesRegex(coordinator.Unreachable, 'a did not answer'):
                c.post('/hh/sync', {}, 't')
        self.assertEqual(c.m['coordinator'], 'a:1')   # unchanged
        self.assertFalse(sharing.keep_addresses(c, {}, read=lambda: {'coordinator': 'a:1'}, write=lambda m: None))


if __name__ == '__main__':
    unittest.main()
