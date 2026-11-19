"""Roles of machines and devices (tutti/roles.py), media devices on the LAN (tutti/lanmedia.py), dispatch by role and
`tutti roles set|unset`. Fake probes and packets; nothing leaves the temp folder."""
import os
import tempfile
import unittest
from types import SimpleNamespace

from tutti import dispatch, lanmedia, roles, rolescmd

PC = 'os=Linux\nwsl=1\nwindows=1\ncores=32\nmem_gb=62\ndocker=1\npwsh=1\nvram_mb=32607\nollama=1\nsender=chrome\n'
NUC = 'os=Linux\ncores=16\nmem_gb=31\ndocker=1\nadb=1\nemulator=1\navds=2\ndesktop=1\nwg=1\n'
MAC = 'os=Darwin\ncores=8\nmem_gb=8\nxcrun=1\ndesktop=1\n'


class DetectTest(unittest.TestCase):
    def test_a_windows_pc_with_a_gpu(self):
        got = roles.detect_machine(PC)
        for r in ('windows-test-runner', 'ui-desktop', 'linux-test-runner', 'docker-host', 'gpu-ai', 'build-machine', 'cast-sender'):
            self.assertIn(r, got)
        self.assertIn('32 GB', got['gpu-ai'])

    def test_a_linux_box_with_emulators_and_a_mac(self):
        nuc = roles.detect_machine(NUC)
        self.assertEqual(sorted(nuc), ['android-test-runner', 'build-machine', 'docker-host', 'emulator-host', 'linux-test-runner',
                                       'ui-desktop', 'vpn-node'])
        mac = roles.detect_machine(MAC)
        self.assertEqual(sorted(mac), ['airplay-sender', 'ios-webkit-runner', 'macos-test-runner', 'ui-desktop'])

    def test_bad_answers_detect_nothing(self):
        self.assertEqual(roles.detect_machine(''), {})
        self.assertEqual(roles.detect_machine('garbage\ncores=lots\navds=x\nemulator=1'), {})
        self.assertEqual(roles.detect_machine('ollama=1'), {'gpu-ai': 'detected: Ollama (no GPU found: CPU models)'})

    def test_config_roles(self):
        cfg = {'failover': {'backup': 'nuc'}, 'browser': {'machine': ''}, 'devices': {'p': {'machine': 'nuc'}}}
        self.assertEqual(sorted(roles.from_config(cfg, 'here', {'transport': 'local'})), ['chrome-browser', 'main'])
        self.assertEqual(sorted(roles.from_config(cfg, 'nuc', {'transport': 'ssh'})), ['android-test-runner', 'backup-main'])
        self.assertEqual(sorted(roles.from_config(cfg, 'pc', {'transport': 'wsl', 'desktop_sender': True})),
                         ['cast-sender', 'windows-test-runner'])

    def test_devices(self):
        self.assertEqual(sorted(roles.detect_device({}, {'cast': '1,1,0,1'})), ['android-device', 'cast-receiver', 'tv-device'])
        self.assertEqual(sorted(roles.detect_device({}, {'cast': '0,0,1,1'})), ['android-device', 'cast-receiver', 'cast-sender'])
        self.assertEqual(sorted(roles.detect_device({'kind': 'tv'}, {})), ['android-device', 'tv-device'])


def down(m, script, **kw):
    return SimpleNamespace(stdout='', returncode=255)   # ssh could not reach the machine


