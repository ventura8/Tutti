"""Pairing over wireless debugging and moving devices to another machine: the QR payload, the pairing service found in fake
`adb mdns services` output, the TV's code flow, the key copy at mode 600 with nothing logged, the move updating the config,
and the rollback. No real phone and no real machine: adb answers come from a fake host; the key copy runs bash over the
`local` transport into temp folders."""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest
from types import SimpleNamespace

import yaml

from tutti import adbhost, adbpair, devices, devicesweb, devmove, keepon, qr, remote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUID = 'adb-ABC123-xYz'
SERIAL = f'{GUID}._adb-tls-connect._tcp'
MDNS = textwrap.dedent(f'''\
    List of discovered mdns services
    {GUID}\t_adb-tls-connect._tcp.\t192.168.1.23:41867
    tutti-q1w2e3\t_adb-tls-pairing._tcp\t192.168.1.23:37415
    adb-TV999-aaa\t_adb-tls-pairing._tcp\t192.168.1.40:40001
    garbage line
''')
LOCAL = {'transport': 'local'}


class FakeHost:
    """adb on a pretend machine: answers from tables, records every call."""

    def __init__(self, name='nuc', mdns=MDNS, paired=GUID, up=True, model='Pixel 9 Pro', fp='new', path='/usr/bin/adb'):
        self.name, self.mdns, self.paired, self.up, self.model, self.fp, self._path = name, mdns, paired, up, model, fp, path
        self.calls, self.connected, self.key, self.wifi = [], False, None, '0'

    def adb(self, *args, timeout=60):
        self.calls.append(args)
        if args[:2] == ('mdns', 'services'):
            return 0, self.mdns() if callable(self.mdns) else self.mdns
        if args[0] == 'pair':
            ok = self.paired and args[2] in ('pw', '123456')
            return (0, f'Successfully paired to {args[1]} [guid={self.paired}]') if ok else (1, 'Failed: Wrong password')
        if args[0] == 'connect':
            self.connected = self.up
            return 0, 'connected'
        if args[-2:] == ('getprop', 'ro.product.model'):
            return 0, self.model + '\n'
        if args[-1] == 'ro.build.characteristics':
            return 0, 'tv\n' if 'TV' in self.model else 'nosdcard\n'
        if args[-2:] == ('list', 'features'):
            return 0, 'feature:android.hardware.camera.any\nfeature:android.hardware.telephony\nfeature:android.hardware.wifi\n'
        if args[-1] == 'ro.build.version.sdk':
            return 0, '35\n'
        return self.more(args)

    def more(self, args):
        """Wireless debugging's setting and `adb devices`."""
        if args[-4:-1] == ('put', 'global', 'adb_wifi_enabled'):
            self.wifi = args[-1]
            return 0, ''
        if args[-3:] == ('get', 'global', 'adb_wifi_enabled'):
            return 0, self.wifi + '\n'
        if args == ('devices',):
            return 0, 'List of devices attached\n' + ''.join(f'{s}\tdevice\n' for s in getattr(self, 'listed', []))
        return 0, ''

    def state(self, serial):
        return 'device' if self.connected else 'missing'

    def services(self):
        return adbpair.parse_services(self.mdns)

    def path(self):
        return self._path

    def version(self):
        return 35 if self._path else 0

    def bash(self, body, stdin=None, timeout=120):
        return 0, self._path + '\n'

    def fingerprint(self):
        return self.fp

    def read_key(self):
        return ('PRIVATE-KEY-TEXT', 'PUB adb@pc') if self.fp else ('', '')

    def write_key(self, priv, pub, stamp):
        self.key = (priv, pub)
        return True, f'tutti-backup-{stamp}' if self.fp else ''

    def restore_key(self, suffix):
        self.calls.append(('restore', suffix))
        return True

    def remove_key(self):
        self.calls.append(('remove',))
        return True

    def restart(self):
        self.calls.append(('restart',))
        return True

    def lock_holder(self, path, stale=None):
        return ''


