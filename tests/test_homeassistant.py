"""Home Assistant on the Hosts card (tutti/homeassistant.py): mDNS discovery, configured URL, online/offline, uptime and
roles. Fake packets and HTTP; nothing leaves the temp folder; no real token is read."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from tutti import homeassistant as ha
from tutti import roles

# TXT-style mDNS payload: service labels + length-prefixed key=value strings (as lanmedia.parse_mdns expects)
HA_TXT = (b'\x0f_home-assistant\x04_tcp\x05local\x00'
          + bytes([18]) + b'location_name=Home'
          + bytes([37]) + b'uuid=0123456789abcdef0123456789abcdef'
          + bytes([17]) + b'version=2024.11.0'
          + bytes([33]) + b'internal_url=http://10.0.0.8:8123')
LANDING = (b'\x0f_home-assistant\x04_tcp\x05local\x00'
           + bytes([16]) + b'version=0000.0.0'
           + bytes([16]) + b'landingpage=True'
           + bytes([33]) + b'internal_url=http://10.0.0.9:80')


class ParseTest(unittest.TestCase):
    def test_mdns_finds_a_running_core_and_skips_the_landing_page(self):
        found = ha.parse_mdns([(HA_TXT, '10.0.0.8'), (LANDING, '10.0.0.9'), (b'nothing', '10.0.0.7')])
        self.assertEqual(len(found), 1)
        row = next(iter(found.values()))
        self.assertEqual(row['name'], 'Home')
        self.assertEqual(row['version'], '2024.11.0')
        self.assertEqual(row['url'], 'http://10.0.0.8:8123')
        self.assertEqual(row['address'], '10.0.0.8:8123')
        self.assertEqual(row['source'], 'mdns')

    def test_configured_url_pins_one_when_mdns_is_empty(self):
        cfg = {'home_assistant': {'url': 'http://ha.example:8123'}}
        got = ha.discover(cfg, mdns_fn=list)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['url'], 'http://ha.example:8123')
        self.assertEqual(got[0]['source'], 'config')

    def test_disabled_finds_nothing(self):
        self.assertEqual(ha.discover({'home_assistant': {'enabled': False}}, mdns_fn=lambda: [(HA_TXT, '10.0.0.8')]), [])


class ProbeTest(unittest.TestCase):
    def test_online_without_token_keeps_mdns_version(self):
        host = ha.from_url('http://10.0.0.8:8123')
        host['version'] = '2024.11.0'
        host['source'] = 'mdns'

        def get(url, token=''):
            if url.endswith('/api/'):
                return 401, '{"message": "Unauthorized"}'
            return 404, ''

        got = ha.probe_one(host, token='', get=get)
        self.assertEqual(got['status'], 'online')
        self.assertEqual(got['version'], '2024.11.0')
        self.assertEqual(got['uptime'], '')
        self.assertIn('home-automation-hub', got['roles_detected'])

    def test_offline_when_the_port_does_not_answer(self):
        got = ha.probe_one(ha.from_url('http://10.0.0.8:8123'), get=lambda *a: (None, None))
        self.assertEqual(got['status'], 'offline')

    def test_token_fills_uptime_and_capability_roles(self):
        started = datetime(2024, 11, 1, 12, 0, tzinfo=timezone.utc)
        now = datetime(2024, 11, 4, 16, 30, tzinfo=timezone.utc)
        states = [
            {'entity_id': 'person.alice', 'state': 'home', 'attributes': {}},
            {'entity_id': 'sensor.plug_power', 'state': '12', 'attributes': {'device_class': 'power'}},
            {'entity_id': 'switch.desk_plug', 'state': 'on', 'attributes': {}},
            {'entity_id': 'sensor.room_temp', 'state': '21', 'attributes': {'device_class': 'temperature'}},
        ]

        def get(url, token=''):
            if url.endswith('/api/'):
                return 200, '{"message": "API running."}'
            if url.endswith('/api/config'):
                return 200, json.dumps({'version': '2024.11.1', 'location_name': 'Home'})
            if url.endswith('/api/states/sensor.uptime'):
                return 200, json.dumps({'state': started.isoformat()})
            if url.endswith('/api/states'):
                return 200, json.dumps(states)
            return 404, ''

        got = ha.probe_one(ha.from_url('http://10.0.0.8:8123'), token='t', get=get, now=now)
        self.assertEqual(got['status'], 'online')
        self.assertEqual(got['version'], '2024.11.1')
        self.assertEqual(got['name'], 'Home')
        self.assertEqual(got['uptime'], '3d 4h')
        for r in ('home-automation-hub', 'presence', 'power/UPS', 'smart-plug', 'sensors'):
            self.assertIn(r, got['roles_detected'])


class RolesAndCacheTest(unittest.TestCase):
    def test_roles_are_registered(self):
        for r in ('home-automation-hub', 'presence', 'power/UPS', 'smart-plug', 'sensors'):
            self.assertIn(r, roles.ROLES)

    def test_view_honours_roles_add(self):
        host = {'name': 'Home', 'address': '10.0.0.8:8123', 'url': 'http://10.0.0.8:8123', 'version': '1',
                'status': 'online', 'uptime': '', 'uptime_seconds': None, 'source': 'mdns', 'uuid': 'u',
                'roles_detected': {'home-automation-hub': 'detected: mDNS'}}
        cfg = {'home_assistant': {'roles_add': ['presence']}}
        row = ha.view_one(host, cfg)
        self.assertEqual(roles.names(row['roles']), ['home-automation-hub', 'presence'])

    def test_hosts_cached_and_errors(self):
        scans = []

        def scan():
            scans.append(1)
            return [{'name': 'Home', 'kind': 'home-assistant', 'address': '10.0.0.8:8123', 'url': 'http://10.0.0.8:8123',
                     'version': '2024.11.0', 'uptime': '', 'uptime_seconds': None, 'status': 'online', 'uuid': 'u',
                     'source': 'mdns', 'roles': []}]

        with tempfile.TemporaryDirectory() as d:
            cfg = {'project': {'state': d}, 'home_assistant': {'enabled': True}}
            first = ha.hosts(cfg, 1000.0, folder=d, scan_fn=scan)
            ha.hosts(cfg, 1030.0, folder=d, scan_fn=scan)
            self.assertEqual(len(scans), 1)
            self.assertEqual(first[0]['name'], 'Home')
            ha.hosts(cfg, 2000.0, folder=d, scan_fn=scan)
            self.assertEqual(len(scans), 2)
        self.assertIn('home_assistant.url', ha.errors({'home_assistant': {'url': 'not-a-url'}})[0])
        self.assertEqual(ha.errors({'home_assistant': {'url': 'http://ha.example:8123'}}), [])
        self.assertEqual(ha.format_uptime(None), '')
        self.assertEqual(ha.format_uptime(90), '1m')
        self.assertEqual(ha.uptime_seconds({'state': 'not-a-date'}), None)


class TokenPathTest(unittest.TestCase):
    def test_token_file_is_read_without_printing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'tok')
            with open(path, 'w', encoding='utf8') as f:
                f.write('secret-token\n')
            cfg = {'home_assistant': {'token_file': path}}
            self.assertEqual(ha.read_token(cfg), 'secret-token')
            self.assertEqual(ha.read_token({'home_assistant': {'token_file': os.path.join(d, 'missing')}}), '')


if __name__ == '__main__':
    unittest.main()
