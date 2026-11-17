"""Dashboard listeners share a port and keep localhost reachable alongside LAN/VPN addresses."""
import errno
import http.client
import tempfile
import threading
import unittest
from unittest import mock

from tutti import auth, config, dashboard, netpolicy


class BindTest(unittest.TestCase):
    def test_auto_detects_lan_and_vpn(self):
        ifaces = [('lo', '', ['127.0.0.1']), ('eth0', '', ['192.168.1.10']), ('wg0', 'wireguard', ['10.8.0.2'])]
        with mock.patch.object(netpolicy, 'interfaces', return_value=ifaces):
            self.assertEqual(dashboard.binds_of({'dashboard': {'bind': 'auto'}}),
                             ['127.0.0.1', '::1', '10.8.0.2', '192.168.1.10'])
        self.assertEqual(dashboard.binds_of({'dashboard': {'bind': ['192.168.1.10', '127.0.0.1']}}),
                         ['127.0.0.1', '192.168.1.10'])

    def test_public_list_member_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config.normalise({'project': {'state': folder}, 'dashboard': {'bind': ['127.0.0.1', '8.8.8.8']}})
            self.assertIn('public address', auth.network_errors(cfg, ifaces=[])[0])
        self.assertTrue(config.validate(config.normalise({'dashboard': {'bind': []}})))

    def test_partial_bind_failure_closes_all_listeners(self):
        httpd = mock.Mock(siblings=[])
        sibling = mock.Mock()
        failure = OSError(errno.EADDRINUSE, 'address in use')
        with mock.patch.object(dashboard, '_make_server', side_effect=[sibling, failure]):
            with self.assertRaisesRegex(OSError, 'dashboard.bind.*port is free'):
                dashboard._attach_siblings(httpd, ['127.0.0.2', '127.0.0.3'], 4777, object())
        self.assertEqual(httpd.siblings, [sibling])
        httpd.server_close.assert_called_once_with()

    def test_auto_bind_stale_address_is_skipped(self):
        httpd = mock.Mock(siblings=[])
        sibling = mock.Mock()
        unavailable = OSError(errno.EADDRNOTAVAIL, 'cannot assign requested address')
        with mock.patch.object(dashboard, '_make_server', side_effect=[sibling, unavailable]):
            dashboard._attach_siblings(httpd, ['127.0.0.2', '192.168.1.77'], 4777, object(), optional=['192.168.1.77'])
        self.assertEqual(httpd.siblings, [sibling])
        httpd.server_close.assert_not_called()

    def test_http_on_lan_and_localhost(self):
        ifaces = netpolicy.interfaces()
        lan = [a for a in netpolicy.private_addresses(ifaces) if ':' not in a]
        if not lan:
            self.skipTest('requires an assigned private IPv4 interface')
        with tempfile.TemporaryDirectory() as folder:
            cfg = config.normalise({'project': {'state': folder}, 'dashboard': {'bind': 'auto'},
                                    'auth': {'client_id': 'test.apps.googleusercontent.com'}})
            # Exercise actual assigned LAN sockets; unavailable IPv6 loopback is optional.
            with mock.patch.object(netpolicy, 'interfaces', return_value=ifaces):
                httpd = dashboard.server(cfg, 0)
            self.addCleanup(httpd.server_close)
            thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            thread.start()
            try:
                for address in ('127.0.0.1', lan[0], '127.0.0.1'):
                    with self.subTest(address=address):
                        connection = http.client.HTTPConnection(address, httpd.server_address[1], timeout=5)
                        try:
                            connection.request('GET', '/api/auth/me')
                            response = connection.getresponse()
                            self.assertEqual(response.status, 200)
                            self.assertTrue(response.read())
                        finally:
                            connection.close()
            finally:
                httpd.shutdown()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
