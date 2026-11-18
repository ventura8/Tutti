"""`tutti move` (docs/MOVE.md) against a fake ssh target and temp folders: the preflight's blockers, the config from the
target's point of view, secrets with mode 600 and never in the log, the cut-over's order, the rollback on a failed health
check, members repointed, an interrupted move that resumes, the bridge and the client's forwarding. No real machine."""
import io
import json
import os
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import tmpdirs
import yaml

from tutti import bridge, clientmode, config, move, movetools

TEXT = """# the owner's comment stays
project:
  state: HOME/state
machines:
  nuc:                     # the build machine
    host: nuc
    slots: 3
  pc:
    host: pc
  laptop:
    host: local
workers:
  agy: {kind: cli, command: [agy, -p, '{prompt}']}
accounts:
  agy-claude:
    window: 5h
    reader: {command: [agy, -p, /usage], bucket: 3p-5h}
  claude:
    window: 7d
    reading_file: HOME/claude-usage.json
household: {bind: 192.168.1.33}
autopilot:
  repos:
    map: {path: HOME/repo}
"""
SECRET = 'TUTTI_GROK_KEY=s3cr3t-value-never-logged'
IFACES = [('lo', '', ['127.0.0.1']), ('enp86s0', '', ['192.168.1.53']), ('docker0', 'bridge', ['172.17.0.1'])]


CARRY = {'tools': [{'name': 'claude', 'copy': ['.local/share/claude/versions/2.1.296'], 'link': '.local/share/claude/versions/2.1.296',
                    'node': False, 'version': '2.1.296 (Claude Code)', 'there': ''}],
         'node': None, 'logins': ['claude', 'gh'], 'repos': {'paths': ['Projects/map'], 'repos': {}}, 'repos_gb': 1.5}


def ok(out=''):
    return subprocess.CompletedProcess([], 0, out, '')