class QrTest(unittest.TestCase):
    def test_the_payload_is_the_android_studio_format(self):
        self.assertEqual(adbpair.qr_payload('tutti-ab12cd', 'Pw12'), 'WIFI:T:ADB;S:tutti-ab12cd;P:Pw12;;')
        name, password = adbpair.session()
        self.assertTrue(name.startswith('tutti-') and name[6:].isalnum() and password.isalnum() and len(password) == 12)

    def test_a_value_that_would_break_the_payload_is_refused(self):
        for bad in ('a;b', 'a:b', '', 'q"'):
            with self.assertRaises(ValueError):
                adbpair.qr_payload('tutti-x', bad)

    def test_the_matrix_has_finders_and_the_terminal_draws_it(self):
        m = qr.matrix(adbpair.qr_payload(*adbpair.session()))
        self.assertEqual(len(m), 29)   # 43 bytes: version 3
        self.assertTrue(all(m[0][:7]) and all(m[6][:7]) and not any(m[1][1:6]))
        self.assertIsNone(qr.matrix('x' * 200))
        lines = qr.terminal('WIFI:T:ADB;S:a;P:b;;').splitlines()
        self.assertEqual(len({len(line) for line in lines}), 1)


class MdnsTest(unittest.TestCase):
    def test_services_are_parsed_with_or_without_the_trailing_dot(self):
        s = adbpair.parse_services(MDNS)
        self.assertEqual(len(s), 3)
        self.assertEqual(s[0], (GUID, '_adb-tls-connect._tcp', '192.168.1.23:41867'))
        self.assertEqual(adbpair.connect_address(s, GUID), '192.168.1.23:41867')
        self.assertIsNone(adbpair.connect_address(s, 'adb-other'))

    def test_the_qr_pairing_is_found_by_its_name_and_the_code_one_is_new_and_not_ours(self):
        s = adbpair.parse_services(MDNS)
        self.assertEqual(adbpair.pairing_service(s, 'tutti-q1w2e3'), '192.168.1.23:37415')
        self.assertIsNone(adbpair.pairing_service(s, 'tutti-other'))
        self.assertEqual(adbpair.pairing_service(s), '192.168.1.40:40001')
        self.assertIsNone(adbpair.pairing_service(s, ignore={('adb-TV999-aaa', '192.168.1.40:40001')}))

    def test_avahi_lines_are_read_as_services(self):
        text = (f'+;eth0;IPv4;{GUID};_adb-tls-connect._tcp;local\n'
                f'=;eth0;IPv4;{GUID};_adb-tls-connect._tcp;local;p.local;192.168.1.252;46003;""\n'
                f'=;eth0;IPv6;{GUID};_adb-tls-connect._tcp;local;p.local;fe80::1;46003;""\n')
        self.assertEqual(adbpair.parse_avahi(text), [(GUID, '_adb-tls-connect._tcp', '192.168.1.252:46003')])

    def test_the_guid_and_the_version_are_read(self):
        self.assertEqual(adbpair.parse_paired('Successfully paired to 1.2.3.4:5 [guid=adb-X-y]'), 'adb-X-y')
        self.assertEqual(adbpair.parse_paired('Failed: Wrong password'), '')
        self.assertEqual(adbpair.guid_of(SERIAL), GUID)
        self.assertEqual(adbpair.guid_of('192.168.1.2:5555'), '')
        self.assertEqual(adbpair.parse_version('Android Debug Bridge version 1.0.41\nVersion 35.0.2-12147458\n'), 35)
        self.assertEqual(adbpair.parse_version('bash: adb: not found'), 0)


