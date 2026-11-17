"""CLI workers on other machines (tutti/cliplace.py, workers.run_remote, delegate.run_away, remote.parse_ready) and a machine
in use giving its work to the others (share.presence_here). Fakes and temp folders only: no machine is called."""
import io
import os
import subprocess
from types import SimpleNamespace

from tests.test_autopilot import Base, account, git, task
from tutti import autopilot, cli, cliplace, config, delegate, parallel, remote, share, state, workers

ROWS_MAIN_IN_USE = [
    {'name': 'laptop', 'status': 'busy', 'free': 0, 'use_pct': 0, 'use_why': 'someone is at its keyboard', 'in_use': True,
     'cores': 16, 'load': 3.0, 'mem_free_gb': 20.0, 'reason': 'its share for Tutti is 0 %'},
    {'name': 'nuc', 'status': 'free', 'free': 2, 'use_pct': 100, 'use_why': '', 'in_use': False, 'cores': 16, 'load': 1.0,
     'mem_free_gb': 30.0, 'reason': 'fit'}]


class Placement(Base):
    def cfg(self, **ap):
        cfg = super().cfg(**ap)
        cfg['machines']['laptop'] = config.machine_of(cfg, 'laptop', {'transport': 'local', 'host': 'local'})
        cfg['workers']['agy']['machines'] = ['nuc', 'laptop']
        return cfg

    def test_a_run_goes_to_the_machine_nobody_uses_not_the_main_in_use(self):
        cfg = self.cfg()
        places = cliplace.rooms(cfg, ROWS_MAIN_IN_USE, {'mem_gb': 20.0, 'cores': 1, 'load': 3.0})
        self.assertEqual(places['laptop'][0], 0)
        self.assertIn('share for Tutti is 0 %', places['laptop'][1])
        self.assertEqual(places['nuc'][0], 43)   # (16 - 1) / 0.25 = 60 by cores, (30 - 4) / 0.6 = 43 by memory: the smaller
        self.assertEqual(cliplace.pick(cfg, 'agy', places, ROWS_MAIN_IN_USE), ('nuc', ''))
        self.assertEqual(cliplace.elsewhere(cfg, places), 43)

    def test_another_machine_comes_before_the_main_and_a_machine_in_use_last(self):
        cfg = self.cfg()
        rows = [{**ROWS_MAIN_IN_USE[0], 'status': 'free', 'use_pct': 100, 'in_use': False}, ROWS_MAIN_IN_USE[1]]
        places = {'laptop': (50, ''), 'nuc': (3, '')}
        self.assertEqual(cliplace.pick(cfg, 'agy', places, rows)[0], 'nuc')
        rows[1] = {**rows[1], 'in_use': True}
        self.assertEqual(cliplace.pick(cfg, 'agy', places, rows)[0], 'laptop')

    def test_no_machine_with_room_says_why_for_each(self):
        cfg = self.cfg()
        rows = [ROWS_MAIN_IN_USE[0], {**ROWS_MAIN_IN_USE[1], 'status': 'offline', 'reason': 'no answer'}]
        places = cliplace.rooms(cfg, rows)
        machine, why = cliplace.pick(cfg, 'agy', places, rows)
        self.assertIsNone(machine)
        self.assertIn('nuc: offline (no answer)', why)
        self.assertIn('laptop: its share for Tutti is 0 %', why)
        self.assertEqual(cliplace.rooms(cfg, [])['nuc'], (0, 'not probed yet'))

    def test_a_worker_without_machines_runs_here_and_the_feature_is_off_without_any(self):
        cfg = self.cfg()
        self.assertEqual(cliplace.machines_of(cfg, 'cursor'), [])
        self.assertEqual(cliplace.pick(cfg, 'cursor', {'laptop': (0, 'laptop: busy')}, [])[0], None)
        del cfg['workers']['agy']['machines']
        self.assertFalse(cliplace.on(cfg))
        cfg['workers']['agy']['machine'] = 'nuc'   # the one-machine form
        self.assertEqual(cliplace.machines_of(cfg, 'agy'), ['nuc'])

    def test_config_errors_name_the_key_and_the_fix(self):
        cfg = self.cfg()
        self.assertEqual(cliplace.errors(cfg), [])
        cfg['machines']['phone'] = {'transport': 'android'}
        cfg['workers']['agy']['machines'] = ['nuc', 'gone', 'phone']
        cfg['workers']['pc']['machines'] = ['nuc']
        cfg['workers']['cursor']['machines'] = 'nuc'
        errs = '\n'.join(cliplace.errors(cfg))
        self.assertIn('workers.agy.machines names "gone", which is not under machines:', errs)
        self.assertIn('a CLI worker runs only on a local, ssh or wsl machine', errs)
        self.assertIn('workers.pc.machines: only a cli worker', errs)
        self.assertIn('workers.cursor.machines is \'nuc\': a list', errs)

    def test_the_parallel_limit_counts_the_other_machines_room(self):
        cfg = self.cfg(max_parallel=None)
        here = {'mem_gb': 20.0, 'cores': 1, 'load': 3.0}   # the main in use: its cores at a share of 0 %
        self.assertEqual(parallel.limit(cfg, 0, 10, here, None)['bounds']['cpu'], 0)
        lim = parallel.limit(cfg, 0, 10, here, None, elsewhere=5)
        self.assertEqual((lim['bounds']['cpu'], lim['limit']), (5, 5))
        self.assertIn('5 more on the other machines', parallel.bounds(cfg, 0, 10, here, None, 5)['cpu'][1])

    def test_the_plan_places_the_run_on_the_free_machine_while_the_main_is_in_use(self):
        cfg = self.cfg()
        cfg['_host'] = {'mem_gb': 20.0, 'cores': 1, 'load': 3.0}
        states = {n: account(n) for n in ('agy-gemini', 'agy-claude')}
        p = autopilot.plan(cfg, [task('t1')], states, ROWS_MAIN_IN_USE, True)
        self.assertEqual(p['dispatch'], [('t1', 'agy-gemini')])
        self.assertEqual(p['place'], {'t1': 'nuc'})

    def test_the_plan_waits_with_the_reason_when_no_machine_has_room(self):
        cfg = self.cfg(ladder=['agy-gemini', 'agy-claude'])
        cfg['_host'] = {'mem_gb': 20.0, 'cores': 1, 'load': 3.0}
        rows = [ROWS_MAIN_IN_USE[0], {**ROWS_MAIN_IN_USE[1], 'load': 16.0}]
        states = {n: account(n) for n in ('agy-gemini', 'agy-claude')}
        p = autopilot.plan(cfg, [task('t1')], states, rows, True)
        self.assertEqual(p['dispatch'], [])
        self.assertIn('no machine a CLI worker runs on has room: laptop: its share for Tutti is 0 %', p['reason'])
        self.assertIn('nuc: load 16.0 of 16 cores at its share', p['reason'])

    def test_dispatch_names_the_machine_only_when_it_is_not_this_computer(self):
        cfg = self.cfg()
        os.makedirs(cfg['autopilot']['repos']['app']['path'])
        seen = []

        def spawn(argv, **_kw):
            seen.append(argv)
            return SimpleNamespace(pid=1)
        t1, t2 = task('t1'), task('t2')
        autopilot.dispatch(cfg, t1, 'agy-gemini', 1.0, spawn, machine='nuc')
        autopilot.dispatch(cfg, t2, 'agy-gemini', 1.0, spawn, machine='laptop')
        self.assertEqual(seen[0][-2:], ['--machine', 'nuc'])
        self.assertNotIn('--machine', seen[1])
        self.assertEqual((t1['machine'], t2['machine']), ('nuc', None))

    def test_run_refuses_a_machine_the_worker_is_not_on(self):
        cfg = self.cfg()
        a = SimpleNamespace(worker='cursor', machine='nuc', extra=None, bg=False)
        self.assertEqual(cli.run_delegated(cfg, a, []), 2)