def facts(**over):
    f = {'os': 'Linux', 'arch': 'x86_64', 'home': '/home/target', 'user': 'target', 'hostname': 'Asus-NUC', 'free_gb': '400',
         'tutti': '/home/target/.local/bin/tutti', 'tutti_version': '9.9.9', 'venv': 'yes', 'rsync': '/usr/bin/rsync',
         'systemd_user': 'running', 'linger': 'yes', 'suspend': 'masked', 'idle_action': '', 'uptime_s': '7200',
         'avahi': '/usr/bin/avahi-publish', 'has_git': '/usr/bin/git', 'reach_pc': 'no', 'ifaces': IFACES}
    f.update(over)
    return f


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdirs.mkdtemp()
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(os.path.join(self.home, '.config', 'tutti'))
        self.env = mock.patch.dict(os.environ, {'HOME': self.home, 'XDG_CONFIG_HOME': os.path.join(self.home, '.config'),
                                                'XDG_STATE_HOME': os.path.join(self.home, '.local', 'state'), 'USER': 'me'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path = os.path.join(self.home, '.config', 'tutti', 'config.yaml')
        with open(self.path, 'w', encoding='utf8') as f:
            f.write(TEXT.replace('HOME', self.home))
        with open(os.path.join(self.home, '.config', 'tutti', 'grok.env'), 'w', encoding='utf8') as f:
            f.write(SECRET)
        self.cfg = config.load(self.path)
        os.makedirs(os.path.join(self.cfg['project']['state'], 'runs'), exist_ok=True)
        with open(os.path.join(self.cfg['project']['state'], 'backlog.json'), 'w', encoding='utf8') as f:
            json.dump({'tasks': []}, f)
        nets = mock.patch.object(move.netpolicy, 'interfaces', return_value=[('wlo1', '', ['192.168.1.33'])])
        nets.start()
        self.addCleanup(nets.stop)
        carry = mock.patch.object(move, 'carried', return_value=CARRY)
        carry.start()
        self.carry_stop = carry.stop
        self.addCleanup(carry.stop)


class FakeOps:
    """The target as a script: answers by what the script does; every call is recorded in order."""

    def __init__(self, facts_out='', health=True, fail=None):
        self.calls, self.health_ok, self.fail, self.secrets = [], health, fail or set(), {}
        self.facts_out = facts_out

    def target(self, script, stdin=None, timeout=600):
        self.calls.append(('target', script))
        if 'uname -s' in script:
            return ok(self.facts_out)
        if 'auth status' in script:
            return ok('claude yes\ngh no\n')
        if 'enable --now' in script and 'start' in self.fail:
            return subprocess.CompletedProcess([], 1, '', 'unit failed')
        return ok()

    def local(self, argv, **_kw):
        self.calls.append(('local', ' '.join(argv)))
        return ok()

    def rsync(self, src, dst, exclude=(), delete=False, back=False):
        self.calls.append(('rsync', f'{src} -> {dst}' + (' delete' if delete else '') + (' back' if back else '')))
        if 'copy' in self.fail and not delete:
            self.fail.discard('copy')
            return subprocess.CompletedProcess([], 23, '', 'rsync: connection lost')
        return ok()

    def rsync_home(self, rel, exclude=(), delete=False, chmod=None):
        self.calls.append(('rsync', f'home {" ".join(rel)}' + (' delete' if delete else '') + (f' chmod {chmod}' if chmod else '')))
        return ok()

    def pipe(self, argv, script):
        self.calls.append(('pipe', f'{" ".join(argv)} | {script}'))
        return ok()

    def put_secret(self, path, text):
        self.calls.append(('secret', path))
        self.secrets[path] = text
        return ok()

    def health(self, port, tries=15, sleep=2):
        self.calls.append(('health', port))
        return {'ok': True, 'host': 'Asus-NUC', 'version': '9.9.9'} if self.health_ok else None


def facts_text(f):
    import base64
    lines = [f'{k}={v}' for k, v in f.items() if k != 'ifaces']
    rows = [{'ifname': n, 'linkinfo': {'info_kind': k} if k else None, 'addr_info': [{'local': a} for a in addrs]}
            for n, k, addrs in f['ifaces']]
    return '\n'.join(lines + ['ifaces=' + base64.b64encode(json.dumps(rows).encode()).decode()])


class PreflightTest(Base):
    def test_an_unreachable_target_is_blocked_with_the_ssh_fix(self):
        blockers, _w, _o = move.judge({'_error': 'ssh: connect to host nuc port 22: No route to host'}, 1)
        self.assertIn('No route to host', blockers[0])
        self.assertIn('BatchMode', blockers[0])

    def test_disk_os_rsync_and_systemd_are_checked(self):
        blockers, _w, _o = move.judge(facts(os='Darwin', free_gb='0.5', rsync='', systemd_user=''), 3)
        text = '\n'.join(blockers)
        for want in ('Darwin', '0.5 GB free', 'rsync is missing', 'systemd --user'):
            self.assertIn(want, text)

    def test_lingering_off_is_one_sudo_line_and_sleep_is_left_alone(self):
        _b, warnings, owner = move.judge(facts(linger='no', suspend='static'), 1)
        self.assertEqual([s['command'] for s in owner], ['sudo loginctl enable-linger target'])   # sleep is left alone (owner)
        self.assertEqual(warnings, [])

    def test_the_facts_are_parsed_from_the_script_output(self):
        f = move.parse_facts(facts_text(facts()))
        self.assertEqual((f['os'], f['home'], f['reach_pc']), ('Linux', '/home/target', 'no'))
        self.assertIn(('enp86s0', '', ['192.168.1.53']), f['ifaces'])

    def test_a_dry_run_prints_the_plan_and_changes_nothing(self):
        plan = move.make_plan(self.cfg, 'nuc', facts())
        text = move.describe(plan)
        for want in ('Move Tutti to nuc', 'preflight', 'client', 'tutti.local', 'OAuth origins: no change'):
            self.assertIn(want, text)
        self.assertFalse(os.path.exists(os.path.join(self.cfg['project']['state'], 'move', 'journal.json')))


    def test_the_work_browser_moving_in_isolated_mode_is_one_root_command(self):
        cmds = [s['command'] for s in move.make_plan(self.cfg, 'nuc', facts())['owner']]
        self.assertNotIn('tutti browser isolate --script | sudo bash', cmds)
        for b, listed in (({'mode': 'isolated'}, True), ({'mode': 'isolated', 'machine': 'nuc'}, True),
                          ({'mode': 'isolated', 'machine': 'pc'}, False), ({'mode': 'existing', 'machine': 'nuc'}, False)):
            self.cfg['browser'] = b
            cmds = [s['command'] for s in move.make_plan(self.cfg, 'nuc', facts())['owner']]
            self.assertEqual('tutti browser isolate --script | sudo bash' in cmds, listed, b)


class RewriteTest(Base):
    def plan(self):
        return move.make_plan(self.cfg, 'nuc', facts())

    def test_machine_names_are_rewritten_from_the_target_point_of_view(self):
        p = self.plan()
        host = yaml.safe_load(p['host_text'])
        self.assertEqual(host['machines']['nuc']['host'], 'local')
        self.assertEqual(host['machines']['laptop'], {'host': '192.168.1.33', 'user': 'me', 'via': 'laptop'})
        self.assertEqual(host['machines']['pc']['via'], 'laptop')   # the target cannot ssh there
        self.assertEqual(host['project']['state'], '/home/target/state')
        self.assertEqual(host['host'], {'machine': 'nuc', 'me': 'nuc', 'address': 'tutti.local', 'port': 4777})   # jobs run there
        self.assertIn('# the owner\'s comment stays', p['host_text'])
        self.assertIn('# the build machine', p['host_text'])

    def test_jobs_readers_and_every_home_path_are_the_target_own(self):
        host = yaml.safe_load(self.plan()['host_text'])
        self.assertNotIn('read_on', host['accounts']['agy-claude'])
        self.assertEqual(host['accounts']['claude']['reading_file'], '/home/target/claude-usage.json')
        self.assertEqual(host['autopilot']['repos']['map']['path'], '/home/target/repo')
        self.assertNotIn(self.home, self.plan()['host_text'])

    def test_no_public_or_wildcard_bind_is_written(self):
        host = yaml.safe_load(self.plan()['host_text'])
        self.assertEqual(host['household']['bind'], 'auto')   # netpolicy.binds: the target's own LAN and VPN addresses
        p = move.make_plan(self.cfg, 'nuc', facts(ifaces=[('lo', '', ['127.0.0.1']), ('eth0', '', ['198.51.100.20'])]))
        self.assertTrue(any('public' in b or 'never exposed' in b for b in p['blockers']), p['blockers'])

    def test_the_host_config_is_valid_and_the_client_block_points_at_it(self):
        p = self.plan()
        hcfg = config.normalise(yaml.safe_load(p['host_text']))
        self.assertEqual(config.validate(hcfg), [])
        self.assertEqual(p['client']['ssh'], 'nuc')
        self.assertEqual(p['client']['config'], '/home/target/.config/tutti/config.yaml')
        self.assertEqual(p['client']['me'], 'laptop')


class RunTest(Base):
    def run_move(self, ops, **kw):
        m = move.Move(self.cfg, 'nuc', ops=ops, **kw)
        m.sleep = lambda _s: None
        return m

    def patches(self, health_host='Asus-NUC'):
        ps = [mock.patch.object(move, 'stop_local', side_effect=lambda cfg, j: [4242]),
              mock.patch.object(move, 'start_local'),
              mock.patch.object(move, 'local_health', return_value={'ok': True, 'host': health_host})]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)
        return ps

    def test_cut_over_order_and_the_client_config(self):
        self.patches()
        ops = FakeOps(facts_text(facts()))
        j = self.run_move(ops).run()
        self.assertEqual(j['status'], 'done')
        order = [f'{c[0]}:{c[1]}' for c in ops.calls]
        def first(prefix):
            return next(i for i, c in enumerate(order) if c.startswith(prefix))
        final_sync = next(i for i, c in enumerate(ops.calls) if c[0] == 'rsync' and 'delete' in c[1])
        start = next(i for i, c in enumerate(ops.calls) if c[0] == 'target' and 'enable --now' in c[1])
        self.assertLess(first('rsync'), final_sync)
        self.assertLess(final_sync, start)
        self.assertLess(start, first('health'))
        self.assertLess(first('health'), next(i for i, c in enumerate(ops.calls) if c[0] == 'local' and 'tutti-client' in c[1]))
        self.assertIn("'freeze'", repr(j['done']))
        self.assertLess(j['done'].index('freeze'), j['done'].index('sync'))
        with open(self.path, encoding='utf8') as f:
            client = yaml.safe_load(f)
        self.assertEqual(client['host']['machine'], 'nuc')
        self.assertEqual(clientmode.role(config.load(self.path)), 'client')
        self.assertTrue(os.path.exists(self.path + '.bak-pre-move'))

    def test_secrets_go_with_mode_600_and_never_into_the_log(self):
        self.patches()
        ops = FakeOps(facts_text(facts()))
        j = self.run_move(ops).run()
        self.assertIn(('rsync', 'home .local/share/claude/versions/2.1.296'), ops.calls)   # the CLI at its version
        self.assertTrue(any(c[0] == 'pipe' and c[1].startswith('gh auth token |') for c in ops.calls))   # gh's own way
        self.assertEqual([s['command'] for s in j['plan']['logins']], ['ssh -t nuc gh auth login --web'])   # refused: its sign-in
        self.assertEqual(ops.secrets['~/.config/tutti/grok.env'], SECRET)
        journal = json.dumps(move.read_journal(self.cfg))
        self.assertNotIn('s3cr3t', journal)
        self.assertIn('grok.env', journal)

    def test_put_secret_writes_mode_600_on_the_target(self):
        tgt = {'name': 'here', 'host': 'local', 'transport': 'local'}
        ops = move.Ops({**self.cfg, 'machines': {'here': tgt}}, 'here')
        self.assertEqual(ops.put_secret('~/.config/tutti/x.env', SECRET).returncode, 0)
        path = os.path.join(self.home, '.config', 'tutti', 'x.env')
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_a_failed_health_check_rolls_back_by_itself(self):
        self.patches()
        ops = FakeOps(facts_text(facts()), health=False)
        m = self.run_move(ops)
        with self.assertRaisesRegex(move.MoveError, 'stopped at health'):
            m.run()
        j = move.read_journal(self.cfg)
        self.assertEqual(j['status'], 'rolled back')
        self.assertTrue(any(c[0] == 'target' and 'disable --now' in c[1] for c in ops.calls))
        move.start_local.assert_called()
        self.assertFalse(os.path.exists(os.path.join(self.cfg['project']['state'], 'move', 'frozen')))
        self.assertEqual(clientmode.role(config.load(self.path)), 'host')

    def test_an_interrupted_move_resumes_where_it_stopped(self):
        self.patches()
        ops = FakeOps(facts_text(facts()), fail={'copy'})
        m = self.run_move(ops)
        with self.assertRaisesRegex(move.MoveError, 'stopped at copy'):
            m.run()
        self.assertEqual(move.read_journal(self.cfg)['done'], ['preflight', 'install', 'tools', 'logins'])
        ops.calls.clear()
        j = self.run_move(ops).run()
        self.assertEqual(j['status'], 'done')
        self.assertFalse(any('uname -s' in c[1] for c in ops.calls if c[0] == 'target' and c is ops.calls[0]))
        self.assertIn('resuming the move to nuc after preflight, install, tools, logins', '\n'.join(j['log']))

    def test_rollback_after_a_move_restores_this_computer(self):
        self.patches()
        ops = FakeOps(facts_text(facts()))
        self.run_move(ops).run()
        move.rollback(self.cfg, ops)
        self.assertEqual(clientmode.role(config.load(self.path)), 'host')
        self.assertTrue(any(c[0] == 'rsync' and 'back' in c[1] for c in ops.calls))   # the newer state comes home


class MembersTest(Base):
    def test_this_login_household_membership_points_at_the_stable_name(self):
        path = os.path.join(self.home, '.config', 'tutti', 'household.json')
        with open(path, 'w', encoding='utf8') as f:
            json.dump({'coordinator': '192.168.1.33:4778', 'token': 't'}, f)
        self.assertTrue(move.repoint_household('tutti.local'))
        with open(path, encoding='utf8') as f:
            self.assertEqual(json.load(f)['coordinator'], 'tutti.local:4778')
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_a_rollback_restores_the_membership(self):
        path = os.path.join(self.home, '.config', 'tutti', 'household.json')
        with open(path, 'w', encoding='utf8') as f:
            json.dump({'coordinator': '192.168.1.33:4778'}, f)
        backups = {}
        move.repoint_household('tutti.local', backups)
        move.write_journal(self.cfg, {'to': 'nuc', 'backups': backups, 'done': []})
        move.rollback(self.cfg, FakeOps())
        with open(path, encoding='utf8') as f:
            self.assertEqual(json.load(f)['coordinator'], '192.168.1.33:4778')

    def test_no_membership_changes_nothing(self):
        self.assertFalse(move.repoint_household('tutti.local'))


class ClientModeTest(Base):
    def client_cfg(self):
        cfg = dict(self.cfg)
        cfg['host'] = {'machine': 'nuc', 'me': 'laptop', 'ssh': 'nuc', 'config': '/h/c.yaml', 'tutti': '/h/tutti', 'state': '/h/state'}
        return cfg

    def test_state_commands_go_to_the_host_and_local_ones_stay(self):
        cfg = self.client_cfg()
        self.assertTrue(clientmode.should_forward(cfg, 'backlog', env={}))
        self.assertFalse(clientmode.should_forward(cfg, 'connect', env={}))
        self.assertFalse(clientmode.should_forward(cfg, 'backlog', env={'TUTTI_LOCAL': '1'}))
        self.assertFalse(clientmode.should_forward(self.cfg, 'backlog', env={}))
        cmd = clientmode.ssh_command(cfg, ['backlog', 'list'])
        self.assertEqual(cmd[-2:], ['nuc', '/h/tutti --config /h/c.yaml backlog list'])

    def test_an_unreachable_host_reads_the_copy_and_says_so(self):
        cfg = self.client_cfg()

        def down(*_a, **_k):   # ssh exits 255 when it cannot connect
            return subprocess.CompletedProcess([], 255)
        self.assertIsNone(clientmode.forward(cfg, ['usage'], run=down, isatty=lambda: False))
        fb = clientmode.fallback_cfg(cfg)
        self.assertTrue(fb['project']['state'].endswith('host-mirror'))
        self.assertIn('read-only copy', fb['_readonly'])
        self.assertTrue(clientmode.is_read_only('backlog', 'list'))
        self.assertFalse(clientmode.is_read_only('backlog', 'add'))
        self.assertIn('cannot be reached', clientmode.refusal(cfg, 'pause'))


class BridgeTest(Base):
    def serve(self):
        """A host end and a client end joined by two pipes, as the client's ssh joins them."""
        r1, w1 = os.pipe()
        r2, w2 = os.pipe()
        to_client, from_client = os.fdopen(w1, 'w'), os.fdopen(r2)
        srv = bridge.Server(self.cfg, 'laptop', stdin=from_client, stdout=to_client, version='t')
        threading.Thread(target=srv.serve, daemon=True).start()
        client_in, client_out = os.fdopen(r1), os.fdopen(w2, 'w')
        lock = threading.Lock()

        def write(msg):
            with lock:
                client_out.write(json.dumps(msg) + '\n')
                client_out.flush()
        c = bridge.Client(self.cfg, write)

        def loop():
            for line in client_in:
                threading.Thread(target=c.handle, args=(json.loads(line),), daemon=True).start()
        threading.Thread(target=loop, daemon=True).start()
        for _ in range(100):
            if bridge.connected(self.cfg, 'laptop'):
                break
            time.sleep(0.02)
        return c

    def test_run_identity_propagates_to_client_and_rejects_bad_labels(self):
        identity, seen = 'a' * 32, []
        fake = SimpleNamespace(pid=123, stdout=[], wait=lambda: 0)
        client = bridge.Client(self.cfg, lambda x: None, popen=lambda *a, **k: seen.append(k['env']) or fake)
        with mock.patch.object(bridge, 'run_files', return_value={}):
            self.assertEqual(client.op_run({'id': '1700000000-123', 'run_identity': identity,
                                            'args': ['run', 'agy', 'fix']}, None)[0], 0)
        self.assertEqual(seen[0]['TUTTI_RUN_IDENTITY'], identity)
        self.assertEqual(client.op_run({'id': '1700000000-123', 'run_identity': 'bad;command',
                                        'args': ['run', 'agy', 'fix']}, None)[0], 2)
        with mock.patch.dict(os.environ, {'TUTTI_RUN_IDENTITY': identity}), \
                mock.patch.object(bridge, 'call', return_value=(0, {'files': {}})) as send:
            self.assertEqual(bridge.exec_run(self.cfg, 'client', '1700000000-123', ['run', 'agy', 'fix'], out=io.StringIO()), 0)
        self.assertEqual(send.call_args.args[3]['run_identity'], identity)

    def test_bound_capability_rejects_old_or_offline_client(self):
        cfg = dict(self.cfg)
        cfg['host'] = {'me': 'host', 'jobs': 'client'}
        with mock.patch.object(bridge, 'call', return_value=(0, {'capabilities': [bridge.BOUND_REPORT_CAP]})):
            self.assertEqual(bridge.bound_client_problem(cfg), '')
        with mock.patch.object(bridge, 'call', return_value=(0, {'version': 'old'})):
            self.assertIn('update and restart', bridge.bound_client_problem(cfg))
        with mock.patch.object(bridge, 'call', side_effect=bridge.BridgeDown('offline')):
            self.assertIn('offline', bridge.bound_client_problem(cfg))

    def test_old_client_rejects_bound_operation_without_starting_work(self):
        messages = []
        client = bridge.Client(self.cfg, messages.append)
        old_ops = tuple(op for op in bridge.OPS if op != 'run-bound')
        with mock.patch.object(bridge, 'OPS', old_ops), mock.patch.object(client, 'op_run') as start:
            client.handle({'id': 1, 'op': 'run-bound', 'args': {'run_identity': 'a' * 32}})
        start.assert_not_called()
        self.assertEqual(messages[-1]['rc'], 2)
        with mock.patch.object(client, 'op_run', return_value=(0, {})) as start:
            self.assertEqual(client.op_run_bound({'run_identity': 'bad'}, lambda x: None)[0], 2)
            self.assertEqual(client.op_run_bound({'run_identity': 'a' * 32}, lambda x: None)[0], 0)
            start.assert_called_once()

    def test_host_uses_bound_operation_and_explains_old_client_refusal(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'TUTTI_BOUND_REPORT': '1', 'TUTTI_RUN_IDENTITY': 'a' * 32}), \
                mock.patch.object(bridge, 'call', return_value=(2, {'problem': 'unknown op'})) as send:
            self.assertEqual(bridge.exec_run(self.cfg, 'client', '1700000000-123', ['run', 'agy', 'fix'], out=out), 2)
        self.assertEqual(send.call_args.args[2], 'run-bound')
        self.assertIn('update and restart tutti-client', out.getvalue())

    def test_a_reading_file_and_a_ping_cross_the_bridge(self):
        with open(os.path.join(self.home, 'claude-usage.json'), 'w', encoding='utf8') as f:
            f.write('{"claude": {"used": 7}}')
        self.serve()
        self.assertEqual(bridge.call(self.cfg, 'laptop', 'ping')[0], 0)
        text, seen = bridge.reading_file(self.cfg, 'laptop', 'claude')
        self.assertIn('"used": 7', text)
        self.assertTrue(seen)
        self.assertEqual([c['machine'] for c in bridge.clients(self.cfg)], ['laptop'])

    def test_the_client_jobs_reach_the_host_jobs_card(self):
        self.serve()
        with mock.patch('tutti.state.jobs_state', return_value={'items': [{'id': 'ship-1', 'status': 'running'},
                                                                           {'id': 'old', 'status': 'done'}]}):
            bridge._JOBS.clear()
            found = bridge.client_jobs(self.cfg)
        self.assertEqual([(j['id'], j['client']) for j in found], [('laptop:ship-1', 'laptop')])

    def test_the_client_runs_only_its_own_programs_in_its_own_folders(self):
        self.serve()
        rc, result = bridge.call(self.cfg, 'laptop', 'exec', {'argv': ['rm', '-rf', '/'], 'cwd': self.home})
        self.assertEqual(rc, 2)
        self.assertIn('refused', result['problem'])
        rc, result = bridge.call(self.cfg, 'laptop', 'exec', {'argv': ['git', '-C', '/etc', 'status']})
        self.assertIn('not one of this computer', result['problem'])
        rc, _ = bridge.call(self.cfg, 'laptop', 'run', {'args': ['run', 'agy', 'x', '--config', '/evil']})
        self.assertEqual(rc, 2)
        rc, result = bridge.call(self.cfg, 'laptop', 'exec', {'argv': ['gh', 'pr', 'list']})   # no folder: refused
        self.assertIn('no folder', result['problem'])
        rc, result = bridge.call(self.cfg, 'laptop', 'exec', {'argv': [os.path.join(self.home, 'repo', 'git'), 'status'],
                                                               'cwd': os.path.join(self.home, 'repo')})
        self.assertIn('by name', result['problem'])   # a path that only ends in git is never run

    def test_no_client_says_how_to_bring_it_back(self):
        with self.assertRaisesRegex(bridge.BridgeDown, 'systemctl --user start tutti-client'):
            bridge.call(self.cfg, 'nobody', 'ping')
        self.assertIsNone(bridge.read_account(self.cfg, 'nobody', 'claude')['used'])

    def test_a_host_with_jobs_elsewhere_runs_autopilot_through_the_bridge(self):
        cfg = dict(self.cfg)
        cfg['host'] = {'machine': 'nuc', 'me': 'nuc', 'jobs': 'laptop'}
        self.assertEqual(bridge.jobs_machine(cfg), 'laptop')
        self.assertEqual(set(bridge.tools(cfg)), {'run', 'gh', 'run_worker'})
        self.assertEqual(bridge.tools(self.cfg), {})


