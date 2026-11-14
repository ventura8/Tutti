"""What the orchestrator uses: delegated runs, `wait run`, heat routing, prepare steps, coverage notes, stale readings, the
reading file, the reading helper and `pr review`. Throwaway repositories and fake workers only; nothing leaves the temp folder."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from tutti import cli, config, dashboard, delegate, helper, pacing, readers, review, routing, ship, workers

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def git(cwd, *args):
    return subprocess.run(['git', '-C', cwd, *args], check=True, capture_output=True, text=True).stdout.strip()


class HeatTest(unittest.TestCase):
    machine = {'slots': 3, 'heat': {'cap_c': 85, 'stop_c': 98, 'hot_slots': 1}}

    def test_the_probe_reads_millidegrees_and_older_probes_still_parse(self):
        self.assertEqual(routing.parse_probe('2 1.5 16 0 89000').temp, 89.0)
        self.assertIsNone(routing.parse_probe('2 1.5 16 0 0').temp)
        old = routing.parse_probe('2 1.5 16 0')
        self.assertTrue(old.reachable)
        self.assertIsNone(old.temp)

    def test_a_hot_machine_takes_one_run_and_a_very_hot_one_none(self):
        cool = routing.Probe(True, 2, 1.0, 16, False, 70.0)
        self.assertEqual(routing.unfit(self.machine, cool), '')
        hot_idle = routing.Probe(True, 3, 1.0, 16, False, 89.0)
        self.assertEqual(routing.unfit(self.machine, hot_idle), '')
        hot_busy = routing.Probe(True, 2, 1.0, 16, False, 89.0)
        self.assertIn('slots capped to 1', routing.unfit(self.machine, hot_busy))
        too_hot = routing.Probe(True, 3, 1.0, 16, False, 99.0)
        self.assertIn('too hot', routing.unfit(self.machine, too_hot))

    def test_a_too_hot_machine_is_never_the_queue(self):
        machines = {'nuc': {**self.machine, 'name': 'nuc'}, 'pc': {'slots': 1, 'name': 'pc'}}
        probes = {'nuc': routing.Probe(True, 3, 1.0, 16, False, 99.0), 'pc': routing.Probe(True, 0, 1.0, 16, False, None)}
        self.assertEqual(routing.choose(['nuc', 'pc'], machines, probes.get).machine, 'pc')

    def test_routing_falls_over_from_a_hot_machine(self):
        machines = {'nuc': {**self.machine, 'name': 'nuc'}, 'laptop': {'slots': 1, 'name': 'laptop'}}
        probes = {'nuc': routing.Probe(True, 1, 1.0, 16, False, 90.0), 'laptop': routing.Probe(True, 1, 1.0, 16, False, None)}
        choice = routing.choose(['nuc', 'laptop'], machines, probes.get)
        self.assertEqual(choice.machine, 'laptop')
        self.assertIn('nuc: hot', choice.reason)

    def test_bad_heat_config_names_the_key(self):
        cfg = config.normalise({'machines': {'m': {'host': 'h', 'heat': {'cap_c': 'hot', 'hot_slots': 0}}}})
        errs = '\n'.join(config.validate(cfg))
        self.assertIn('machines.m.heat.cap_c', errs)
        self.assertIn('machines.m.heat.hot_slots', errs)


class PrepareAndCoverageTest(unittest.TestCase):
    rules = [{'when': 'playwright|e2e/', 'run': 'python3 scripts/ci/e2e_build.py'}]

    def test_a_playwright_run_builds_first_once(self):
        self.assertEqual(routing.prepare('npx playwright test e2e/x.spec.ts', self.rules),
                         '{ python3 scripts/ci/e2e_build.py; } && { npx playwright test e2e/x.spec.ts; }')

    def test_a_failed_step_stops_a_compound_command(self):
        cmd = routing.prepare('echo ran; npx playwright test', [{'when': 'playwright', 'run': 'false'}])
        out = subprocess.run(['bash', '-c', cmd.replace('npx playwright test', 'true')], capture_output=True, text=True).stdout
        self.assertEqual(out, '')
        already = 'python3 scripts/ci/e2e_build.py && npx playwright test'
        self.assertEqual(routing.prepare(already, self.rules), already)

    def test_other_commands_are_left_alone(self):
        self.assertEqual(routing.prepare('npx vitest run', self.rules), 'npx vitest run')
        self.assertEqual(routing.prepare('npx vitest run', None), 'npx vitest run')

    def test_coverage_off_is_reported(self):
        self.assertIn('coverage NOT checked', routing.coverage_note('npx vitest related --coverage.enabled=false'))
        self.assertIn('coverage NOT checked', routing.coverage_note('pytest --no-cov tests'))
        self.assertEqual(routing.coverage_note('npx vitest run --coverage'), '')

    def test_bad_prepare_rule_names_the_key(self):
        cfg = config.normalise({'test': {'prepare': [{'when': '(', 'run': 'x'}, {'when': 'a'}]}})
        errs = '\n'.join(config.validate(cfg))
        self.assertIn('test.prepare[0].when', errs)
        self.assertIn('test.prepare[1] needs run', errs)

    def test_the_test_command_keeps_its_own_options(self):
        a = cli.parser().parse_args(['test', 't', 'echo', '-n', 'x'])
        self.assertEqual(a.command, ['echo', '-n', 'x'])


class ChangedPlaceholderTest(unittest.TestCase):
    def test_all_and_by_extension(self):
        changed = ['a.ts', 'b.py', 'c d.js']
        self.assertEqual(ship.expand_changed('x {changed}', changed), "x 'a.ts\nb.py\nc d.js'")
        self.assertEqual(ship.expand_changed('F={changed:ts,js}', changed), "F='a.ts\nc d.js'")
        self.assertEqual(ship.expand_changed('P={changed:rs}', changed), "P=''")

    def test_a_path_with_a_space_reaches_a_check_whole(self):
        with tempfile.TemporaryDirectory() as d:
            ship.git(d, 'init', '-q')
            for name in ('my file.ts', 'plain.ts', 'é.ts'):
                with open(os.path.join(d, name), 'w', encoding='utf8') as f:
                    f.write('x\n')
            found = ship.pending(d)
            self.assertEqual(found, ['my file.ts', 'plain.ts', 'é.ts'])
            env = {'TUTTI_CHANGED': ship.join_changed(found)}
            self.assertEqual(ship.split_changed(env['TUTTI_CHANGED']), found)
            cmd = ship.expand_changed('F={changed:ts}; printf "%s\\n" "$F" | xargs -d "\\n" ls -1 > out.txt', found)
            self.assertEqual(ship.shell(d, cmd, {}).returncode, 0)
            with open(os.path.join(d, 'out.txt'), encoding='utf8') as f:
                self.assertEqual(sorted(f.read().splitlines()), found)
            self.assertEqual(ship.split_changed(''), [])
            with self.assertRaises(ship.ShipError):          # a missing base is an error, not "nothing changed"
                ship.changed_since(d, 'origin/no-such-branch')


class WorkerCommandTest(unittest.TestCase):
    def test_an_account_replaces_the_flags_the_worker_has(self):
        argv = ['agy', '-p', 'brief', '--mode', 'accept-edits', '--settings', '{"permissions":{"allow":["command(git)"]}}']
        claude = ['--model', 'claude-sonnet-5-5-medium', '--settings', '{"permissions":{"allow":["command(*)"]}}']
        self.assertEqual(workers.with_args(argv, claude), ['agy', '-p', 'brief', '--mode', 'accept-edits', '--settings', claude[3],
                                                           '--model', 'claude-sonnet-5-5-medium'])
        # a bare flag and a flag the worker lacks are added; the worker's own words stay
        self.assertEqual(workers.with_args(['x', '--quiet'], ['--trust', '--n', '2']), ['x', '--quiet', '--trust', '--n', '2'])
        self.assertEqual(workers.with_args(['x', '-p', 'brief'], []), ['x', '-p', 'brief'])

    def test_log_placeholder_filled_or_dropped_with_its_flag(self):
        w = {'command': ['agy', '-p', '{prompt}', '--log-file', '{log}']}
        self.assertEqual(workers.command(w, 'do it', '/tmp/x.log'), ['agy', '-p', 'do it', '--log-file', '/tmp/x.log'])
        self.assertEqual(workers.command(w, 'do it'), ['agy', '-p', 'do it'])
        self.assertEqual(workers.command(w, 'explain {log}', '/x')[2], 'explain {log}')


FAKE_WORKER = '''import os, sys
prompt = sys.argv[1]
if 'nothing' not in prompt:
    with open('a.txt', 'a') as f:
        f.write('two\\n')
    with open('new.txt', 'w') as f:
        f.write('new\\n')
if len(sys.argv) > 2:
    with open(sys.argv[2], 'w') as f:
        f.write('soft-denying tool confirmation "Bash" at step 3\\n' if 'deny' in prompt else 'ok\\n')
print('worker done')
'''


class DelegateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.repo = os.path.join(d, 'repo')
        os.makedirs(self.repo)
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'config', 'user.email', 'tester@example.invalid')
        git(self.repo, 'config', 'user.name', 'Tester')
        with open(os.path.join(self.repo, 'a.txt'), 'w', encoding='utf8') as f:
            f.write('one\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'chore: start')
        self.script = os.path.join(d, 'worker.py')
        with open(self.script, 'w', encoding='utf8') as f:
            f.write(FAKE_WORKER)
        self.said = []

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, checks):
        return config.normalise({
            'project': {'worktrees': os.path.join(self.tmp.name, 'wt'), 'state': os.path.join(self.tmp.name, 'state')},
            'workers': {'fake': {'kind': 'cli', 'command': [sys.executable, self.script, '{prompt}', '{log}'], 'denied': 'soft-denying'}},
            'delegate': {'checks': checks}})

    def run_task(self, cfg, task='add a line', **kw):
        with mock.patch('sys.stdout'):
            return delegate.delegate(cfg, 'fake', task, [], self.repo, say=self.said.append, **kw)

    def test_happy_path_diff_checks_and_report(self):
        cfg = self.cfg([{'run': 'echo "[SUCCESS]" $TUTTI_CHANGED', 'coverage': False}])
        r = self.run_task(cfg)
        self.assertEqual(r['rc'], 0)
        self.assertEqual(r['changed'], ['a.txt', 'new.txt'])
        with open(r['diff'], encoding='utf8') as f:
            patch = f.read()
        self.assertIn('+two', patch)
        self.assertIn('new.txt', patch)
        self.assertEqual(git(r['worktree'], 'status', '--porcelain', '--untracked-files=no'), 'M a.txt')   # index restored
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')                                    # the repo is untouched
        self.assertEqual(r['checks'][0]['rc'], 0)
        self.assertIn('[SUCCESS] a.txt new.txt', r['checks'][0]['verdict'])
        with open(r['report'], encoding='utf8') as f:
            text = f.read()
        self.assertIn('coverage NOT checked', text)
        self.assertIn('tutti runs discard', text)
        delegate.remove_tree(r['worktree'], self.said.append)
        self.assertFalse(os.path.exists(r['worktree']))

    def test_a_failing_check_a_denial_and_an_empty_diff_fail_the_run(self):
        r = self.run_task(self.cfg([{'run': 'exit 3'}]))
        self.assertEqual(r['rc'], 1)
        self.assertEqual(r['checks'][0]['rc'], 3)
        denied = self.run_task(self.cfg([]), 'please deny', discard=True)
        self.assertEqual(denied['rc'], 1)
        self.assertIn('soft-denying', denied['denied'][0])
        self.assertTrue(denied['worktree_removed'])
        nothing = self.run_task(self.cfg([]), 'do nothing')
        self.assertEqual((nothing['rc'], nothing['changed'], nothing['checks']), (1, [], []))
        with open(nothing['report'], encoding='utf8') as f:
            self.assertIn('nothing was tested', f.read())

    def test_a_named_track_is_reused_and_a_stray_folder_refused(self):
        cfg = self.cfg([])
        first = self.run_task(cfg, track='t1', checks=False)
        again = self.run_task(cfg, track='t1', checks=False)
        self.assertEqual(first['worktree'], again['worktree'])
        self.assertFalse(again['created'])
        self.assertIsNone(again['checks'])
        os.makedirs(os.path.join(self.tmp.name, 'wt', 'tutti-run-stray'))
        with self.assertRaises(ship.ShipError):
            self.run_task(cfg, track='stray')

    def test_wait_run_ends_with_one_line(self):
        cfg = self.cfg([{'run': 'true'}])
        r = self.run_task(cfg)
        with open(delegate.paths(cfg, r['id'])['rc'], 'w', encoding='utf8') as f:
            f.write(f'{r["rc"]}\n')
        out = mock.MagicMock()
        with mock.patch('sys.stdout', out):
            rc = cli.cmd_wait(cfg, SimpleNamespace(kind='run', target=r['id'], pattern=None, stall=0, stall_file=None, timeout=5, poll=0.1))
        self.assertEqual(rc, 0)
        line = ''.join(c.args[0] for c in out.write.call_args_list)
        self.assertIn(f'run {r["id"]} done', line)
        self.assertIn('checks 1/1 passed', line)
        self.assertEqual(line.count('\n'), 1)
        self.assertIn(r['id'], delegate.listing(cfg))

    def test_ids_and_worktrees_from_a_report_are_checked(self):
        cfg = self.cfg([])
        self.assertTrue(delegate.valid_id('1794658174-265882-2'))
        for bad in ('../x', '1-1/../../etc', 'abc'):
            self.assertFalse(delegate.valid_id(bad))
            self.assertIsNone(delegate.load_report(cfg, bad))
        root = os.path.join(self.tmp.name, 'wt')
        mine = os.path.join(os.path.realpath(root), 'tutti-run-1-1')
        self.assertEqual(delegate.own_worktree(cfg, os.path.join(root, 'tutti-run-1-1')), mine)
        self.assertIsNone(delegate.own_worktree(cfg, self.repo))
        self.assertIsNone(delegate.own_worktree(cfg, os.path.join(root, 'other')))
        self.assertEqual(cli.cmd_runs(cfg, SimpleNamespace(action='discard', id='../x')), 2)

    def test_wait_run_on_an_unknown_id_says_so(self):
        cfg = self.cfg([])
        with self.assertRaises(config.ConfigError):
            cli.cmd_wait(cfg, SimpleNamespace(kind='run', target='1-1', pattern=None, stall=0, stall_file=None, timeout=1, poll=0.1))


class StallTest(unittest.TestCase):
    def test_a_folder_counts_by_its_newest_file_and_several_paths_by_the_newest(self):
        from tutti import waiting
        with tempfile.TemporaryDirectory() as d:
            log, folder = os.path.join(d, 'run.log'), os.path.join(d, 'run')
            os.makedirs(folder)
            for p in (log, os.path.join(folder, 'check.log')):
                with open(p, 'w', encoding='utf8') as f:
                    f.write('x')
            os.utime(log, (1, 1))
            os.utime(folder, (1, 1))
            now = os.path.getmtime(os.path.join(folder, 'check.log'))
            self.assertFalse(waiting.stalled([log, folder], 60, clock=lambda: now + 10)())   # the check log is fresh
            self.assertTrue(waiting.stalled([log, folder], 60, clock=lambda: now + 120)())
            self.assertTrue(waiting.stalled(log, 60, clock=lambda: now)())


class StaleReadingTest(unittest.TestCase):
    def test_a_hand_read_account_is_reminded_after_two_hours(self):
        a = {'window': '7d', 'resets': '2026-11-21T00:00Z'}
        fresh = [{'at': (NOW - timedelta(hours=1)).isoformat(), 'account': 'claude', 'used': 10}]
        old = [{'at': (NOW - timedelta(hours=3)).isoformat(), 'account': 'claude', 'used': 10}]
        self.assertFalse(pacing.account_state('claude', a, fresh, NOW)['stale'])
        s = pacing.account_state('claude', a, old, NOW)
        self.assertTrue(s['stale'])
        self.assertIn('tutti usage log claude <percent>', s['remind'])
        self.assertTrue(pacing.account_state('claude', a, [], NOW)['stale'])

    def test_an_automatically_read_account_is_never_stale(self):
        a = {'window': '5h', 'resets': '2026-11-14T15:00Z', 'reader': {'command': ['x'], 'bucket': 'b'}}
        self.assertFalse(pacing.account_state('agy', a, [], NOW)['stale'])

    def test_an_account_behind_says_how_many_tasks_it_can_take(self):
        a = {'window': '7d', 'resets': '2026-11-21T00:00Z', 'worker': 'agy'}
        s = pacing.account_state('agy', a, [{'at': NOW.isoformat(), 'account': 'agy', 'used': 0, 'source': 'reader'}], NOW)
        self.assertEqual(s['verdict'], 'behind')
        self.assertGreaterEqual(s['room_tasks'], 1)
        self.assertIn('bounded task(s) can go to agy', s['action'])
        self.assertTrue(s['auto'])


class ReadingFileTest(unittest.TestCase):
    def test_each_version_of_the_file_is_logged_once(self):
        with tempfile.TemporaryDirectory() as d:
            log, f = os.path.join(d, 'usage.jsonl'), os.path.join(d, 'claude.json')
            accounts = {'claude': {'reading_file': f}}
            with open(f, 'w', encoding='utf8') as fh:
                json.dump({'used': 42, 'resets': '2026-11-21T00:00Z'}, fh)
            self.assertEqual(len(readers.record_files(log, accounts)[0]), 1)
            self.assertEqual(readers.record_files(log, accounts), ([], []))
            entry = pacing.read_log(log)[0]
            self.assertEqual((entry['used'], entry['source']), (42.0, 'file'))

    def test_plain_and_bad_files(self):
        self.assertEqual(readers.parse_reading_file('37 %')[0], 37.0)
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, 'c.txt')
            with open(f, 'w', encoding='utf8') as fh:
                fh.write('lots')
            written, problems = readers.record_files(os.path.join(d, 'u.jsonl'), {'claude': {'reading_file': f}})
            self.assertEqual(written, [])
            self.assertIn('accounts.claude.reading_file', problems[0])


class HelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config.normalise({'project': {'state': self.tmp.name}, 'accounts': {
            'cursor': {'usage_url': 'https://cursor.example/dashboard/spending'},
            'agy': {'usage_url': 'https://agy.example/usage', 'reader': {'command': ['x'], 'bucket': 'b'}}}})

    def tearDown(self):
        self.tmp.cleanup()

    def test_page_reading_records_good_json_and_refuses_bad_shapes(self):
        log = os.path.join(self.tmp.name, 'usage.jsonl')
        now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
        good = json.dumps({'readings': [{'account': 'cursor', 'used': 25, 'resets': '2026-12-10T00:00Z', 'how': 'Cursor Models'}],
                           'missing': []})
        self.assertEqual(cli.page_reading(self.cfg, log, good, now), 0)
        with open(log, encoding='utf8') as f:
            entry = json.loads(f.readline())
        self.assertEqual((entry['account'], entry['used'], entry['source']), ('cursor', 25.0, 'page script'))
        for bad in ('not json', '{"readings": null}', '{"readings": [null]}', '{"readings": [], "missing": [1]}', '[]'):
            self.assertEqual(cli.page_reading(self.cfg, log, bad, now), 2, bad)
        self.assertEqual(cli.page_reading(self.cfg, log, json.dumps({'readings': [], 'missing': ['Other Models']}), now), 1)
        self.assertEqual(cli.page_reading(self.cfg, log, json.dumps({'readings': [{'account': 'ghost', 'used': 5}]}), now), 2)

    def test_the_page_script_lists_hand_read_pages_only(self):
        self.assertEqual(cli.page_scripts(self.cfg, 'nothing-matches'), 2)

    def test_the_key_is_kept_and_checked(self):
        k = helper.key(self.tmp.name)
        self.assertEqual(helper.key(self.tmp.name), k)
        self.assertEqual(os.stat(os.path.join(self.tmp.name, helper.KEY_FILE)).st_mode & 0o777, 0o600)
        self.assertTrue(helper.key_ok(self.tmp.name, k))
        self.assertFalse(helper.key_ok(self.tmp.name, 'other'))
        self.assertFalse(helper.key_ok(self.tmp.name, 'clé-é'))

    def test_bookmarklets_only_for_hand_read_accounts_and_with_escaped_percent(self):
        self.assertEqual(list(helper.helper_accounts(self.cfg)), ['cursor'])
        js = helper.bookmarklet('cursor', 'KEY', 'http://127.0.0.1:4777')
        self.assertTrue(js.startswith('javascript:(function () {'))
        self.assertNotIn('%\\s', js)
        self.assertIn('%25', js)
        page = helper.page(self.cfg, self.tmp.name, 4777).decode()
        self.assertIn('Tutti: read cursor', page)
        self.assertNotIn('<script', page)

    def test_a_helper_reading_needs_the_key(self):
        k = helper.key(self.tmp.name)
        good = json.dumps({'account': 'cursor', 'used': 40, 'key': k}).encode()
        bad = json.dumps({'account': 'cursor', 'used': 40, 'key': 'made-up'}).encode()
        self.assertEqual(dashboard.save_reading(self.cfg, bad, NOW)[0], 403)
        code, answer = dashboard.save_reading(self.cfg, good, NOW)
        self.assertEqual(code, 200)
        self.assertEqual(answer['reading']['source'], 'helper')


class FakeGh:
    """`gh` answers from a table; records every call."""

    def __init__(self, copilot_ok=False, events=(), dropped=False, graphql_ok=False):
        self.calls, self.copilot_ok, self.events = [], copilot_ok, list(events)
        self.dropped, self.graphql_ok = dropped, graphql_ok

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ['api', '-X']:
            if self.copilot_ok:
                return 0, json.dumps({'requested_reviewers': [{'login': 'Copilot'}]}), ''
            if self.dropped:   # GitHub's 201 that does not add the reviewer
                return 0, json.dumps({'requested_reviewers': []}), ''
            return 1, '', 'HTTP 422: Reviews may only be requested from collaborators'
        if args[:2] == ['api', 'graphql']:
            nodes = [{'requestedReviewer': {'login': 'copilot-pull-request-reviewer'}}] if self.graphql_ok else []
            return 0, json.dumps({'data': {'requestReviewsByLogin': {'pullRequest': {'reviewRequests': {'nodes': nodes}}}}}), ''
        if args[0] == 'api' and args[-2:-1] == ['--jq']:
            return 0, 'PR_node\n', ''
        if args[0] == 'api':
            assert '--slurp' not in args, 'gh before 2.62 has no --slurp'
            key = 'submitted_at' if args[-1].split('?')[0].endswith('/reviews') else 'created_at'
            items = [e for e in self.events if key in e]
            return 0, json.dumps(items[:1]) + '\n' + json.dumps(items[1:]), ''   # two pages back to back, as --paginate prints them
        if args[:2] == ['pr', 'diff']:
            return 0, 'diff --git a/x b/x\n+bug\n', ''
        return 0, '', ''


class ReviewTest(unittest.TestCase):
    def cfg(self, d, reviewers=('copilot', 'coderabbit')):
        return config.normalise({'project': {'state': d}, 'workers': {'cheap': {'kind': 'cli', 'command': ['cheap', '{prompt}']}},
                                 'review': {'reviewers': list(reviewers), 'timeout_minutes': 0, 'fallback': ['cheap']}})

    def test_status_reads_limits_and_reviews(self):
        since = NOW - timedelta(minutes=1)
        limited = [('coderabbitai[bot]', NOW, '<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->', 'comment')]
        self.assertEqual(review.status('coderabbit', limited, since), 'limited')
        ack = [('coderabbitai[bot]', NOW, 'Action not completed. Review rate limited.', 'comment')]
        self.assertEqual(review.status('coderabbit', ack, since), 'limited')
        self.assertEqual(review.status('coderabbit', [('coderabbitai[bot]', NOW, 'Review triggered.', 'comment')], since), 'pending')
        self.assertEqual(review.status('copilot', [('copilot-pull-request-reviewer[bot]', NOW, 'ok', 'review')], since), 'reviewed')
        old = [('copilot-pull-request-reviewer[bot]', since - timedelta(hours=1), 'old', 'review')]
        self.assertEqual(review.status('copilot', old, since), 'pending')

    def test_an_edited_comment_counts_at_its_update_time(self):
        edited = {'user': {'login': 'coderabbitai[bot]'}, 'created_at': '2026-11-14T11:00:00Z', 'updated_at': '2026-11-14T12:00:00Z',
                  'body': 'Review limit reached'}
        fake = FakeGh(events=[edited])
        evs = review.events('o/r', 7, fake)
        self.assertEqual(review.status('coderabbit', evs, NOW - timedelta(minutes=5)), 'limited')

    def test_no_reviewer_comes_so_a_local_review_is_posted(self):
        with tempfile.TemporaryDirectory() as d:
            fake = FakeGh()
            said = []
            done = SimpleNamespace(returncode=0, stdout='- `x:1` - a bug - fix it', stderr='')
            with mock.patch.object(review.workers, 'available', return_value=(True, 'cheap')), \
                    mock.patch.object(review.subprocess, 'run', return_value=done) as run:
                rc = review.review(self.cfg(d), 7, 'o/r', gh_fn=fake, say=said.append, sleep=lambda s: None)
            self.assertEqual(rc, 0)
            self.assertIn('copilot will not come', said[0])
            self.assertIn('no automated review will come', ' '.join(said))
            self.assertEqual(run.call_args.args[0][0], 'cheap')
            posted = [c for c in fake.calls if c[:2] == ['pr', 'comment'] and '--body-file' in c]
            self.assertEqual(len(posted), 1)

    def test_a_review_that_arrives_ends_the_wait(self):
        with tempfile.TemporaryDirectory() as d:
            later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
            fake = FakeGh(copilot_ok=True, events=[{'user': {'login': 'copilot-pull-request-reviewer[bot]'}, 'submitted_at': later,
                                                     'body': 'looks fine'}])
            said = []
            rc = review.review(self.cfg(d, ('copilot',)), 7, 'o/r', timeout_minutes=1, gh_fn=fake, say=said.append, sleep=lambda s: None)
            self.assertEqual(rc, 0)
            self.assertIn('copilot reviewed', said[-1])

    def test_a_silently_dropped_request_falls_back_to_graphql(self):
        fake = FakeGh(dropped=True, graphql_ok=True)
        self.assertEqual(review.request('o/r', 7, 'copilot', fake), '')
        self.assertEqual(fake.calls[0][-1], 'reviewers[]=Copilot')
        self.assertTrue(any(c[:2] == ['api', 'graphql'] for c in fake.calls))

    def test_a_request_github_never_lists_says_where_to_look(self):
        why = review.request('o/r', 7, 'copilot', FakeGh(dropped=True))
        self.assertIn('did not list Copilot', why)
        self.assertIn('Settings > Copilot > Code review', why)
        self.assertFalse(review.copilot_listed('not json'))

    def test_no_fallback_worker_says_review_it_yourself(self):
        with tempfile.TemporaryDirectory() as d:
            said = []
            with mock.patch.object(review.workers, 'available', return_value=(False, 'cheap is not on PATH')):
                rc = review.review(self.cfg(d, ()), 7, 'o/r', gh_fn=FakeGh(), say=said.append)
            self.assertEqual(rc, 1)
            self.assertIn('review the diff yourself', said[-1])

    def test_bad_review_config(self):
        cfg = config.normalise({'review': {'reviewers': ['someone'], 'fallback': ['nobody'], 'timeout_minutes': -1}})
        errs = '\n'.join(config.validate(cfg))
        for key in ('review.reviewers', 'review.fallback', 'review.timeout_minutes'):
            self.assertIn(key, errs)


if __name__ == '__main__':
    unittest.main()


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config.normalise({'project': {'state': self.tmp.name}, 'dashboard': {
            'watch_scripts': ['nuc-test.sh'], 'watch_agents': ['agy'], 'task_globs': [os.path.join(self.tmp.name, 'tasks', '*.output')]}})

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_running_nuc_test_shows_as_running_with_progress(self):
        from tutti import discover, state
        script, log = os.path.join(self.tmp.name, 'nuc-test.sh'), os.path.join(self.tmp.name, 'run.log')
        with open(script, 'w', encoding='utf8') as f:
            f.write('echo "[150/177] e2e/specs/x.spec.ts"\nsleep 30\n')
        with open(log, 'w', encoding='utf8') as out:
            p = subprocess.Popen(['bash', script, 'track', 'npx playwright test'], stdout=out, stderr=out)
        try:
            for _ in range(50):
                with open(log, encoding='utf8') as f:
                    if f.read():
                        break
                import time as t
                t.sleep(0.05)
            jobs = state.jobs_state(self.cfg, __import__('time').time())['items']
            mine = [j for j in jobs if j['pid'] == p.pid]
            self.assertEqual(len(mine), 1)
            self.assertEqual((mine[0]['kind'], mine[0]['status'], mine[0]['log']), ('script', 'running', log))
            self.assertEqual(mine[0]['progress']['pct'], 85)
            self.assertIn('nuc-test.sh track npx playwright test', mine[0]['title'])
            self.assertNotIn(p.pid, [j['pid'] for j in discover.script_jobs(config.normalise({}), 0, 600)])   # not watched: not shown
        finally:
            p.kill()
            p.wait()

    def test_programs_interpreters_and_wrappers(self):
        from tutti import discover
        self.assertEqual(discover.program(['bash', '/x/nuc-test.sh', 't']), ('nuc-test.sh', 2))
        self.assertEqual(discover.program(['/usr/bin/agy', '-p', 'secret prompt']), ('agy', 1))
        self.assertEqual(discover.program(['bash', '-c']), ('bash', 2))

    def test_an_agent_shows_program_and_folder_but_never_its_prompt(self):
        from tutti import discover
        j = discover.agent_job('/proc', os.getpid(), 'agy', 0)
        self.assertTrue(j['title'].startswith('agy in '))
        self.assertEqual(j['tail'], [])

    def test_registered_jobs_and_their_result(self):
        from tutti import discover
        folder = os.path.join(self.tmp.name, 'jobs')
        log = os.path.join(self.tmp.name, 'j.log')
        with open(log, 'w', encoding='utf8') as f:
            f.write('stage 2 of 4\n')
        discover.register(folder, os.getpid(), 'ship', log)
        j = discover.registered_jobs(folder, __import__('time').time(), 600, lambda pid: True)[0]
        self.assertEqual((j['status'], j['progress']['pct']), ('running', 50))
        self.assertTrue(discover.finish(folder, os.getpid(), 1))
        self.assertEqual(discover.registered_jobs(folder, __import__('time').time(), 600, lambda pid: False)[0]['status'], 'failed')
        self.assertFalse(discover.finish(folder, 1, 0))

    def test_task_outputs_show_time_only(self):
        from tutti import discover
        os.makedirs(os.path.join(self.tmp.name, 'tasks'))
        with open(os.path.join(self.tmp.name, 'tasks', 'b1.output'), 'w', encoding='utf8') as f:
            f.write('token=abc [3/4]\n')
        j = discover.task_jobs(self.cfg, __import__('time').time())[0]
        self.assertEqual((j['kind'], j['status'], j['tail']), ('agent task', 'running', []))
        self.assertEqual(j['progress']['pct'], 75)

    def test_remote_containers_become_jobs(self):
        from tutti import state
        jobs = state.remote_jobs([{'name': 'nuc', 'containers': ['hcm-nuc-x-1 3 minutes ago']}])
        self.assertEqual((jobs[0]['kind'], jobs[0]['title']), ('remote', 'hcm-nuc-x-1 on nuc (started 3 minutes ago)'))


class AccountsTest(unittest.TestCase):
    def test_a_disabled_account_is_gone_everywhere_but_its_readings_stay(self):
        cfg = config.normalise({'accounts': {'claude': {'window': '7d'}, 'premium': {'enabled': False, 'window': '7d', 'worker': 'x'}}})
        self.assertEqual(list(cfg['accounts']), ['claude'])
        self.assertEqual(cfg['disabled_accounts'], ['premium'])

    def test_one_reading_file_for_several_accounts(self):
        text = json.dumps({'claude': {'used': 7, 'resets': '2026-11-21T01:59Z'}, 'claude-fable': 0})
        self.assertEqual(readers.parse_reading_file(text, 'claude')[0], 7.0)
        self.assertEqual(readers.parse_reading_file(text, 'claude-fable')[0], 0.0)
        with self.assertRaises(ValueError):
            readers.parse_reading_file(text, 'cursor')
        with tempfile.TemporaryDirectory() as d:
            f, log = os.path.join(d, 'c.json'), os.path.join(d, 'u.jsonl')
            with open(f, 'w', encoding='utf8') as fh:
                fh.write(text)
            written, _ = readers.record_files(log, {'claude': {'reading_file': f}, 'claude-fable': {'reading_file': f}})
            self.assertEqual(sorted(e['account'] for e in written), ['claude', 'claude-fable'])

    def test_one_bookmarklet_per_page_reads_every_meter(self):
        cfg = config.normalise({'accounts': {
            'cursor': {'usage_url': 'https://cursor.example/spending', 'helper_label': 'Cursor Models'},
            'cursor-other': {'usage_url': 'https://cursor.example/spending', 'helper_label': 'Other Models'}}})
        pages = helper.helper_pages(cfg)
        self.assertEqual(pages, {'https://cursor.example/spending': [{'account': 'cursor', 'label': 'Cursor Models'},
                                                                      {'account': 'cursor-other', 'label': 'Other Models'}]})
        self.assertIn('Other Models', helper.bookmarklet(pages['https://cursor.example/spending'], 'K', 'http://127.0.0.1:1'))


class CopilotTest(unittest.TestCase):
    def test_ai_credit_lines_are_summed(self):
        lines = ['done', 'AI Credits 6.84 (3s)', 'Tokens ↑ 27.3k • ↓ 1.2k', 'AI Credits 1.16 (1s)']
        self.assertEqual(readers.credits_in(lines), 8.0)
        self.assertEqual(readers.credits_in(['Tokens ↑ 27.3k']), 0)

    def test_premium_request_report(self):
        data = {'usageItems': [{'product': 'Copilot', 'grossQuantity': 30}, {'product': 'Actions', 'grossQuantity': 999},
                               {'product': 'Copilot', 'grossQuantity': 15}]}
        used, reset = readers.github_premium(data, 300, NOW)
        self.assertEqual((used, reset.isoformat()), (15.0, '2026-12-01T00:00:00+00:00'))
        with self.assertRaises(ValueError):
            readers.github_premium({'message': 'Not Found'}, 300, NOW)

    def test_a_missing_scope_says_the_command_and_never_a_token(self):
        err = 'gh: This API operation needs the "user" scope. To request it, run:  gh auth refresh -h github.com -s user\ntoken: gho_x'
        hint = readers.sign_in_hint(err)
        self.assertIn('gh auth refresh -h github.com -s user', hint)
        self.assertNotIn('gho_', hint)

    def test_runs_are_counted_per_worker_today_and_outside_agents_flagged(self):
        from tutti import discover
        with tempfile.TemporaryDirectory() as d:
            cfg = config.normalise({'project': {'state': d, 'worktrees': d}})
            now = 1794700000.0
            delegate.log_run(cfg, 'agy', 'agy-claude', 0, 'x', ['AI Credits 2'], now=now)
            delegate.log_run(cfg, 'agy', 'agy-claude', 1, None, [], now=now)
            delegate.log_run(cfg, 'cursor', 'cursor', 0, None, [], now=now - 86400)
            self.assertEqual(delegate.runs_today(cfg, now), {'agy': 2})
            self.assertTrue(discover.started_by_tutti(cfg, os.path.join(d, 'tutti-run-1-1')))
            self.assertTrue(discover.started_by_tutti(cfg, '/tmp/tutti-review-abc'))
            self.assertFalse(discover.started_by_tutti(cfg, '/home/someone/project'))