class RemoteRun(Base):
    def cfg(self, **ap):
        cfg = super().cfg(**ap)
        cfg['scouts'] = {'grok': {'key_env': 'GROK_KEY'}}
        return cfg

    def test_the_remote_script_runs_the_same_brief_with_the_secrets_unset_there(self):
        cfg = self.cfg()
        line = workers.remote_script(cfg, 'agy', 'fix the link', ['README.md'], '/home/u/tutti/cli-ap-t1', ['--model', 'g'])
        self.assertTrue(line.startswith("cd /home/u/tutti/cli-ap-t1 && echo $$ > /home/u/tutti/cli-ap-t1.pid && exec env "))
        for k in ('GH_TOKEN', 'GITHUB_TOKEN', 'GROK_KEY'):
            self.assertIn(f'-u {k} ', line)
        self.assertIn('TUTTI_BACKGROUND=1', line)
        self.assertIn('fix the link', line)
        self.assertIn('--model g', line)

    def test_run_remote_passes_the_output_through(self):
        cfg = self.cfg()
        out = io.StringIO()
        fake = SimpleNamespace(stdout=iter(['working\n', 'done\n']), poll=lambda: 0, wait=lambda: 0, kill=lambda: None)
        rc, lines = workers.run_remote(cfg, 'agy', 'x', [], {'name': 'nuc'}, '/p', out, popen=lambda m, s: fake, stop=lambda: None)
        self.assertEqual((rc, lines), (0, ['working', 'done']))
        self.assertEqual(out.getvalue(), 'working\ndone\n')

    def test_a_dropped_connection_fails_the_attempt_and_stops_the_worker_there(self):
        cfg = self.cfg()
        stopped = []
        fake = SimpleNamespace(stdout=iter(['working\n']), poll=lambda: 255, wait=lambda: remote.DROPPED, kill=lambda: None)
        rc, lines = workers.run_remote(cfg, 'agy', 'x', [], {'name': 'nuc'}, '/p', io.StringIO(), popen=lambda m, s: fake,
                                       stop=lambda: stopped.append(1))
        self.assertEqual(rc, 255)
        self.assertIn('the connection to nuc failed or dropped', lines[-1])
        self.assertEqual(stopped, [1])

    def repo(self):
        repo = os.path.join(self.tmp.name, 'r')
        os.makedirs(repo)
        git(repo, 'init', '-q')
        git(repo, 'config', 'user.email', 't@x.example')
        git(repo, 'config', 'user.name', 't')
        with open(os.path.join(repo, 'a.txt'), 'w', encoding='utf8') as f:
            f.write('one\n')
        git(repo, 'add', '-A')
        git(repo, 'commit', '-qm', 'base')
        return repo

    def patch_of(self, repo, files):
        """The patch a remote copy would send back: HEAD plus `files`, made in a clone."""
        other = os.path.join(self.tmp.name, 'other')
        subprocess.run(['git', 'clone', '-q', repo, other], check=True)
        for name, text in files.items():
            with open(os.path.join(other, name), 'w', encoding='utf8') as f:
                f.write(text)
        git(other, 'add', '-A')
        return subprocess.run(['git', '-C', other, 'diff', '--cached', '--binary', 'HEAD'], capture_output=True, check=True).stdout

    def test_run_away_brings_the_change_back_into_the_worktree_here(self):
        cfg, wt = self.cfg(), self.repo()
        with open(os.path.join(wt, 'a.txt'), 'w', encoding='utf8') as f:
            f.write('earlier\n')   # the tree's own earlier change: the copy there had it, the patch holds it again
        patch = self.patch_of(wt, {'a.txt': 'earlier\nmore\n', 'b.txt': 'new\n'})
        log, wlog = io.StringIO(), os.path.join(self.tmp.name, 'worker.log')
        rc, _ = delegate.run_away(cfg, 'agy', 'x', [], wt, log, (), wlog, {'name': 'nuc', 'root': 'tutti'}, 'ap-t1',
                                  prepare=lambda *a: '/home/u/tutti/cli-ap-t1', run=lambda *a, **k: (0, ['ok']),
                                  diff=lambda m, p: patch, read=lambda m, p: 'agy log\n')
        self.assertEqual(rc, 0)
        with open(os.path.join(wt, 'a.txt'), encoding='utf8') as f:
            self.assertEqual(f.read(), 'earlier\nmore\n')
        self.assertTrue(os.path.exists(os.path.join(wt, 'b.txt')))
        with open(wlog, encoding='utf8') as f:
            self.assertEqual(f.read(), 'agy log\n')
        self.assertIn('on nuc: its change is in the worktree here', log.getvalue())

    def test_run_away_fails_cleanly_when_the_machine_cannot_be_reached(self):
        cfg, wt = self.cfg(), self.repo()

        def down(*_a):
            raise RuntimeError('copying the tree to nuc failed (exit 255)')
        log = io.StringIO()
        rc, lines = delegate.run_away(cfg, 'agy', 'x', [], wt, log, (), os.path.join(self.tmp.name, 'w.log'),
                                      {'name': 'nuc', 'root': 'tutti'}, 'ap-t1', prepare=down)
        self.assertEqual(rc, remote.DROPPED)
        self.assertIn('nuc could not take the run (copying the tree to nuc failed (exit 255))', lines[0])

        def lost(*_a):
            raise RuntimeError('reading the diff from nuc failed (exit 255): the connection dropped')
        rc, lines = delegate.run_away(cfg, 'agy', 'x', [], wt, io.StringIO(), (), os.path.join(self.tmp.name, 'w.log'),
                                      {'name': 'nuc', 'root': 'tutti'}, 'ap-t1', prepare=lambda *a: '/p',
                                      run=lambda *a, **k: (0, []), diff=lost, read=lambda m, p: '')
        self.assertEqual(rc, remote.DROPPED)
        self.assertIn('its change stays on nuc in /p', lines[-1])
        self.assertEqual(git(wt, 'status', '--porcelain'), '')   # nothing applied here

    def test_away_machine_is_none_for_this_computer(self):
        cfg = self.cfg()
        cfg['machines']['laptop'] = {'transport': 'local', 'name': 'laptop'}
        self.assertIsNone(delegate.away_machine(cfg, 'laptop'))
        self.assertIsNone(delegate.away_machine(cfg, None))
        self.assertEqual(delegate.away_machine(cfg, 'nuc')['name'], 'nuc')


