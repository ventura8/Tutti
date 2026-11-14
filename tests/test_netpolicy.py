"""Never on the internet (docs/HOUSEHOLD.md, *Network*): a server may listen only on localhost, private, LAN or VPN
addresses; a public address or a wildcard is refused with the reason. No real network. (The pure part of the household
branch's tests; the coordinator's and the dashboard's come with that branch.)"""
import unittest

from tutti import netpolicy

IFACES = [('lo', '', ['127.0.0.1', '::1']), ('wlo1', '', ['192.168.1.33', 'fe80::1']), ('docker0', 'bridge', ['172.17.0.1']),
          ('tailscale0', '', ['100.101.102.103']), ('wg0', 'wireguard', ['203.0.113.7']), ('eth1', '', ['198.51.100.20'])]


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
        self.assertEqual(netpolicy.binds('auto', 'household.bind', IFACES), ['127.0.0.1', '100.101.102.103', '192.168.1.33',
                                                                              '203.0.113.7'])

    def test_the_config_check_names_a_public_or_wildcard_bind(self):
        self.assertIn('every interface', netpolicy.errors('mcp.bind', '0.0.0.0')[0])
        self.assertIn('public address', netpolicy.errors('mcp.bind', '8.8.8.8')[0])
        self.assertEqual(netpolicy.errors('mcp.bind', '192.168.1.5') + netpolicy.errors('mcp.bind', None), [])


if __name__ == '__main__':
    unittest.main()