class FreezeTest(Base):
    def test_a_frozen_state_refuses_changes_and_allows_reads(self):
        os.makedirs(os.path.join(self.cfg['project']['state'], 'move'), exist_ok=True)
        open(os.path.join(self.cfg['project']['state'], 'move', 'frozen'), 'w').close()
        self.assertIn('moving', move.frozen_note(self.cfg, 'backlog', 'add'))
        self.assertIn('moving', move.frozen_note(self.cfg, 'pause'))
        self.assertEqual(move.frozen_note(self.cfg, 'backlog', 'list'), '')
        self.assertEqual(move.frozen_note(self.cfg, 'move'), '')

    def test_a_public_host_name_is_refused(self):
        cfg = dict(self.cfg)
        cfg['host'] = {'machine': 'nuc', 'me': 'nuc', 'address': 'tutti.example.com'}
        self.assertTrue(any('never a public name' in e for e in move.errors(cfg)))
        cfg['host'] = {'machine': 'nuc', 'me': 'nuc', 'address': 'tutti.local'}
        self.assertEqual(move.errors(cfg), [])


class ViewTest(Base):
    def test_where_tutti_runs_lists_the_targets_and_the_last_move(self):
        v = move.view(self.cfg, started=1.0)
        self.assertEqual((v['machine'], v['role']), ('laptop', 'host'))
        self.assertEqual(v['targets'], ['nuc', 'pc'])
        self.assertIn(socket.gethostname(), v['hostname'])


