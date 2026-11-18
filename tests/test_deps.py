"""Declared needs (tutti/deps.py): pinned and checked in the config, probed and installed at user level by one generated
script, root-only ones turned into the admin's command; a bad checksum never installs anything."""
import hashlib
import os
import re
import stat
import subprocess
import unittest

import tmpdirs

from tutti import config, deps, dispatch

SHA = 'a' * 64


def fake_bin(folder, name, body):
    p = os.path.join(folder, name)
    with open(p, 'w', encoding='utf8') as f:
        f.write('#!/bin/bash\n' + body + '\n')
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)


class ConfigTest(unittest.TestCase):
    def test_pinned_needs_pass(self):
        cfg = {'projects': {'app': {'needs': [{'npm': 'typescript@5.6.3'}, {'pip': 'ruff==0.8.1'}, {'apt': 'adb'}],
                                    'tests': {'e2e': {'needs': [{'playwright': 'chromium', 'version': '1.49.0'},
                                                                {'docker': 'node@sha256:' + SHA}]}}}},
               'workers': {'w': {'needs': [{'ollama': 'qwen3:8b'}, {'bin': 'pwsh', 'download': {'url': 'https://x.example/p.tgz',
                                                                                                 'sha256': SHA, 'unpack': 'tar'}}]}}}
        self.assertEqual(deps.errors(cfg), [])
        self.assertEqual(len(deps.declared(cfg)), 7)

    def test_unpinned_or_unchecked_needs_are_refused_with_the_fix(self):
        cfg = {'workers': {'w': {'needs': [{'npm': 'typescript'}, {'pip': 'ruff'}, {'docker': 'node:24'},
                                           {'bin': 'pwsh', 'download': {'url': 'http://x.example/p', 'sha256': 'no'}},
                                           {'bin': 'adb'}, {'playwright': 'chromium'}, {'what': 1}]}},
               'projects': {'p': {'needs': 'adb'}}}
        errs = '\n'.join(deps.errors(cfg))
        for words in ('typescript@5.6.3', 'ruff==0.8.1', 'node@sha256', 'https://', "root: <the admin's", 'version:',
                      'one of npm', 'projects.p.needs is'):
            self.assertIn(words, errs)

    def test_the_config_check_includes_needs(self):
        errs = config.validate(config.normalise({'workers': {'w': {'kind': 'cli', 'command': ['x'], 'needs': [{'npm': 'x'}]}}}))
        self.assertTrue(any('not pinned' in e for e in errs))


class ScriptTest(unittest.TestCase):
    def setUp(self):
        self.home = tmpdirs.mkdtemp()
        self.bin = os.path.join(self.home, 'fakebin')
        os.makedirs(self.bin)

    def run_script(self, needs):
        env = {'HOME': self.home, 'PATH': f'{self.bin}:/usr/bin:/bin'}
        return subprocess.run(['bash', '-c', deps.script(needs)], capture_output=True, text=True, env=env, timeout=300)

    def test_a_missing_model_is_pulled_and_reported_installed(self):
        flag = os.path.join(self.home, 'pulled')
        fake_bin(self.bin, 'ollama', f'if [ "$1" = list ]; then [ -e {flag} ] && echo "qwen3:8b x"; else touch {flag}; fi')
        r = self.run_script([{'ollama': 'qwen3:8b'}])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(deps.parse(r.stdout)['installed'], ['ollama qwen3:8b'])
        self.assertEqual(deps.parse(self.run_script([{'ollama': 'qwen3:8b'}]).stdout)['installed'], [])   # present: no work

    def test_an_install_that_does_not_fix_it_fails_and_says_which(self):
        fake_bin(self.bin, 'ollama', 'exit 0')
        r = self.run_script([{'ollama': 'qwen3:8b'}])
        self.assertEqual(r.returncode, 1)
        self.assertEqual(deps.parse(r.stdout)['failed'], ['ollama qwen3:8b'])

    def test_an_npm_package_at_another_version_is_installed_pinned(self):
        log = os.path.join(self.home, 'npm.log')
        fake_bin(self.bin, 'npm', f'echo "$@" >> {log}; if [ "$1" = ls ]; then grep -q install {log} && '
                                  'echo \'{"dependencies":{"typescript":{"version":"5.6.3"}}}\' || '
                                  'echo \'{"dependencies":{"typescript":{"version":"5.0.0"}}}\'; fi')
        r = self.run_script([{'npm': 'typescript@5.6.3'}])
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(log) as f:
            content = f.read()
        self.assertIn('install -g --prefix', content)
        self.assertIn('typescript@5.6.3', content)

    def test_a_root_only_need_becomes_one_admin_command(self):
        fake_bin(self.bin, 'dpkg', 'exit 1')
        r = self.run_script([{'apt': 'adb'}])
        self.assertEqual(r.returncode, 1)
        self.assertEqual(deps.parse(r.stdout)['root'], ['sudo apt-get install -y adb'])

    def _download(self, payload, sha):
        src = os.path.join(self.home, 'payload')
        with open(src, 'wb') as f:
            f.write(payload)
        # curl stand-in: copies the local payload to -o's target (no network in tests)
        fake_bin(self.bin, 'curl', f'while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done; cp {src} "$out"')
        return self.run_script([{'bin': 'mytool', 'download': {'url': 'https://x.example/mytool', 'sha256': sha}}])

    def test_a_download_with_the_right_checksum_is_installed_and_linked(self):
        body = b'#!/bin/sh\necho hi\n'
        r = self._download(body, hashlib.sha256(body).hexdigest())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.access(os.path.join(self.home, '.local', 'bin', 'mytool'), os.X_OK))

    def test_a_download_with_a_wrong_checksum_installs_nothing(self):
        r = self._download(b'evil', SHA)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(deps.parse(r.stdout)['failed'], ['bin mytool'])
        self.assertFalse(os.path.exists(os.path.join(self.home, '.local', 'bin', 'mytool')))

    def test_never_piped_into_a_shell(self):
        s = deps.script([{'bin': 't', 'download': {'url': 'https://x.example/t', 'sha256': SHA}}])
        self.assertIsNone(re.search(r'\|\s*(sudo\s+)?(ba)?sh\b', s))
        self.assertLess(s.index('sha256sum -c'), s.index('chmod'))


