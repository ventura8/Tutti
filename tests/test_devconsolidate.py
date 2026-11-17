"""One adb host for the household's devices: `tutti devices move` consolidates the devices already on the target (other
machines' stray connections disconnected, Windows runners switched to adb_via_device_host), and a lease on such a runner
reaches the devices through the device machine's adb server over an ssh tunnel (tutti/adbvia.py). Fake hosts only."""
import io
import json
import os
import shutil
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

import tmpdirs
import yaml

from tutti import adbvia, devicescmd, devicesweb, devmove

PHONE = 'adb-46191FDAP003RM-OikUIT._adb-tls-connect._tcp'
TV = 'adb-52141HFAG0JYZ9-3pyAfV._adb-tls-connect._tcp'
CONFIG = """\
machines:
  nuc: {host: nuc}            # the device machine
  nuc-desktop: {host: nuc}
  pc: {host: w5090, transport: wsl}
  rpi: {host: rpi}
  laptop: {host: local}
devices:
  phone: {serial: PHONE, machine: nuc}
  tv: {serial: TV, machine: nuc, kind: tv}
projects:
  app: {runner: pc}
""".replace('PHONE', PHONE).replace('TV,', TV + ',')


class Fake:
    """A machine's adb: `listing` is what `adb devices` prints (None: no adb server runs; 'down': unreachable)."""

    def __init__(self, name, listing=None, services='', own=''):
        self.name, self.listing, self.services_text, self.own = name, listing, services, own
        self.calls = []
        self.reconnects = False

    def bash(self, body, timeout=120, **_k):
        self.calls.append(('bash', body))
        if self.listing == 'down':
            return 255, f'{self.name} did not answer: timed out'
        if self.listing is None:
            return 4, ''
        if self.listing == 'noadb':
            return 127, ''
        return 0, 'List of devices attached\n' + ''.join(f'{s}\tdevice\n' for s in self.listing)

    def adb(self, *args, timeout=60):
        self.calls.append(args)
        if args[0] == 'disconnect' and not self.reconnects:
            self.listing = [s for s in self.listing if s != args[1]]
        if args == ('devices',):
            return 0, 'List of devices attached\n' + ''.join(f'{s}\tdevice\n' for s in self.own.split())
        if args[-1] == 'get-serialno':
            return 0, {'192.168.1.107:33785': '52141HFAG0JYZ9'}.get(args[1], 'other') + '\n'
        return 0, ''

    def services(self):
        from tutti import adbpair
        return adbpair.parse_services(self.services_text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdirs.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, 'tutti.config.yaml')
        with open(self.path, 'w', encoding='utf8') as f:
            f.write(CONFIG)
        self.cfg = {**yaml.safe_load(CONFIG), '_path': self.path}
        for n, d in self.cfg['devices'].items():
            d['name'] = n
        self.nuc = Fake('nuc', services='adb-46191FDAP003RM-OikUIT\t_adb-tls-connect._tcp\t192.168.1.252:35825\n',
                        own='192.168.1.107:33785 ' + PHONE)
        self.hosts = {'nuc': self.nuc, 'nuc-desktop': Fake('nuc-desktop', ['should-not-be-asked']),
                      'pc': Fake('pc', ['192.168.1.252:35825', PHONE, '192.168.1.107:33785', 'emulator-5580']),
                      'rpi': Fake('rpi', 'down'), 'laptop': Fake('laptop', None)}

    def scan(self, names=()):
        devs = devmove.named(self.cfg, 'nuc', list(names))
        return devmove.scan(self.cfg, 'nuc', devs, lambda n: self.hosts[n], self.nuc)

    def data(self):
        with open(self.path, encoding='utf8') as f:
            return yaml.safe_load(f)


