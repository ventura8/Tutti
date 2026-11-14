"""`tutti update`: the plan per program, the machine script (run for real through local bash on fake programs), the owner's
command, the ollama pulls, the daily round and the config's checks."""
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from tutti import config, dashboard, inuse, update


def cfg_of(raw, folder):
    raw = {'project': {'worktrees': folder, 'state': os.path.join(folder, 'state')}, **raw}
    cfg = config.normalise(raw)
    cfg['_path'] = os.path.join(folder, 'tutti.config.yaml')
    return cfg


def fake(folder, name, body):
    p = os.path.join(folder, name)
    with open(p, 'w', encoding='utf-8') as f:
        f.write('#!/bin/bash\n' + body + '\n')
    os.chmod(p, stat.S_IRWXU)
    return p


class PlanTest(unittest.TestCase):
    def test_programs_from_workers_and_config(self):
        cfg = config.normalise({'workers': {
            'cursor': {'kind': 'cli', 'command': ['cursor-agent', '-p', '{prompt}']},
            'mine': {'kind': 'cli', 'command': ['mytool'], 'update': {'command': 'pipx upgrade mytool'}},
            'quiet': {'kind': 'cli', 'command': ['other'], 'update': False},
            'small': {'kind': 'ollama', 'host': '127.0.0.1:1', 'model': 'm'}}, 'update': {'programs': ['gemini', 'cursor-agent']}})
        p = update.programs(cfg)
        self.assertEqual(p['cursor-agent'], {'version': '', 'update': 'cursor-agent update'})   # '': `cursor-agent --version`
        self.assertEqual(p['mytool']['update'], 'pipx upgrade mytool')
        self.assertEqual(p['gemini']['update'], '')      # found on the machine: npm, or a manual note
        self.assertNotIn('other', p)
        self.assertEqual(len(p), 3)

    def test_parse_keeps_only_marked_rows_and_shortens_versions(self):
        row = '\t'.join([update.MARK, 'copilot', 'updated', 'GitHub Copilot CLI 1.0.112.', 'GitHub Copilot CLI 1.0.113.', 'done'])
        text = f'noise\n{row}\n{update.MARK}\tbad\n'
        rows = update.parse(text)
        self.assertEqual(rows, [{'name': 'copilot', 'status': 'updated', 'before': '1.0.112', 'after': '1.0.113', 'detail': 'done'}])

    def test_owner_command_for_an_old_package_and_a_switched_off_source(self):
        res = {'laptop': {'items': [
            {'name': 'cursor', 'status': 'package-old', 'before': '3.24.18', 'after': '3.30.14', 'detail': ''},
            {'name': 'grok-bot', 'status': 'package', 'before': '0.71.2', 'after': '0.71.2',
             'detail': '/etc/apt/sources.list.d/grok-bot.sources'},
            {'name': 'gemini', 'status': 'owner', 'before': '0.1', 'after': '', 'detail': 'sudo npm install -g @google/gemini-cli@latest'},
            {'name': 'agy', 'status': 'current', 'before': '1', 'after': '1', 'detail': ''}]}, 'nuc': {'items': [], 'error': None}}
        [o] = update.owner_commands(res)
        self.assertEqual(o['machine'], 'laptop')
        self.assertTrue(o['command'].startswith("sudo sed -i 's/^Enabled: *no/Enabled: yes/' /etc/apt/sources.list.d/grok-bot.sources && "))
        self.assertIn('sudo apt update && sudo apt install --only-upgrade cursor grok-bot', o['command'])
        self.assertTrue(o['command'].endswith('sudo npm install -g @google/gemini-cli@latest'))
        self.assertIn('cursor 3.24.18 -> 3.30.14', o['why'])

    def test_nothing_for_the_owner_when_all_is_current(self):
        res = {'a': {'items': [{'name': 'cursor', 'status': 'package', 'before': '1', 'after': '1', 'detail': ''}]}}
        self.assertEqual(update.owner_commands(res), [])


