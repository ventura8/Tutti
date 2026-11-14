"""autopilot.fill_room and auto_review (tutti/fill.py): free room filled from the repos' sources, retries that fall back to a
weaker step with room, the stopped line naming every step, and a result reviewed and landed by its repo's policy. Throwaway
repositories and fake gh / reviewer calls only."""
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.test_autopilot import FREE, Base, account, attempt, git, task
from tutti import autopilot, backlog, cli, config, dashboard, fill


def done(rc=0, out='', err=''):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class FillTest(Base):
    def states(self, room=5):
        return {'agy-gemini': account('agy-gemini', room=room), 'agy-claude': account('agy-claude', room=room),
                'cursor': account('cursor', 'ahead', None)}

    def repo(self):
        path = os.path.join(self.tmp.name, 'repo')
        os.makedirs(os.path.join(path, 'tests'))
        git(path, 'init', '-q')
        for name, text in (('a.py', 'x = 1\n'), ('b.py', 'y = 2  # TODO(dev): name it\n'), ('tests/test_a.py', 'pass\n'),
                           ('__init__.py', ''), ('vite.config.ts', '')):
            with open(os.path.join(path, name), 'w', encoding='utf8') as f:
                f.write(text)
        git(path, 'add', '-A')
        return path

    def test_untested_sources_skip_tests_configs_and_entry_points(self):
        files = ['src/a.py', 'src/b.ts', 'tests/test_a.py', 'src/b.test.ts', 'src/c.ts', 'src/__init__.py', 'x.config.js',
                 'src/t.d.ts', 'README.md']
        self.assertEqual([p for _, p in backlog.untested_items(files)], ['src/c.ts'])

    def test_free_room_is_filled_from_the_sources_up_to_the_cap_and_never_twice(self):
        self.repo()
        cfg = self.cfg(fill_room=True)
        info = self.round(cfg, 1000.0)
        self.assertEqual(info['room'], 11)                  # 5 + 5 on agy, and the free local model while cursor is ahead
        self.assertEqual(info['added'], 2)                  # the TODO in b.py and b.py without a test; a.py has one
        self.assertEqual({t['source'] for t in backlog.load(cfg)['tasks']}, {'todo', 'untested'})
        with backlog.locked(cfg) as data:
            for t in data['tasks']:
                t['status'] = 'dropped'                     # a dropped task is never queued again
        again = self.round(cfg, 2000.0)
        self.assertEqual(again['added'], 0)
        self.assertIn('nothing new', again['note'])

    def test_a_free_local_model_is_room_to_fill_while_the_online_accounts_are_spared(self):
        cfg = self.cfg(fill_room=True)
        self.assertEqual(fill.room(cfg, [], self.states(room=0), FREE), 1)   # agy has none, cursor is ahead: pc is the room
        cfg['workers']['pc']['machine'] = 'nuc'
        hot = [{**FREE[0], 'status': 'loaded', 'reason': 'too hot: CPU 99 C (nothing new at 98 C or more)'}]
        self.assertEqual(fill.room(cfg, [], self.states(room=0), hot), 0)   # the governor: a hot machine is no room

    def test_no_room_or_the_switch_off_reads_nothing(self):
        self.repo()
        cfg = self.cfg(fill_room=True)
        run = mock.Mock()
        cfg['workers']['pc']['machine'] = 'nuc'
        paused = [{**FREE[0], 'status': 'paused', 'reason': 'yield flag is up'}]   # the local model's machine yields too
        self.assertIsNone(fill.gather(cfg, backlog.load(cfg), self.states(room=0), paused, 1.0, run))
        fill.set_fill(cfg, False)
        self.assertIsNone(fill.gather(cfg, backlog.load(cfg), self.states(), FREE, 1.0, run))
        run.assert_not_called()
        self.assertFalse(autopilot.view(cfg)['fill_room'])

    def test_want_caps_at_ahead_per_run_less_the_fresh_queue(self):
        cfg = self.cfg()
        self.assertEqual(fill.want(cfg, [], 46), 2 * fill.AHEAD)
        self.assertEqual(fill.want(cfg, [task('t1'), task('t2', attempts=[attempt('agy-gemini')])], 46), 2 * fill.AHEAD - 1)
        self.assertEqual(fill.want(cfg, [], 0), 0)

    def test_pick_goes_round_robin_over_repos(self):
        found = [('a', 'one', [], 's', ''), ('a', 'two', [], 's', ''), ('b', 'three', [], 's', '')]
        self.assertEqual([x[1] for x in fill.pick({'tasks': []}, found, 2)], ['one', 'three'])

    def test_a_missing_repo_folder_is_named(self):
        cfg = self.cfg(fill_room=True)
        self.assertIn('no folder', self.round(cfg, 1.0)['note'])

    def test_built_bundles_and_prose_todos_are_not_tasks(self):
        files = ['cdn/engine/engine.iife.js', 'web/compare-CNW-vnBa.js', 'src/x.min.js', 'pkg/dist/a.js', 'src/real.ts', 'scripts/x.sh']
        self.assertEqual([p for _, p in backlog.untested_items(files)], ['src/real.ts'])
        lines = ['README.md:85:seed reads `TODO(owner)` and gaps', 'a.py:3:x = 1  # see `TODO(dev)` docs', 'b.py:4:# TODO(dev): fix']
        self.assertEqual([p for _, p in backlog.todo_items(lines)], ['b.py'])

    def test_an_import_makes_a_file_tested_a_bare_word_does_not(self):
        text = 'from tutti import owner\nconst c = require("./city-icon")\n# the state of the config\n'
        files = ['tutti/owner.py', 'src/city-icon.ts', 'tutti/state.py', 'tutti/config.py']
        self.assertEqual([p for _, p in backlog.untested_items(files, text)], ['tutti/state.py', 'tutti/config.py'])
        self.assertEqual([p for _, p in backlog.untested_items(['build/release.py'], '', built_dirs=())], ['build/release.py'])

    def test_the_untested_switch_and_its_config_check(self):
        self.repo()
        cfg = self.cfg()
        repo = {**cfg['autopilot']['repos']['app'], 'untested': False}
        self.assertNotIn('untested', {src for _, _, src in backlog.sources(repo, untested=True)})
        cfg['autopilot']['repos']['app'].update(untested='no', built='dist')
        errs = '\n'.join(config.validate(cfg))
        self.assertIn('autopilot.repos.app.untested', errs)
        self.assertIn('autopilot.repos.app.built', errs)

    def test_an_apostrophe_is_not_a_quote_but_a_string_is(self):
        lines = ["a.py:1:# don't forget, TODO(dev): x", "b.py:2:help = 'reads TODO(owner) items'", 'c.py:3:x = "it\'s"  # TODO(dev): y']
        self.assertEqual([p for _, p in backlog.todo_items(lines)], ['a.py', 'c.py'])

    def test_a_short_name_is_not_tested_by_a_longer_test_name(self):
        self.assertEqual([p for _, p in backlog.untested_items(['a.py', 'tests/test_data.py', 'b.ts', 'b.spec.ts'])], ['a.py'])

    def test_a_hung_gaps_command_gives_nothing_instead_of_blocking(self):
        run = mock.Mock(side_effect=subprocess.TimeoutExpired('gaps', 1))
        self.assertEqual(backlog._out(run, ['x']), '')

    def round(self, cfg, now):
        found = fill.gather(cfg, backlog.load(cfg), self.states(), FREE, now)
        with backlog.locked(cfg) as data:
            info = fill.fill(cfg, data, self.states(), FREE, now, found)
        with autopilot.state_locked(cfg) as st:
            st['fill'] = info
        return info

    def test_the_stopped_line_names_every_step_with_room_too(self):
        states = {**self.states(), 'agy-gemini': account('agy-gemini', 'on pace'), 'agy-claude': account('agy-claude', room=1)}
        cfg = self.cfg(ladder=['agy-gemini', 'agy-claude'])
        p = autopilot.plan(cfg, [task('t1', size='L')], states, FREE, True)
        self.assertIn('agy-gemini: at its even-pace line', p['reason'])
        self.assertIn('agy-claude: room for 1, but the queued tasks need 3', p['reason'])

    def test_a_retry_falls_back_below_when_the_steps_above_have_no_room(self):
        paused = [{'name': 'nuc', 'status': 'free', 'free': 2, 'reason': 'fit'}]
        cfg = self.cfg(ladder=['agy-gemini', 'agy-claude', 'cursor'])
        tries = [attempt('agy-gemini'), attempt('agy-claude')]
        p = autopilot.plan(cfg, [task('t1', attempts=tries)], self.states(), paused, True)
        self.assertEqual(p['dispatch'], [('t1', 'agy-claude')])
        fine = autopilot.plan(cfg, [task('t1', attempts=tries)], {**self.states(), 'cursor': account('cursor', 'on pace', None)},
                              paused, True)
        self.assertEqual(fine['dispatch'], [('t1', 'cursor')])   # the step above still comes first

    def test_the_dashboard_switch_takes_fill_room_and_refuses_a_bad_body(self):
        cfg = self.cfg(enabled=False)
        self.assertEqual(dashboard.save_autopilot(SimpleNamespace(cfg=cfg), b'{"enabled": true, "fill_room": true}')[0], 200)
        self.assertTrue(autopilot.enabled(cfg) and fill.fill_on(cfg))
        self.assertEqual(dashboard.save_autopilot(SimpleNamespace(cfg=cfg), b'{"fill_room": "yes"}')[0], 400)
        self.assertEqual(dashboard.save_autopilot(SimpleNamespace(cfg=cfg), b'{}')[0], 400)

    def test_config_refuses_a_bad_land_and_switch(self):
        cfg = self.cfg(fill_room='yes')
        cfg['autopilot']['repos']['app']['land'] = 'push'
        errs = '\n'.join(config.validate(cfg))
        self.assertIn('autopilot.fill_room', errs)
        self.assertIn('autopilot.repos.app.land', errs)

    def test_cli_fill_switch_and_its_bad_word(self):
        cfg = self.cfg()
        self.assertEqual(cli.cmd_autopilot(cfg, SimpleNamespace(action='fill', arg='on')), 0)
        self.assertTrue(fill.fill_on(cfg))
        with mock.patch.object(cli, 'say'):
            self.assertEqual(cli.cmd_autopilot(cfg, SimpleNamespace(action='fill', arg='maybe')), 2)