class FlowTest(unittest.TestCase):
    def test_a_tv_gets_the_code_flow_and_a_phone_the_qr_code(self):
        self.assertEqual(adbpair.flow_for('tv'), 'code')
        self.assertEqual(adbpair.flow_for('phone'), 'qr')
        self.assertEqual(adbpair.flow_for(None), 'qr')
        self.assertEqual(adbpair.flow_for('tv', 'usb'), 'usb')
        with self.assertRaises(adbpair.PairError):
            adbpair.flow_for(None, 'bluetooth')
        self.assertIn('Android TV OS build', adbpair.steps('tv', 'code')[0])
        self.assertIn('QR code', adbpair.steps('phone', 'qr')[-1])

    def test_kind_capabilities_and_name_come_from_the_device(self):
        feats = adbpair.parse_features('feature:android.software.leanback\nfeature:android.hardware.wifi\nfeature:reqGlEsVersion=0x30002\n')
        self.assertEqual(adbpair.kind_of('nosdcard', feats), 'tv')
        self.assertEqual(adbpair.kind_of('tablet,nosdcard', set()), 'tablet')
        self.assertEqual(adbpair.kind_of('nosdcard', set()), 'phone')
        self.assertEqual(adbpair.capabilities(feats), ['tv', 'wifi'])
        self.assertEqual(adbpair.name_for('Pixel 9 Pro', {'pixel9pro'}), 'pixel9pro2')
        self.assertEqual(adbpair.name_for('', set()), 'device')

    def test_the_qr_flow_waits_for_the_scan_then_pairs_and_connects(self):
        polls = iter(['List of discovered mdns services\n', MDNS, MDNS, MDNS])
        host = FakeHost(mdns=lambda: next(polls))
        serial, live = adbpair.pair(host, name='tutti-q1w2e3', password='pw', wait=10, sleep=lambda s: None)
        self.assertEqual((serial, live), (SERIAL, SERIAL))
        self.assertIn(('pair', '192.168.1.23:37415', 'pw'), host.calls)
        info = adbpair.describe(host, serial)
        self.assertEqual((info['model'], info['kind'], info['sdk']), ('Pixel 9 Pro', 'phone', 35))
        self.assertEqual(info['capabilities'], ['camera', 'telephony', 'wifi'])

    def test_the_code_flow_skips_a_pairing_screen_open_before_it_started(self):
        host = FakeHost()
        before = adbpair.baseline(host)
        self.assertEqual(before, {('tutti-q1w2e3', '192.168.1.23:37415'), ('adb-TV999-aaa', '192.168.1.40:40001')})
        with self.assertRaisesRegex(adbpair.PairError, 'no device asked'):
            adbpair.pair(host, code='123456', ignore=before, wait=2, sleep=lambda s: None)

    def test_a_device_adb_lists_by_ip_port_is_connected_and_kept_by_its_mdns_name(self):
        host = FakeHost(up=False)
        host.state = lambda s: 'device' if s == '192.168.1.23:41867' and ('connect', s) in host.calls else 'missing'
        serial, live = adbpair.pair(host, name='tutti-q1w2e3', password='pw', wait=3, sleep=lambda s: None)
        self.assertEqual((serial, live), (SERIAL, '192.168.1.23:41867'))

    def test_a_wrong_code_or_no_device_says_what_to_do(self):
        with self.assertRaisesRegex(adbpair.PairError, 'wrong code'):
            adbpair.pair(FakeHost(), code='000000', wait=0, sleep=lambda s: None)
        with self.assertRaisesRegex(adbpair.PairError, 'same Wi-Fi'):
            adbpair.pair(FakeHost(mdns=''), name='tutti-q1w2e3', password='pw', wait=4, sleep=lambda s: None)

    def test_the_usb_fallback_connects_over_the_lan(self):
        host = FakeHost()
        host.adb = lambda *a, timeout=60: (0, 'inet 192.168.1.9/24 brd') if 'wlan0' in a else FakeHost.adb(host, *a)
        self.assertEqual(adbpair.via_usb(host, 'R58', sleep=lambda s: None), '192.168.1.9:5555')
        listed = f'List of devices attached\nR58\tdevice\n1.2.3.4:5555\tdevice\n{SERIAL}\tdevice\nX\tunauthorized\n'
        self.assertEqual(adbpair.usb_serials(listed), ['R58'])


class KeyCopyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.old_dir, self.new_dir = os.path.join(self.tmp, 'old'), os.path.join(self.tmp, 'new')
        os.makedirs(self.old_dir)
        pair = (('adbkey', '-----BEGIN PRIVATE KEY-----\nSECRETSECRET\n-----END PRIVATE KEY-----\n'), ('adbkey.pub', 'QUJD pc@home\n'))
        for name, text in pair:
            with open(os.path.join(self.old_dir, name), 'w', encoding='utf8') as f:
                f.write(text)

    def test_the_key_lands_at_mode_600_an_old_one_is_backed_up_and_nothing_is_logged(self):
        os.makedirs(self.new_dir)
        with open(os.path.join(self.new_dir, 'adbkey'), 'w', encoding='utf8') as f:
            f.write('NUC-OWN-KEY\n')
        with open(os.path.join(self.new_dir, 'adbkey.pub'), 'w', encoding='utf8') as f:
            f.write('TlVD nuc@home\n')
        old = adbhost.Host('pc', LOCAL, key_dir=self.old_dir)
        new = adbhost.Host('nuc', LOCAL, key_dir=self.new_dir)
        new.restart = lambda: True
        self.assertNotEqual(old.fingerprint(), new.fingerprint())
        said = []
        ok, backup = devmove.copy_key(old, new, '20261114', said.append)
        self.assertTrue(ok)
        self.assertEqual(backup, 'tutti-backup-20261114')
        self.assertEqual(old.fingerprint(), new.fingerprint())
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.new_dir, 'adbkey')).st_mode), 0o600)
        with open(os.path.join(self.new_dir, 'adbkey'), encoding='utf8') as f:
            self.assertIn('SECRETSECRET', f.read())
        with open(os.path.join(self.new_dir, 'adbkey.tutti-backup-20261114'), encoding='utf8') as f:
            self.assertEqual(f.read(), 'NUC-OWN-KEY\n')
        self.assertNotIn('SECRET', ' '.join(said))
        self.assertTrue(new.restore_key(backup))
        with open(os.path.join(self.new_dir, 'adbkey'), encoding='utf8') as f:
            self.assertEqual(f.read(), 'NUC-OWN-KEY\n')

    def test_the_key_never_travels_in_argv(self):
        seen = []

        def sh(machine, script, capture=False, timeout=None, stdin=None):
            seen.append(script)
            return subprocess.run(['bash', '-c', script], capture_output=True, text=True, input=stdin, check=False)
        new = adbhost.Host('nuc', LOCAL, key_dir=self.new_dir, sh=sh)
        ok, backup = new.write_key('-----BEGIN PRIVATE KEY-----\nSECRETSECRET\n', 'QUJD pc@home\n', 'x')
        self.assertTrue(ok)
        self.assertEqual(backup, '')
        self.assertFalse(any('SECRET' in s for s in seen))

    def test_the_key_is_read_from_stdout_only(self):
        def sh(machine, script, capture=False, timeout=None, stdin=None):
            return SimpleNamespace(returncode=0, stdout='KEY\n', stderr='** WARNING: ssh banner\n')
        self.assertEqual(adbhost.Host('pc', LOCAL, key_dir='/k', sh=sh).read_key(), ('KEY\n', 'KEY\n'))

    def test_no_key_on_the_old_machine_means_pairing_again(self):
        self.assertEqual(devmove.key_action('', 'abc'), 'repair')
        self.assertEqual(devmove.key_action('abc', 'abc'), 'same')
        self.assertEqual(devmove.key_action('abc', ''), 'copy')
        self.assertEqual(devmove.key_action('abc', 'def'), 'replace')
        self.assertEqual(devmove.key_action('abc', 'def', 'keep'), 'ask')
        self.assertEqual(devmove.key_action('abc', 'abc', 'repair'), 'repair')


CONFIG = textwrap.dedent(f'''\
    # the owner's comment stays
    machines:
      pc: {{host: pc.example}}
      nuc: {{host: nuc.example}}
    devices:
      phone:
        serial: {SERIAL}
        machine: pc   # its adb is the PC's
        adb: /mnt/c/adb.exe
''')


class MoveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, 'tutti.config.yaml')
        with open(self.path, 'w', encoding='utf8') as f:
            f.write(CONFIG)
        self.cfg = {'_path': self.path, 'machines': {'pc': {'transport': 'ssh'}, 'nuc': {'transport': 'ssh'}},
                    'devices': {'phone': {'name': 'phone', 'serial': SERIAL, 'machine': 'pc', 'adb': '/mnt/c/adb.exe',
                                          'lock': '~/.tutti/locks/device-phone.lock'}}}

    def data(self):
        with open(self.path, encoding='utf8') as f:
            return yaml.safe_load(f)

    def pre(self, old, new, choice=None, lease=''):
        found = devmove.pick(self.cfg, 'nuc', [])
        return found, devmove.preflight(self.cfg, 'nuc', found, old, new, choice, lease_fn=lambda c, n: lease)

    def test_the_move_copies_the_key_updates_the_config_and_disconnects_the_old_machine(self):
        old, new = FakeHost('pc', fp='pckey'), FakeHost('nuc', fp='')
        old.connected = True   # the PC's adb still has it
        found, pre = self.pre(old, new)
        self.assertEqual(pre['problems'], [])
        self.assertEqual(pre['key'], 'copy')
        self.assertTrue(any('mode 600' in line for line in devmove.plan_lines(pre, found)))
        said = []
        result = devmove.run(self.cfg, 'nuc', found, old, new, pre, say=said.append, sleep=lambda s: None,
                             pair_fn=lambda d: self.fail('a move with the key copied asks nothing'))
        self.assertEqual(result, {'moved': ['phone'], 'stayed': {}, 'restored': False, 'wifi_was': {'phone': '0'}})
        self.assertEqual(old.wifi, '1')   # set on over the old machine's live connection before anything moved
        self.assertIn(('-s', SERIAL, 'shell', 'settings', 'put', 'global', 'adb_wifi_enabled', '1'), new.calls)   # re-asserted
        self.assertEqual(new.key, ('PRIVATE-KEY-TEXT', 'PUB adb@pc'))
        self.assertNotIn('PRIVATE-KEY-TEXT', ' '.join(said))
        phone = self.data()['devices']['phone']
        self.assertEqual(phone['machine'], 'nuc')
        self.assertNotIn('adb', phone)   # the NUC finds adb on its PATH
        with open(self.path, encoding='utf8') as f:
            self.assertIn("# the owner's comment stays", f.read())
        self.assertIn(('disconnect', SERIAL), old.calls)

    def test_a_device_that_does_not_connect_stays_and_the_key_is_put_back(self):
        old, new = FakeHost('pc', fp='pckey'), FakeHost('nuc', fp='nuckey', up=False)
        found, pre = self.pre(old, new)   # a different key on the new machine: replaced by default, backed up
        self.assertEqual(pre['key'], 'replace')
        result = devmove.run(self.cfg, 'nuc', found, old, new, pre, say=lambda m: None, wait=3, sleep=lambda s: None)
        self.assertEqual(result['moved'], [])
        self.assertIn('did not connect on nuc', result['stayed']['phone'])
        self.assertTrue(result['restored'])
        self.assertIn(('restore', 'tutti-backup-' + new.calls[-2][1].split('tutti-backup-')[1]), new.calls)
        self.assertEqual(self.data()['devices']['phone']['machine'], 'pc')
        self.assertNotIn(('disconnect', SERIAL), old.calls)
        self.assertIn('stays on pc', devmove.summary(result, 'nuc', 'pc')[1])

    def test_a_different_device_on_the_new_machine_is_not_taken(self):
        self.cfg['devices']['phone']['model'] = 'Pixel 9 Pro'
        old, new = FakeHost('pc', fp='k'), FakeHost('nuc', fp='k', model='Pixel 7')
        found, pre = self.pre(old, new)
        self.assertEqual(pre['key'], 'same')
        result = devmove.run(self.cfg, 'nuc', found, old, new, pre, say=lambda m: None, sleep=lambda s: None)
        self.assertIn('sees a Pixel 7', result['stayed']['phone'])

    def test_preflight_names_each_problem(self):
        old, new = FakeHost('pc', fp='pckey'), FakeHost('nuc', fp='nuckey', mdns='')
        _, pre = self.pre(old, new, lease='dispatch lease 7 (app e2e)')
        text = ' '.join(pre['problems'])
        self.assertIn('not seen over mDNS from nuc', text)
        self.assertIn('in use (dispatch lease 7', text)
        self.assertEqual(pre['key'], 'replace')   # no choice asked: the NUC's own key is backed up
        _, pre = self.pre(old, new, 'keep')
        self.assertIn('--keep-key', ' '.join(pre['problems']))
        _, pre = self.pre(FakeHost('pc'), FakeHost('nuc', path=''))
        self.assertTrue(pre['install'])

    def test_unknown_names_and_mixed_sources_are_refused(self):
        with self.assertRaisesRegex(devmove.MoveError, 'no machine "mars"'):
            devmove.pick(self.cfg, 'mars', [])
        with self.assertRaisesRegex(devmove.MoveError, 'no device "tv"'):
            devmove.pick(self.cfg, 'nuc', ['tv'])
        with self.assertRaisesRegex(devmove.MoveError, 'already on pc'):
            devmove.pick(self.cfg, 'pc', [])

    def test_a_paired_device_is_saved_once(self):
        info = {'model': 'Chromecast HD', 'kind': 'tv', 'capabilities': ['tv', 'wifi']}
        name = devmove.save_device(self.path, self.data()['devices'], 'nuc', 'adb-TV-1._adb-tls-connect._tcp', info)
        self.assertEqual(name, 'chromecasthd')
        self.assertEqual(self.data()['devices']['chromecasthd']['kind'], 'tv')
        again = devmove.save_device(self.path, self.data()['devices'], 'pc', 'adb-TV-1._adb-tls-connect._tcp', info)
        self.assertEqual(again, 'chromecasthd')
        self.assertEqual(self.data()['devices']['chromecasthd']['machine'], 'pc')


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        path = os.path.join(self.tmp, 'tutti.config.yaml')
        with open(path, 'w', encoding='utf8') as f:
            f.write(CONFIG)
        machines = {'pc': {'transport': 'local'}, 'nuc': {'transport': 'ssh'}}
        self.collector = SimpleNamespace(cfg={'_path': path, 'project': {'state': self.tmp}, 'machines': machines, 'devices': {}})
        devicesweb.PAIR['current'] = None

    def post(self, body, **kw):
        return devicesweb.pair_post(self.collector, json.dumps(body).encode(), host_fn=lambda n: FakeHost(n), start=False, **kw)

    def test_pair_a_device_shows_the_qr_code_and_a_tv_asks_for_its_code(self):
        code, view = self.post({'action': 'start'})
        self.assertEqual((code, view['flow'], view['machine']), (202, 'qr', 'pc'))
        self.assertTrue(view['payload'].startswith('WIFI:T:ADB;S:tutti-'))
        self.assertEqual(len(view['matrix']), len(qr.matrix(view['payload'])))
        self.assertEqual(self.post({'action': 'start'})[0], 409)
        self.post({'action': 'cancel'})
        code, view = self.post({'action': 'start', 'kind': 'tv'})
        self.assertEqual(view['flow'], 'code')
        self.assertNotIn('matrix', view)
        self.assertEqual(self.post({'action': 'code', 'code': '12ab'})[0], 400)
        polls = iter(['', MDNS, MDNS])
        devicesweb.PAIR['current'].host = FakeHost(model='Chromecast TV', mdns=lambda: next(polls, MDNS))
        self.assertEqual(self.post({'action': 'code', 'code': '123456'})[0], 200)
        devicesweb.PAIR['current'].run(wait=2)
        state = self.post({'action': 'status'})[1]
        self.assertEqual(state['state'], 'paired', state['message'])
        self.assertEqual(state['device']['kind'], 'tv')

    def test_a_cancelled_pairing_saves_nothing(self):
        self.post({'action': 'start'})
        p = devicesweb.PAIR['current']
        p.host = FakeHost(mdns='')
        self.post({'action': 'cancel'})
        p.run(wait=4)
        self.assertEqual((p.state, p.message), ('failed', 'cancelled'))
        with open(self.collector.cfg['_path'], encoding='utf8') as f:
            self.assertNotIn('pixel9pro', f.read())

    def test_no_adb_or_an_old_one_is_said_before_pairing(self):
        code, answer = devicesweb.pair_post(self.collector, b'{"action": "start"}', host_fn=lambda n: FakeHost(n, path=''), start=False)
        self.assertEqual(code, 400)
        self.assertIn('--install-adb', answer['error'])

    def test_move_devices_returns_the_plan_or_says_why_not(self):
        self.collector.cfg['devices'] = {'phone': {'name': 'phone', 'serial': SERIAL, 'machine': 'pc', 'lock': '/tmp/x.lock'}}
        hosts = {'pc': FakeHost('pc', fp='k'), 'nuc': FakeHost('nuc', fp='')}
        code, answer = devicesweb.move_post(self.collector, json.dumps({'to': 'nuc'}).encode(),
                                            hosts_fn=lambda n, adb=None: hosts[n])
        self.assertEqual(code, 200, answer)
        self.assertEqual(answer['key'], 'copy')
        self.assertIn('move phone from pc to nuc', answer['plan'][0])
        self.assertEqual(devicesweb.move_post(self.collector, b'{"to": "mars"}')[0], 400)


