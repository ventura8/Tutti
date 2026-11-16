"""Self-healing (tutti/selfheal.py): each check repairs what it finds and logs it; a person's step goes to the needs queue
once; a broken check never stops the others."""
import json
import os
import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import tmpdirs
import yaml

from tutti import config, failovercmd, procs, selfheal, settings


def done(rc=0, out=''):
    return subprocess.CompletedProcess([], rc, out, '')


class Fake:
    def __init__(self, answers=None):
        self.scripts, self.answers = [], answers or {}

    def __call__(self, script, timeout=600):
        self.scripts.append(script)
        for key, r in self.answers.items():
            if key in script:
                return r
        return done()


def make(extra=None):
    d = tmpdirs.mkdtemp()
    raw = {'project': {'state': os.path.join(d, 'state'), 'worktrees': d}, **(extra or {})}
    path = os.path.join(d, 'tutti.config.yaml')
    with open(path, 'w', encoding='utf8') as f:
        yaml.safe_dump(raw, f)
    return config.load(path)


def ctx(cfg, run=None, http=None, which=lambda p: f'/bin/{p}'):
    asked = []
    c = selfheal.Ctx(cfg, run=run or Fake(), http=http or (lambda u: {'models': []}), now=time.time(),
                     ask=lambda cfg, key, q, why: asked.append((key, q)), which=which)
    return c, asked


class NeedsCheck(unittest.TestCase):
    def test_installed_needs_are_repairs_root_ones_one_question(self):
        cfg = make({'workers': {'w': {'kind': 'cli', 'command': ['x'], 'needs': [{'apt': 'adb'}, {'ollama': 'm'}]}}})
        run = Fake({'missing=0': done(1, 'TUTTI-DEP-INSTALLED: ollama m\nTUTTI-DEP-ROOT: sudo apt-get install -y adb\n')})
        c, asked = ctx(cfg, run)
        self.assertEqual(selfheal.check_needs(c), ['installed ollama m'])
        self.assertEqual(len(asked), 1)
        self.assertIn('sudo apt-get install -y adb', asked[0][1])

    def test_nothing_declared_runs_nothing(self):
        run = Fake()
        self.assertEqual(selfheal.check_needs(ctx(make(), run)[0]), [])
        self.assertEqual(run.scripts, [])


class SuiteNeeds(unittest.TestCase):
    def test_a_suites_root_need_is_asked_on_its_machine_another_machines_worker_is_left(self):
        cfg = make({'machines': {'pc': {'transport': 'local'}, 'gpu': {'transport': 'local'}},
                    'projects': {'app': {'tests': {'e2e': {'run': 'x', 'needs': [{'apt': 'adb'}]}}}},
                    'workers': {'far': {'kind': 'cli', 'command': ['far'], 'machine': 'gpu', 'needs': [{'ollama': 'm'}]}}})
        cfg['failover'] = {**(cfg.get('failover') or {}), 'me': 'pc', 'main': 'pc'}
        run = Fake({'missing=0': done(1, 'TUTTI-DEP-ROOT: sudo apt-get install -y adb\n')})
        c, asked = ctx(cfg, run)
        selfheal.check_needs(c)
        self.assertIn('adb', run.scripts[0])
        self.assertNotIn('ollama', run.scripts[0])
        self.assertEqual(len(asked), 1)


class WorkersCheck(unittest.TestCase):
    def test_a_signed_out_worker_asks_once_with_its_login_command(self):
        cfg = make({'workers': {'w': {'kind': 'cli', 'command': ['agent', '{prompt}'], 'login_check': ['agent', 'status'],
                                      'login': 'agent login'}}})
        c, asked = ctx(cfg, Fake({'agent status': done(1)}))
        selfheal.check_workers(c)
        self.assertIn('agent login', asked[0][1])

    def test_a_signed_in_worker_asks_nothing_a_missing_one_does(self):
        cfg = make({'workers': {'w': {'kind': 'cli', 'command': ['agent'], 'login_check': ['agent', 'status']}}})
        c, asked = ctx(cfg)
        selfheal.check_workers(c)
        self.assertEqual(asked, [])
        c, asked = ctx(cfg, which=lambda p: None)
        selfheal.check_workers(c)
        self.assertIn('Install agent', asked[0][1])