class LandTest(Base):
    def setUp(self):
        super().setUp()
        self.wt = os.path.join(self.tmp.name, 'wt1')
        os.makedirs(self.wt)

    def review_task(self, cfg, **extra):
        t = {**task('t1', 'review', [{**attempt('agy-gemini', ok=True), 'gates': 'true'}]), 'branch': 'autopilot/t1-x',
             'worktree': self.wt, **extra}
        with backlog.locked(cfg) as data:
            data['tasks'].append(t)
            data['next'] = 2
        return t

    def gitfake(self, fail=()):
        calls = []

        def run(cwd, *args):
            calls.append(args)
            if args[0] in fail:
                return done(1, err=f'{args[0]} refused')
            return done(0, 'HEAD' if args[0] == 'rev-parse' else 'feat: x' if args[0] == 'log' else 'diff')
        return run, calls

    def test_verdict_reads_no_findings_and_bullets(self):
        self.assertEqual(fill.verdict('**No findings.**'), (True, []))
        self.assertFalse(fill.verdict('No findings.\n- a.py:1 - x - y')[0])
        self.assertEqual(fill.verdict('- a.py:1 - wrong - fix\n- b.py:2 - x - y'), (False, ['a.py:1 - wrong - fix', 'b.py:2 - x - y']))
        self.assertFalse(fill.verdict('')[0])

    def test_a_clean_review_merges_for_land_merge(self):
        cfg = self.cfg()
        cfg['autopilot']['repos']['app'].update(land='merge', base='main')
        self.review_task(cfg)
        run, calls = self.gitfake()
        gh = mock.Mock(side_effect=[done(0, 'https://github.example/o/r/pull/7\n'), done(1, err='cannot delete'), done(0, 'MERGED\n')])
        with mock.patch.object(fill, 'ask_reviewer', return_value=('cheap', 'No findings.')):
            out = fill.land(cfg, 't1', run=run, gh=gh)
        self.assertIn('done', out)
        t = backlog.load(cfg)['tasks'][0]
        self.assertEqual((t['status'], t['pr']), ('done', 'https://github.example/o/r/pull/7'))
        self.assertIn('merged into main', t['publish_note'])
        self.assertEqual(gh.call_args_list[1][0][0][:3], ['gh', 'pr', 'merge'])
        self.assertIn(('rebase', '-q', 'FETCH_HEAD'), calls)
        self.assertIn(('push', '-q', 'origin', '--delete', 'autopilot/t1-x'), calls)   # merged: the remote branch goes
        self.assertIn(('diff', '--stat', '--patch', 'FETCH_HEAD...HEAD'), calls)     # the whole branch is reviewed

    def test_land_pr_opens_a_pull_request_and_never_merges(self):
        cfg = self.cfg()
        cfg['autopilot']['repos']['app'].update(land='pr', base='dev')
        self.review_task(cfg)
        run, _ = self.gitfake()
        gh = mock.Mock(return_value=done(0, 'https://github.example/o/r/pull/8'))
        self.assertEqual(fill.open_pr({'worktree': self.wt, 'pr': 'u'}, 'dev', 's', 'b', mock.Mock(return_value=done(1, err='no')))[0],
                         None)                              # a draft that cannot be marked ready does not land
        with mock.patch.object(fill, 'ask_reviewer', return_value=('cheap', 'No findings')):
            fill.land(cfg, 't1', run=run, gh=gh)
        self.assertEqual(gh.call_count, 1)
        self.assertIn('for a person to merge', backlog.load(cfg)['tasks'][0]['publish_note'])

    def test_findings_send_it_back_and_the_third_time_parks_it(self):
        cfg = self.cfg()
        self.review_task(cfg, reviews=1)
        run, _ = self.gitfake()
        with mock.patch.object(fill, 'ask_reviewer', return_value=('cheap', 'No findings, but:\n- a.py:3 - off by one - use <=')):
            fill.land(cfg, 't1', run=run, gh=mock.Mock())
        t = backlog.load(cfg)['tasks'][0]
        self.assertEqual((t['status'], t['reviews'], t['notes']), ('queued', 2, ['a.py:3 - off by one - use <=']))
        with backlog.locked(cfg) as data:
            data['tasks'][0]['status'] = 'review'
        run, _ = self.gitfake()
        with mock.patch.object(fill, 'ask_reviewer', return_value=('cheap', '- a.py:3 - off by one - use <=')):
            fill.land(cfg, 't1', run=run, gh=mock.Mock())
        t = backlog.load(cfg)['tasks'][0]
        self.assertEqual(t['status'], 'needs-person')
        self.assertIn('a.py:3 - off by one - use <=', t['notes'])

    def test_a_conflicting_rebase_needs_a_person_and_a_failed_fetch_waits(self):
        cfg = self.cfg()
        cfg['autopilot']['repos']['app']['base'] = 'main'
        self.review_task(cfg)
        run, calls = self.gitfake(fail=('rebase',))
        fill.land(cfg, 't1', run=run, gh=mock.Mock())
        self.assertIn('does not rebase cleanly', backlog.load(cfg)['tasks'][0]['parked'])
        self.assertIn(('rebase', '--abort'), calls)
        with backlog.locked(cfg) as data:
            data['tasks'][0].update(status='review', parked=None)
        fill.land(cfg, 't1', run=self.gitfake(fail=('fetch',))[0], gh=mock.Mock(), now=100.0)
        t = backlog.load(cfg)['tasks'][0]
        self.assertEqual((t['status'], t['retry_after']), ('review', 100.0 + fill.FILL_EVERY))

    def test_old_or_missing_gates_send_it_back_to_run_them(self):
        cfg = self.cfg()
        self.review_task(cfg)
        cfg['autopilot']['repos']['app']['check'] = 'ruff check .'
        fill.land(cfg, 't1', run=self.gitfake()[0], gh=mock.Mock())
        self.assertEqual(backlog.load(cfg)['tasks'][0]['status'], 'queued')
        cfg['autopilot']['repos']['app']['check'] = ''
        self.assertIn('lint and type gates', fill.gated(cfg, backlog.load(cfg)['tasks'][0]))

    def test_a_reviewer_without_room_leaves_it_for_the_next_round(self):
        cfg = self.cfg()
        self.review_task(cfg)
        with mock.patch.object(fill, 'ask_reviewer', side_effect=RuntimeError('no reviewer has room')):
            fill.land(cfg, 't1', run=self.gitfake()[0], gh=mock.Mock())
        t = backlog.load(cfg)['tasks'][0]
        self.assertEqual(t['status'], 'review')
        self.assertIn('could not run', t['publish_note'])
        self.assertGreater(t['retry_after'], 0)            # not again every round
        self.assertEqual(fill.review_round(cfg, backlog.load(cfg), 1.0, lambda pid: True, mock.Mock()), [])

    def test_the_round_starts_one_review_at_a_time_and_frees_a_dead_one(self):
        cfg = self.cfg(auto_review=True)
        cfg['autopilot']['repos']['app']['land'] = 'pr'
        os.makedirs(config.state_dir(cfg), exist_ok=True)
        data = {'next': 3, 'tasks': [{**task('t1', 'review'), 'branch': 'b1'}, {**task('t2', 'review'), 'branch': 'b2'}]}
        spawn = mock.Mock(return_value=SimpleNamespace(pid=4242))
        events = fill.review_round(cfg, data, 1.0, lambda pid: True, spawn)
        self.assertEqual(spawn.call_count, 1)
        self.assertIn('t1', events[0])
        self.assertEqual(fill.review_round(cfg, data, 2.0, lambda pid: True, spawn), [])
        fill.review_round(cfg, data, 3.0, lambda pid: False, spawn)   # the land process died: t1 is freed and started again
        self.assertEqual(spawn.call_count, 2)

    def test_the_reviewer_runs_in_an_empty_folder_with_the_diff(self):
        cfg = self.cfg()
        cfg['review']['fallback'] = ['cursor']
        seen = {}

        def run_worker(argv, cwd):
            with open(os.path.join(cwd, 'pr.diff'), encoding='utf8') as f:
                seen['diff'] = f.read()
            return subprocess.CompletedProcess(argv, 0, 'No findings.', '')
        with mock.patch.object(fill.review, 'pick_fallback', return_value=('cursor', [])):
            self.assertEqual(fill.ask_reviewer(cfg, 'DIFF', run_worker), ('cursor', 'No findings.'))
        self.assertEqual(seen['diff'], 'DIFF')
        with mock.patch.object(fill.review, 'pick_fallback', return_value=(None, 'all spent')):
            with self.assertRaises(RuntimeError):
                fill.ask_reviewer(cfg, 'DIFF')


if __name__ == '__main__':
    unittest.main()
