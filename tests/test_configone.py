"""One config per user: Settings saves through a symlink into the real file, and a drifted copy is found (doctor, Settings)
and merged into the one in use, then linked to it."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from tutti import cli, config, configone, settings

MINE = """# mine: keep this comment
project:
  state: STATE
energy:
  price_per_kwh: 1.2    # the owner's price
"""
COPY = """project:
  state: STATE
energy:
  price_per_kwh: 0.9
pacing:
  schedule:
    days: [mon, tue]
"""


class Collector:
    def __init__(self, path):
        self.cfg, self.env = config.load(path), {}

    def reload(self):
        self.cfg = config.load(self.cfg['_path'])
        return self.cfg


class OneConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = tmp.name
        state = os.path.join(self.d, 'state')
        self.xdg = os.path.join(self.d, 'xdg')
        self.env = {'XDG_CONFIG_HOME': self.xdg}
        self.mine = os.path.join(self.xdg, 'tutti', 'config.yaml')
        self.project = os.path.join(self.d, 'project')
        self.copy = os.path.join(self.project, 'tutti.config.yaml')
        os.makedirs(os.path.dirname(self.mine))
        os.makedirs(self.project)
        for path, text in ((self.mine, MINE), (self.copy, COPY)):
            with open(path, 'w', encoding='utf8') as f:
                f.write(text.replace('STATE', state))
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)

    def read(self, path):
        with open(path, encoding='utf8') as f:
            return f.read()

    def test_the_drifted_copy_is_found_with_what_differs(self):
        cfg = config.load(self.mine)
        found = configone.diverging(cfg, start=self.project, env=self.env)
        self.assertEqual([d['path'] for d in found], [self.copy])
        self.assertEqual(found[0]['missing'], ['pacing'])
        self.assertEqual(found[0]['conflicts'], ['energy.price_per_kwh'])
        self.assertIn('tutti doctor --merge-configs', configone.warnings(cfg, self.project, self.env)[0])

    def test_identical_or_linked_copies_are_not_reported(self):
        os.remove(self.copy)
        os.symlink(self.mine, self.copy)
        cfg = config.load(self.mine)
        self.assertEqual(configone.diverging(cfg, start=self.project, env=self.env), [])

    def test_merge_adds_the_missing_keeps_mine_and_links_the_copy(self):
        cfg = config.load(self.mine)
        done = configone.merge(cfg, start=self.project, env=self.env, clock=lambda: 0)
        text = self.read(self.mine)
        self.assertIn('# mine: keep this comment', text)
        merged = config.load(self.mine)
        self.assertEqual(merged['energy']['price_per_kwh'], 1.2)
        self.assertEqual(merged['pacing']['schedule']['days'], ['mon', 'tue'])
        self.assertEqual(done['merged'][0]['kept'], ['energy.price_per_kwh'])
        self.assertTrue(os.path.islink(self.copy))
        self.assertEqual(os.path.realpath(self.copy), os.path.realpath(self.mine))
        self.assertIn('price_per_kwh: 0.9', self.read(done['merged'][0]['backup']))
        self.assertEqual(configone.diverging(merged, start=self.project, env=self.env), [])

    def test_doctor_reports_the_divergence_and_merges_on_request(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch('os.getcwd', return_value=self.project), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), mock.patch.object(cli.doctor, 'check_git', return_value=[]):
            self.assertEqual(cli.main(['--config', self.mine, 'doctor']), 1)
            self.assertIn('differs from the config in use', err.getvalue())
            self.assertEqual(cli.main(['--config', self.mine, 'doctor', '--merge-configs']), 0)
        self.assertTrue(os.path.islink(self.copy))

    def test_settings_shows_the_copy_and_merges_it_in_one_click(self):
        with mock.patch('os.getcwd', return_value=self.project):
            c = Collector(self.mine)
            self.assertEqual(len(settings.view(c.cfg)['copies']), 1)
            code, answer = settings.save(c, json.dumps({'action': 'merge-configs'}).encode())
            self.assertEqual(code, 200)
            self.assertEqual(c.cfg['pacing']['schedule']['days'], ['mon', 'tue'])
            self.assertEqual(settings.view(c.cfg)['copies'], [])

    def test_a_symlinked_copy_is_moved_as_the_link_not_its_target(self):
        target = os.path.join(self.d, 'elsewhere.yaml')
        os.replace(self.copy, target)
        os.symlink(target, self.copy)
        done = configone.merge(config.load(self.mine), start=self.project, env=self.env)
        self.assertEqual(done['merged'][0]['path'], self.copy)
        self.assertTrue(os.path.isfile(target) and not os.path.islink(target))   # someone else's file is untouched
        self.assertEqual(os.path.realpath(self.copy), os.path.realpath(self.mine))

    def test_a_link_that_cannot_be_made_puts_the_copy_back(self):
        with mock.patch('os.symlink', side_effect=OSError('no symlink rights')):
            done = configone.merge(config.load(self.mine), start=self.project, env=self.env)
        self.assertIn('no symlink rights', done['merged'][0]['error'])
        self.assertFalse(os.path.islink(self.copy))
        self.assertIn('price_per_kwh: 0.9', self.read(self.copy))
        self.assertEqual(config.load(self.mine)['pacing']['schedule']['days'], ['mon', 'tue'])

    def test_a_repository_config_elsewhere_is_not_a_copy(self):
        cfg = config.load(self.mine)
        self.assertEqual(configone.diverging(cfg, start=self.d, env=self.env), [])

    def test_a_merge_that_would_break_the_config_writes_nothing(self):
        with open(self.copy, 'a', encoding='utf8') as f:
            f.write('machines:\n  box: {transport: carrier-pigeon}\n')
        cfg = config.load(self.mine)
        before = self.read(self.mine)
        with self.assertRaises(config.ConfigError):
            configone.merge(cfg, start=self.project, env=self.env)
        self.assertEqual(self.read(self.mine), before)
        self.assertFalse(os.path.islink(self.copy))

    def test_a_settings_save_through_a_symlink_keeps_the_symlink(self):
        link = os.path.join(self.d, 'link.yaml')
        os.symlink(self.mine, link)
        c = Collector(link)
        code, _ = settings.save(c, json.dumps({'path': ['energy', 'price_per_kwh'], 'value': 1.5}).encode())
        self.assertEqual(code, 200)
        self.assertTrue(os.path.islink(link))
        self.assertIn('price_per_kwh: 1.5', self.read(self.mine))
        self.assertIn('# mine: keep this comment', self.read(self.mine))


if __name__ == '__main__':
    unittest.main()