class OllamaCheck(unittest.TestCase):
    cfg_raw = {'workers': {'m': {'kind': 'ollama', 'host': '127.0.0.1:11434', 'model': 'qwen3:8b'}}}

    def test_a_down_server_is_started(self):
        def down(url):
            raise OSError('refused')
        run = Fake()
        c, _ = ctx(make(self.cfg_raw), run, http=down)
        self.assertIn('started it', selfheal.check_ollama(c)[0])
        self.assertIn('ollama serve', run.scripts[0])

    def test_a_missing_model_is_pulled_a_present_one_left(self):
        run = Fake()
        c, _ = ctx(make(self.cfg_raw), run)
        self.assertIn('pulled', selfheal.check_ollama(c)[0])
        c, _ = ctx(make(self.cfg_raw), run, http=lambda u: {'models': [{'name': 'qwen3:8b'}]})
        self.assertEqual(selfheal.check_ollama(c), [])


class TunnelAdbCheck(unittest.TestCase):
    def test_a_down_tunnel_is_started_an_up_one_left(self):
        cfg = make({'workers': {'big': {'kind': 'ollama', 'host': '127.0.0.1:11436', 'model': 'm',
                                        'tunnel': {'ssh': 'gpu.example', 'remote': '127.0.0.1:11434'}}}})
        run = Fake({'tunnel status': done(0, 'loop not running')})
        self.assertIn('started it', selfheal.check_tunnels(ctx(cfg, run)[0])[0])
        self.assertIn('tunnel start big', run.scripts[-1])
        self.assertEqual(selfheal.check_tunnels(ctx(cfg, Fake({'tunnel status': done(0, 'loop 12 running')}))[0]), [])

    def test_adb_restarted_and_a_dropped_wireless_device_reconnected(self):
        cfg = make({'machines': {'pc': {'transport': 'local'}}, 'devices': {'tab': {'serial': 'S1', 'machine': 'pc',
                                                                                  'wireless': ['192.0.2.5:5555']}}})
        cfg['failover'] = {**(cfg.get('failover') or {}), 'me': 'pc'}
        run = Fake({'adb devices': done(1, '')})
        lines = selfheal.check_adb(ctx(cfg, run)[0])
        self.assertIn('adb server', lines[0])
        self.assertIn('reconnected', lines[1])
        ok = Fake({'adb devices': done(0, 'List of devices attached\n192.0.2.5:5555\tdevice\n')})
        self.assertEqual(selfheal.check_adb(ctx(cfg, ok)[0]), [])


class DiskCheck(unittest.TestCase):
    def test_janitor_removes_only_old_tutti_folders(self):
        root = tmpdirs.mkdtemp()
        old = time.time() - 3 * 3600
        for name, inside in (('tmpabcdefgh', ['state', 'tutti.config.yaml']), ('tutti-tests-x', ['a']),
                             ('tmpzzzzzzzz', ['state', 'photos']), ('mine', ['state'])):
            os.makedirs(os.path.join(root, name))
            for e in inside:
                open(os.path.join(root, name, e), 'w').close()
                os.utime(os.path.join(root, name, e), (old, old))
            os.utime(os.path.join(root, name), (old, old))
        os.makedirs(os.path.join(root, 'tutti-t-new'))   # fresh: a test running now
        busy = os.path.join(root, 'tutti-tests-busy')     # an old folder, but a run writes deep inside it now
        os.makedirs(os.path.join(busy, 'wt', 'sub'))
        open(os.path.join(busy, 'wt', 'sub', 'f'), 'w').close()
        for x in (os.path.join(busy, 'wt'), busy):
            os.utime(x, (old, old))
        self.assertEqual(selfheal.janitor([root], time.time()), 2)
        self.assertEqual(sorted(os.listdir(root)), ['mine', 'tmpzzzzzzzz', 'tutti-t-new', 'tutti-tests-busy'])

    def test_low_disk_after_cleaning_asks_the_admin(self):
        c, asked = ctx(make())
        c.cfg['selfheal'] = {'temp_dirs': []}
        def low(path):
            return SimpleNamespace(f_files=1000, f_favail=10, f_blocks=1000, f_bavail=500, f_frsize=4096)
        selfheal.check_disk(c, low)
        self.assertTrue(asked and 'Make room' in asked[0][1])


