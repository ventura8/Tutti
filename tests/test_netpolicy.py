"""Never on the internet (docs/HOUSEHOLD.md, *Network*): the coordinator, the dashboard and join links may use only
localhost, private, LAN or VPN addresses; a public address or a wildcard is refused with the reason. No real network."""
import os
import tempfile
import unittest

from tutti import config, coordinator, household, netpolicy, users

IFACES = [('lo', '', ['127.0.0.1', '::1']), ('wlo1', '', ['192.168.1.33', 'fe80::1']), ('docker0', 'bridge', ['172.17.0.1']),
          ('tailscale0', '', ['100.101.102.103']), ('wg0', 'wireguard', ['203.0.113.7']), ('eth1', '', ['198.51.100.20'])]


def cfg_with(**household_cfg):
    d = tempfile.mkdtemp()
    cfg = config.normalise({'project': {'state': os.path.join(d, 'state')}, 'household': household_cfg})
    cfg['_path'] = os.path.join(d, 'c.yaml')
    return cfg


class PolicyTest(unittest.TestCase):
    def test_private_lan_cgnat_ula_and_wireguard_are_allowed(self):
        vpn = netpolicy.vpn_addresses(IFACES)
        for a in ('127.0.0.1', '::1', '10.1.2.3', '172.20.0.1', '192.168.1.33', '100.101.102.103', 'fd28::1', '203.0.113.7'):
            self.assertTrue(netpolicy.private(a, vpn), a)
        for a in ('8.8.8.8', '198.51.100.20', '2001:db8::1', 'nonsense'):
            self.assertFalse(netpolicy.private(a, vpn), a)

    def test_a_public_bind_is_refused_and_names_the_allowed_addresses(self):
        with self.assertRaisesRegex(netpolicy.PublicAddress, 'never exposed to the internet.*192.168.1.33'):
            netpolicy.check('198.51.100.20', 'household.bind', IFACES)
        with self.assertRaisesRegex(netpolicy.PublicAddress, 'every interface'):
            netpolicy.check('0.0.0.0', 'household.bind', IFACES)
        with self.assertRaisesRegex(netpolicy.PublicAddress, 'public address'):
            netpolicy.check('tutti.example', 'invite', IFACES, resolve_fn=lambda h: ['93.184.216.34'])
        self.assertEqual(netpolicy.check('pc.lan', 'x', IFACES, resolve_fn=lambda h: ['192.168.1.40']), ['192.168.1.40'])

    def test_auto_binds_localhost_and_the_lan_and_vpn_addresses_never_a_bridge_or_public_one(self):
        self.assertEqual(netpolicy.binds('auto', 'household.bind', IFACES), ['127.0.0.1', '::1', '100.101.102.103', '192.168.1.33',
                                                                              '203.0.113.7', 'fe80::1%wlo1'])

    def test_auto_adds_ula_and_the_routes_own_address(self):
        ifaces = [('lo', '', ['127.0.0.1']), ('eth0', '', ['fd28::5'])]
        self.assertEqual(netpolicy.binds('auto', 'b', ifaces, route='10.0.0.7'), ['127.0.0.1', '::1', 'fd28::5', '10.0.0.7'])
        self.assertNotIn('8.8.8.8', netpolicy.binds('auto', 'b', ifaces, route='8.8.8.8'))

    def test_an_explicit_bind_on_a_container_bridge_is_refused(self):
        with self.assertRaisesRegex(netpolicy.PublicAddress, 'container bridge'):
            netpolicy.check('172.17.0.1', 'household.bind', IFACES)

    def test_the_config_check_knows_a_wireguard_address_outside_the_private_ranges(self):
        self.assertEqual(netpolicy.errors('household.bind', '203.0.113.7', IFACES), [])
        self.assertIn('public address', netpolicy.errors('household.bind', '198.51.100.20', IFACES)[0])

    def test_without_ip_the_ifconfig_fallback_still_finds_the_vpn(self):
        text = ('lo0: flags=8049<UP,LOOPBACK> mtu 16384\n\tinet 127.0.0.1 netmask 0xff000000\n'
                'en0: flags=8863<UP> mtu 1500\n\tinet 192.168.1.50 netmask 0xffffff00\n\tinet6 fe80::aa%en0 prefixlen 64\n'
                'utun4: flags=8051<UP> mtu 1380\n\tinet 203.0.113.9 --> 203.0.113.9 netmask 0xffffffff\n')

        def run(argv, **kw):
            if argv[0] == 'ip':
                raise OSError('no ip here')
            from types import SimpleNamespace
            return SimpleNamespace(stdout=text)
        ifaces = netpolicy.interfaces(run)
        self.assertEqual(netpolicy.check('203.0.113.9', 'household.bind', ifaces), ['203.0.113.9'])
        self.assertIn('192.168.1.50', netpolicy.private_addresses(ifaces))

    def test_the_config_check_names_a_public_or_wildcard_bind(self):
        self.assertIn('every interface', netpolicy.errors('mcp.bind', '0.0.0.0')[0])
        self.assertIn('public address', netpolicy.errors('mcp.bind', '8.8.8.8')[0])
        self.assertEqual(netpolicy.errors('mcp.bind', '192.168.1.5') + netpolicy.errors('mcp.bind', None), [])
        errs = users.errors(cfg_with(bind='0.0.0.0')) + users.errors(cfg_with(bind='8.8.8.8'))
        self.assertEqual(len(errs), 2)
        self.assertIn('never exposed to the internet', errs[0])
        self.assertEqual(users.errors(cfg_with()), [])   # auto, the default


class ServersRefuseTest(unittest.TestCase):
    def setUp(self):   # a login with no household (the join and init checks come after that one)
        from unittest import mock
        patcher = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': tempfile.mkdtemp()})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_coordinator_never_listens_on_a_public_address(self):
        cfg = cfg_with()
        with self.assertRaises(netpolicy.PublicAddress):
            coordinator.server(cfg, port=0, bind='198.51.100.20', ifaces=IFACES)

    def test_the_coordinator_never_listens_on_every_interface(self):
        cfg = cfg_with(bind='0.0.0.0')
        with self.assertRaises(netpolicy.PublicAddress):
            coordinator.servers(cfg, port=0, ifaces=IFACES)

    def test_a_join_to_a_public_address_is_refused_before_any_request(self):
        code = household.make_code('8.8.8.8', 4778, household.new_secret())
        cfg = cfg_with()
        with self.assertRaisesRegex(netpolicy.PublicAddress, 'public address'):
            coordinator.join(cfg, code, 'Bo', 'cursor')

    def test_init_never_puts_a_public_address_in_a_join_code(self):
        from unittest import mock

        class NoRoute:
            def __init__(self, *a):
                pass

            def connect(self, _addr):
                raise OSError('no route')

            def close(self):
                pass
        public_only = [('eth0', '', ['198.51.100.20'])]
        with mock.patch.object(coordinator.socket, 'socket', NoRoute):
            with self.assertRaisesRegex(household.HouseholdError, 'no LAN or VPN address'):
                coordinator.lan_address(ifaces=public_only)
            self.assertEqual(coordinator.lan_address(ifaces=IFACES), '100.101.102.103')
        cfg = cfg_with()
        with self.assertRaisesRegex(netpolicy.PublicAddress, 'public address'):
            coordinator.init(cfg, 'home', host='8.8.8.8', port=4778)


if __name__ == '__main__':
    unittest.main()
