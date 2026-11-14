"""The optional private network (docs/NETWORK.md): detection from fake probes and fake CLIs, the install command per OS, Funnel
refused, the setup plan and checklist, WireGuard keys (RFC 7748 vector, mode 600, never in state), configs per peer, address
allocation, the QR payload, the router mapped only after consent, the adb transport choice with a fake adb, the ssh LAN fallback,
and Tutti on a plain LAN with no VPN at all. No real machine, router or phone."""
import argparse
import io
import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest

from tutti import config, dashboard, devices, netpolicy, remote, vpn, vpncmd, wireguard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NO_VPN = 'OS=Linux\nTS=\nSUDO=1\nPKG=apt-get\n'
TS_UP = ('OS=Linux\nTS=1\nWG=1\nPKG=apt-get\n--status\n'
         '{"BackendState":"Running","Self":{"DNSName":"box.tail0.ts.net.","TailscaleIPs":["100.101.1.2"]},'
         '"CurrentTailnet":{"MagicDNSEnabled":true}}\n--funnel\n{}\n')
FUNNEL_ON = {'TCP': {'443': {'HTTPS': True}}, 'Web': {'box.tail0.ts.net:443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:4777'}}}},
             'AllowFunnel': {'box.tail0.ts.net:443': True}}


class Done:
    def __init__(self, stdout='', returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, '', returncode


class FakeSh:
    """remote.sh with canned answers: records every script and every stdin (a key on stdin is checked never to be stored)."""

    def __init__(self, probe=NO_VPN, packages=''):
        self.probe, self.packages, self.calls = probe, packages, []

    def __call__(self, machine, script, capture=False, timeout=None, stdin=None, ssh_opts=()):
        self.calls.append((machine.get('name'), script, stdin))
        if 'uname -s' in script:
            return Done(self.probe)
        if 'pm list packages' in script:
            return Done(self.packages)
        if 'echo written' in script:
            return Done('written\n')
        return Done('')


def write_cfg(text):
    d = tempfile.mkdtemp()
    path = os.path.join(d, 'tutti.config.yaml')
    with open(path, 'w', encoding='utf8') as f:
        f.write(textwrap.dedent(text).replace('STATE', os.path.join(d, 'state')))
    return config.load(path)


LAN_CFG = '''\
    project: {state: STATE}
    machines:
      here: {transport: local}
      box: {host: box.example, user: me}
    devices:
      phone: {serial: R58, machine: here, wireless: ["192.168.1.20:5555"]}
    '''


def args(**kw):
    base = {'action': 'setup', 'provider': None, 'dry_run': False, 'authkey': None, 'cert': False}
    return argparse.Namespace(**{**base, **kw})


class Detection(unittest.TestCase):
    def test_no_vpn_parsed(self):
        f = vpn.parse_probe(NO_VPN)
        self.assertTrue(f['reachable'])
        self.assertFalse(f['tailscale'])
        self.assertFalse(vpn.logged_in(f))
        self.assertTrue(f['sudo'])

    def test_tailscale_logged_in(self):
        f = vpn.parse_probe(TS_UP)
        self.assertTrue(vpn.logged_in(f))
        self.assertEqual(f['dns_name'], 'box.tail0.ts.net')
        self.assertTrue(f['magicdns'])

    def test_unreachable(self):
        self.assertEqual(vpn.parse_probe(''), {'reachable': False})

    def test_probe_script_with_fake_tailscale(self):
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, 'tailscale')
            with open(fake, 'w', encoding='utf8') as f:
                f.write('#!/bin/sh\n[ "$1" = status ] && echo \'{"BackendState":"NeedsLogin"}\'\n[ "$1" = funnel ] && echo "{}"\nexit 0\n')
            os.chmod(fake, 0o755)
            env = {**os.environ, 'PATH': d + os.pathsep + os.environ['PATH']}
            out = subprocess.run(['bash', '-c', vpn.PROBE], capture_output=True, text=True, env=env, check=True).stdout
        f = vpn.parse_probe(out)
        self.assertTrue(f['tailscale'])
        self.assertEqual(f['state'], 'NeedsLogin')

    def test_android_package(self):
        self.assertTrue(vpn.android_has('package:com.tailscale.ipn\n', vpn.TS_PACKAGE))
        self.assertFalse(vpn.android_has('package:com.tailscale.ipn.beta\n', vpn.TS_PACKAGE))
        self.assertTrue(vpn.play_store('R58', vpn.TS_PACKAGE).endswith('-d market://details?id=com.tailscale.ipn'))

    def test_phone_probe_uses_wireless_transports(self):
        cfg = write_cfg(LAN_CFG)
        sh = FakeSh(packages='package:com.tailscale.ipn\n')
        phones = vpncmd.probe_phones(cfg, vpn.TS_PACKAGE, sh)
        self.assertTrue(phones['phone']['has'])
        self.assertIn('192.168.1.20:5555', sh.calls[0][1])

    def test_cert_name_quoted(self):
        self.assertIn("'a;b'", vpn.cert_command('a;b', '/s'))


