"""The compute work queue (tutti/workq.py): due jobs by project priority on free machines (share, quiet hours), heavy ones on
the machine with the most cores, a first failure run again (flaky hunting), findings and failures as backlog tasks; and the
dashboard's /api/vpn body. A throwaway repository and fake spawns only."""
import os
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

from tests.test_autopilot import Base
from tutti import backlog, vpncmd, workq

NOON = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


def rows():
    return [{'name': 'nuc', 'status': 'free', 'free': 2, 'cores': 16}, {'name': 'laptop', 'status': 'free', 'free': 1, 'cores': 8},
            {'name': 'pc', 'status': 'busy', 'free': 0, 'cores': 32}]


class WqBase(Base):
    def wq_cfg(self, **extra):
        cfg = self.cfg()
        cfg['machines'].update({'laptop': {'name': 'laptop', 'transport': 'local', 'slots': 1},
                                'pc': {'name': 'pc', 'transport': 'ssh', 'slots': 2}})
        cfg['routing']['order'] = ['laptop', 'nuc', 'pc']
        cfg['projects'] = {'Map': {'priority': 2}, 'Tutti': {'priority': 0}}
        cfg['work_queue'] = {'enabled': True, 'jobs': [], **extra}
        return cfg


class Plan(WqBase):
    def test_heavy_jobs_go_to_the_most_cores_and_light_ones_by_routing(self):
        cfg = self.wq_cfg()
        heavy, light = {'name': 'static', 'run': 'x', 'heavy': True}, {'name': 'unit', 'run': 'y'}
        got = workq.assign(cfg, [heavy, light], rows(), set(), NOON)
        self.assertEqual([(j['name'], m) for j, m in got], [('static', 'nuc'), ('unit', 'laptop')])   # the busy pc takes none
        self.assertEqual(workq.assign(cfg, [light], rows(), {'nuc', 'laptop'}, NOON), [])          # one queue job per machine

    def test_quiet_hours_and_a_used_up_share_keep_a_machine_out(self):
        cfg = self.wq_cfg()
        cfg['machines']['nuc']['quiet_hours'] = ['00:00-23:59']
        got = workq.assign(cfg, [{'name': 'a', 'run': 'x', 'heavy': True}], rows(), set(), NOON)
        self.assertEqual(got[0][1], 'laptop')
        shared = [{**r, 'free': 0, 'status': 'busy'} for r in rows()]   # use_pct already capped the free slots to 0
        self.assertEqual(workq.assign(cfg, [{'name': 'a', 'run': 'x'}], shared, set(), NOON), [])

    def test_a_heavy_job_here_waits_in_this_computers_limited_window_and_a_light_one_runs(self):
        cfg = self.wq_cfg()
        here_heavy, here_light = {'name': 'h', 'run': 'x', 'where': 'here', 'heavy': True}, {'name': 'l', 'run': 'y', 'where': 'here'}
        limited = [{**r, 'limited': r['name'] == 'laptop'} for r in rows()]   # laptop is this computer (transport local)
        self.assertEqual([j['name'] for j, _ in workq.assign(cfg, [here_heavy, here_light], limited, set(), NOON)], ['l'])
        self.assertEqual([j['name'] for j, _ in workq.assign(cfg, [here_heavy], rows(), set(), NOON)], ['h'])

    def test_findings_are_matched_in_the_whole_log_not_only_its_tail(self):
        log = os.path.join(self.tmp.name, 'run.log')
        with open(log, 'w', encoding='utf8') as f:
            f.write('src/a.ts | 71.5 % | FAIL\n' + 'noise\n' * (workq.TAIL * 2))
        job = {'name': 'tests', 'findings': {'pattern': r'^(?P<file>\S+) \| (?P<pct>[\d.]+) % .*FAIL$'}}
        titles = [t for t, _, _ in workq.tasks_of(job, 'pass', 'abc', workq.log_of(log))]
        self.assertEqual(titles, ['Raise the coverage of src/a.ts'])
        self.assertEqual(workq.log_of(log, limit=6), 'noise\n')   # the bound keeps the end
        self.assertEqual(workq.log_of(os.path.join(self.tmp.name, 'missing')), '')

    def test_the_most_important_project_goes_first(self):
        cfg = self.wq_cfg()
        items = [{'name': 'map', 'run': 'x', 'project': 'Map'}, {'name': 'tutti', 'run': 'y', 'project': 'Tutti'}]
        self.assertEqual([j['name'] for j in workq.order(cfg, items, {})][0], 'tutti')

    def test_due_by_commit_period_and_rerun(self):
        self.assertTrue(workq.due({'every': 'commit'}, None, 'abc', 0))
        self.assertFalse(workq.due({'every': 'commit'}, {'commit': 'abc', 'at': 0}, 'abc', 10))
        self.assertTrue(workq.due({'every': 'commit'}, {'commit': 'abc', 'rerun': True}, 'abc', 10))
        self.assertFalse(workq.due({'every': '6h'}, {'at': 0}, 'abc', 3600))
        self.assertTrue(workq.due({'every': '6h'}, {'at': 0}, 'abc', 6 * 3600))

    def test_a_moved_head_starts_afresh_and_a_lost_run_files_nothing(self):
        last = {'commit': 'old', 'rerun': True}
        self.assertTrue(workq.due({'every': 'commit'}, last, 'new', 10))     # a new commit is a fresh first run
        self.assertEqual(workq.verdict(0, last, 'new'), ('pass', False))     # not "flaky" on another commit
        self.assertEqual(workq.verdict(None, last, 'old'), ('lost', False))  # no exit status: no verdict, no task

    def test_bad_findings_are_refused_and_never_break_a_round(self):
        cfg = self.wq_cfg(jobs=[{'name': 'a', 'run': 'x', 'findings': {'pattern': '(?P<file>[', 'title': '{file}'}},
                                {'name': 'b', 'run': 'x', 'findings': {'pattern': '(?P<file>\\S+)', 'title': '{pct} {file}'}}])
        errs = '\n'.join(workq.errors(cfg))
        self.assertIn('does not compile', errs)
        self.assertIn('no group for', errs)
        job = {'name': 'b', 'findings': {'pattern': '(?P<file>\\S+)', 'title': '{pct}'}}
        self.assertEqual(workq.tasks_of(job, 'pass', 'abc', ''), [])       # refused config: nothing, no exception

    def test_a_routed_job_runs_through_tutti_test_on_its_machine(self):
        cfg = self.wq_cfg()
        argv = workq.argv_of(cfg, {'name': 'map static', 'run': 'bash ci.sh'}, 'nuc', '/wt', '/s/rc')
        self.assertIn("test --on nuc --dir /wt workq-map-static 'bash ci.sh'", argv[2])
        self.assertTrue(argv[2].endswith("; echo $? > /s/rc"))

    def test_a_failure_is_run_again_then_named_flaky_or_failing(self):
        self.assertEqual(workq.verdict(1, {}), ('retry', True))
        self.assertEqual(workq.verdict(0, {'rerun': True}), ('flaky', False))
        self.assertEqual(workq.verdict(1, {'rerun': True}), ('fail', False))
        job = {'name': 'tests', 'findings': {'pattern': r'^(?P<file>\S+) \| (?P<pct>[\d.]+) % .*FAIL$'}}
        tail = 'src/a.ts | 71.5 % | 70 | 60 | FAIL\nsrc/b.ts | 99 % | 99 | 99 | ok\nexit 1'
        titles = [t for t, _, _ in workq.tasks_of(job, 'fail', 'abcdef1234', tail)]
        self.assertEqual(titles[0], 'Raise the coverage of src/a.ts')
        self.assertIn('fails on abcdef1', titles[1])

    def test_bad_jobs_are_named_with_the_fix(self):
        cfg = self.wq_cfg(jobs=[{'name': 'a'}, {'name': 'b', 'run': 'x', 'where': 'mars', 'every': 'often'},
                                {'name': 'c', 'run': 'x', 'findings': {'pattern': 'no group'}}])
        errs = '\n'.join(workq.errors(cfg))
        self.assertIn('needs name: and run:', errs)
        self.assertIn('use route, here', errs)
        self.assertIn('use commit', errs)
        self.assertIn('(?P<file>', errs)