if __name__ == '__main__':
    unittest.main()


class CarryTest(unittest.TestCase):
    def setUp(self):
        self.home = tmpdirs.mkdtemp()

    def test_each_cli_is_carried_by_its_own_install_folder(self):
        def real(p):
            return {'/b/claude': f'{self.home}/.local/share/claude/versions/2.1.296',
                    '/b/gemini': f'{self.home}/.local/lib/node_modules/@google/gemini-cli/bundle/gemini.js',
                    '/b/agy': f'{self.home}/.local/bin/agy', '/b/node': '/usr/bin/node'}[p]
        which = {'claude': '/b/claude', 'gemini': '/b/gemini', 'agy': '/b/agy', 'node': '/b/node'}.get
        c = movetools.carry('claude', self.home, which, real)
        self.assertEqual((c['copy'], c['node']), (['.local/share/claude/versions/2.1.296'], False))
        g = movetools.carry('gemini', self.home, which, real)
        self.assertEqual((g['copy'], g['node']), (['.local/lib/node_modules/@google/gemini-cli'], True))
        self.assertIsNone(movetools.carry('agy', self.home, which, real)['link'])
        self.assertIsNone(movetools.carry('node', self.home, which, real))   # a package manager's: not carried
        self.assertIsNone(movetools.carry('codex', self.home, which, real))
        script = movetools.link_script([c, g])
        self.assertIn('ln -sfn "$HOME/".local/share/claude/versions/2.1.296 "$HOME/.local/bin/"claude', script)

    def test_the_same_version_there_is_not_copied_again(self):
        with mock.patch.object(movetools, 'carry', side_effect=lambda n, h, w=None: {'name': n, 'copy': [n], 'link': None, 'node': False}
                               if n == 'claude' else None):
            fake = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '2.1.296 (Claude Code)\n', ''))
            self.assertEqual(movetools.tools_plan(self.home, {'ver_claude': '2.1.296 (Claude Code)'}, runner=fake), [])
            self.assertEqual(len(movetools.tools_plan(self.home, {'ver_claude': '2.0.1'}, runner=fake)), 1)

    def test_node_is_checked_against_its_sums(self):
        s = movetools.node_script('v24.19.0', 'x86_64')
        self.assertIn('node-v24.19.0-linux-x64.tar.xz', s)
        self.assertIn('sha256sum -c', s)

    def test_login_files_are_found_by_name_only_and_checked_by_each_tool(self):
        os.makedirs(os.path.join(self.home, '.claude'))
        open(os.path.join(self.home, '.claude', '.credentials.json'), 'w').close()
        with mock.patch('builtins.open', side_effect=AssertionError('a login file was read')):
            self.assertEqual(movetools.login_paths(self.home, ['claude', 'gh']), ['.claude/.credentials.json'])
        script = movetools.verify_script(['claude', 'gh'])
        self.assertIn('claude auth status', script)
        self.assertNotIn('echo "$out"', script)   # a status line (an email) is matched, never printed
        self.assertEqual(movetools.parse_verify('claude yes\ngh no\n'), {'claude': True, 'gh': False})
        self.assertIn('[ -s "$HOME/".gemini/oauth_creds.json ]', movetools.verify_script(['gemini']))   # its login file, not --version

    def test_worktrees_are_repaired_with_their_new_paths(self):
        script = movetools.repair_script({'/home/a/Projects/map': ['/home/a/Projects/wt/x']}, '/home/a', '/home/b')
        self.assertIn("git -C /home/b/Projects/map worktree repair /home/b/Projects/wt/x || exit 1", script)

    def test_the_repo_set_keeps_only_top_folders_under_home(self):
        repo = os.path.join(self.home, 'Projects', 'map')
        wt = os.path.join(self.home, 'Projects', 'wt')
        for d in (repo, os.path.join(wt, 'x'), '/tmp'):
            os.makedirs(d, exist_ok=True)
        cfg = {'autopilot': {'repos': {'map': {'path': repo}}}, 'project': {'worktrees': wt}}
        fake = mock.Mock(return_value=subprocess.CompletedProcess([], 0, f'worktree {repo}\nworktree {wt}/x\nworktree /tmp\n', ''))
        got = movetools.repo_set(cfg, self.home, lambda p: p, runner=fake)
        self.assertEqual(got['paths'], ['Projects/map', 'Projects/wt'])