class Install(unittest.TestCase):
    def test_per_os(self):
        self.assertIn('tailscale.com/install.sh', vpn.install_tailscale('linux')[0])
        self.assertEqual(vpn.install_tailscale('macos')[0], 'brew install --cask tailscale')
        self.assertIn('Tailscale.Tailscale', vpn.install_tailscale('windows')[0])
        self.assertIn('dnf', vpn.install_wireguard('linux', 'dnf')[0])
        self.assertIn('WireGuard.WireGuard', vpn.install_wireguard('windows')[0])

    def test_authkey_only_on_stdin(self):
        self.assertIn('file:/dev/stdin', vpn.up_command(authkey=True))
        sh = FakeSh()
        vpncmd.authkey_join({'name': 'box'}, 'tskey-auth-TEST', sh)
        _, script, stdin = sh.calls[0]
        self.assertNotIn('tskey', script)
        self.assertEqual(stdin, 'tskey-auth-TEST\n')

    def test_empty_authkey_does_nothing(self):
        cfg = write_cfg(LAN_CFG)
        sh, out = FakeSh(), io.StringIO()
        self.assertEqual(vpncmd.authkey(cfg, 'box', sh, out, read=lambda _p: ''), 2)
        self.assertEqual(sh.calls, [])


class Funnel(unittest.TestCase):
    def test_refused_on_tutti_port(self):
        self.assertEqual(vpn.funnel_hits(FUNNEL_ON, [4777]), ['box.tail0.ts.net:443'])
        err = vpn.funnel_error({'funnel': FUNNEL_ON}, [4777])
        self.assertIn('never on the internet', err)
        self.assertIn('tailscale funnel --https=443 off', err)

    def test_other_port_not_flagged(self):
        self.assertEqual(vpn.funnel_hits(FUNNEL_ON, [4778]), [])
        self.assertEqual(vpn.funnel_error({'funnel': {}}, [4777]), '')

    def test_cli_refuses_funnel(self):
        cfg = write_cfg(LAN_CFG)
        out = io.StringIO()
        self.assertEqual(vpncmd.cmd_vpn(cfg, args(action='funnel'), sh=FakeSh(), out=out), 2)
        self.assertIn('never turns Funnel on', out.getvalue())

    def test_dashboard_refuses_to_start(self):
        cfg = write_cfg(LAN_CFG)
        probe = TS_UP.replace('--funnel\n{}', '--funnel\n' + json.dumps(FUNNEL_ON))
        self.assertIn('Funnel', vpncmd.funnel_refusal(cfg, run=lambda *a, **k: Done(probe)))
        self.assertEqual(vpncmd.funnel_refusal(cfg, run=lambda *a, **k: Done(NO_VPN)), '')