class DispatchTest(unittest.TestCase):
    def test_a_suites_needs_run_before_its_command_and_none_add_nothing(self):
        cfg = {'projects': {'app': {'needs': [{'apt': 'adb'}], 'tests': {'e2e': {'needs': [{'ollama': 'm'}]}}, 'b': {}}}}
        needs = deps.for_suite(cfg, 'app', 'e2e')
        self.assertEqual(needs, [{'apt': 'adb'}, {'ollama': 'm'}])
        self.assertTrue(deps.prefix(needs).startswith('bash -c '))
        self.assertEqual(deps.prefix(deps.for_suite(cfg, 'other', 'x')), '')

    def test_test_argv_puts_the_needs_before_the_suites_command(self):
        d = tmpdirs.mkdtemp()
        cfg = {'_path': os.path.join(d, 'c.yaml'), 'projects': {'app': {'tests': {'e2e': {'needs': [{'ollama': 'm'}]}}}}}
        s = {'project': 'app', 'name': 'e2e', 'run': 'npm test {shard}', 'dir': d}
        argv = dispatch.test_argv(cfg, {'kind': 'machine', 'name': 'pc'}, s, 'a')
        self.assertTrue(argv[-1].startswith('bash -c '))
        self.assertTrue(argv[-1].endswith('&& npm test a'))

    def test_a_playwright_need_is_met_only_by_its_own_version(self):
        n = {'playwright': 'chromium', 'version': '1.49.0'}
        self.assertIn('playwright-1.49.0-chromium.ok', deps.probe(n))
        self.assertIn('playwright-1.49.0-chromium.ok', deps.install(n))

    def test_installs_of_the_other_kinds_are_user_level_and_pinned(self):
        self.assertIn('pip install --user ruff==0.8.1', deps.install({'pip': 'ruff==0.8.1'}))
        self.assertIn('--require-hashes', deps.install({'pip': 'ruff==0.8.1', 'sha256': SHA}))
        self.assertEqual(deps.install({'docker': 'n@sha256:' + SHA}), f'docker pull n@sha256:{SHA}')
        self.assertIn('dotnet workload install android', deps.install({'dotnet-workload': 'android'}))
        self.assertIn('sudo dotnet workload install android', deps.root_command({'dotnet-workload': 'android'}))
        zipped = deps.install({'bin': 't', 'download': {'url': 'https://x.example/t.zip', 'sha256': SHA, 'unpack': 'zip', 'path': 'b/t'}})
        self.assertIn('python3 -m zipfile -e', zipped)
        self.assertIn('tar -xf', deps.install({'bin': 't', 'download': {'url': 'https://x', 'sha256': SHA, 'unpack': 'tar'}}))


if __name__ == '__main__':
    unittest.main()