class Round(WqBase):
    def repo(self):
        path = os.path.join(self.tmp.name, 'repo')
        os.makedirs(path)
        subprocess.run(['git', 'init', '-q', path], check=True)
        with open(os.path.join(path, 'a.txt'), 'w', encoding='utf8') as f:
            f.write('x\n')
        subprocess.run(['git', '-C', path, 'add', '-A'], check=True)
        subprocess.run(['git', '-C', path, '-c', 'user.email=t@example', '-c', 'user.name=t',
                        '-c', 'maintenance.auto=false', '-c', 'gc.auto=0', 'commit', '-qm', 'x'], check=True)
        return path

    def test_a_round_starts_collects_reruns_and_files_the_failure(self):
        repo = self.repo()
        cfg = self.wq_cfg(jobs=[{'name': 'check', 'run': 'false', 'repo': 'app', 'where': 'here', 'project': 'Tutti'}])
        cfg['autopilot']['repos']['app']['path'] = repo
        started = []

        def spawn(argv, **k):
            started.append(argv)
            subprocess.run(argv, check=False)   # run it now: it writes its rc file
            return SimpleNamespace(pid=999999)
        events = workq.tick(cfg, rows(), 100.0, spawn=spawn, is_alive=lambda pid: False)
        self.assertIn('check started on here', events[0])
        events = workq.tick(cfg, rows(), 200.0, spawn=spawn, is_alive=lambda pid: False)
        self.assertTrue(any('retry' in e for e in events))                    # the first failure is run again
        self.assertEqual(len(started), 2)
        events = workq.tick(cfg, rows(), 300.0, spawn=spawn, is_alive=lambda pid: False)
        self.assertTrue(any(': fail' in e for e in events))
        titles = [t['title'] for t in backlog.load(cfg)['tasks']]
        self.assertTrue(any('work-queue job check fails' in t for t in titles))
        self.assertIn('check', workq.describe(workq.view(cfg)))

    def test_a_pass_after_a_failure_files_a_flaky_task_and_a_lost_run_reruns(self):
        repo = self.repo()
        flag = os.path.join(self.tmp.name, 'flag')
        cfg = self.wq_cfg(jobs=[{'name': 'wobbly', 'run': f'test -e {flag} || {{ touch {flag}; false; }}', 'repo': 'app',
                                 'where': 'here'}])
        cfg['autopilot']['repos']['app']['path'] = repo

        def spawn(argv, **k):
            subprocess.run(argv, check=False)
            return SimpleNamespace(pid=999999)
        for t in (100.0, 200.0, 300.0):
            events = workq.tick(cfg, rows(), t, spawn=spawn, is_alive=lambda pid: False)
        self.assertTrue(any(': flaky' in e for e in events))
        self.assertTrue(any(t['title'].startswith('Flaky: the work-queue job wobbly') for t in backlog.load(cfg)['tasks']))
        lost = workq.tick(cfg, rows(), 400.0, spawn=lambda argv, **k: SimpleNamespace(pid=999999), is_alive=lambda pid: False)
        self.assertEqual(lost, [])                                         # a new period has not come: nothing due

    def test_a_worktree_that_cannot_be_made_is_an_event_not_a_task(self):
        cfg = self.wq_cfg(jobs=[{'name': 'x', 'run': 'true', 'repo': 'app', 'where': 'here'}])
        cfg['autopilot']['repos']['app']['path'] = self.repo()

        def run(argv, **k):
            if 'worktree' in argv:
                return SimpleNamespace(returncode=128, stdout='', stderr='fatal: no space')
            return subprocess.run(argv, **k)
        events = workq.tick(cfg, rows(), 1.0, spawn=self.fail, run=run)
        self.assertIn('not started: git worktree add failed', events[0])
        self.assertEqual(backlog.load(cfg)['tasks'], [])


class VpnBody(Base):
    def test_the_raw_body_is_read_as_json_and_a_bad_one_is_refused(self):
        cfg = self.cfg()
        code, answer = vpncmd.api(cfg, b'{"action": "status"}')
        self.assertEqual(code, 200)
        self.assertIn('phones', answer)
        self.assertEqual(vpncmd.api(cfg, b'not json')[0], 400)
