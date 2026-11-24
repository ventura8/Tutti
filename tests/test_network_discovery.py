"""Unit tests for LAN/VPN network discovery (tutti/network_discovery.py) and Collector integration."""
import os
import tempfile
import unittest

from tutti import config, dashboard, network_discovery


def _cfg(raw, folder):
    return config.normalise({'project': {'worktrees': folder, 'state': os.path.join(folder, 'state')}, **raw})


class ParsersTest(unittest.TestCase):
    def test_parse_arp_detects_computer_and_raspberry_pi(self):
        out = (
            '192.168.1.10 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE\n'
            '192.168.1.11 dev eth0 lladdr dc:a6:32:11:22:33 STALE\n'
            '192.168.1.12 dev eth0 FAILED\n'
        )
        items = network_discovery.parse_arp(out)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['type'], 'computer')
        self.assertEqual(items[1]['type'], 'raspberry pi')

    def test_parse_adb_extracts_serial_and_model(self):
        out = 'List of devices attached\nABC123\tdevice usb:1-1 model:Pixel_8\nOFF999\toffline\n'
        items = network_discovery.parse_adb(out)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['name'], items[0]['serial'], items[0]['type'], items[0]['roles']),
                         ('Pixel_8', 'ABC123', 'android', ['adb']))

    def test_parse_mdns_classifies_services(self):
        out = (
            '=;eth0;IPv4;Office Printer;_ipp._tcp;local;printer.local;192.168.1.20;631;\n'
            '=;eth0;IPv4;Living Room;_googlecast._tcp;local;cast.local;192.168.1.21;8009;\n'
            '=;eth0;IPv4;Shelly Plug;_http._tcp;local;shelly.local;192.168.1.22;80;\n'
            '+;eth0;IPv4;Ignored;_ssh._tcp;local\n'
        )
        items = network_discovery.parse_mdns(out)
        by_ip = {i['ip']: i for i in items}
        self.assertEqual(by_ip['192.168.1.20']['type'], 'printer')
        self.assertEqual(by_ip['192.168.1.21']['type'], 'chromecast')
        self.assertEqual(by_ip['192.168.1.22']['type'], 'smart plug')

    def test_parse_ssdp_classifies_dlna_and_ignores_empty(self):
        items = network_discovery.parse_ssdp([
            ('192.168.1.30', 'HTTP/1.1 200 OK\r\nSERVER: LivingTV/1.0 DLNA/1.5\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n'),
            ('192.168.1.31', ''),
        ])
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['name'], items[0]['type'], items[0]['roles']), ('LivingTV', 'tv', ['dlna']))


class MergeAndFilterTest(unittest.TestCase):
    def test_merges_by_ip_and_filters_configured(self):
        cfg = {
            'devices': {'pixel': {'serial': 'SER1'}},
            'machines': {'box': {'host': '192.168.1.50'}},
        }
        raw = [
            {'name': '192.168.1.10', 'ip': '192.168.1.10', 'type': 'computer', 'roles': [], 'sources': ['ARP']},
            {'name': 'nas-box', 'ip': '192.168.1.10', 'type': 'nas', 'roles': ['nas'], 'sources': ['mDNS']},
            {'name': '192.168.1.50', 'ip': '192.168.1.50', 'type': 'computer', 'roles': ['ssh'], 'sources': ['SSH']},
            {'name': 'Pixel', 'ip': 'SER1', 'serial': 'SER1', 'type': 'android', 'roles': ['adb'], 'sources': ['ADB']},
            {'name': 'Phone2', 'ip': 'SER2', 'serial': 'SER2', 'type': 'android', 'roles': ['adb'], 'sources': ['ADB']},
        ]
        out = network_discovery.merge_and_filter(cfg, raw)
        self.assertEqual(len(out), 2)
        by_name = {d['name']: d for d in out}
        self.assertEqual(by_name['nas-box']['sources'], ['ARP', 'mDNS'])
        self.assertEqual(by_name['Phone2']['possible_roles'], ['adb', 'ssh'])

    def test_scan_uses_injected_probes_without_network(self):
        def run_fn(argv):
            if argv[0] == 'ip':
                return '192.168.1.40 dev eth0 lladdr aa:bb:cc:dd:ee:01 REACHABLE\n'
            return ''

        out = network_discovery.scan({}, run_fn=run_fn, ssdp_fn=lambda: [], ssh_fn=lambda ip, port: port == 22)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['roles'], ['ssh'])
        self.assertEqual(out[0]['sources'], ['ARP', 'SSH'])

    def test_scan_skips_default_probes_when_offline_or_no_daemons(self):
        for var in ('TUTTI_OFFLINE', 'TUTTI_NO_DAEMONS'):
            prev = os.environ.get(var)
            os.environ[var] = '1'
            try:
                self.assertEqual(network_discovery.scan({}), [])
            finally:
                if prev is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = prev


class CollectorDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg({'machines': {'here': {'host': 'local'}}}, self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_collector_exposes_discovered_from_injected_fn(self):
        fake = [{'name': 'pi-one', 'ip': '192.168.1.99', 'type': 'raspberry pi', 'roles': ['ssh'], 'possible_roles': ['ssh', 'worker'],
                 'sources': ['ARP', 'SSH']}]
        c = dashboard.Collector(self.cfg, probe_fn=lambda m: {'status': 'ok'}, available_fn=lambda w: True,
                                readings_fn=lambda d: {}, power_fn=lambda m: {}, ollama_fn=lambda u: [],
                                discover_fn=lambda cfg: fake)
        c.refresh()
        self.assertEqual(c.snapshot()['discovered'], fake)