class EffectiveTest(unittest.TestCase):
    def test_override_add_and_off(self):
        det = {'docker-host': 'detected: docker', 'main': 'config: here'}
        self.assertEqual([(r['role'], r['source']) for r in roles.effective({}, det)], [('docker-host', 'detected'), ('main', 'config')])
        got = roles.effective({'roles_add': ['gpu-ai', 'main'], 'roles_off': ['docker-host']}, det)
        self.assertEqual([(r['role'], r['source']) for r in got], [('main', 'config'), ('gpu-ai', 'set')])
        self.assertEqual(roles.names(roles.effective({'roles': ['ui-desktop']}, det)), ['ui-desktop'])
        self.assertEqual(roles.effective({'roles': []}, det), [])   # an empty override is no roles
        self.assertEqual(roles._probe({'transport': 'ssh'}, {'at': 1.0, 'detected': {'x': 'y'}}, 1e6, down, False)[0]['detected'],
                         {'x': 'y'})

    def test_machine_roles_probe_once_a_day_and_keep_an_answer_when_one_fails(self):
        cfg = {'machines': {'pc': {'transport': 'wsl'}, 'nuc': {'transport': 'ssh'}, 'phone': {'transport': 'android'}}}
        asked = []

        def sh(m, script, **kw):
            asked.append(m['transport'])
            if m['transport'] == 'ssh' and len(asked) > 2:
                raise OSError('down')
            return SimpleNamespace(stdout=PC if m['transport'] == 'wsl' else NUC)
        with tempfile.TemporaryDirectory() as d:
            first = roles.machine_roles(cfg, 1000.0, sh=sh, folder=d)
            self.assertIn('gpu-ai', roles.names(first['pc']))
            roles.machine_roles(cfg, 2000.0, sh=sh, folder=d)
            self.assertEqual(asked, ['wsl', 'ssh'])
            again = roles.machine_roles(cfg, 2000.0, sh=sh, folder=d, refresh=True)
            self.assertEqual(roles._probe({'transport': 'ssh'}, {'at': 2000.0}, 2700.0, sh, False)[1], True)   # never answered: soon
            self.assertIn('emulator-host', roles.names(again['nuc']))   # the failed probe kept its last answer
            self.assertEqual(roles.names(roles.cached(cfg, folder=d)['nuc']), roles.names(again['nuc']))
            self.assertEqual(again['phone'], [])


class DispatchTest(unittest.TestCase):
    def test_a_suite_goes_only_where_every_role_is(self):
        w = {'targets': ['machine'], 'roles': ['windows-test-runner', 'ui-desktop'], 'machines': None, 'devices': None}
        self.assertTrue(dispatch.compatible({'kind': 'machine', 'name': 'pc', 'roles': ['windows-test-runner', 'ui-desktop', 'gpu-ai']}, w))
        self.assertFalse(dispatch.compatible({'kind': 'machine', 'name': 'nuc', 'roles': ['ui-desktop', 'linux-test-runner']}, w))
        self.assertFalse(dispatch.compatible({'kind': 'machine', 'name': 'x'}, w))
        self.assertTrue(dispatch.compatible({'kind': 'machine', 'name': 'x'}, {**w, 'roles': []}))

    def test_bad_roles_in_a_suite_are_config_errors(self):
        self.assertEqual(dispatch._roles_errors('k', None), [])
        self.assertIn('a list of roles', dispatch._roles_errors('k', 'ui-desktop')[0])
        self.assertIn('not a role', dispatch._roles_errors('k', ['ui-desktop', 'flying-car'])[0])
        self.assertEqual(dispatch._roles_errors('k', ['gpu-ai']), [])
        self.assertIn('each role is a name', dispatch._roles_errors('k', [{}])[0])


SOUNDBAR_TXT = b'\x08_airplay\x04_tcp\x05local\x00' + bytes([14]) + b'model=HT-A7000' + bytes([11]) + b'fmodel=nope'
CAST = b'\x0b_googlecast\x04_tcp\x05local\x00' + bytes([14]) + b'fn=Living Room' + bytes([13]) + b'md=Google TV1'