class Plan(unittest.TestCase):
    def test_choice_respects_setting(self):
        self.assertEqual(vpn.choice({}), 'tailscale')
        self.assertEqual(vpn.choice({'vpn': {'prefer': 'wireguard'}}), 'wireguard')
        self.assertEqual(vpn.choice({'vpn': {'prefer': 'wireguard'}}, 'tailscale'), 'tailscale')
        self.assertTrue(vpn.errors({'vpn': {'prefer': 'openvpn'}}))
        self.assertTrue(vpn.errors({'vpn': {'wireguard': {'port': 80}}}))

    def test_tailscale_plan_no_sudo_is_a_checklist(self):
        facts = {'box': {**vpn.parse_probe(NO_VPN), 'sudo': False}}
        auto, you = vpn.tailscale_plan(facts, {'phone': {'has': False, 'serial': 'R58'}})
        ids = [s['id'] for s in vpncmd.checklist(you)]
        self.assertEqual(ids, ['install-box', 'join-box', 'phone-phone'])
        self.assertEqual([s['id'] for s in auto], ['store-phone'])

    def test_joined_machines_get_tailnet_name_with_lan_fallback(self):
        hosts = vpn.suggested_hosts({'box': {'host': '192.168.1.5', 'transport': 'ssh'}}, {'box': vpn.parse_probe(TS_UP)})
        self.assertEqual(hosts, {'box': {'host': 'box.tail0.ts.net', 'lan': '192.168.1.5'}})


class WireGuardKeys(unittest.TestCase):
    def test_rfc7748_vector(self):
        k = bytes.fromhex('77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a')
        self.assertEqual(wireguard._x25519(k, 9).hex(), '8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a')

    def test_keypair_clamped(self):
        import base64
        priv, pub = wireguard.keypair(random=lambda n: b'\xff' * n)
        raw = base64.b64decode(priv)
        self.assertEqual(raw[0] & 7, 0)
        self.assertEqual(raw[31] & 0xc0, 0x40)
        self.assertEqual(wireguard.public_key(priv), pub)

    def test_key_file_mode_600_and_kept(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, 'HOME': home}
            r = subprocess.run(['bash', '-c', wireguard.key_write_script()], input='PRIV\n', capture_output=True, text=True, env=env)
            self.assertEqual(r.stdout.strip(), 'written')
            self.assertNotIn('PRIV', r.stdout + r.stderr)
            path = os.path.join(home, '.config/tutti/wireguard/tutti.key')
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
            r = subprocess.run(['bash', '-c', wireguard.key_write_script()], input='OTHER\n', capture_output=True, text=True, env=env)
            self.assertEqual(r.stdout.strip(), 'kept')
            with open(path, encoding='utf8') as f:
                self.assertEqual(f.read().strip(), 'PRIV')


class WireGuardConfigs(unittest.TestCase):
    def state(self):
        st = {'subnet': '10.77.77.0/24', 'hub': 'hub', 'port': 51820, 'endpoint': '', 'hub_lan': '192.168.1.2',
              'addresses': wireguard.assign('10.77.77.0/24', 'hub', ['box']), 'keys': {'hub': 'HUBPUB', 'box': 'BOXPUB'}}
        return st

    def test_hub_and_peer(self):
        st = self.state()
        hub = wireguard.hub_conf(st, '/k')
        self.assertIn('ListenPort = 51820', hub)
        self.assertIn('PublicKey = BOXPUB\nAllowedIPs = 10.77.77.2/32', hub)
        self.assertNotIn('PrivateKey', hub)
        peer = wireguard.peer_conf(st, 'box', '/k')
        self.assertIn('Address = 10.77.77.2/24', peer)
        self.assertIn('Endpoint = 192.168.1.2:51820', peer)
        self.assertIn('PostUp = wg set %i private-key /k', peer)
        self.assertNotIn('PrivateKey', peer)

    def test_qr_payload_has_the_phone_key_state_does_not(self):
        st = self.state()
        st['addresses'] = wireguard.assign(st['subnet'], 'hub', ['box', 'phone'], st['addresses'])
        text = wireguard.phone_payload(st, 'phone', ('PHONEPRIV', 'PHONEPUB'))
        self.assertIn('PrivateKey = PHONEPRIV', text)
        self.assertIn('PersistentKeepalive = 25', text)
        self.assertEqual(st['keys']['phone'], 'PHONEPUB')
        self.assertNotIn('PHONEPRIV', json.dumps(st))

    def test_qr_svg_from_qrencode_or_text(self):
        self.assertTrue(wireguard.qr_svg('x', run=lambda *a, **k: Done('<svg/>')).startswith('<svg'))
        self.assertEqual(wireguard.qr_svg('x', run=lambda *a, **k: Done('', 127)), '')

    def test_subnet_avoids_lan_and_tailscale(self):
        taken = wireguard.parse_networks(['10.77.77.5/24', '192.168.1.10/24'])
        self.assertEqual(wireguard.pick_subnet(taken), '10.78.77.0/24')
        with self.assertRaises(ValueError):
            wireguard.pick_subnet([], '100.100.0.0/24')
        with self.assertRaises(ValueError):
            wireguard.pick_subnet(taken, '192.168.1.0/24')

    def test_addresses_stable(self):
        a = wireguard.assign('10.77.77.0/24', 'hub', ['b', 'c'])
        self.assertEqual(a, {'hub': '10.77.77.1', 'b': '10.77.77.2', 'c': '10.77.77.3'})
        self.assertEqual(wireguard.assign('10.77.77.0/24', 'hub', ['c', 'd'], {'c': '10.77.77.3'})['d'], '10.77.77.2')


