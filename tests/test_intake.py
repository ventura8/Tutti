"""All household work through Tutti (docs/INTAKE.md): a session claims, notes, releases and completes tasks; leases a machine or
a device for a task; asks the owner on its task and gets it back; `tutti run claude` schedules Claude-only work under its pace;
the MCP tools map to those commands; the intake guard finds commits and leases no task explains; `tutti connect --repo` adds
the AGENTS.md section once. Nothing leaves the temp folder; no real machine, device or account is used."""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tutti import autopilot, backlog, claudework, cli, config, connect, intake, mcpserver, needs

T0 = 1_800_000_000.0


def git(cwd, *args, env=None):
    return subprocess.run(['git', '-C', cwd, *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, 'app')
        os.makedirs(self.repo)

    def cfg(self):
        raw = {'project': {'state': os.path.join(self.tmp.name, 'state')},
               'machines': {'nuc': {'host': 'nuc.example', 'slots': 1}},
               'workers': {'cursor': {'kind': 'cli', 'command': ['cursor', '{prompt}']},
                           'claude': {'kind': 'cli', 'command': ['claude', '-p', '{prompt}']}},
               'accounts': {'cursor': {'worker': 'cursor'}, 'claude': {'worker': 'claude'}},
               'autopilot': {'accounts': ['cursor'], 'ladder': ['cursor'], 'repos': {'app': {'path': self.repo, 'check': 'true'}}}}
        cfg = config.normalise(raw)
        self.assertEqual(config.validate(cfg), [])
        cfg['_path'] = os.path.join(self.tmp.name, 'tutti.config.yaml')
        return cfg

    def cli(self, cfg, *argv):
        with mock.patch.object(config, 'load', return_value=cfg), mock.patch('sys.stdout'), mock.patch('sys.stderr'):
            return cli.main(list(argv))

    def task(self, cfg, tid='t1'):
        return backlog.find(backlog.load(cfg), tid)


class Sessions(Base):
    def test_claim_note_release_and_done(self):
        cfg = self.cfg()
        self.assertEqual(self.cli(cfg, 'backlog', 'add', 'app', 'Fix the map legend'), 0)
        self.assertEqual(self.cli(cfg, 'backlog', 'claim', 't1', '--who', 'claude-map'), 0)
        t = self.task(cfg)
        self.assertEqual((t['status'], t['claimed_by']), ('claimed', 'claude-map'))
        self.assertEqual(autopilot.candidates(cfg, t), ['cursor'])   # still a task, but queued() leaves a claimed one alone
        self.assertEqual(backlog.queued(backlog.load(cfg)), [])
        self.assertEqual(self.cli(cfg, 'backlog', 'note', 't1', '--note', 'legend moved'), 0)
        self.assertIn('legend moved', self.task(cfg)['notes'])
        self.assertEqual(self.cli(cfg, 'backlog', 'release', 't1', '--who', 'claude-map'), 0)
        self.assertEqual(self.task(cfg)['status'], 'queued')
        self.assertEqual(self.cli(cfg, 'backlog', 'done', 't1'), 0)
        self.assertEqual(self.task(cfg)['status'], 'done')
        self.assertTrue(self.task(cfg)['closed_at'])

    def test_bad_claims_are_refused_with_the_fix(self):
        cfg = self.cfg()
        self.cli(cfg, 'backlog', 'add', 'app', 'Fix it')
        with mock.patch.dict(os.environ, {'TUTTI_SESSION': ''}):
            self.assertEqual(self.cli(cfg, 'backlog', 'claim', 't1'), 2)   # no --who
        self.cli(cfg, 'backlog', 'claim', 't1', '--who', 'a')
        self.assertEqual(self.cli(cfg, 'backlog', 'claim', 't1', '--who', 'b'), 2)   # someone else's
        self.assertEqual(self.cli(cfg, 'backlog', 'note', 't1'), 2)   # no --note
        self.cli(cfg, 'backlog', 'done', 't1')
        self.assertEqual(self.cli(cfg, 'backlog', 'claim', 't1', '--who', 'a'), 2)   # not open
        self.assertEqual(self.cli(cfg, 'backlog', 'release', 't1', '--who', 'a'), 2)   # not claimed

    def test_ask_owner_parks_the_task_and_the_answer_gives_it_back_to_the_session(self):
        cfg = self.cfg()
        self.cli(cfg, 'backlog', 'add', 'app', 'Pick a colour')
        self.cli(cfg, 'backlog', 'claim', 't1', '--who', 'claude-map')
        self.assertEqual(self.cli(cfg, 'needs', 'ask', 't1', 'Which colour?', '--option', 'red', '--option', 'blue'), 0)
        self.assertEqual(self.task(cfg)['status'], 'needs-person')
        ok, _ = needs.answer(cfg, 'q1', 'blue')
        self.assertTrue(ok)
        t = self.task(cfg)
        self.assertEqual(t['status'], 'claimed')   # back to the session, never to the autopilot
        self.assertTrue(any('blue' in n for n in t['notes']))

    def test_ask_owner_refuses_a_closed_task_and_a_missing_question(self):
        cfg = self.cfg()
        self.cli(cfg, 'backlog', 'add', 'app', 'x')
        self.assertEqual(self.cli(cfg, 'needs', 'ask', 't1'), 2)
        self.cli(cfg, 'backlog', 'done', 't1')
        self.assertEqual(self.cli(cfg, 'needs', 'ask', 't1', 'Why?'), 2)


class Leases(Base):
    def test_claim_release_and_another_sessions_lease(self):
        cfg = self.cfg()
        self.assertEqual(self.cli(cfg, 'lease', 'claim', 'machine', 'nuc', '--who', 'a', '--task', 't1'), 0)
        self.assertEqual(self.cli(cfg, 'lease', 'claim', 'machine', 'nuc', '--who', 'b'), 2)
        self.assertEqual(self.cli(cfg, 'lease', 'release', 'machine', 'nuc', '--who', 'b'), 2)
        self.assertEqual(self.cli(cfg, 'lease', 'release', 'machine', 'nuc', '--who', 'a'), 0)
        self.assertEqual(intake.read_leases(cfg), {})
        self.assertEqual(self.cli(cfg, 'lease', 'list'), 0)

    def test_unknown_names_kinds_and_minutes_are_refused(self):
        cfg = self.cfg()
        self.assertEqual(self.cli(cfg, 'lease', 'claim', 'machine', 'nope', '--who', 'a'), 2)
        self.assertEqual(self.cli(cfg, 'lease', 'claim', 'machine', 'nuc', '--who', 'a', '--minutes', '0'), 2)
        self.assertEqual(self.cli(cfg, 'lease', 'claim'), 2)
        with self.assertRaises(intake.IntakeError):
            intake.claim({}, 'robot', 'x', 'a', '', 5, T0)
        with self.assertRaises(intake.IntakeError):
            intake.claim({}, 'machine', 'x', '', '', 5, T0)

    def test_a_device_lease_also_claims_the_device(self):
        cfg = self.cfg()
        cfg['devices'] = {'phone': {'machine': 'nuc', 'serial': 's1'}}
        from tutti import devicescmd
        with mock.patch.object(devicescmd, 'cmd_claim', return_value=0) as claim:
            self.assertEqual(self.cli(cfg, 'lease', 'claim', 'device', 'phone', '--who', 'a', '--task', 't1'), 0)
        self.assertEqual(claim.call_args[0][1].names, ['phone'])
        with mock.patch.object(devicescmd, 'cmd_claim', return_value=3):
            self.assertEqual(self.cli(cfg, 'lease', 'release', 'device', 'phone', '--who', 'a'), 3)


class ClaudeWorker(Base):
    def test_started_when_the_account_has_room_and_queued_in_panic(self):
        cfg = self.cfg()
        started = []

        def dispatch(cfg_, task, step, now):
            started.append((task['id'], step))
            task.update(status='running', run='1-1')
            return '1-1'
        tid, run, why = claudework.start(cfg, 'app', 'Check the map by eye', state_fn=lambda c, n: {'name': n, 'verdict': 'behind'},
                                         dispatch_fn=dispatch)
        self.assertEqual((tid, run, started), ('t1', '1-1', [('t1', 'claude')]))
        t = self.task(cfg)
        self.assertEqual((t['source'], t['pin'], t['status']), ('claude', 'worker:claude', 'running'))
        tid, run, why = claudework.start(cfg, 'app', 'Another look', state_fn=lambda c, n: {'name': n, 'verdict': 'x', 'panic': True},
                                         dispatch_fn=dispatch)
        self.assertEqual((tid, run), ('t2', None))
        self.assertIn('panic', why)
        self.assertEqual(autopilot.candidates(cfg, self.task(cfg, 't2')), ['claude'])   # only the claude account runs it later

    def test_no_claude_account_or_unknown_repo_is_refused(self):
        cfg = self.cfg()
        with self.assertRaises(claudework.ClaudeError):
            claudework.start(cfg, 'nope', 'x')
        del cfg['accounts']['claude']
        with self.assertRaises(claudework.ClaudeError):
            claudework.start(cfg, 'app', 'x')

    def test_the_cli_routes_run_claude_with_a_repo_through_the_backlog(self):
        cfg = self.cfg()
        with mock.patch.object(claudework, 'start', return_value=('t1', None, 'queued')) as start:
            self.assertEqual(self.cli(cfg, 'run', 'claude', 'look at it', '--repo', self.repo), 0)
        self.assertEqual(start.call_args[0][1:3], ('app', 'look at it'))
        self.assertEqual(self.cli(cfg, 'run', 'claude', 'look', '--repo', '/elsewhere'), 2)


class Mcp(Base):
    def test_the_session_tools_map_to_the_commands(self):
        cfg, calls = self.cfg(), []

        def runner(path, args):
            calls.append(args)
            return 0, 'ok'
        for name, args in (('backlog_claim', {'id': 't1', 'who': 's'}), ('backlog_update', {'id': 't1', 'note': 'n'}),
                           ('backlog_release', {'id': 't1', 'who': 's'}), ('backlog_complete', {'id': 't1'}),
                           ('lease_claim', {'kind': 'machine', 'name': 'nuc', 'who': 's', 'task': 't1'}),
                           ('lease_release', {'kind': 'machine', 'name': 'nuc', 'who': 's'}), ('leases', {}),
                           ('usage_report', {'account': 'claude', 'percent': '42'}),
                           ('ask_owner', {'task': 't1', 'question': 'Which?', 'options': ['a', 'b']}),
                           ('claude_task', {'repo': 'app', 'task': 'look'}), ('outside_work', {})):
            err, _ = mcpserver.call(cfg, name, args, 'admin', runner=runner)
            self.assertFalse(err, name)
        self.assertEqual(calls[0], ['backlog', '--who', 's', 'claim', 't1'])
        self.assertEqual(calls[8], ['needs', '--option=a', '--option=b', '--', 'ask', 't1', 'Which?'])
        self.assertEqual(calls[9], ['run', '--repo', 'app', '--size', 'S', '--', 'claude', 'look'])
        for argv in calls:   # every mapped command parses
            cli.parser().parse_args(argv)

    def test_a_viewer_cannot_claim_and_an_option_like_id_is_refused(self):
        cfg = self.cfg()
        err, text = mcpserver.call(cfg, 'backlog_claim', {'id': 't1', 'who': 's'}, 'viewer')
        self.assertTrue(err)
        self.assertIn('admin', text)
        err, text = mcpserver.call(cfg, 'backlog_complete', {'id': '--all'}, 'admin')
        self.assertTrue(err)
        self.assertIn('may not start with', text)


class Guard(Base):
    def commit(self, msg, when, branch=None):
        env = {**os.environ, 'GIT_AUTHOR_DATE': f'@{int(when)} +0000', 'GIT_COMMITTER_DATE': f'@{int(when)} +0000',
               'GIT_AUTHOR_NAME': 'p', 'GIT_AUTHOR_EMAIL': 'p@x.example', 'GIT_COMMITTER_NAME': 'p', 'GIT_COMMITTER_EMAIL': 'p@x.example'}
        if branch:
            git(self.repo, 'checkout', '-q', '-B', branch, env=env)
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', msg, env=env)

    def test_commits_without_a_task_and_leases_without_one_are_outside(self):
        import time
        cfg = self.cfg()
        git(self.repo, 'init', '-q', '-b', 'main')
        now = time.time()
        self.cli(cfg, 'backlog', 'add', 'app', 'Fix it')
        self.commit('fix: the legend (t1)', now - 600)
        self.commit('chore: quick hack', now - 500)
        self.commit('fix: x', now - 400, branch='autopilot/t9-x')
        with intake.leases_locked(cfg) as data:
            intake.claim(data, 'machine', 'nuc', 'a', '', 30, now)
        r = intake.report(cfg, now)
        self.assertEqual([c['subject'] for c in r['commits']], ['chore: quick hack'])
        self.assertEqual([v['why'] for v in r['leases']], ['leased for no task'])
        self.assertIn('quick hack', intake.lines(r))
        self.assertEqual(self.cli(cfg, 'intake', '--strict'), 1)

    def test_a_claimed_window_links_a_commit_and_an_unreadable_repo_is_named(self):
        tasks = [{'id': 't1', 'repo': 'app', 'status': 'claimed', 'claimed_at': T0}]
        c = {'sha': 'a', 'at': T0 + 60, 'author': 'p', 'refs': [], 'text': 'tweak', 'subject': 'tweak'}
        self.assertEqual(intake.outside_commits([c], tasks, 'app', {}, T0 + 120), [])
        bot = {**c, 'author': 'dependabot[bot]', 'at': T0 - 999}
        self.assertEqual(intake.outside_commits([bot], tasks, 'app', {}, T0), [])
        cfg = self.cfg()
        r = intake.report(cfg, T0)   # the folder is not a git repo
        self.assertEqual(r['unreadable'], ['app'])
        self.assertIn('could not be read', intake.lines(r))
        self.assertEqual(self.cli(cfg, 'intake', '--json'), 0)

    def test_view_is_cached(self):
        cfg = self.cfg()
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout=''))
        intake.view(cfg, T0, run)
        intake.view(cfg, T0 + 10, run)
        self.assertEqual(run.call_count, 1)
        intake.view(cfg, T0 + intake.CACHE_SECONDS + 1, run)
        self.assertEqual(run.call_count, 2)
        intake._CACHE.clear()


