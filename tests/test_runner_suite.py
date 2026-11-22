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
        self.assertEqual(argv[argv.index('--who') + 2:argv.index('--')], ['pixeltablet', 'googletv'])
        from tutti import cli
        self.assertEqual(cli.parser().parse_args(argv[argv.index('devices'):]).names[:2], ['pixeltablet', 'googletv'])
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
        host = SimpleNamespace(name='pc', adb=lambda *x, timeout=30: adb.append(x) or (0, 'device\n'))
        a = SimpleNamespace(names=['pixeltablet', 'googletv', '--', 'run', '{addr:googletv}'], runner='pc', who='ff', action='lease')
        with mock.patch.object(devicescmd, 'cmd_claim', claim), mock.patch('tutti.adbhost.Host', lambda *x, **k: host), \
                mock.patch('tutti.remote.sh', lambda m, line, **k: ran.append(line) or SimpleNamespace(returncode=4)):
            rc = devicescmd.lease_on_runner(cfg, a)
        self.assertEqual(rc, 4)
        self.assertIn('run 1.2.3.9:7', ran[0])
        self.assertEqual(actions, [('claim', ['pixeltablet', 'googletv']), ('release', ['pixeltablet', 'googletv'])])
        self.assertEqual([x[0] for x in adb], ['connect', 'connect', '-s', '-s', 'disconnect', 'disconnect'])

    def test_runner_preserves_command_separator(self):
        cfg = {'devices': {'phone': {}}, 'machines': {'pc': {'transport': 'ssh'}}}
        rc, ran, _ = self.lease(cfg, ['phone', '--', 'run', '--', '-x'])
        self.assertEqual(rc, 0)
        self.assertIn('run -- -x', ran[0])

    def test_local_lease_preserves_command_separator(self):
        import contextlib
        a = SimpleNamespace(names=['phone', '--', 'run', '--', '-x'], runner=None, who='ci', action='lease')
        def claim(cfg, args):
            if args.action == 'claim':
                print('ADDR=serial')
            return 0
        with mock.patch.object(devicescmd, 'cmd_claim', claim), \
                mock.patch('tutti.adbhost.Host'), \
                mock.patch.object(devicescmd, 'Renewer', return_value=contextlib.nullcontext()), \
                mock.patch('subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(devicescmd.cmd_lease({}, a), 0)
        self.assertEqual(run.call_args.args[0], ['run', '--', '-x'])

    def lease(self, cfg, words, state='device\n'):
        actions, ran = [], []

        def claim(c, a):
            actions.append(a.action)
            if a.action == 'claim':
                print('ADDR=1.2.3.4:5')
            return 0
        host = SimpleNamespace(name='pc', adb=lambda *x, timeout=30: (0, state))
        a = SimpleNamespace(names=words, runner='pc', who='ff', action='lease')
        with mock.patch.object(devicescmd, 'cmd_claim', claim), mock.patch('tutti.adbhost.Host', lambda *x, **k: host), \
                mock.patch('tutti.remote.sh', lambda m, line, **k: ran.append(line) or SimpleNamespace(returncode=0)):
            return devicescmd.lease_on_runner(cfg, a), ran, actions

    def test_a_device_the_runner_cannot_reach_never_runs(self):
        cfg = {'devices': {'pixeltablet': {'name': 'pixeltablet'}}, 'machines': {'pc': {'transport': 'ssh'}}}
        rc, ran, actions = self.lease(cfg, ['pixeltablet', '--', 'run'], state='offline\n')
        self.assertEqual((rc, ran), (3, []))
        self.assertEqual(actions, ['claim', 'release'])

    def test_a_tv_refuses_a_denied_command_and_the_script_learns_its_class(self):
        cfg = {'devices': {'tv': {'name': 'tv', 'kind': 'tv'}}, 'machines': {'pc': {'transport': 'ssh'}}}
        rc, ran, _ = self.lease(cfg, ['tv', '--', 'adb', 'shell', 'wm', 'size', '1x1'])
        self.assertEqual((rc, ran), (5, []))
        rc, ran, _ = self.lease(cfg, ['tv', '--', 'run'])
        self.assertEqual(rc, 0)
        self.assertIn('TUTTI_DEVICE_CLASS=tv', ran[0])

    def test_a_project_runner_is_its_suites_default_and_test_goes_there_unsynced(self):
        from tutti import cli
        cfg = {'projects': {'MyApp': {'runner': 'pc', 'tests': {'e2e': {'run': 'x {shard}', 'shards': ['A']}}}}}
        self.assertEqual(dispatch.suites(cfg)[0]['runner'], 'pc')
        seen = []
        with mock.patch.object(devicescmd, 'lease_on_runner', lambda c, a: seen.append((a.runner, a.names)) or 0):
            a = SimpleNamespace(track='MyApp', device='pixeltablet', command=['--', 'pwsh', 'x'], on=None, hold_device=None,
                                input=None, fetch=None)
            self.assertEqual(cli.cmd_test(cfg, a), 0)
        self.assertEqual(seen, [('pc', ['pixeltablet', '--', 'pwsh x'])])
        a.command, a.fetch = ['pwsh', 'x'], ['r.xml']
        self.assertEqual(cli.cmd_test(cfg, a), 2)   # --fetch needs a synced tree
        a.command, a.fetch = ['run', '--filter', ''], None
        self.assertEqual(cli.cmd_test(cfg, a), 2)   # an empty filter never runs


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


class YieldTest(unittest.TestCase):
    def test_a_runner_suite_waits_while_its_runner_yields(self):
        pend = [{'project': 'RoRadio', 'key': 'cast/1', 'runner': 'w5090'}, {'project': 'RoRadio', 'key': 'e2e/1'}]
        out = dispatch.runners_free(pend, [{'name': 'w5090', 'paused': True, 'status': 'paused'}])
        self.assertIn('yield flag is up', out[0]['wait'])
        self.assertNotIn('wait', out[1])   # a suite on the devices' own machine is not held back
        self.assertNotIn('wait', dispatch.runners_free(pend, [{'name': 'w5090', 'status': 'free'}])[0])

    def test_a_runner_lease_takes_nothing_while_the_runner_yields(self):
        cfg = {'devices': {'pixeltablet': {}}, 'machines': {'pc': {'transport': 'ssh', 'yield_file': '/y'}}}
        a = SimpleNamespace(names=['pixeltablet', '--', 'run'], runner='pc', who='ci', action='lease', minutes=120)
        with mock.patch('tutti.remote.yield_note', lambda m: 'RoRadio pipeline'), \
                mock.patch.object(devicescmd, 'cmd_claim', lambda c, x: self.fail('nothing is leased')):
            self.assertEqual(devicescmd.lease_on_runner(cfg, a), 3)


class TestCommandWords(unittest.TestCase):
    def test_options_after_the_track_are_options(self):
        from tutti import cli
        a = SimpleNamespace(command=['--on', 'w5090-win', '--fetch', 'C:\\x\\r.txt', '--', 'pwsh', '-c', 'a b'], dir=None, on=None,
                            device=None, wait_minutes=30, fetch=None, input=None, hold_device=None)
        cli.late_options(a)
        self.assertEqual((a.on, a.fetch, a.command), ('w5090-win', ['C:\\x\\r.txt'], ['pwsh', '-c', 'a b']))
        self.assertEqual(cli.command_line(a.command), "pwsh -c 'a b'")
        self.assertEqual(cli.command_line(['make', '&&', 'run', '$ANDROID_SERIAL', '--', '-x']), 'make && run $ANDROID_SERIAL -- -x')
        self.assertEqual(cli.command_line(['echo "x y"']), 'echo "x y"')

    def test_windows_paths_and_the_project_tree(self):
        from tutti import cli
        self.assertEqual(cli.wsl_path('C:\\Users\\v\\R-tr'), '/mnt/c/Users/v/R-tr')
        self.assertEqual(cli.wsl_path('/home/x'), '/home/x')
        cfg = {'projects': {'MyApp': {'windows': {'pc': 'C:\\R'}}}, 'machines': {'pc': {'transport': 'wsl'}}}
        ran = []
        a = SimpleNamespace(track='MyApp', command=['--on', 'pc', 'pwsh', 'x y'], dir=None, on=None, device=None,
                            wait_minutes=30, fetch=None, input=None, hold_device=None)
        with mock.patch('tutti.remote.sh', lambda m, line, **k: ran.append(line) or SimpleNamespace(returncode=0)), \
                mock.patch('tutti.mayrun.refusal', lambda cfg, name: ''):
            self.assertEqual(cli.cmd_test(cfg, a), 0)
        self.assertEqual(ran, ["cd /mnt/c/R && pwsh 'x y'"])
        a.command, a.on = ['--on', 'pc', 'pwsh', 'x y'], None
        with mock.patch('tutti.remote.sh', lambda m, line, **k: ran.append(line)), \
                mock.patch('tutti.mayrun.refusal', lambda cfg, name: 'too hot: CPU 99 C'):
            self.assertEqual(cli.cmd_test(cfg, a), 3)   # a hot or paused machine is refused before anything runs there
        self.assertEqual(len(ran), 1)

    def test_a_windows_file_is_fetched_where_it_is(self):
        import base64
        import os
        import tempfile

        from tutti import cli
        got = SimpleNamespace(returncode=0, stdout=base64.b64encode(b'ok').decode(), stderr='')
        with tempfile.TemporaryDirectory() as d, mock.patch('tutti.remote.sh', lambda m, line, **k: got):
            self.assertEqual(cli.fetch_windows({'name': 'pc'}, 'C:\\a\\res.txt', d), 0)
            with open(os.path.join(d, 'res.txt'), 'rb') as f:
                self.assertEqual(f.read(), b'ok')


if __name__ == '__main__':
    unittest.main()
