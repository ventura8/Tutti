"""A Wear OS watch (the household's Pixel Watch): a device kind of its own, with stricter battery and heat rules and no runs
off its charger; a wrong kind still says which ones exist."""
import json
import os
import shutil
import subprocess
import unittest

from tutti import dashboard, devices


class Watch(unittest.TestCase):
    def test_a_watch_runs_only_on_its_charger_cool_and_charged(self):
        p = devices.policy({'kind': 'watch'})
        self.assertEqual((p['require_charging'], p['min_battery'], p['max_battery_c']), (True, 60, 38))
        self.assertEqual(devices.policy({'kind': 'watch', 'policy': {'min_battery': 80}})['min_battery'], 80)
        self.assertFalse(devices.policy({'kind': 'phone'})['require_charging'])

    def test_a_kind_tutti_does_not_know_names_the_ones_it_does(self):
        cfg = {'machines': {'here': {'transport': 'local'}},
               'devices': {'w': {'serial': 'X', 'machine': 'here', 'kind': 'watch'},
                           'z': {'serial': 'Y', 'machine': 'here', 'kind': 'fridge'}}}
        errs = devices.errors(cfg)
        self.assertTrue(any("'fridge'" in e and 'watch' in e for e in errs))
        self.assertFalse(any("'watch'" in e for e in errs))


    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_devices_tab_lists_every_device_and_the_ones_on_the_network(self):
        with open(os.path.join(dashboard.WEB, 'discovered.js'), encoding='utf8') as f:
            src = f.read()
        stub = ('function el(tag, attrs, ...kids) { return { tag, attrs: attrs || {}, kids: kids.flat() }; }\n'
                "function text(n) { return typeof n === 'string' ? n : n ? "
                "[(n.attrs || {}).text || '', ...(n.kids || []).map(text)].join(' ') : ''; }\n")
        s = {'devices': {'items': [{'name': 'pixelwatch', 'kind': 'watch', 'status': 'idle', 'reason': '', 'battery': 71}],
                         'lan': [{'name': 'TV', 'address': '192.0.2.20', 'roles': [{'role': 'smart-tv'}]}]}}
        js = (f'{stub}\n{src}\nconsole.log(JSON.stringify([deviceListNodes({json.dumps(s)}).map(text).join(" "), '
              'deviceListNodes({}).map(text).join(" ")]));')
        shown, empty = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
        for part in ('pixelwatch · watch', 'battery 71 %', 'TV · on the network', 'smart-tv'):
            self.assertIn(part, shown)
        self.assertIn('pair one', empty)


if __name__ == '__main__':
    unittest.main()