class ReadingsTest(Base):
    def test_a_changed_reading_file_goes_to_the_host_once(self):
        f = os.path.join(self.home, 'claude-usage.json')
        with open(f, 'w', encoding='utf8') as fh:
            fh.write('{"claude": {"used": 7}}')
        cfg = dict(self.cfg)
        cfg['host'] = {'machine': 'nuc', 'me': 'laptop', 'ssh': 'nuc', 'home': '/home/target'}
        calls = []

        def run(argv, **kw):
            calls.append((argv[-1], kw['input']))
            return subprocess.CompletedProcess(argv, 0, '', '')
        clientmode._PUSHED.clear()
        self.assertEqual(clientmode.push_readings(cfg, run=run), ['/home/target/claude-usage.json'])
        self.assertFalse(clientmode.push_readings(cfg, run=run))
        self.assertIn('/home/target/claude-usage.json', calls[0][0])


class SshAccessTest(unittest.TestCase):
    CONFIG = "Host *\n  ServerAliveInterval 15\nHost w5090\n  HostName 192.168.1.154\n  User ventu\n  IdentityFile ~/.ssh/id_pc\n" \
             "Host nuc other\n  HostName 192.168.1.53\n"

    def test_the_machine_entry_and_its_key_are_found_by_alias(self):
        b = movetools.ssh_blocks({'w5090', 'nuc'}, self.CONFIG)
        self.assertEqual(list(b), ['w5090'])   # a multi-alias block is never rewritten for another machine
        self.assertEqual((b['w5090']['hostname'], b['w5090']['identities']), ('192.168.1.154', ['~/.ssh/id_pc']))
        self.assertIn('User ventu', b['w5090']['block'])
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, '.ssh'))
            with open(os.path.join(home, '.ssh', 'id_pc'), 'w') as f:
                f.write('k')
            text = self.CONFIG.replace('~/.ssh/id_pc', f'{home}/.ssh/id_pc')
            got = movetools.ssh_carry(['w5090'], home, read=lambda _p: text)
            self.assertIn('IdentityFile ~/.ssh/id_pc', got['w5090']['block'])   # the target's own home, not this one

    def test_the_target_gets_the_entry_once_and_the_host_key(self):
        e = {'alias': 'w5090', 'block': 'Host w5090\n  HostName 192.168.1.154\n', 'hostname': '192.168.1.154', 'port': '22'}
        script = movetools.ssh_script({'w5090': e}, lambda _e: ['192.168.1.154 ssh-ed25519 AAAA'])
        self.assertIn("grep -qiE '^Host[[:space:]]+w5090", script)
        # audit 2026-11 (L): an alias from ~/.ssh/config with a quote or a backtick stays one word on the target
        evil = movetools.ssh_script({'x': {**e, 'alias': 'a`touch /tmp/pwned`"b'}}, lambda _e: [])
        import shlex as _shlex
        words = _shlex.split(evil)
        pattern = words[words.index('-qiE') + 1]
        self.assertTrue(pattern.startswith('^Host') and '`touch' in pattern and 'pwned`' in pattern)   # literal text, never run
        self.assertIn('known_hosts', script)
        self.assertIn('chmod 600', script)
        found = '# Host 192.168.1.154 found: line 3\n192.168.1.154 ssh-ed25519 AAAA\n'
        fake = mock.Mock(return_value=subprocess.CompletedProcess([], 0, found, ''))
        self.assertEqual(movetools.known_hosts(e, runner=fake), ['192.168.1.154 ssh-ed25519 AAAA'])