class ScriptTest(unittest.TestCase):
    """The machine script, run through local bash against fake programs in a temp folder."""

    def run_script(self, progs, check=False, extra=''):
        with tempfile.TemporaryDirectory() as t:
            ver = os.path.join(t, 'ver')
            with open(ver, 'w', encoding='utf-8') as f:
                f.write('1.0.0')
            fake(t, 'gooda', f'if [ "$1" = update ]; then echo 2.0.0 > {ver}; echo "Updated."; else echo "gooda $(cat {ver})"; fi')
            fake(t, 'brokena', 'if [ "$1" = update ]; then echo "network down" >&2; exit 3; fi; echo 5.0')
            npm = os.path.join(t, 'lib', 'node_modules', '@x', 'tool', 'bin')
            os.makedirs(npm)
            os.symlink(fake(npm, 'tool.js', 'echo 0.9'), os.path.join(t, 'npmtool'))
            fake(t, 'npm', f'echo "$@" > {t}/npm-args')
            env = {**os.environ, 'PATH': f'{t}:{os.environ["PATH"]}'}
            text = update.script(progs, [], check) + extra
            out = subprocess.run(['bash', '-c', text], capture_output=True, text=True, env=env, check=False)
            args = ''
            if os.path.exists(os.path.join(t, 'npm-args')):
                with open(os.path.join(t, 'npm-args'), encoding='utf-8') as f:
                    args = f.read()
            return {r['name']: r for r in update.parse(out.stdout)}, args, t

    def spec(self, p, upd=None):
        return {'version': f'{p} --version', 'update': f'{p} update' if upd is None else upd}

    def test_update_failure_npm_and_missing(self):
        progs = {'gooda': self.spec('gooda'), 'brokena': self.spec('brokena'), 'npmtool': self.spec('npmtool', ''),
                 'nothere': self.spec('nothere')}
        rows, npm_args, t = self.run_script(progs)
        self.assertEqual((rows['gooda']['status'], rows['gooda']['before'], rows['gooda']['after']), ('updated', '1.0.0', '2.0.0'))
        self.assertEqual(rows['brokena']['status'], 'failed')
        self.assertIn('exit 3: network down', rows['brokena']['detail'])
        self.assertEqual(rows['npmtool']['status'], 'current')
        self.assertEqual(npm_args.split(), ['install', '-g', '--prefix', t, '@x/tool@latest'])
        self.assertEqual(rows['nothere']['status'], 'missing')

    def test_check_changes_nothing(self):
        rows, npm_args, _ = self.run_script({'gooda': self.spec('gooda'), 'npmtool': self.spec('npmtool', '')}, check=True)
        self.assertEqual((rows['gooda']['status'], rows['gooda']['before']), ('checked', '1.0.0'))
        self.assertEqual(npm_args, '')

    def test_a_running_program_is_skipped(self):
        with tempfile.TemporaryDirectory() as t:
            busy = fake(t, 'busyprog', '[ -n "$1" ] && { echo 1.0; exit 0; }; sleep 30')
            proc = subprocess.Popen([busy])
            try:
                for _ in range(200):   # until the fork has become the script (before that its command line is Python's)
                    with open(f'/proc/{proc.pid}/cmdline', 'rb') as f:
                        if b'busyprog' in f.read():
                            break
                    threading.Event().wait(0.01)
                env = {**os.environ, 'PATH': f'{t}:{os.environ["PATH"]}'}
                text = update.script({'busyprog': self.spec('busyprog')}, [])
                out = subprocess.run(['bash', '-c', text], capture_output=True, text=True, env=env, check=False)
            finally:
                proc.kill()
                proc.wait()
            [row] = update.parse(out.stdout)
            self.assertEqual(row['status'], 'busy')

    def test_a_package_that_is_not_installed(self):
        text = update.script({}, ['tutti-no-such-package'])
        out = subprocess.run(['bash', '-c', text], capture_output=True, text=True, check=False)
        [row] = update.parse(out.stdout)
        self.assertEqual(row['status'], 'missing')


