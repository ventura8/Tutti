"""When a device goes away its shards move to devices that can run them, wait with a reason when none can, and go back
when it returns (tutti/reroute.py, dispatch.follow_devices)."""
import shutil
import tempfile
import unittest

from tutti import dispatch, reroute

CFG = {'devices': {'pixel9pro': {'kind': 'phone', 'model': 'Pixel 9 Pro', 'capabilities': ['camera', 'touchscreen']},
                   'pixeltablet': {'kind': 'tablet', 'capabilities': ['touchscreen']},
                   'googletv': {'kind': 'tv', 'capabilities': ['tv']}}}


def shard(key, devices, **kw):
    return {'project': 'RoRadio', 'key': key, 'devices': devices, **kw}


class RerouteTest(unittest.TestCase):
    def test_a_phones_shards_move_to_the_tablet_and_tv_work_stays_on_the_tv(self):
        pend = [shard('e2e/1', ['pixel9pro']), shard('e2e/2', ['pixel9pro']), shard('tv/1', ['googletv'])]
        out, report = reroute.plan(CFG, pend, {'pixel9pro': 'planned absence'})
        self.assertEqual([w['devices'] for w in out], [['pixeltablet'], ['pixeltablet'], ['googletv']])
        self.assertEqual(report['pixel9pro']['moved'], {'pixeltablet': 2})
        lines = reroute.summary(CFG, report, {'pixel9pro': 'planned absence'})
        self.assertEqual(lines, ['Pixel 9 Pro away (planned absence): 2 shards moved to pixeltablet'])

    def test_work_only_that_device_can_do_waits_with_the_reason(self):
        pend = [shard('cam/1', ['pixel9pro'], requires={'kinds': ['phone'], 'capabilities': ['camera']})]
        out, report = reroute.plan(CFG, pend, {'pixel9pro': 'away'})
        self.assertEqual(out[0]['devices'], ['pixel9pro'])
        self.assertIn('no other device fits', report['pixel9pro']['waits'][0])
        self.assertIn('1 waits for it', reroute.summary(CFG, report, {'pixel9pro': 'away'})[0])

    def test_nothing_moves_while_the_device_is_here_or_another_named_one_is(self):
        pend = [shard('e2e/1', ['pixel9pro']), shard('e2e/2', ['pixel9pro', 'pixeltablet'])]
        self.assertEqual(reroute.plan(CFG, pend, {})[0], pend)   # back: its shards are its own again
        out, _ = reroute.plan(CFG, pend, {'pixel9pro': 'away'})
        self.assertEqual(out[1]['devices'], ['pixel9pro', 'pixeltablet'])   # the tablet already takes it

    def test_the_dispatcher_tells_once_and_again_when_the_device_returns(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        cfg = {**CFG, 'project': {'state': tmp}}
        st = {'events': []}
        rows = [{'name': 'pixel9pro', 'status': 'held'}, {'name': 'pixeltablet', 'status': 'ready'}]
        out = dispatch.follow_devices(cfg, st, 1.0, [shard('e2e/1', ['pixel9pro'])], rows, {})
        self.assertEqual(out[0]['devices'], ['pixeltablet'])
        dispatch.follow_devices(cfg, st, 2.0, [shard('e2e/1', ['pixel9pro'])], rows, {})
        self.assertEqual(len(st['events']), 1)
        back = dispatch.follow_devices(cfg, st, 3.0, [shard('e2e/1', ['pixel9pro'])], [{'name': 'pixel9pro', 'status': 'ready'}], {})
        self.assertEqual(back[0]['devices'], ['pixel9pro'])
        self.assertIn('every device is back', st['events'][0]['text'])


class PairTest(unittest.TestCase):
    CFG = {'devices': {'pixeltablet': {'kind': 'tablet', 'roles': ['cast-sender']},
                       'googletv': {'kind': 'tv', 'roles': ['cast-receiver']}}}

    def test_a_cast_suite_holds_the_receiver_so_both_are_leased_together(self):
        w = reroute.paired(self.CFG, shard('cast/1', ['pixeltablet'], pair={'role': 'cast-receiver'}))
        self.assertEqual(w['holds'], ['googletv'])
        self.assertFalse(dispatch.holds_free(w, {'device:pixeltablet'}, set()))   # the TV busy: the pair waits
        self.assertTrue(dispatch.holds_free(w, {'device:pixeltablet', 'device:googletv'}, set()))

    def test_no_receiver_here_means_it_waits_with_the_reason(self):
        w = reroute.paired(self.CFG, shard('cast/1', ['pixeltablet'], pair={'role': 'cast-receiver'}), away={'googletv'})
        self.assertIn('no cast-receiver is here', w['wait'])
        plain = shard('e2e/1', ['pixeltablet'])
        self.assertIs(reroute.paired(self.CFG, plain), plain)

    def test_a_paired_claim_takes_both_or_none(self):
        from types import SimpleNamespace

        from tutti import devicescmd
        calls = []

        def fake(cfg, a):
            calls.append((a.action, a.names[0]))
            return 3 if a.names[0] == 'googletv' and a.action == 'claim' else 0
        old = devicescmd.cmd_claim
        devicescmd.cmd_claim = lambda cfg, a: fake(cfg, a) if len(a.names) == 1 else old(cfg, a)
        try:
            rc = devicescmd.claim_together({}, SimpleNamespace(names=['pixeltablet', 'googletv'], action='claim', who='ff'))
        finally:
            devicescmd.cmd_claim = old
        self.assertEqual(rc, 3)
        self.assertEqual(calls, [('claim', 'pixeltablet'), ('claim', 'googletv'), ('release', 'pixeltablet')])


class CastRolesTest(unittest.TestCase):
    def test_the_households_devices_are_detected_as_expected(self):
        from tutti import castroles as c
        self.assertEqual(c.decide('1,1,0,1'), [c.RECEIVER])                # Google TV Streamer: a receiver only
        self.assertEqual(c.decide('0,0,1,1'), [c.RECEIVER, c.SENDER])      # Pixel Tablet, Pixel 9 Pro: both
        self.assertEqual(c.decide(''), [])
        self.assertEqual(c.roles_of({'roles': [c.SENDER]}, [c.RECEIVER]), [c.SENDER])   # the admin's override

    def test_machines_with_chrome_are_senders_and_asked_once_a_day(self):
        from types import SimpleNamespace

        from tutti import castroles as c
        cfg = {'project': {'state': '/nonexistent'}, 'machines': {'pc': {'transport': 'ssh'}, 'nuc': {'transport': 'ssh'}}}
        asked, store = [], {}
        answers = {'pc': 'sender=chrome', 'nuc': 'sender=none'}

        def sh(m, script, **kw):
            name = 'pc' if m is cfg['machines']['pc'] else 'nuc'
            asked.append(name)
            return SimpleNamespace(stdout=answers[name])
        roles = c.machine_roles(cfg, 1000.0, sh=sh, read=lambda: store, write=store.update)
        self.assertEqual(roles, {'pc': [c.SENDER], 'nuc': []})
        c.machine_roles(cfg, 2000.0, sh=sh, read=lambda: store, write=store.update)
        self.assertEqual(asked, ['pc', 'nuc'])   # the second look within a day asks nobody

    def test_the_scheduler_picks_any_present_pair_and_a_sender_target_only(self):
        roles = {'pixel9pro': ['cast-receiver', 'cast-sender'], 'pixeltablet': ['cast-receiver', 'cast-sender'],
                 'googletv': ['cast-receiver']}
        cast = shard('cast/1', ['pixeltablet'], pair={'role': 'cast-receiver', 'sender': True})
        self.assertEqual(reroute.paired(CFG, cast, set(), roles)['holds'], ['pixel9pro'])   # the phone receives
        self.assertEqual(reroute.paired(CFG, cast, {'pixel9pro'}, roles)['holds'], ['googletv'])   # phone away: the TV
        w = {**cast, 'targets': ['android', 'machine']}
        pc = {'kind': 'machine', 'id': 'machine:pc', 'name': 'pc', 'roles': ['cast-sender']}
        nuc = {'kind': 'machine', 'id': 'machine:nuc', 'name': 'nuc', 'roles': []}
        self.assertTrue(dispatch.compatible(pc, {**w, 'machines': None}))   # Chrome on the PC sends to the TV
        self.assertFalse(dispatch.compatible(nuc, {**w, 'machines': None}))


if __name__ == '__main__':
    unittest.main()