FAKE_ADB = textwrap.dedent(f'''\
    #!/usr/bin/env bash
    # a fake adb: the device shows up only after `adb connect <its new ip:port>` (its port changed after a reboot)
    st="$FAKE_DIR/connected"
    case "$1" in
      version) echo "Version 35.0.2-1";;
      devices) printf 'List of devices attached\\n'; [ -e "$st" ] && printf '{SERIAL}\\tdevice\\n';;
      connect) [ "$2" = 192.168.1.23:41867 ] && touch "$st"; echo "connected to $2";;
      mdns) printf '{GUID}\\t_adb-tls-connect._tcp\\t192.168.1.23:41867\\n';;
    esac
    exit 0
''')


class TuttiMoveTest(unittest.TestCase):
    def test_tutti_move_with_devices_moves_every_device_once_per_machine(self):
        from tutti import move
        cfg = {'machines': {'laptop': {'transport': 'local'}, 'nuc': {'transport': 'ssh'}},
               'devices': {'p': {'machine': 'laptop'}, 'q': {'machine': 'pc'}, 'r': {'machine': 'nuc'}}}
        got = []
        rc = move.devices_follow(cfg, SimpleNamespace(to='nuc'), move_fn=lambda c, a: got.append((a.to, a.names, a.repair)) or 0)
        self.assertEqual((rc, got), (0, [('nuc', ['p'], False), ('nuc', ['q'], False)]))


