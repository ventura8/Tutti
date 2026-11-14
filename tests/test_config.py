import os
import tempfile
import unittest

from tutti import config

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
            self.assertEqual(config.find(deep, {}), path)
            self.assertEqual(config.find(deep, {'TUTTI_CONFIG': '/elsewhere.yaml'}), '/elsewhere.yaml')

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


if __name__ == '__main__':
    unittest.main()