class StateConfigCheck(unittest.TestCase):
    def test_a_corrupt_state_file_is_put_back_from_its_snapshot(self):
        cfg = make()
        folder = config.state_dir(cfg)
        os.makedirs(folder, exist_ok=True)
        p = os.path.join(folder, 'backlog.json')
        with open(p, 'w') as f:
            json.dump({'tasks': []}, f)
        c, asked = ctx(cfg)
        self.assertEqual(selfheal.check_state(c), [])   # snapshot taken
        with open(p, 'w') as f:
            f.write('{broken')
        self.assertIn('put back', selfheal.check_state(c)[0])
        self.assertEqual(json.load(open(p)), {'tasks': []})

    def test_a_broken_config_falls_back_to_the_last_good_backup(self):
        cfg = make()
        backups = os.path.join(config.state_dir(cfg), settings.BACKUPS)
        os.makedirs(backups)
        good = open(cfg['_path']).read()
        with open(os.path.join(backups, 'tutti.config.yaml.20261115-100000'), 'w') as f:
            f.write(good)
        with open(cfg['_path'], 'w') as f:
            f.write('dashboard: [not: a mapping\n')
        c = ctx(cfg)[0]
        self.assertEqual(selfheal.check_config(c), [])   # first seen: maybe a save in progress
        c.now += selfheal.CONFIG_GRACE + 1
        line = selfheal.check_config(c)[0]
        self.assertIn('put back', line)
        self.assertEqual(open(cfg['_path']).read(), good)
        self.assertTrue(os.path.exists(cfg['_path'] + '.bad'))

    def test_a_config_that_parses_but_fails_a_check_is_an_edit_never_replaced(self):
        cfg = make()
        with open(cfg['_path'], 'a') as f:
            f.write('dispatch: 3\n')
        c = ctx(cfg)[0]
        selfheal.check_config(c)
        c.now += 10 * selfheal.CONFIG_GRACE
        self.assertEqual(selfheal.check_config(c), [])
        self.assertIn('dispatch: 3', open(cfg['_path']).read())

    def test_a_good_config_is_left_and_no_good_backup_changes_nothing(self):
        cfg = make()
        self.assertEqual(selfheal.check_config(ctx(cfg)[0]), [])
        with open(cfg['_path'], 'w') as f:
            f.write('dashboard: [x\n')
        c = ctx(cfg)[0]
        selfheal.check_config(c)
        c.now += selfheal.CONFIG_GRACE + 1
        self.assertEqual(selfheal.check_config(c), [])
        os.remove(cfg['_path'] + '.unreadable-since')
        self.assertEqual(open(cfg['_path']).read(), 'dashboard: [x\n')


class UpdateCheck(unittest.TestCase):
    def test_a_newer_release_is_installed_once_per_period(self):
        c, _ = ctx(make())
        calls = []
        def upd(home):
            calls.append(home)
            return 0, '0.14.1 -> 0.15.0'
        self.assertIn('->', selfheal.check_update(c, upd, release=True)[0])
        self.assertEqual(selfheal.check_update(c, upd, release=True), [])   # asked again only after UPDATE_EVERY
        self.assertEqual(len(calls), 1)

    def test_a_git_checkout_is_not_self_updated(self):
        c, _ = ctx(make())
        self.assertEqual(selfheal.check_update(c, lambda h: (0, 'x -> y'), release=False), [])


