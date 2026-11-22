"""The Discovered tab (t220): what is on the network and not set up yet, from every source; one failing source leaves the
others; admin-only."""
import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

from tutti import dashboard, discovered

NEIGH = """192.0.2.10 dev eth0 lladdr 02:00:00:00:00:10 REACHABLE
192.0.2.11 dev eth0 lladdr 02:00:00:00:00:11 STALE
192.0.2.12 dev eth0  FAILED
192.0.2.13 dev eth0 lladdr 02:00:00:00:00:13 INCOMPLETE
fe80::1 dev eth0 lladdr 02:00:00:00:00:01 router REACHABLE
172.17.0.3 dev docker0 lladdr 02:42:ac:11:00:03 STALE
172.18.0.2 dev br-1a2b3c lladdr 02:42:ac:12:00:02 REACHABLE
192.0.2.10 dev wlan0 lladdr 02:00:00:00:00:10 REACHABLE
"""
CFG = {'machines': {'builder': {'host': 'builder.example'}}, 'devices': {'pixel': {'serial': '192.0.2.40:5555'}}}


class View(unittest.TestCase):
    def test_live_ipv4_neighbours_once(self):
        self.assertEqual([n['address'] for n in discovered.parse_neigh(NEIGH)], ['192.0.2.10', '192.0.2.11'])
        self.assertEqual(discovered.parse_neigh(''), [])

    def test_every_source_merges_and_what_is_set_up_is_left_out(self):
        lan = [{'name': 'TV', 'address': '192.0.2.20', 'roles': [{'role': 'smart-tv'}]}]
        neigh = discovered.parse_neigh(NEIGH) + [{'address': '192.0.2.20', 'mac': 'x'}, {'address': '192.0.2.40', 'mac': 'y'},
                                                  {'address': '192.0.2.5', 'mac': 'z'}]
        adb = [{'serial': 'EMU1', 'state': 'device', 'model': 'Pixel'}, {'serial': 'EMU2', 'state': 'unauthorized', 'model': ''}]
        items = discovered.view(CFG, lan, neigh, adb, ['192.0.2.10'], resolve={'builder.example': '192.0.2.5'}.get)
        got = {i['address']: (i['kind'], i['roles'], i['found_by']) for i in items}
        self.assertEqual(got['192.0.2.10'], ('computer', [], ['neighbour table', 'ssh answers']))
        self.assertEqual(got['192.0.2.20'], ('media', ['smart-tv'], ['mDNS / SSDP', 'neighbour table']))
        self.assertEqual(got['EMU1'][0], 'android')
        for gone in ('192.0.2.40', '192.0.2.5', 'EMU2'):   # a configured device and machine; an adb device not allowed yet
            self.assertNotIn(gone, got)
        self.assertEqual([i['kind'] for i in items], ['computer', 'android', 'media', 'host'])
        self.assertTrue(all(i['can'] for i in items))

    def test_a_failing_source_is_named_and_the_others_still_answer(self):
        def run(cmd, **_kw):
            if cmd[0] == 'adb':
                raise OSError('adb crashed')
            return mock.Mock(stdout=NEIGH)

        def lan(_cfg, _now):
            raise OSError('no multicast')
        items, problems = discovered.gather({}, 0, run=run, which=lambda c: c != 'nothing', probe=lambda a: a == '192.0.2.11',
                                            lan_fn=lan)
        self.assertEqual({i['address']: i['kind'] for i in items}, {'192.0.2.11': 'computer', '192.0.2.10': 'host'})
        self.assertIn('media scan failed (no multicast)', problems[0])
        _items, problems = discovered.gather({}, 0, run=run, which=lambda c: None, probe=lambda a: False, lan_fn=lambda c, n: [])
        self.assertIn('`ip` is not installed', problems[0])

    def test_the_route_is_an_admins(self):
        from tutti import authweb
        self.assertIn(discovered.API, authweb.ADMIN_GET)
        c = mock.Mock(cfg={})
        with mock.patch.object(discovered, 'gather', return_value=([], [])):
            self.assertEqual(json.loads(dashboard.admin_json(c, discovered.API)[1]), {'items': [], 'problems': []})
        self.assertIsNone(dashboard.admin_json(c, '/api/other'))

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_row_says_what_it_is_its_roles_and_what_it_could_be(self):
        with open(os.path.join(dashboard.WEB, 'discovered.js'), encoding='utf8') as f:
            src = f.read()
        stub = ('function el(tag, attrs, ...kids) { return { tag, attrs: attrs || {}, kids: kids.flat() }; }\n'
                "function text(n) { return typeof n === 'string' ? n : n ? "
                "[(n.attrs || {}).text || '', ...(n.kids || []).map(text)].join(' ') : ''; }\n")
        item = discovered.view({}, [{'name': 'TV', 'address': '192.0.2.20', 'roles': [{'role': 'smart-tv'}]}])[0]
        js = f'{stub}\n{src}\nconsole.log(JSON.stringify(text(discoveredRow({json.dumps(item)}))));'
        out = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
        for part in ('TV · 192.0.2.20', 'Media device', 'Roles now: smart-tv', 'Could be: media target'):
            self.assertIn(part, out)


if __name__ == '__main__':
    unittest.main()