class Readiness(Base):
    def test_ready_installed_and_signed_in(self):
        self.assertEqual(remote.parse_ready('nuc', 'agy', 0, 'VERSION 1.5.9\nLOGIN yes\n'), (True, 'agy 1.5.9 on nuc'))

    def test_missing_not_signed_in_and_unreachable_say_the_fix(self):
        self.assertIn('not on PATH on nuc', remote.parse_ready('nuc', 'agy', 0, 'MISSING\n')[1])
        ok, note = remote.parse_ready('nuc', 'agy', 0, 'VERSION 1.5.9\nLOGIN no ~/.gemini/oauth_creds.json\n')
        self.assertFalse(ok)
        self.assertIn('not signed in (~/.gemini/oauth_creds.json missing)', note)
        self.assertIn('does not answer over ssh', remote.parse_ready('nuc', 'agy', 255, '')[1])

    def test_workers_lists_each_machine_a_cli_runs_on(self):
        cfg = self.cfg()
        cfg['workers']['agy'].update(machines=['nuc'], login='~/.gemini/oauth_creds.json')
        asked = []

        def ready(machine, program, logins):
            asked.append((machine['name'], program, logins))
            return False, f'{program} is not on PATH on {machine["name"]}: install it there and sign in once'
        text = workers.listing(cfg, lambda w: (True, 'here'), ready)
        self.assertEqual(asked, [('nuc', 'agy', ['~/.gemini/oauth_creds.json'])])
        self.assertIn('on nuc', text)
        self.assertIn('DOWN   agy is not on PATH on nuc', text)


