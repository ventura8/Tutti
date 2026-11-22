"""The role matrix (t234): every machine, every role it has, could have (turned off) or cannot, with why and the fix, from
the daily probe's facts; a machine never probed says how to probe it. Shown on the Machines tab and by `tutti roles matrix`."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tutti import dashboard, rolematrix, roles, rolescmd

PROBE = 'os=Linux\ncores=4\nmem_gb=8\ndocker=1\ndesktop=1\n'


def rec(text=PROBE):
    return {'at': 1, 'detected': roles.detect_machine(text), 'facts': roles.parse(text)}


class Matrix(unittest.TestCase):
    def test_each_role_has_a_state_a_reason_and_a_fix(self):
        cfg = {'machines': {'box': {'host': 'box.example', 'roles_off': ['docker-host']}}, 'failover': {'backup': 'box'}}
        rows = {r['role']: r for r in rolematrix.machine_rows(cfg, 'box', cfg['machines']['box'], rec())}
        self.assertEqual(set(rows), set(rolematrix.MACHINE_ROLES))
        self.assertEqual((rows['linux-test-runner']['state'], rows['backup-main']['state']), ('has', 'has'))
        self.assertEqual((rows['docker-host']['state'], rows['docker-host']['fix']), ('off', 'tutti roles set box docker-host'))
        self.assertEqual(rows['build-machine']['state'], 'cannot')
        self.assertIn('4 cores and 8 GB', rows['build-machine']['why'])
        self.assertIn('ollama.com', rows['gpu-ai']['fix'])
        self.assertIn('--install-adb', rows['android-test-runner']['fix'])
        self.assertEqual(rows['windows-test-runner']['fix'], '')   # nothing Tutti can tell changes an OS
        self.assertIn('tutti move --to box', rows['main']['fix'])

    def test_a_machine_never_probed_says_how_to_probe_it(self):
        cfg = {'machines': {'new': {'host': 'new.example'}}}
        m = rolematrix.matrix(cfg, {})[0]
        docker = next(r for r in m['rows'] if r['role'] == 'docker-host')
        self.assertEqual((docker['why'], docker['fix']), ('not probed yet (or it did not answer)', 'tutti machines --roles --refresh'))
        self.assertEqual(m['has'] + m['off'] + m['cannot'], len(rolematrix.MACHINE_ROLES))

    def test_the_probe_keeps_its_facts(self):
        r, new = roles._probe({'host': 'x'}, {}, 100, lambda m, s, **k: mock.Mock(returncode=0, stdout=PROBE), True)
        self.assertTrue(new)
        self.assertEqual(r['facts']['cores'], '4')


class Surfaces(unittest.TestCase):
    def test_the_route_and_the_command(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {'project': {'state': d}, 'machines': {'box': {'host': 'box.example'}}}
            with open(os.path.join(d, roles.FILE), 'w') as f:
                json.dump({'box': rec()}, f)
            code, answer = rolematrix.get(cfg)
            self.assertEqual((code, answer['machines'][0]['name']), (200, 'box'))
            lines = []
            self.assertEqual(rolescmd.cmd_roles(cfg, mock.Mock(action='matrix'), lines.append), 0)
        self.assertTrue(lines[0].startswith('box: '))
        self.assertTrue(any(line.startswith('  - build-machine: 4 cores') for line in lines))
        with mock.patch.object(rolematrix, 'get', return_value=(200, {'machines': [], 'roles': {}})):
            code, body, *_ = dashboard.get(mock.Mock(cfg={}), rolematrix.API, 'tok', {})
        self.assertEqual(code, 200)

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_page_lists_each_machine_with_its_rows(self):
        with open(os.path.join(dashboard.WEB, 'discovered.js'), encoding='utf8') as f:
            src = f.read()
        stub = ('function el(tag, attrs, ...kids) { return { tag, attrs: attrs || {}, kids: kids.flat().filter((k) => k !== null) }; }\n'
                "function text(n) { return typeof n === 'string' ? n : n ? "
                "[(n.attrs || {}).text || '', ...(n.kids || []).map(text)].join(' ') : ''; }\n")
        d = {'machines': rolematrix.matrix({'machines': {'box': {'host': 'b'}}}, {'box': rec()})}
        js = f'{stub}\n{src}\nconsole.log(JSON.stringify(matrixNodes({json.dumps(d)}).map(text).join(" ")));'
        out = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
        for part in ('box: ', 'linux-test-runner · Has', 'gpu-ai · Cannot', 'Fix: '):
            self.assertIn(part, out)
        empty = f'{stub}\n{src}\nconsole.log(JSON.stringify(matrixNodes({{machines: []}}).map(text).join(" ")));'
        self.assertIn('No machine', subprocess.run(['node', '-e', empty], capture_output=True, text=True, check=True).stdout)


if __name__ == '__main__':
    unittest.main()