class Snippet(Base):
    def test_added_once_replaced_when_older_and_the_users_text_kept(self):
        new, changed = intake.insert_snippet('# Rules\n')
        self.assertTrue(changed)
        self.assertTrue(new.startswith('# Rules\n\n'))
        self.assertEqual(intake.insert_snippet(new), (new, False))
        old = f'# Rules\n\n{intake.MARK_START}\nold text\n{intake.MARK_END}\n\nmore\n'
        new, changed = intake.insert_snippet(old)
        self.assertTrue(changed)
        self.assertNotIn('old text', new)
        self.assertTrue(new.endswith('more\n'))

    def test_connect_repo_writes_agents_md_and_refuses_a_folder_that_is_not_a_repo(self):
        import io
        cfg = self.cfg()
        out = io.StringIO()
        a = mock.Mock(repo=self.repo)   # the rest of connect's options are never read with --repo
        self.assertEqual(connect.cmd(cfg, a, out), 2)
        git(self.repo, 'init', '-q')
        with open(os.path.join(self.repo, 'CLAUDE.md'), 'w', encoding='utf8') as f:
            f.write('# Claude\n')
        self.assertEqual(connect.cmd(cfg, a, out), 0)
        with open(os.path.join(self.repo, 'CLAUDE.md'), encoding='utf8') as f:
            self.assertIn(intake.MARK_START, f.read())
        self.assertEqual(connect.cmd(cfg, a, out), 0)
        self.assertIn('already has', out.getvalue())


if __name__ == '__main__':
    unittest.main()
