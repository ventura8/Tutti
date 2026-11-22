"""Home Assistant on the Machines tab (t268): version, address, online and roles from its /api/config; a missing or refused
token, a wrong URL or an instance that does not answer each say what to do; the token never leaves the request."""
import json
import os
import shutil
import socket
import subprocess
import unittest
import urllib.error
from unittest import mock

from tutti import dashboard, discovered, homeassistant, lanmedia

CFG = {'home_assistant': {'url': 'http://ha.example:8123/'}}
DOC = {'version': '2026.11.1', 'location_name': 'Home', 'time_zone': 'Europe/Bucharest', 'unit_system': {}}


class Settings(unittest.TestCase):
    def test_the_url_and_token_name_are_checked(self):
        self.assertIsNone(homeassistant.settings({}))
        self.assertEqual(homeassistant.settings(CFG), {'url': 'http://ha.example:8123', 'token_env': 'TUTTI_HA_TOKEN'})
        for bad, why in (({'url': 'ftp://ha.example'}, 'http'), ({'url': 'http://u:p@ha.example'}, 'without a user'),
                         ({'url': 'http://ha.example', 'token_env': 'bad name'}, 'token_env'), ('x', 'needs url')):
            with self.assertRaisesRegex(homeassistant.HaError, why):
                homeassistant.settings({'home_assistant': bad})


class Status(unittest.TestCase):
    def test_a_connected_instance_shows_version_address_and_role(self):
        seen = []

        def fetch(url, token):
            seen.append((url, token))
            return DOC
        d = homeassistant.status(CFG, fetch, token_fn=lambda name: 'tok' if name == 'TUTTI_HA_TOKEN' else '')
        self.assertEqual(seen, [('http://ha.example:8123/api/config', 'tok'), ('http://ha.example:8123/api/states', 'tok')])
        item = d['item']
        self.assertEqual((item['name'], item['address'], item['online'], item['version']), ('Home', 'ha.example', True, '2026.11.1'))
        self.assertEqual(item['roles'][0]['role'], 'home-automation-hub')
        self.assertNotIn('tok', json.dumps(d))   # the token is never in the answer

    def test_each_failure_says_what_to_do(self):
        def refused(url, token):
            raise urllib.error.HTTPError(url, 401, 'no', {}, None)

        def down(url, token):
            raise OSError('connection refused')
        d = homeassistant.status(CFG, refused, token_fn=lambda n: 'tok')
        self.assertTrue(d['item']['online'])
        self.assertIn('long-lived access token', d['item']['problem'])
        d = homeassistant.status(CFG, down, token_fn=lambda n: 'tok')
        self.assertFalse(d['item']['online'])
        self.assertIn('does not answer', d['item']['problem'])
        d = homeassistant.status(CFG, lambda url, token: None, token_fn=lambda n: '')
        self.assertIn('add a token as TUTTI_HA_TOKEN', d['item']['problem'])
        self.assertEqual(homeassistant.status({}, down)['configured'], False)
        self.assertIn('needs url', homeassistant.status({'home_assistant': 'x'}, down)['problem'])

    def test_the_route_answers_without_a_secret(self):
        c = mock.Mock(cfg={})
        code, body, *_ = dashboard.get(c, homeassistant.API, 'tok', {})
        self.assertEqual((code, json.loads(body)['configured']), (200, False))