class RoundTest(unittest.TestCase):
    def test_repairs_are_logged_and_counted_today_a_broken_check_does_not_stop_others(self):
        cfg = make()
        c, _ = ctx(cfg)

        def broken(ctx_):
            raise RuntimeError('boom')
        lines = selfheal.round_(c, (('broken', broken), ('disk', lambda x: ['removed 3 left-over Tutti temp folder(s)'])))
        self.assertIn('the check failed', lines[0])
        v = selfheal.view(cfg)
        self.assertEqual(v['today'], 1)
        self.assertEqual(v['items'][0]['check'], 'disk')

    def test_the_failover_loop_heals_on_its_period_and_logs_its_own_repairs(self):
        cfg = make()
        ran, now = [], time.time()
        def inline(fn):
            fn()
        last = failovercmd.heal(cfg, ['backlog.json was corrupt: put back from its last good snapshot'], 0, now,
                                lambda c: ran.append(1) or [], start=inline)
        self.assertEqual((last, ran), (now, [1]))
        self.assertEqual(failovercmd.heal(cfg, [], last, now + 10, lambda c: ran.append(1) or []), now)   # not due yet
        self.assertEqual(selfheal.view(cfg, now=time.time())['items'][0]['check'], 'state')

    def test_a_stray_is_reaped_within_one_round_of_the_service(self):
        cfg = make()
        self.assertIn('strays', dict(selfheal.LOOP_CHECKS))
        stopped = 'stopped keep-awake 4242: its test run is gone'
        with mock.patch.object(procs, 'daemons_allowed', return_value=True), \
                mock.patch.object(procs, 'reap', lambda log: [stopped]), \
                mock.patch.object(selfheal, 'LOOP_CHECKS', (('strays', selfheal.check_strays),)):
            failovercmd.heal(cfg, [], 0.0, time.time(), start=lambda fn: fn())
        v = selfheal.view(cfg, now=time.time())
        self.assertEqual((v['today'], v['items'][0]['check']), (1, 'strays'))

    def test_the_service_loop_ends_when_its_code_changes_so_systemd_starts_the_new_code(self):
        stamps, slept = iter([1.0, 1.0, 2.0]), []
        with mock.patch.object(failovercmd, 'one_round', return_value=[]) as rounds, \
                mock.patch.object(failovercmd, 'heal', return_value=0.0):
            self.assertEqual(failovercmd.loop(make(), sleep=slept.append, stamp=lambda: next(stamps), restart=lambda: 'restarted'),
                             'restarted')
        self.assertEqual((len(slept), rounds.call_count), (1, 1))   # no round on the old code after the change
        d = tmpdirs.mkdtemp()
        os.makedirs(os.path.join(d, 'web'))
        with open(os.path.join(d, 'web', 'app.js'), 'w', encoding='utf8') as f:
            f.write('x')
        os.utime(os.path.join(d, 'web', 'app.js'), (5.0, 5.0))
        self.assertEqual(failovercmd.code_stamp(d), 5.0)   # a subfolder's file counts
        self.assertEqual(failovercmd.code_stamp('/nonexistent-folder'), 0.0)
        with mock.patch.object(failovercmd.os, 'execv') as execv:
            failovercmd.reexec()
        self.assertEqual(execv.call_args.args[0], failovercmd.sys.executable)

    def test_config_errors(self):
        self.assertEqual(selfheal.errors({'selfheal': {'every_seconds': 600}}), [])
        self.assertEqual(len(selfheal.errors({'selfheal': {'every_seconds': 5, 'temp_dirs': 'x'}})), 2)


if __name__ == '__main__':
    unittest.main()