class ScanTest(Base):
    def test_strays_are_found_by_guid_and_by_ip_and_nothing_else_is_touched(self):
        plan = self.scan()
        self.assertEqual(plan['strays'], {'pc': [('192.168.1.252:35825', 'phone'), (PHONE, 'phone'), ('192.168.1.107:33785', 'tv')]})
        self.assertNotIn('emulator-5580', str(plan['strays']))   # an emulator is not a household device

    def test_the_targets_own_box_is_not_asked_and_unreachable_machines_are_skipped_with_a_reason(self):
        plan = self.scan()
        self.assertEqual(self.hosts['nuc-desktop'].calls, [])
        self.assertIn('did not answer', plan['skipped']['rpi'])
        self.assertIn('no adb server runs', plan['skipped']['laptop'])

    def test_a_machine_with_no_adb_server_is_asked_only_whether_one_listens(self):
        self.scan()
        body = self.hosts['laptop'].calls[0][1]
        self.assertIn('sport = :5037', body)
        self.assertIn('Get-NetTCPConnection -LocalPort 5037', body)   # a Windows adb.exe: its listener on Windows

    def test_a_wsl_runner_is_switched_and_already_switched_ones_are_left(self):
        self.assertEqual(self.scan()['runners'], ['pc'])
        self.cfg['machines']['pc']['adb_via_device_host'] = True
        self.assertEqual(self.scan()['runners'], [])
        self.cfg['machines']['pc'] = {'host': 'w5090'}
        self.assertIn('pc', self.scan()['runners_skipped'])

    def test_only_the_named_devices_are_consolidated(self):
        plan = self.scan(['tv'])
        self.assertEqual(plan['strays'], {'pc': [('192.168.1.107:33785', 'tv')]})

    def test_the_plan_says_each_step_and_nothing_left_says_so(self):
        lines = devmove.consolidate_lines(self.scan())
        self.assertIn(f'pc: adb disconnect {PHONE} (phone; the pairing stays)', lines)
        self.assertIn('pc: set machines.pc.adb_via_device_host: true (its suites use nuc\'s adb over an ssh tunnel)', lines)
        self.hosts['pc'].listing = ['emulator-5580']
        self.cfg['machines']['pc']['adb_via_device_host'] = True
        plan = self.scan()
        self.assertFalse(devmove.pending(plan))
        self.assertIn('nothing to consolidate', devmove.consolidate_lines(plan)[-1])


class ConsolidateTest(Base):
    def test_strays_are_disconnected_never_unpaired_and_the_runner_flag_is_written_with_comments_kept(self):
        said = []
        result = devmove.consolidate(self.cfg, self.scan(), lambda n: self.hosts[n], said.append, sleep=lambda s: None)
        pc = self.hosts['pc']
        self.assertEqual([c for c in pc.calls if c[0] == 'disconnect'],
                         [('disconnect', '192.168.1.252:35825'), ('disconnect', PHONE), ('disconnect', '192.168.1.107:33785')])
        self.assertFalse(any(c[0] in ('kill-server', 'pair', 'unpair') for c in pc.calls if isinstance(c, tuple) and c[0] != 'bash'))
        self.assertEqual(pc.listing, ['emulator-5580'])
        self.assertEqual(result['back'], {})
        self.assertTrue(self.data()['machines']['pc']['adb_via_device_host'])
        with open(self.path, encoding='utf8') as f:
            self.assertIn('# the device machine', f.read())
        self.assertIn('consolidated on nuc: 3 stray connection(s) disconnected', devmove.consolidate_summary(result, 'nuc')[0])

    def test_a_connection_that_comes_back_by_itself_is_reported(self):
        self.hosts['pc'].reconnects = True
        result = devmove.consolidate(self.cfg, self.scan(), lambda n: self.hosts[n], lambda m: None, sleep=lambda s: None)
        self.assertIn('pc', result['back'])
        self.assertIn('ADB_MDNS_AUTO_CONNECT=0', devmove.consolidate_summary(result, 'nuc')[1])