class Lookup(unittest.TestCase):
    def setUp(self):
        homeassistant.KNOWN.clear()
        self.addCleanup(homeassistant.KNOWN.clear)

    def flaky(self, fails):
        calls = []

        def fetch(url, token):
            calls.append(url)
            if len(calls) <= fails:
                raise urllib.error.URLError(socket.gaierror(-2, 'Name or service not known'))
            return DOC
        return fetch, calls

    def test_a_flaky_name_is_retried(self):
        fetch, calls = self.flaky(2)
        got = homeassistant.reliable(fetch, 'http://ha.local:8123/api/config', 't', resolve=lambda h: '192.0.2.98',
                                     sleep=lambda s: None)
        self.assertEqual((got, len(calls)), (DOC, 3))
        self.assertEqual(homeassistant.KNOWN['ha.local'], '192.0.2.98')

    def test_after_the_tries_the_last_address_is_used_and_without_one_it_says_what_to_do(self):
        homeassistant.KNOWN['ha.local'] = '192.0.2.98'
        fetch, calls = self.flaky(3)
        homeassistant.reliable(fetch, 'http://ha.local:8123/api/config', 't', resolve=lambda h: '192.0.2.98', sleep=lambda s: None)
        self.assertEqual(calls[-1], 'http://192.0.2.98:8123/api/config')
        homeassistant.KNOWN.clear()
        fetch, _ = self.flaky(9)
        with self.assertRaisesRegex(OSError, 'did not resolve'):
            homeassistant.reliable(fetch, 'http://ha.local:8123/api/config', 't', sleep=lambda s: None)

    def test_a_refused_token_is_not_retried(self):
        calls = []

        def refused(url, token):
            calls.append(url)
            raise urllib.error.HTTPError(url, 401, 'no', {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            homeassistant.reliable(refused, 'http://ha.local:8123/api/config', 't', sleep=lambda s: None)
        self.assertEqual(len(calls), 1)


STATES = [
    {'entity_id': 'person.owner', 'state': 'home', 'attributes': {'friendly_name': 'Owner'}},
    {'entity_id': 'person.guest', 'state': 'not_home', 'attributes': {'friendly_name': 'Guest'}},
    {'entity_id': 'sensor.nuc_plug_power', 'state': '81.3', 'attributes': {'device_class': 'power', 'unit_of_measurement': 'W',
                                                                           'friendly_name': 'NUC Plug NUC Plug Power'}},
    {'entity_id': 'sensor.pc_plug_power', 'state': '0.35', 'attributes': {'device_class': 'power', 'unit_of_measurement': 'kW'}},
    {'entity_id': 'sensor.pi5_plug_power', 'state': 'unavailable', 'attributes': {'device_class': 'power', 'unit_of_measurement': 'W'}},
    {'entity_id': 'sensor.nuc_plug_energy', 'state': '173', 'attributes': {'device_class': 'energy', 'unit_of_measurement': 'kWh'}},
]


class Overview(unittest.TestCase):
    def test_people_power_and_each_machines_plug(self):
        o = homeassistant.overview(STATES, ['nuc', 'w5090', 'pi5'], {'w5090': 'sensor.pc_plug_power'})
        self.assertEqual(o['people'], [{'name': 'Owner', 'home': True}, {'name': 'Guest', 'home': False}])
        self.assertEqual((o['power_w'], o['sensors']), (431, 2))   # kW read as W; unavailable and energy left out
        self.assertEqual(o['top'][0], {'name': 'pc plug', 'w': 350})
        self.assertEqual(o['top'][1]['name'], 'NUC Plug')   # "NUC Plug NUC Plug" said once
        self.assertEqual(o['machines'], {'nuc': 81.3, 'w5090': 350.0})   # by name, and by the config's plugs
        self.assertEqual(homeassistant.overview('junk'), {'people': [], 'power_w': 0, 'top': [], 'machines': {}, 'sensors': 0})

    def test_a_failed_states_read_keeps_the_card(self):
        def fetch(url, token):
            if url.endswith('/api/states'):
                raise OSError('timed out')
            return DOC
        d = homeassistant.status(CFG, fetch, token_fn=lambda n: 'tok')
        self.assertEqual(d['item']['version'], '2026.11.1')
        self.assertIn('people and power could not be read', d['item']['problem'])


class Found(unittest.TestCase):
    def test_mdns_names_it_and_discovered_leaves_out_the_configured_one(self):
        self.assertEqual(lanmedia.SERVICES['_home-assistant._tcp'], 'home-automation-hub')
        neigh = [{'address': '192.0.2.50', 'mac': 'x'}, {'address': '192.0.2.51', 'mac': 'y'}]
        items = discovered.view(CFG, (), neigh, resolve={'ha.example': '192.0.2.50'}.get)
        self.assertEqual([i['address'] for i in items], ['192.0.2.51'])

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_card_says_what_it_is_and_how_to_connect_it(self):
        with open(os.path.join(dashboard.WEB, 'discovered.js'), encoding='utf8') as f:
            src = f.read()
        stub = ('function el(tag, attrs, ...kids) { return { tag, attrs: attrs || {}, kids: kids.flat() }; }\n'
                "function text(n) { return typeof n === 'string' ? n : n ? "
                "[(n.attrs || {}).text || '', ...(n.kids || []).map(text)].join(' ') : ''; }\n")
        on = homeassistant.status(CFG, lambda u, t: STATES if u.endswith('/api/states') else DOC, token_fn=lambda n: 'tok')
        js = (f'{stub}\n{src}\nconsole.log(JSON.stringify([haNodes({json.dumps(on)}).map(text).join(" "), '
              f'haNodes({{configured: false}}).map(text).join(" ")]));')
        shown, off = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
        for part in ('Home · ha.example', 'online · version 2026.11.1', 'Roles: home-automation-hub', 'People: Owner home',
                     'Power now: 431 W'):
            self.assertIn(part, shown)
        self.assertIn('home-assistant.env', off)


if __name__ == '__main__':
    unittest.main()