class Router(unittest.TestCase):
    def test_no_consent_no_call(self):
        calls = []
        with self.assertRaises(wireguard.ConsentNeeded):
            wireguard.open_port('upnp', 51820, None, run=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])

    def test_only_the_udp_port(self):
        calls = []
        ok, _ = wireguard.open_port('upnp', 51820, True, run=lambda argv, **k: calls.append(argv) or Done('ok'))
        self.assertTrue(ok)
        self.assertEqual(calls, [['upnpc', '-e', 'tutti-wireguard', '-r', '51820', 'UDP']])

    def test_natpmp_lifetime_positive(self):
        calls = []
        wireguard.open_port('natpmp', 51820, True, run=lambda argv, **k: calls.append(argv) or Done('ok'))
        self.assertGreater(int(calls[0][-1]), 0)   # 0 deletes a mapping (RFC 6886)

    def test_no_router_support_gives_steps(self):
        with self.assertRaises(ValueError) as e:
            wireguard.open_port('', 51820, True)
        self.assertIn('UDP 51820', str(e.exception))

    def test_dashboard_needs_consent(self):
        cfg = write_cfg(LAN_CFG)
        status, answer = vpncmd.api(cfg, {'action': 'open-port'}, run=lambda argv, **k: Done('Found valid IGD'))
        self.assertEqual(status, 403)
        self.assertIn('click', answer['error'])


class AdbTransport(unittest.TestCase):
    FAKE = textwrap.dedent('''\
        #!/usr/bin/env bash
        # fake adb: ONLINE lists the serials that answer; `connect` adds CONNECTABLE ones
        state="$(dirname "$0")/online"
        if [ "$1" = connect ]; then case " $CONNECTABLE " in *" $2 "*) echo "$2" >> "$state";; esac; exit 0; fi
        if [ "$1" = -s ] && [ "$3" = get-state ]; then
          grep -qx "$2" "$state" 2>/dev/null && { echo device; exit 0; }
          case " $ONLINE " in *" $2 "*) echo device; exit 0;; esac
          exit 1
        fi
        ''')

    def pick(self, online, connectable=''):
        d = {'serial': 'R58', 'wireless': ['192.168.1.20:5555', 'phone.tail0.ts.net:5555']}
        with tempfile.TemporaryDirectory() as t:
            adb = os.path.join(t, 'adb')
            with open(adb, 'w', encoding='utf8') as f:
                f.write(self.FAKE)
            os.chmod(adb, 0o755)
            env = {**os.environ, 'PATH': t + os.pathsep + os.environ['PATH'], 'ONLINE': online, 'CONNECTABLE': connectable}
            return subprocess.run(['bash', '-c', f'echo {devices.serial_expr(d)}'], capture_output=True, text=True, env=env).stdout.strip()

    def test_usb_first(self):
        self.assertEqual(self.pick('R58 192.168.1.20:5555'), 'R58')

    def test_lan_wifi_reconnected(self):
        self.assertEqual(self.pick('', '192.168.1.20:5555'), '192.168.1.20:5555')

    def test_tailnet_when_away(self):
        self.assertEqual(self.pick('', 'phone.tail0.ts.net:5555'), 'phone.tail0.ts.net:5555')

    def test_nothing_falls_back_to_serial(self):
        self.assertEqual(self.pick(''), 'R58')

    def test_plain_serial_unchanged(self):
        self.assertEqual(devices.serial_expr({'serial': 'R58'}), 'R58')

    def test_bad_wireless_rejected(self):
        self.assertTrue(devices.wireless_errors('devices.p', ['no-port']))
        self.assertEqual(devices.wireless_errors('devices.p', ['10.0.0.2:5555']), [])