class ExternalLockTest(unittest.TestCase):
    def test_a_move_waits_for_another_tools_lock_file(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        theirs = os.path.join(tmp, 'adb-{serial}.lock')
        d = {'name': 'p', 'serial': 'R58', 'lock': os.path.join(tmp, 'tutti.lock'), 'external_lock': theirs}
        old = adbhost.Host('pc', LOCAL, key_dir=tmp)
        self.assertEqual(devmove.busy({}, d, old, lease_fn=lambda c, n: ''), '')
        with open(theirs.replace('{serial}', 'R58'), 'w', encoding='utf8') as f:
            f.write('phone-e2e subagent 2026-11-14\n')
        self.assertEqual(devmove.busy({}, d, old, lease_fn=lambda c, n: ''), 'phone-e2e subagent 2026-11-14')
        an_hour_ago = time.time() - 3600
        os.utime(theirs.replace('{serial}', 'R58'), (an_hour_ago, an_hour_ago))
        self.assertEqual(devmove.busy({}, d, old, lease_fn=lambda c, n: ''), '')   # left behind: stale after 30 min


class KeepOnTest(unittest.TestCase):
    """After a reboot Android turns Wireless debugging off: Tutti turns it back on through any machine that reaches the
    device, and asks the user only when none does."""

    def setUp(self):
        keepon._TRIED.clear()
        self.cfg = {'machines': {'pc': {'transport': 'wsl'}, 'nuc': {'transport': 'ssh'}, 'phone': {'transport': 'android'}},
                    'devices': {}}
        self.d = {'name': 'pixel', 'serial': SERIAL, 'machine': 'nuc'}

    def test_wireless_debugging_off_after_a_reboot_is_turned_back_on_over_usb_on_another_machine(self):
        nuc, pc = FakeHost('nuc'), FakeHost('pc')
        pc.listed = ['ABC123']   # plugged into the PC by USB; the NUC sees nothing since the reboot
        readings = iter([{'adb': 'ok', 'state': 'missing'}, {'adb': 'ok', 'state': 'device'}])
        read = keepon.readings_with_revive(self.cfg, lambda m, d, **kw: next(readings),
                                           hosts_fn=lambda n, adb=None: {'nuc': nuc, 'pc': pc}[n])
        r = read({}, self.d)
        self.assertEqual(r['state'], 'device')
        self.assertIn('turned back on through pc (USB)', r['revived'])
        self.assertEqual(pc.wifi, '1')
        self.assertEqual(keepon.usb_serial_of('adb-46191FDAP003RM-OikUIT'), '46191FDAP003RM')

    def test_no_path_left_asks_the_user_once_with_the_reason(self):
        read = keepon.readings_with_revive(self.cfg, lambda m, d, **kw: {'adb': 'ok', 'state': 'missing'},
                                           hosts_fn=lambda n, adb=None: FakeHost(n))
        r = read({}, self.d)
        self.assertEqual(r['last_step'], keepon.LAST_STEP)
        st = devices.device_state(self.d | {'lock': '/x'}, r)
        self.assertEqual(st['status'], 'offline')
        self.assertIn('Android turns it off after a reboot', st['reason'])
        self.assertIsNone(keepon.revive(self.cfg, self.d, lambda n, adb=None: self.fail('not again within 5 min')))

    def test_a_connected_device_is_not_touched(self):
        read = keepon.readings_with_revive(self.cfg, lambda m, d, **kw: {'adb': 'ok', 'state': 'device'},
                                           hosts_fn=lambda n, adb=None: self.fail('nothing to revive'))
        self.assertEqual(read({}, self.d)['state'], 'device')


class ReconnectTest(unittest.TestCase):
    def test_a_device_whose_port_changed_is_reconnected_through_mdns(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        adb = os.path.join(tmp, 'adb')
        with open(adb, 'w', encoding='utf8') as f:
            f.write(FAKE_ADB)
        os.chmod(adb, 0o755)
        env = {**os.environ, 'ADB': adb, 'FAKE_DIR': tmp, 'DEVICE_SERIAL': SERIAL}
        out = subprocess.run(['bash', os.path.join(ROOT, 'scripts', 'device-readings.sh')], env=env, capture_output=True,
                             text=True, check=True).stdout
        self.assertIn('resolved=192.168.1.23:41867', out)
        self.assertIn('state=device', out)


if __name__ == '__main__':
    unittest.main()


class RshTest(unittest.TestCase):
    """`tutti rsh` on a WSL machine (backlog t38): a script with `;` or new lines, and a command with arguments."""
    M = {'name': 'pc', 'host': 'pc.example', 'transport': 'wsl', 'wsl_helper': '/h/run64.sh'}

    def run_script(self, args):
        script = remote.rsh_command(self.M, args)
        return subprocess.run(['bash', '-c', script], capture_output=True, text=True, check=False).stdout

    def test_a_script_with_semicolons_and_new_lines_runs_whole(self):
        self.assertEqual(self.run_script(['echo a; echo b']), 'a\nb\n')
        self.assertEqual(self.run_script(['echo a\nfor i in 1 2; do echo $i; done']), 'a\n1\n2\n')

    def test_a_command_keeps_its_arguments_each_quoted(self):
        self.assertEqual(remote.rsh_command(self.M, ['/mnt/c/a b/adb.exe', 'devices']), "'/mnt/c/a b/adb.exe' devices")
        self.assertEqual(self.run_script(['printf', '%s|', 'x y', "it's"]), "x y|it's|")

    def test_git_and_rsync_put_the_host_first(self):
        self.assertEqual(remote.rsh_command(self.M, ['-o', 'BatchMode=yes', 'pc.example', 'git-upload-pack x']), 'git-upload-pack x')
        self.assertEqual(remote.rsh_command(self.M, ['pc.example', 'rsync', '--server', '.']), 'rsync --server .')
        self.assertEqual(remote.rsh_command(self.M, ['pc', 'status']), 'pc status')   # a command named like the machine

    def test_the_script_travels_base64_encoded_to_wsl(self):
        cmd = remote.argv(self.M, remote.rsh_command(self.M, ['echo a; echo "b c"']))
        packed = cmd[-1].split()[-1]
        import base64
        self.assertEqual(base64.b64decode(packed).decode(), 'cd ~ && echo a; echo "b c"')