class CommandTest(Base):
    def args(self, **kw):
        return SimpleNamespace(**{'to': 'nuc', 'names': [], 'dry_run': True, 'install_adb': False, 'keep_key': False,
                                  'repair': False, **kw})

    def run_move(self, **kw):
        out = io.StringIO()
        with mock.patch.object(devicescmd, 'host_for', lambda cfg, n, adb=None: self.hosts[n]), redirect_stdout(out):
            rc = devicescmd.cmd_move(self.cfg, self.args(**kw))
        return rc, out.getvalue()

    def test_a_dry_run_with_every_device_on_the_target_shows_the_strays_not_nothing_to_move(self):
        rc, out = self.run_move()
        self.assertEqual(rc, 0)
        self.assertNotIn('nothing to move', out)
        self.assertIn(f'pc: adb disconnect {PHONE}', out)
        self.assertIn('dry run: nothing changed', out)
        self.assertEqual([c for c in self.hosts['pc'].calls if c[0] == 'disconnect'], [])
        self.assertNotIn('adb_via_device_host', open(self.path, encoding='utf8').read())

    def test_the_real_run_consolidates(self):
        with mock.patch.object(devmove.time, 'sleep', lambda s: None):
            rc, out = self.run_move(dry_run=False)
        self.assertEqual(rc, 0, out)
        self.assertIn('consolidated on nuc', out)
        self.assertTrue(self.data()['machines']['pc']['adb_via_device_host'])


class WebTest(Base):
    def post(self, body):
        return devicesweb.move_post(SimpleNamespace(cfg=self.cfg), json.dumps(body).encode(), hosts_fn=lambda n, adb=None: self.hosts[n],
                                    popen=lambda argv, **k: SimpleNamespace(argv=argv, poll=lambda: None))

    def test_the_dry_run_answers_the_consolidation(self):
        code, answer = self.post({'to': 'nuc'})
        self.assertEqual(code, 200)
        self.assertTrue(answer['todo'])
        self.assertEqual(answer['moves'], [])
        self.assertEqual(answer['runners'], ['pc'])
        self.assertIn(PHONE, answer['strays']['pc'])

    def test_nothing_to_do_is_refused_and_a_second_move_waits(self):
        self.hosts['pc'].listing = []
        self.cfg['machines']['pc']['adb_via_device_host'] = True
        code, answer = self.post({'to': 'nuc'})
        self.assertFalse(answer['todo'])
        self.assertIn('nothing to do', answer['plan'][-1])
        self.assertEqual(self.post({'to': 'nuc', 'dry_run': False})[0], 409)

    def test_the_move_starts_once_and_its_progress_is_read(self):
        devicesweb.MOVE.update(proc=None, log='')
        with mock.patch.object(devicesweb.config, 'state_dir', lambda c: self.tmp):
            code, answer = self.post({'to': 'nuc', 'dry_run': False})
            self.assertEqual(code, 202, answer)
            self.assertEqual(self.post({'to': 'nuc', 'dry_run': False})[0], 409)
        with open(answer['log'], 'w', encoding='utf8') as f:
            f.write('consolidated on nuc\n')
        code, status = self.post({'action': 'status'})
        self.assertEqual((code, status['running'], status['lines']), (200, True, ['consolidated on nuc']))
        devicesweb.MOVE.update(proc=None, log='')


