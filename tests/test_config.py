import copy
import os
import tempfile
import unittest

from tutti import cli, config, state

EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tutti.config.example.yaml')


class ConfigTest(unittest.TestCase):
    def test_example_config_loads_clean(self):
        cfg = config.load(EXAMPLE)
        self.assertEqual(cfg['routing']['order'], ['builder', 'gpu-box'])
        self.assertEqual(cfg['machines']['here']['transport'], 'local')
        self.assertEqual(cfg['machines']['here']['runner']['memory_gb'], 8)
        self.assertEqual(cfg['machines']['builder']['runner']['memory_gb'], 12)

    def test_defaults_fill_a_minimal_config(self):
        cfg = config.normalise({'machines': {'a': {'host': 'a.example'}}})
        self.assertEqual(cfg['routing']['order'], ['a'])
        self.assertEqual(cfg['machines']['a']['slots'], 2)
        self.assertEqual(config.validate(cfg), [])

    def test_explicit_locks_set_the_slot_count(self):
        cfg = config.normalise({'machines': {'a': {'host': 'local', 'locks': ['/tmp/x.lock']}}})
        self.assertEqual(cfg['machines']['a']['slots'], 1)

    def test_every_problem_is_named_with_its_key(self):
        cfg = config.normalise({
            'machines': {'a': {'transport': 'carrier-pigeon', 'slots': 0}, 'w': {'host': 'w', 'transport': 'wsl'},
                         'd': {'host': 'd', 'runner': {'kind': 'docker'}}},
            'routing': {'order': ['a', 'ghost']},
            'workers': {'x': {'kind': 'cli'}, 'y': {'kind': 'ollama', 'host': 'h', 'machine': 'ghost'}, 'z': {'kind': 'fax'}},
            'accounts': {'c': {'share': 2}},
        })
        errs = '\n'.join(config.validate(cfg))
        for key in ('machines.a.transport', 'machines.a.host', 'machines.a.slots', 'machines.w.wsl_helper', 'machines.d.runner.image',
                    'routing.order names "ghost"', 'workers.x.command', 'workers.y.model', 'workers.y.machine', 'workers.z.kind',
                    'accounts.c.share'):
            self.assertIn(key, errs)

    def test_a_bad_file_raises_with_the_path(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'tutti.config.json')
            with open(path, 'w', encoding='utf8') as f:
                f.write('{"routing": {"order": ["nowhere"]}}')
            with self.assertRaises(config.ConfigError) as ctx:
                config.load(path)
            self.assertIn(path, str(ctx.exception))

    def test_find_prefers_env_then_walks_up(self):
        with tempfile.TemporaryDirectory() as d:
            deep = os.path.join(d, 'a', 'b')
            os.makedirs(deep)
            path = os.path.join(d, 'tutti.config.yaml')
            open(path, 'w', encoding='utf8').close()
            self.assertEqual(config.find(deep, {'XDG_CONFIG_HOME': os.path.join(d, 'none')}), path)
            self.assertEqual(config.find(deep, {'TUTTI_CONFIG': '/elsewhere.yaml'}), '/elsewhere.yaml')

    def test_every_entry_point_resolves_the_one_xdg_config(self):
        # the bug: a project copy read by commands started in its folder, the XDG one by the dashboard and MCP
        with tempfile.TemporaryDirectory() as d:
            project, other = os.path.join(d, 'project', 'deep'), os.path.join(d, 'elsewhere')
            os.makedirs(project)
            os.makedirs(other)
            with open(os.path.join(d, 'project', 'tutti.config.yaml'), 'w', encoding='utf8') as f:
                f.write('machines: {}\n')
            xdg = os.path.join(d, 'xdg', 'tutti')
            os.makedirs(xdg)
            with open(os.path.join(xdg, 'config.yaml'), 'w', encoding='utf8') as f:
                f.write('machines: {}\n')
            env = {'XDG_CONFIG_HOME': os.path.join(d, 'xdg')}
            found = {config.find(start, env) for start in (project, other, d)}
            self.assertEqual(found, {os.path.join(xdg, 'config.yaml')})

    def test_writing_through_a_symlink_keeps_the_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            real, link = os.path.join(d, 'tutti.config.yaml'), os.path.join(d, 'xdg', 'config.yaml')
            with open(real, 'w', encoding='utf8') as f:
                f.write('a: 1\n')
            os.chmod(real, 0o600)
            os.makedirs(os.path.dirname(link))
            os.symlink(real, link)
            self.assertEqual(config.write_text(link, 'a: 2\n'), real)
            self.assertTrue(os.path.islink(link))
            with open(real, encoding='utf8') as f:
                self.assertEqual(f.read(), 'a: 2\n')
            self.assertEqual(os.stat(real).st_mode & 0o777, 0o600)
            self.assertEqual(sorted(os.listdir(d)), ['tutti.config.yaml', 'xdg'])   # no temp file left behind

    def test_find_uses_xdg_config_yaml_then_tutti_config_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            empty = os.path.join(d, 'empty')
            os.makedirs(empty)
            xdg = os.path.join(d, 'xdg')
            tutti = os.path.join(xdg, 'tutti')
            os.makedirs(tutti)
            env = {'XDG_CONFIG_HOME': xdg}
            fallback = os.path.join(tutti, 'tutti.config.yaml')
            open(fallback, 'w', encoding='utf8').close()
            self.assertEqual(config.find(empty, env), fallback)
            canonical = os.path.join(tutti, 'config.yaml')
            open(canonical, 'w', encoding='utf8').close()
            self.assertEqual(config.find(empty, env), canonical)

    def test_missing_config_names_the_xdg_path_and_the_copy(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(config.ConfigError) as ctx:
            config.load(start=d, env={'XDG_CONFIG_HOME': d})
        msg = str(ctx.exception)
        dest = os.path.join(d, 'tutti', 'config.yaml')
        self.assertIn(dest, msg)
        self.assertIn(f'cp {config.EXAMPLE} {dest}', msg)
        self.assertNotIn('tutti.config.yaml in', msg)


class LeadHoursWarningTest(unittest.TestCase):
    def test_lead_hours_other_than_one_gets_the_fix(self):
        from tutti import cli
        cfg = {'_path': '/c.yaml', 'accounts': {'a': {'lead_hours': 3}, 'b': {'lead_hours': 1}, 'c': {}}}
        lines = cli.lead_hours_warnings(cfg)
        self.assertEqual(len(lines), 1)
        self.assertIn('set lead_hours: 1 for "a" in /c.yaml', lines[0])

    def test_an_owner_chosen_lead_hours_is_not_questioned(self):
        from tutti import cli
        self.assertEqual(cli.lead_hours_warnings({'_path': '/c.yaml', 'accounts': {'a': {'lead_hours': 2}, 'b': {'lead_hours': 0.5}}}), [])


class OptInTest(unittest.TestCase):
    RAW = {'machines': {'a': {'host': 'a.example'}},
           'workers': {'agy': {'kind': 'cli', 'command': ['agy', '{prompt}']},
                       'copilot': {'kind': 'cli', 'command': ['copilot', '{prompt}'], 'costs_money': True}},
           'accounts': {'agy': {'worker': 'agy'}, 'copilot': {'worker': 'copilot'}, 'api': {'costs_money': True}},
           'routing': {'workers': ['agy', 'copilot']}, 'main': {'fallback': ['agy', 'copilot'], 'commands': {'copilot': ['c']}},
           'review': {'fallback': ['copilot']}}

    def test_what_costs_money_is_off_until_enabled(self):
        cfg = config.normalise(copy.deepcopy(self.RAW))
        self.assertEqual(list(cfg['workers']), ['agy'])
        self.assertEqual(list(cfg['accounts']), ['agy'])        # copilot's account goes with its worker; api costs money
        self.assertEqual((cfg['routing']['workers'], cfg['main']['fallback'], cfg['review']['fallback']), (['agy'], ['agy'], []))
        self.assertEqual(cfg['main']['commands'], {})
        self.assertEqual(config.validate(cfg), [])
        self.assertIn({'name': 'copilot', 'kind': 'worker', 'costs_money': True}, cfg['off'])

    def test_enabled_true_turns_it_on_and_enabled_false_turns_anything_off(self):
        raw = copy.deepcopy(self.RAW)
        raw['workers']['copilot']['enabled'] = True
        raw['accounts']['agy']['enabled'] = False
        cfg = config.normalise(raw)
        self.assertEqual(sorted(cfg['workers']), ['agy', 'copilot'])
        self.assertEqual(sorted(cfg['accounts']), ['copilot'])
        self.assertEqual(cfg['routing']['workers'], ['agy', 'copilot'])

    def test_an_off_worker_says_how_to_turn_it_on(self):
        cfg = config.normalise(copy.deepcopy(self.RAW))
        cfg['_path'] = '/c.yaml'
        with self.assertRaises(config.ConfigError) as e:
            cli.worker_of(cfg, 'copilot', 'cli')
        self.assertIn('costs money per use and is opt-in', str(e.exception))
        self.assertIn('workers.copilot.enabled: true', str(e.exception))
        self.assertIn('copilot (worker): costs money per use', state.off_notes(cfg)[0])


if __name__ == '__main__':
    unittest.main()