class ModelsTest(Base):
    def test_a_model_on_this_loopback_reaches_the_host_through_the_client_ssh(self):
        cfg = dict(self.cfg)
        cfg['workers'] = {**cfg['workers'], 'laptop': {'kind': 'ollama', 'host': '127.0.0.1:11434'},
                          'pc': {'kind': 'ollama', 'host': '127.0.0.1:11436', 'machine': 'pc', 'tunnel': {'ssh': 'pc'}}}
        self.assertEqual(movetools.reverse_ports(cfg), [('laptop', 11435, '127.0.0.1:11434')])
        cfg['host'] = {'machine': 'nuc', 'me': 'laptop', 'ssh': 'nuc', 'config': '/c', 'reverse': [[11435, '127.0.0.1:11434']]}
        argv = bridge.ssh_argv(cfg)
        self.assertIn('127.0.0.1:11435:127.0.0.1:11434', argv)   # the host's own loopback: never a LAN bind


class FinalSyncTest(Base):
    def test_the_cut_over_copies_only_git_folders_and_autopilot_worktrees(self):
        repo = os.path.join(self.home, 'repo')
        os.makedirs(os.path.join(repo, '.git'))
        cfg = dict(self.cfg)
        cfg['project'] = {**cfg['project'], 'worktrees': os.path.join(self.home, 'wt')}
        for n in ('tutti-run-ap-t1', 'feature'):
            os.makedirs(os.path.join(self.home, 'wt', n))
        got = move.final_paths(cfg, {'repos': {repo: []}}, self.home)
        self.assertEqual(got, ['repo/.git', 'wt/tutti-run-ap-t1'])   # a feature worktree was copied while Tutti ran


class TargetSshTest(Base):
    def test_the_target_is_never_given_ssh_access_to_itself(self):
        seen = []
        self.carry_stop()
        with mock.patch.object(movetools, 'ssh_carry', side_effect=lambda aliases, home: seen.extend(aliases) or {}), \
                mock.patch.object(movetools, 'repo_set', return_value={'paths': [], 'repos': {}}), \
                mock.patch.object(movetools, 'tools_plan', return_value=[]), mock.patch.object(movetools, 'login_tools', return_value=[]):
            plan = move.make_plan(self.cfg, 'nuc', facts(reach_pc='no'))
        self.assertEqual(seen, ['pc'])
        self.assertEqual(plan['unreachable'], ['pc'])   # no entry for pc here: it stays probed through this computer


class VanishedTest(unittest.TestCase):
    def test_a_file_that_vanished_during_rsync_is_not_a_failure(self):
        self.assertEqual(move.must(subprocess.CompletedProcess(['rsync'], 24, '', 'vanished'), 'x').returncode, 24)
        with self.assertRaisesRegex(move.MoveError, 'x failed'):
            move.must(subprocess.CompletedProcess(['rsync'], 23, '', 'partial'), 'x')