class LanOnly(unittest.TestCase):
    """No Tailscale, no WireGuard, no VPN of Tutti's: everything still works."""

    def test_config_valid_without_vpn(self):
        cfg = write_cfg(LAN_CFG)
        self.assertEqual(config.validate(cfg), [])
        self.assertEqual(cfg['vpn']['prefer'], 'tailscale')

    def test_dashboard_binds_lan_addresses(self):
        ifaces = [('lo', '', ['127.0.0.1']), ('enp1s0', '', ['192.168.1.10']), ('docker0', 'bridge', ['172.17.0.1'])]
        got = netpolicy.binds('auto', 'dashboard', ifaces)
        self.assertIn('192.168.1.10', got)
        self.assertNotIn('172.17.0.1', got)
        self.assertTrue(all(netpolicy.private(a) for a in got))

    def test_any_own_vpn_client_subnet_is_private(self):
        for client in ('10.8.0.6', '172.20.3.4', '192.168.50.9', 'fd00::5'):
            self.assertTrue(netpolicy.private(client), client)
        self.assertFalse(netpolicy.private('8.8.8.8'))

    def test_status_with_no_vpn(self):
        cfg = write_cfg(LAN_CFG)
        out = io.StringIO()
        self.assertEqual(vpncmd.cmd_vpn(cfg, args(action='status'), sh=FakeSh(), out=out), 0)
        self.assertIn('Nothing to do', out.getvalue())
        self.assertIn('tailscale not installed', out.getvalue())

    def test_ssh_lan_fallback(self):
        m = {'host': 'box.tail0.ts.net', 'lan': '192.168.1.5'}
        self.assertEqual(remote.pick_host(m, reach=lambda h: h == '192.168.1.5'), '192.168.1.5')
        self.assertEqual(remote.pick_host(m, reach=lambda h: True), 'box.tail0.ts.net')
        self.assertEqual(remote.pick_host({'host': 'box.example'}, reach=lambda h: 1 / 0), 'box.example')

    def test_card_status_empty(self):
        cfg = write_cfg(LAN_CFG)
        status, answer = vpncmd.api(cfg, {'action': 'status'})
        self.assertEqual((status, answer['checklist'], answer['provider']), (200, [], ''))
        self.assertIn('/api/vpn', dashboard.POSTS)