class Presence(Base):
    def test_a_vnc_viewer_or_an_interactive_agent_takes_the_machine_out(self):
        self.assertEqual(share.presence_here({}, agents_fn=lambda m: ['claude'], vnc_fn=lambda: 1),
                         ['1 VNC viewer(s) connected', 'an interactive agent works there (claude)'])
        pct, why = share.pct({'use_pct': 100}, None, None, None, None, ['an interactive agent works there (claude)'])
        self.assertEqual((pct, why), (0, 'an interactive agent works there (claude)'))
        m = {'name': 'laptop', 'transport': 'local', 'host': 'local', 'slots': 2, 'min_free_gb': 4}
        from tutti import routing
        row = state.machine_state(m, routing.Probe(True, 2, 0.1, 16), 1, present=['1 VNC viewer(s) connected'])
        self.assertEqual((row['in_use'], row['use_pct'], row['free']), (True, 0, 0))

    def test_nobody_there_keeps_the_share(self):
        self.assertEqual(share.presence_here({}, agents_fn=lambda m: [], vnc_fn=lambda: 0), [])
        m = {'name': 'laptop', 'transport': 'local', 'host': 'local', 'slots': 2, 'min_free_gb': 4}
        from tutti import routing
        row = state.machine_state(m, routing.Probe(True, 2, 0.1, 16), 1, idle_s=3600)
        self.assertEqual((row['in_use'], row['use_pct']), (False, 100))

    def proc(self, pid, comm, tty, env):
        d = os.path.join(self.tmp.name, 'proc', str(pid))
        os.makedirs(d)
        with open(os.path.join(d, 'stat'), 'w', encoding='utf8') as f:
            f.write(f'{pid} ({comm}) S 1 {pid} {pid} {tty} 0 0\n')
        with open(os.path.join(d, 'environ'), 'wb') as f:
            f.write(b'\0'.join(env))
        return os.path.dirname(d)

    def test_an_agent_in_a_terminal_counts_tuttis_own_and_detached_ones_do_not(self):
        self.proc(10, 'claude', 34816, [b'HOME=/h'])
        self.proc(11, 'agy', 34817, [b'TUTTI_BACKGROUND=1'])   # a run Tutti started
        self.proc(12, 'cursor-agent', 0, [b'HOME=/h'])          # no terminal: a headless run
        root = self.proc(13, 'bash', 34818, [b'HOME=/h'])
        self.assertEqual(share.interactive_agents({}, root), ['claude'])
        self.assertEqual(share.interactive_agents({'agent_programs': ['codex']}, root), [])
        self.assertEqual(share.interactive_agents({}, os.path.join(self.tmp.name, 'missing')), [])