class LanTest(unittest.TestCase):
    def test_mdns_answers_become_devices_with_roles(self):
        found = lanmedia.parse_mdns([(SOUNDBAR_TXT, '10.0.0.5'), (CAST, '10.0.0.9'), (b'nothing here', '10.0.0.7')])
        view = {d['address']: d for d in lanmedia.view(found)}
        self.assertEqual(view['10.0.0.5']['name'], 'HT-A7000')
        self.assertEqual([r['role'] for r in view['10.0.0.5']['roles']], ['airplay-receiver'])
        self.assertEqual([r['role'] for r in view['10.0.0.9']['roles']], ['chromecast-builtin', 'smart-tv'])
        self.assertEqual(view['10.0.0.9']['name'], 'Living Room')   # fn= (the Google Home name) over the model
        self.assertNotIn('10.0.0.7', view)
        self.assertTrue(lanmedia.mdns_query('_airplay._tcp').endswith(b'\x05local\x00\x00\x0c\x80\x01'))

    def test_ssdp_and_avahi(self):
        bar = 'HTTP/1.1 200 OK\r\nSERVER: Linux/2.6 UPnP/1.0 HT-A7000/0.01\r\nST: urn:...:MediaRenderer:1'
        found = lanmedia.parse_ssdp([(bar, '10.0.0.5'),
                                     ('HTTP/1.1 200 OK\r\nST: urn:...:InternetGatewayDevice:1', '10.0.0.1')])
        self.assertEqual(found['10.0.0.5']['name'], 'HT-A7000')
        self.assertNotIn('10.0.0.1', found)
        avahi = '=;eth0;IPv4;Soundbar;_spotify-connect._tcp;local;sb.local;10.0.0.5;443;"VERSION=1.0"\n+;eth0;IPv4;x;_raop._tcp;local\n'
        lanmedia.parse_avahi(avahi, found)
        self.assertIn('spotify-connect', found['10.0.0.5']['roles'])

    def test_scanned_once_a_day_and_overridden_by_the_config(self):
        scans = []

        def scan():
            scans.append(1)
            return lanmedia.view(lanmedia.parse_mdns([(SOUNDBAR_TXT, '10.0.0.5')]))
        cfg = {'project': {'state': '/nonexistent'}, 'lan_devices': {'10.0.0.5': {'roles_add': ['bluetooth-audio']}}}
        with tempfile.TemporaryDirectory() as d:
            got = lanmedia.devices(cfg, 1000.0, folder=d, scan_fn=scan)
            lanmedia.devices(cfg, 2000.0, folder=d, scan_fn=scan)
            self.assertEqual(len(scans), 1)
            self.assertEqual(roles.names(got[0]['roles']), ['airplay-receiver', 'bluetooth-audio'])

    def test_a_failing_scan_finds_nothing(self):
        def run(*a, **k):
            raise OSError('no avahi')
        self.assertEqual(lanmedia.scan(run=run, which=lambda n: '/usr/bin/x', ssdp_fn=list, mdns_fn=list), [])


class CommandTest(unittest.TestCase):
    def test_set_and_unset_edit_the_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'tutti.config.yaml')
            with open(path, 'w', encoding='utf8') as f:
                f.write('machines:\n  pc:\n    transport: wsl\n')
            out = []
            cfg = {'_path': path, 'machines': {'pc': {}}, 'project': {'state': d}}
            ns = SimpleNamespace(action='set', name='pc', role='gpu-ai', all=False)
            self.assertEqual(rolescmd.cmd_roles(cfg, ns, out=out.append), 0)
            self.assertEqual(rolescmd.cmd_roles(cfg, SimpleNamespace(**{**vars(ns), 'action': 'unset', 'role': 'ui-desktop'}),
                                                out=out.append), 0)
            with open(path, encoding='utf8') as f:
                text = f.read()
            self.assertIn('roles_add', text)
            self.assertIn('roles_off', text)
            self.assertEqual(rolescmd.cmd_roles(cfg, SimpleNamespace(**{**vars(ns), 'role': 'flying-car'}), out=out.append), 2)
            self.assertEqual(rolescmd.cmd_roles(cfg, SimpleNamespace(**{**vars(ns), 'role': None}), out=out.append), 2)
            self.assertEqual(rolescmd.cmd_roles(cfg, SimpleNamespace(**{**vars(ns), 'name': 'pcc'}), out=out.append), 2)   # a typo
            self.assertEqual(rolescmd._kind(cfg, '10.0.0.5'), 'lan_devices')
            roles.edit(path, 'machines', 'pc', 'gpu-ai', False)
            with open(path, encoding='utf8') as f:
                self.assertNotIn('gpu-ai', f.read().split('roles_off')[0])
            with open(path, 'a', encoding='utf8') as f:
                f.write('    roles: [ui-desktop]\n')
            self.assertEqual(rolescmd.cmd_roles(cfg, ns, out=out.append), 2)   # an override is set: refused


if __name__ == '__main__':
    unittest.main()
