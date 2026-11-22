"""machine -> host (t406, first slice): `hosts:` in the config, `tutti hosts`, the dashboard's Hosts tab at #hosts; every old
name keeps working, and a config with both keys is refused with the fix."""
import json
import os
import shutil
import subprocess
import unittest

from tutti import cli, config, dashboard


class Hosts(unittest.TestCase):
    def test_hosts_is_read_as_machines_and_both_are_refused(self):
        cfg = config.normalise({'hosts': {'nuc': {'host': 'nuc.example'}}})
        self.assertEqual(list(cfg['machines']), ['nuc'])
        self.assertEqual(list(config.normalise({'machines': {'nuc': {'host': 'nuc.example'}}})['machines']), ['nuc'])
        with self.assertRaisesRegex(config.ConfigError, 'both hosts: and machines:'):
            config.normalise({'hosts': {'a': {'host': 'a.example'}}, 'machines': {'b': {'host': 'b.example'}}})

    def test_tutti_hosts_is_tutti_machines(self):
        parser = cli.parser()
        self.assertIs(parser.parse_args(['hosts']).fn, parser.parse_args(['machines']).fn)

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_hosts_and_machines_open_the_same_tab(self):
        with open(os.path.join(dashboard.WEB, 'navmenu.js'), encoding='utf8') as f:
            src = f.read()
        views = '[tuttiView("#hosts"), tuttiView("#machines"), tuttiView("#hostsx"), NAV_LABELS.machines]'
        js = f'{src}\nconsole.log(JSON.stringify({views}));'
        got = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(got, ['machines', 'machines', 'chat', 'Hosts'])


if __name__ == '__main__':
    unittest.main()