class RunTest(unittest.TestCase):
    def test_run_saves_and_reports_an_unreachable_machine(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = cfg_of({'machines': {'a': {'host': 'local'}, 'b': {'host': 'b.example'}},
                          'workers': {'c': {'kind': 'cli', 'command': ['cursor-agent']}}}, t)

            def sh(machine, text, capture, timeout):
                if machine['name'] == 'b':
                    return subprocess.CompletedProcess([], 255, '', 'ssh: connect to host b.example port 22: No route to host\n')
                return subprocess.CompletedProcess([], 0, f'{update.MARK}\tcursor-agent\tcurrent\t1.2\t1.2\t\n', '')
            data = update.run(cfg, sh=sh, clock=lambda: 1000.0)
            self.assertEqual(data['machines']['a']['items'][0]['status'], 'current')
            self.assertIn('No route to host', data['machines']['b']['error'])
            self.assertEqual(update.load(cfg)['at'], 1000.0)
            self.assertIn('b: unreachable', update.report(data))

    def test_check_does_not_save(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = cfg_of({'machines': {'a': {'host': 'local'}}}, t)
            update.run(cfg, check=True, sh=lambda *a, **k: subprocess.CompletedProcess([], 0, '', ''))
            self.assertIsNone(update.load(cfg))

    def test_models_are_pulled_and_a_dead_server_says_how_to_fix_it(self):
        cfg = config.normalise({'workers': {'small': {'kind': 'ollama', 'host': '127.0.0.1:1', 'model': 'm:9b'},
                                            'gone': {'kind': 'ollama', 'host': '127.0.0.1:2', 'model': 'x'}}})
        digests = iter(['aaa', 'bbb'])

        def call(host, path, payload=None, timeout=30):
            if host.endswith(':2'):
                raise OSError('Connection refused')
            if path == '/api/ps':
                return {'models': []}
            return {'models': [{'name': 'm:9b', 'digest': next(digests)}]} if path == '/api/tags' else {'status': 'success'}
        rows = {r['name']: r for r in update.pull_models(cfg, call=call)}
        self.assertEqual(rows['m:9b (small)']['status'], 'updated')
        self.assertEqual(rows['x (gone)']['status'], 'failed')
        self.assertIn('start it or its tunnel', rows['x (gone)']['detail'])


class RoundTest(unittest.TestCase):
    def test_due(self):
        cfg = config.normalise({})
        self.assertTrue(update.due(cfg, 100000, None))
        self.assertFalse(update.due(cfg, 100000, {'at': 100000 - 3600}))
        self.assertTrue(update.due(cfg, 100000, {'at': 100000 - 25 * 3600}))
        self.assertFalse(update.due(config.normalise({'update': {'auto': False}}), 100000, None))

    def test_the_dashboard_runs_one_round_at_a_time_and_shows_it(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = cfg_of({}, t)
            col = dashboard.Collector(cfg, probe_fn=None, available_fn=None, clock=lambda: 5000.0, reader=object(), stored_fn=lambda m: {})
            gate = threading.Event()

            def slow(c, clock, when_due):
                gate.wait(5)
                update.save(c, {'at': clock(), 'machines': {'here': {'items': [], 'error': None}}, 'models': [], 'owner': []})
            with mock.patch.object(update, 'run', slow):
                self.assertTrue(col.update_round())
                self.assertFalse(col.update_round(force=True))   # already running
                gate.set()
                for _ in range(100):
                    if not col.updating.locked():
                        break
                    threading.Event().wait(0.02)
            self.assertFalse(col.update_round())   # done a moment ago: not due
            v = update.view(cfg, update.load(cfg))
            self.assertEqual((v['at'], v['machines'][0]['name'], v['auto']), (5000.0, 'here', True))


class ConfigTest(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertEqual(update.view(config.normalise({}), None)['every_hours'], 24)
        self.assertFalse([e for e in config.validate(config.normalise({})) if e.startswith('update')])

    def test_bad_values_name_the_key_and_the_fix(self):
        errs = config.validate(config.normalise({'update': {'auto': 'yes', 'every_hours': 0, 'packages': ['ok', 'rm -rf /'],
                                                            'programs': 'agy'}}))
        joined = '\n'.join(errs)
        self.assertIn('update.auto is', joined)
        self.assertIn('update.every_hours is 0', joined)
        self.assertIn("update.packages names 'rm -rf /'", joined)
        self.assertIn('update.programs is', joined)


if __name__ == '__main__':
    unittest.main()


class ReviewFixesTest(unittest.TestCase):
    """The findings of the first review: locks, partial rounds, failed rounds, bad model servers, yield flags."""

    def test_a_tutti_run_holding_the_use_lock_makes_the_update_wait(self):
        with tempfile.TemporaryDirectory() as t:
            fake(t, 'lockedprog', '[ "$1" = update ] && touch "$(dirname "$0")/updated"; echo 1.0')
            env = {**os.environ, 'PATH': f'{t}:{os.environ["PATH"]}'}
            state = os.path.join(t, 'state')
            text = update.script({'lockedprog': {'version': '', 'update': 'lockedprog update'}}, [], locks=inuse.lock_dir(state))
            with inuse.using(state, 'lockedprog'):
                out = subprocess.run(['bash', '-c', text], capture_output=True, text=True, env=env, check=False)
            [row] = update.parse(out.stdout)
            self.assertEqual(row['status'], 'busy')
            self.assertFalse(os.path.exists(os.path.join(t, 'updated')))
            out = subprocess.run(['bash', '-c', text], capture_output=True, text=True, env=env, check=False)
            self.assertEqual(update.parse(out.stdout)[0]['status'], 'current')
            self.assertTrue(os.path.exists(os.path.join(t, 'updated')))

    def test_a_program_path_with_a_space_reads_its_version(self):
        with tempfile.TemporaryDirectory() as t:
            d = os.path.join(t, 'my tools')
            os.mkdir(d)
            prog = fake(d, 'agentx', 'echo agentx 3.2.1')
            out = subprocess.run(['bash', '-c', update.script({prog: {'version': '', 'update': ''}}, [], check=True)],
                                 capture_output=True, text=True, check=False)
            [row] = update.parse(out.stdout)
            self.assertEqual((row['status'], row['before']), ('manual', '3.2.1'))

    def test_one_round_at_a_time_per_config(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = cfg_of({}, t)
            with inuse.exclusive(update.path(cfg) + '.lock') as mine:
                self.assertTrue(mine)
                with self.assertRaises(update.Busy):
                    update.run(cfg)

    def test_a_partial_round_merges_and_keeps_the_full_rounds_time(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = cfg_of({'machines': {'a': {'host': 'local'}, 'b': {'host': 'local'}}}, t)

            def sh(version):
                return lambda m, text, capture, timeout: subprocess.CompletedProcess(
                    [], 0, f'{update.MARK}\tagy\tcurrent\t{version}\t{version}\t\n', '')
            update.run(cfg, sh=sh('1.0'), clock=lambda: 100.0)
            update.run(cfg, only='b', sh=sh('2.0'), clock=lambda: 200.0)
            last = update.load(cfg)
            self.assertEqual(last['at'], 100.0)
            self.assertEqual((last['machines']['a']['items'][0]['before'], last['machines']['b']['items'][0]['before']), ('1.0', '2.0'))

    def test_a_script_stopped_part_way_is_an_error(self):
        r = update.run_machine({'name': 'a'}, '', sh=lambda *a, **k: subprocess.CompletedProcess(
            [], 124, f'{update.MARK}\tagy\tcurrent\t1\t1\t\n', 'killed'))
        self.assertEqual(len(r['items']), 1)
        self.assertIn('stopped part way', r['error'])

    def test_a_failed_round_is_retried_in_an_hour_not_a_day(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = cfg_of({}, t)
            update.save(cfg, {'at': 0, 'machines': {}, 'models': [], 'owner': []})
            update.record_failure(cfg, 100000, 'boom')
            last = update.load(cfg)
            self.assertEqual((last['at'], last['error']), (0, 'boom'))
            self.assertFalse(update.due(cfg, 100000 + 60, last))
            self.assertTrue(update.due(cfg, 100000 + update.RETRY_SECONDS, last))

    def test_a_bad_model_server_and_a_lent_machine(self):
        cfg = config.normalise({'machines': {'gpu': {'host': 'gpu.example'}}, 'workers': {
            'odd': {'kind': 'ollama', 'host': '127.0.0.1:1', 'model': 'm'},
            'big': {'kind': 'ollama', 'host': '127.0.0.1:2', 'model': 'b', 'machine': 'gpu'}}})
        with mock.patch.object(update.local, 'yield_cached', lambda m: 'lent'):
            rows = {r['name']: r for r in update.pull_models(cfg, call=lambda h, p, *a: {'models': [None]})}
        self.assertEqual(rows['m (odd)']['status'], 'failed')
        self.assertEqual(rows['b (big)']['status'], 'busy')
        self.assertEqual([r['name'] for r in update.pull_models(cfg, check=True, call=lambda h, p, *a: {'models': []}, only='gpu')],
                         ['b (big)'])

    def test_a_loaded_model_is_not_pulled(self):
        cfg = config.normalise({'workers': {'s': {'kind': 'ollama', 'host': '127.0.0.1:1', 'model': 'm:9b'}}})
        calls = []

        def call(host, path, payload=None, timeout=30):
            calls.append(path)
            return {'models': [{'name': 'm:9b', 'digest': 'abc'}]}
        [row] = update.pull_models(cfg, call=call)
        self.assertEqual(row['status'], 'busy')
        self.assertNotIn('/api/pull', calls)


class WslTest(unittest.TestCase):
    def test_a_windows_copy_on_a_wsl_path_is_not_this_machines_program(self):
        with tempfile.TemporaryDirectory() as t:
            win = os.path.join(t, 'win')
            os.mkdir(win)
            fake(win, 'winprog', 'exit 127')
            env = {**os.environ, 'PATH': f'{win}:{os.environ["PATH"]}', 'TUTTI_WIN_PREFIX': win + '/'}
            text = update.script({'winprog': {'version': '', 'update': 'winprog update'}}, [])
            out = subprocess.run(['bash', '-c', text], capture_output=True, text=True, env=env, check=False)
            [row] = update.parse(out.stdout)
            self.assertEqual((row['status'], row['detail']), ('missing', 'only a Windows copy is on PATH here'))