class Setup(unittest.TestCase):
    def test_dry_run_changes_nothing(self):
        cfg = write_cfg(LAN_CFG)
        sh, out = FakeSh(), io.StringIO()
        self.assertEqual(vpncmd.cmd_vpn(cfg, args(dry_run=True), sh=sh, out=out), 0)
        self.assertTrue(all('uname -s' in s or 'pm list packages' in s for _, s, _ in sh.calls))
        self.assertFalse(os.path.exists(vpncmd.state_path(cfg)))

    def test_wireguard_setup_and_rerun(self):
        cfg = write_cfg(LAN_CFG)
        sh, out = FakeSh(probe=NO_VPN.replace('SUDO=1\n', '')), io.StringIO()
        a = args(provider='wireguard')
        orig = wireguard.taken_networks
        wireguard.taken_networks = lambda run=None: wireguard.parse_networks(['10.77.77.1/24'])
        try:
            self.assertEqual(vpncmd.cmd_vpn(cfg, a, sh=sh, out=out), 0)
            first = vpncmd.read_state(cfg)
            sent = [stdin for _, s, stdin in sh.calls if 'echo written' in s]
            self.assertEqual(len(sent), 2)   # here and box: one key each, sent on stdin only
            with open(vpncmd.state_path(cfg), encoding='utf8') as f:
                text = f.read()
            self.assertFalse(any(k.strip() in text for k in sent))
            wg = first['wireguard']
            self.assertEqual(wg['subnet'], '10.78.77.0/24')
            self.assertEqual(wg['addresses']['here'], '10.78.77.1')
            ids = [s['id'] for s in first['checklist']]
            self.assertEqual(ids, ['up-here', 'up-box', 'qr-phone', 'router'])
            sh.calls.clear()
            vpncmd.cmd_vpn(cfg, a, sh=sh, out=io.StringIO())
            self.assertEqual([c for c in sh.calls if 'echo written' in c[1]], [])   # keys kept: the rerun continues
            self.assertEqual(vpncmd.read_state(cfg)['wireguard']['addresses'], wg['addresses'])
        finally:
            wireguard.taken_networks = orig

    def test_guided_interface_for_tutti_setup(self):
        cfg = write_cfg(LAN_CFG)
        sh = FakeSh(probe=NO_VPN.replace('SUDO=1\n', ''))
        choices, steps = vpncmd.guided_setup(cfg, sh)
        self.assertEqual(choices[0]['key'], 'vpn.prefer')
        self.assertEqual([s['kind'] for s in steps], ['sudo', 'sudo', 'sudo', 'sudo', 'phone'])
        self.assertTrue(all('uname -s' in c[1] or 'pm list packages' in c[1] for c in sh.calls))

    def run_wg(self, sh, taken=('10.77.77.1/24',)):
        cfg = write_cfg(LAN_CFG)
        orig = wireguard.taken_networks
        wireguard.taken_networks = lambda run=None: wireguard.parse_networks(list(taken))
        try:
            self.assertEqual(vpncmd.cmd_vpn(cfg, args(provider='wireguard'), sh=sh, out=io.StringIO()), 0)
        finally:
            wireguard.taken_networks = orig
        return vpncmd.read_state(cfg)

    def test_hub_unreachable_peers_wait(self):
        class HubDown(FakeSh):
            def __call__(self, machine, script, **kw):
                if machine.get('transport') == 'local' and 'uname -s' in script:
                    return Done('', 255)
                return super().__call__(machine, script, **kw)
        st = self.run_wg(HubDown(probe=NO_VPN))
        self.assertIn('waits for the hub', [s['title'] for s in st['checklist'] if s['id'] == 'up-box'][0])

    def test_kept_key_public_half_recovered(self):
        class Kept(FakeSh):
            def __call__(self, machine, script, **kw):
                if 'echo written' in script:
                    return Done('kept\nKEPTPUB=\n')
                return super().__call__(machine, script, **kw)
        st = self.run_wg(Kept(probe=NO_VPN))
        self.assertEqual(st['wireguard']['keys']['box'], 'KEPTPUB=')

    def test_config_write_failure_is_a_step(self):
        class NoDisk(FakeSh):
            def __call__(self, machine, script, **kw):
                if 'cat > "$HOME/.config/tutti/wireguard/tutti.conf"' in script:
                    return Done('', 1)
                return super().__call__(machine, script, **kw)
        st = self.run_wg(NoDisk(probe=NO_VPN))
        self.assertTrue(any('not written' in s['title'] for s in st['checklist']))

    def test_hub_follows_tutti_move(self):
        cfg = {'machines': {'here': {'transport': 'local'}, 'nuc': {}}, 'host': {'machine': 'nuc'}}
        self.assertEqual(vpncmd.hub_name(cfg), 'nuc')
        self.assertEqual(vpncmd.hub_name({**cfg, 'vpn': {'wireguard': {'hub': 'here'}}}), 'here')
        self.assertEqual(vpncmd.hub_name({**cfg, 'host': {}}), 'here')

    def test_qr_unknown_phone(self):
        cfg = write_cfg(LAN_CFG)
        self.assertEqual(vpncmd.api(cfg, {'action': 'qr', 'phone': '../x'})[0], 400)



class RunnerEnv(unittest.TestCase):
    def test_wireless_device_passes_the_picked_serial_through(self):
        m = {'name': 'here', 'runner': {'kind': 'docker', 'memory_gb': 1, 'cpus': 1, 'image': 'img', 'env': {'ANDROID_SERIAL': None}}}
        line = remote.runner_command(m, 't', 'true')
        self.assertIn('-e ANDROID_SERIAL ', line)
        self.assertNotIn('ANDROID_SERIAL=', line)


if __name__ == '__main__':
    unittest.main()
