"""A suite that runs on a named runner machine (the PC's PowerShell and adb) with its devices on another machine (the NUC),
over the leases' addresses: the dispatcher's command, the runner's environment, and the lease taken and given back."""
import unittest
from types import SimpleNamespace
from unittest import mock

from tutti import devicescmd, dispatch


class RunnerTest(unittest.TestCase):
    def test_the_runner_line_fills_the_addresses(self):
        line = devicescmd.runner_line('run-e2e.ps1 -Serial {addr:pixeltablet} -CastTablet {addr:googletv}',
                                      {'pixeltablet': '1.2.3.4:5', 'googletv': '1.2.3.9:7'})
        self.assertIn('-Serial 1.2.3.4:5 -CastTablet 1.2.3.9:7', line)
        self.assertIn('ANDROID_SERIAL=1.2.3.4:5', line)
        self.assertIn('TUTTI_ADDR_GOOGLETV=1.2.3.9:7', line)

    def test_the_dispatcher_runs_a_runner_suite_through_a_paired_lease(self):
        cfg = {'_path': '/c.yaml', 'project': {'state': '/tmp'}}
        s = {'project': 'RoRadio', 'name': 'cast', 'run': 'pwsh run-e2e.ps1 -Filter {shard}', 'runner': 'pc', 'dir': '/tmp'}
        argv = dispatch.test_argv(cfg, {'kind': 'android', 'name': 'pixeltablet', 'holds': ['googletv']}, s, 'TabletCastUITest')
        self.assertEqual(argv[argv.index('lease') + 1:argv.index('--runner')], ['pixeltablet', 'googletv'])
        self.assertEqual(argv[-1], 'pwsh run-e2e.ps1 -Filter TabletCastUITest')
        self.assertIn('pc', argv)

    def test_a_runner_lease_connects_runs_there_and_gives_both_back(self):
        cfg = {'devices': {'pixeltablet': {}, 'googletv': {}}, 'machines': {'pc': {'transport': 'ssh'}}}
        actions, ran, adb = [], [], []

        def claim(c, a):
            actions.append((a.action, list(a.names)))
            if a.action == 'claim':
                print('ADDR=1.2.3.4:5\nADDR=1.2.3.9:7')
            return 0
        host = SimpleNamespace(adb=lambda *x, timeout=30: adb.append(x) or (0, ''))
        a = SimpleNamespace(names=['pixeltablet', 'googletv', '--', 'run', '{addr:googletv}'], runner='pc', who='ff', action='lease')
        with mock.patch.object(devicescmd, 'cmd_claim', claim), mock.patch('tutti.adbhost.Host', lambda *x, **k: host), \
                mock.patch('tutti.remote.sh', lambda m, line, **k: ran.append(line) or SimpleNamespace(returncode=4)):
            rc = devicescmd.lease_on_runner(cfg, a)
        self.assertEqual(rc, 4)
        self.assertIn('run 1.2.3.9:7', ran[0])
        self.assertEqual(actions, [('claim', ['pixeltablet', 'googletv']), ('release', ['pixeltablet', 'googletv'])])
        self.assertEqual([x[0] for x in adb], ['connect', 'connect', 'disconnect', 'disconnect'])


class IdleLeaseTest(unittest.TestCase):
    def test_a_lease_is_renewed_only_while_its_command_runs(self):
        import time
        renewed = []
        a = SimpleNamespace(names=['pixeltablet', 'googletv'], who='ci', action='claim', minutes=120)
        with devicescmd.Renewer({}, a, claim_fn=lambda c, x: renewed.append((x.action, x.minutes)), every=0.05):
            time.sleep(0.3)   # the command runs
        n = len(renewed)
        time.sleep(0.2)   # it ended: no more renewals, so an idle lease lapses within IDLE_MINUTES
        self.assertGreaterEqual(n, 3)
        self.assertEqual(len(renewed), n)
        self.assertEqual(set(renewed), {('claim', devicescmd.IDLE_MINUTES)})
        self.assertEqual(a.minutes, 120)   # the caller's own namespace is not changed

    def test_a_command_lease_asks_for_the_idle_length_not_hours(self):
        cfg = {'devices': {'pixeltablet': {}}, 'machines': {'pc': {'transport': 'ssh'}}}
        seen = []

        def claim(c, a):
            seen.append((a.action, a.minutes))
            return 3   # busy: nothing runs, nothing is held
        a = SimpleNamespace(names=['pixeltablet', '--', 'run'], runner='pc', who='ci', action='lease', minutes=120)
        with mock.patch.object(devicescmd, 'cmd_claim', claim):
            self.assertEqual(devicescmd.lease_on_runner(cfg, a), 3)
        self.assertEqual(seen, [('claim', devicescmd.IDLE_MINUTES)])


if __name__ == '__main__':
    unittest.main()