class AdbViaTest(unittest.TestCase):
    CFG = {'machines': {'pc': {'host': 'w5090', 'adb_via_device_host': True}, 'nuc': {'host': 'nuc.lan'}, 'laptop': {}}}

    def test_a_runner_that_asks_uses_the_devices_machine_only_when_they_share_one(self):
        self.assertEqual(adbvia.host_of(self.CFG, 'pc', [{'machine': 'nuc'}, {'machine': 'nuc'}]), 'nuc')
        self.assertEqual(adbvia.host_of(self.CFG, 'pc', [{'machine': 'nuc'}, {'machine': 'laptop'}]), '')
        self.assertEqual(adbvia.host_of(self.CFG, 'pc', [{'machine': 'pc'}]), '')
        self.assertEqual(adbvia.host_of(self.CFG, 'pc', [{'machine': 'mars'}]), '')
        self.assertEqual(adbvia.host_of({'machines': {'pc': {}, 'nuc': {}}}, 'pc', [{'machine': 'nuc'}]), '')

    def test_the_prefix_opens_the_tunnel_and_exports_the_socket_for_windows(self):
        line = adbvia.prefix(self.CFG, 'nuc')
        self.assertIn("'5038:127.0.0.1:5037','nuc.lan'", adbvia.windows_tunnel(adbvia.ssh_target(self.CFG, 'nuc')))
        self.assertIn('nuc.lan', line)
        self.assertIn('export ADB_SERVER_SOCKET=tcp:127.0.0.1:5038', line)
        self.assertIn('ADB_SERVER_SOCKET', line.split('WSLENV=')[1])
        self.assertTrue(adbvia.windows_tunnel('nuc').startswith('if (-not (Get-NetTCPConnection -LocalPort 5038'))

    def test_an_inline_wslenv_list_gets_the_socket_too(self):
        self.assertEqual(adbvia.with_socket('WSLENV=ANDROID_SERIAL:TUTTI_DEVICE_CLASS powershell.exe -C x'),
                         'WSLENV=ANDROID_SERIAL:TUTTI_DEVICE_CLASS:ADB_SERVER_SOCKET powershell.exe -C x')
        same = 'WSLENV=ADB_SERVER_SOCKET:A powershell.exe; echo $WSLENV=x'
        self.assertEqual(adbvia.with_socket(same), same)


class LeaseViaTest(unittest.TestCase):
    """A lease on a runner with adb_via_device_host: checked on the device machine, never connected or disconnected on the
    runner, and the command gets the tunnel prefix and the socket in its inline WSLENV."""

    def run_lease(self, via):
        cfg = {'_path': '/x', 'machines': {'pc': {'host': 'w5090', 'transport': 'wsl', 'adb_via_device_host': via},
                                           'nuc': {'host': 'nuc'}},
               'devices': {'phone': {'name': 'phone', 'serial': PHONE, 'machine': 'nuc', 'adb': '/n/adb'}}}
        made, ran = [], []

        class Host:
            def __init__(self, name, machine, adb=None):
                self.name, self.adb_path, self.calls = name, adb, []
                made.append(self)

            def adb(self, *args, timeout=60):
                self.calls.append(args)
                return 0, 'device\n'

        def claim(c, a):
            print('ADDR=192.168.1.252:35825' if a.action == 'claim' else 'released')
            return 0
        a = SimpleNamespace(names=['phone', '--', 'WSLENV=ANDROID_SERIAL powershell.exe run'], runner='pc', who='t', action='', minutes=1)
        with mock.patch('tutti.adbhost.Host', Host), mock.patch.object(devicescmd, 'cmd_claim', claim), \
                mock.patch('tutti.remote.sh', lambda m, line, **k: ran.append(line) or SimpleNamespace(returncode=0)), \
                mock.patch.object(devicescmd, 'Renewer', mock.MagicMock()), redirect_stdout(io.StringIO()):
            rc = devicescmd.lease_on_runner(cfg, a)
        return rc, made[0], ran[0]

    def test_via_the_device_host(self):
        rc, host, line = self.run_lease(True)
        self.assertEqual((rc, host.name, host.adb_path), (0, 'nuc', '/n/adb'))
        self.assertNotIn('disconnect', [c[0] for c in host.calls])
        self.assertIn('export ADB_SERVER_SOCKET=tcp:127.0.0.1:5038', line)
        self.assertIn('WSLENV=ANDROID_SERIAL:ADB_SERVER_SOCKET powershell.exe run', line)

    def test_without_the_flag_the_runner_uses_its_own_adb_as_before(self):
        rc, host, line = self.run_lease(False)
        self.assertEqual((rc, host.name), (0, 'pc'))
        self.assertIn(('disconnect', '192.168.1.252:35825'), host.calls)
        self.assertNotIn('ADB_SERVER_SOCKET', line)


if __name__ == '__main__':
    unittest.main()
