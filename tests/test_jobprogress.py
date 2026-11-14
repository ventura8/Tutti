"""Layered progress for every running job (exact, known shape, estimate from history, agents) and the facts of every job."""
import json
import os
import subprocess
import tempfile
import unittest

from tutti import config, delegate, jobfacts, jobprogress, review, state

NOW = 1_800_000_000.0


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf8') as f:
        f.write(text)
    return path


def cfg_of(folder, **dashboard):
    cfg = config.normalise({'project': {'worktrees': folder, 'state': os.path.join(folder, 'state')}, 'dashboard': dashboard,
                            'accounts': {'agy-claude': {'window': '5h', 'worker': 'agy'}}})
    cfg['_path'] = os.path.join(folder, 'tutti.config.yaml')
    return cfg


def job(**kw):
    base = {'id': 'x:1', 'kind': 'quiet', 'title': 'npm test', 'pid': 1, 'log': None, 'started': None, 'updated': None,
            'idle_seconds': None, 'rc': None, 'status': 'running', 'tail': [], 'progress': None}
    return {**base, **kw}


def iso(ts):
    return state.iso(ts)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.cfg = cfg_of(self.dir)
        os.makedirs(os.path.join(self.dir, 'state'))
        jobprogress._histories.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def one(self, j, now=NOW):
        return jobprogress.annotate(self.cfg, [j], now)[0]


class ExactTest(Base):
    def test_new_counters_in_logs(self):
        p = state.progress_of_line
        self.assertEqual(p('static 3/51 eslint')['pct'], 6)
        self.assertEqual(p('  1,048,576  45%   10.00MB/s    0:01:02')['pct'], 45)
        self.assertEqual(p('Step 3/12 : RUN make')['pct'], 25)
        self.assertEqual(p('slot.sh: waiting for a free slot (all 3 busy)')['source'], 'queue')
        for line in ('on 11/14 at noon', 'coverage 90%', 'a 1/2 b c'):
            self.assertIsNone(p(line), line)

    def test_out_of_the_queue_the_waiting_line_no_longer_counts(self):
        log = write(os.path.join(self.dir, 'a.log'), 'slot.sh: waiting for a free slot (all 3 busy)\nslot.sh: got a slot\nbuilding\n')
        self.assertIsNone(state.progress(log, state.tail(log)))
        log = write(os.path.join(self.dir, 'b.log'), 'slot.sh: waiting for a free slot (all 3 busy)\n')
        self.assertEqual(state.progress(log, state.tail(log))['source'], 'queue')

    def test_exact_layer_label_and_eta(self):
        j = self.one(job(started=iso(NOW - 100), progress={'done': 50, 'total': 100, 'pct': 50, 'source': 'log'}))
        p = j['progress']
        self.assertEqual((p['layer'], p['pct'], p['determinate'], p['eta_seconds']), ('exact', 50, True, 100))
        self.assertIn('a counter in its log', p['label'])

    def test_queue_says_waiting(self):
        p = self.one(job(progress={'done': 0, 'total': 3, 'pct': 0, 'source': 'queue'}))['progress']
        self.assertIn('waiting for a slot: all 3 are busy', p['label'])

    def test_counters_inside_an_agent_transcript_are_not_progress(self):
        path = write(os.path.join(self.dir, 'a1.output'), '{"type":"user","text":"[3/4]"}\n')
        p = self.one(job(kind='agent task', path=path, progress={'done': 3, 'total': 4, 'pct': 75, 'source': 'log'}))['progress']
        self.assertNotEqual(p['layer'], 'exact')


class ShapeTest(Base):
    def test_a_delegated_run_reports_its_phases(self):
        log = write(os.path.join(self.dir, 'state', 'runs', '1-2.log'), 'run: new worktree /x at abc\nworking\n')
        p = self.one(job(kind='run', title='run agy: x', log=log, started=iso(NOW - 60)))['progress']
        self.assertEqual((p['layer'], p['done'], p['total'], p['pct']), ('shape', 1, 3, 33))
        self.assertIn('phase 2 of 3: the worker works', p['label'])
        write(log, 'run: new worktree /x\nrun: checking the change (1 check(s))\n')
        self.assertEqual(self.one(job(kind='run', title='run agy: x', log=log))['progress']['step'], 'checks')

    def test_configured_phases_and_counted_lines(self):
        self.cfg = cfg_of(self.dir, shapes={
            'ship': {'match': r'^ship\.sh', 'phases': [{'name': 'commit'}, {'name': 'checks', 'when': r'^\[targeted\]'},
                                                       {'name': 'push', 'when': '^To '}, {'name': 'deploy', 'when': '^deployed'}]},
            'stage': {'match': r'PIPELINE_STAGE=(\w+)', 'count': r'^\[gate\] ', 'total': 4}})
        log = write(os.path.join(self.dir, 's.log'), '[targeted] ok\nTo github.com:x\n')
        p = self.one(job(kind='script', title='ship.sh feat: x', log=log))['progress']
        self.assertEqual((p['pct'], p['step']), (50, 'push'))
        log2 = write(os.path.join(self.dir, 'g.log'), '[gate] a\n[gate] b\n[gate] c\n')
        p = self.one(job(kind='script', title='nuc-test.sh t PIPELINE_STAGE=static bash x', log=log2))['progress']
        self.assertEqual((p['layer'], p['done'], p['total'], p['pct']), ('shape', 3, 4, 75))

    def test_total_command_is_cached_and_failure_is_none(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout='51\n')
        self.assertEqual(jobprogress.total_of('count {1}', None, ('static',), run=run, clock=lambda: 0), 51)
        self.assertEqual(jobprogress.total_of('count {1}', None, ('static',), run=run, clock=lambda: 10), 51)
        self.assertEqual(calls, ["count static"])
        self.assertIsNone(jobprogress.total_of('nothing', None, (), run=lambda c, **k: subprocess.CompletedProcess(c, 1, stdout='')))

    def test_shape_config_errors(self):
        errs = config._shape_errors({'a': {'phases': []}, 'only': {'match': 'x', 'machine': 'nuc'},
                                     'b': {'match': '(', 'phases': [{'name': 'x'}]},
                                     'c': {'kind': 'run', 'count': 'x'}, 'd': {'kind': 'run', 'phases': [{'when': 'x'}]}})
        text = '\n'.join(errs)
        self.assertNotIn('dashboard.shapes.only', text)
        for want in ('dashboard.shapes.a: give it match', 'dashboard.shapes.b.match', 'dashboard.shapes.c.count: give total',
                     'dashboard.shapes.d.phases[0]'):
            self.assertIn(want, text)
        self.assertEqual(config._shape_errors({'ok': {'kind': 'run', 'phases': [{'name': 'a'}, {'name': 'b', 'when': 'x'}]}}), [])


class EstimateTest(Base):
    def done_job(self, i, seconds):
        return job(id=f'quiet:{i}', status='done', started=iso(NOW - 1000), updated=iso(NOW - 1000 + seconds), rc=0)

    def test_history_gives_an_estimate_capped_and_late(self):
        jobprogress.annotate(self.cfg, [self.done_job(i, s) for i, s in enumerate((100, 200, 300))], NOW)
        p = self.one(job(id='quiet:new', started=iso(NOW - 100)))['progress']
        self.assertEqual((p['layer'], p['pct'], p['eta_seconds']), ('estimate', 50, 100))
        self.assertIn('estimated from 3 earlier runs', p['label'])
        p = self.one(job(id='quiet:new', started=iso(NOW - 500)))['progress']
        self.assertEqual((p['pct'], p['late']), (95, True))
        self.assertIn('taking longer than usual', p['label'])

    def test_history_is_kept_on_disk_and_per_machine(self):
        jobprogress.annotate(self.cfg, [self.done_job(i, 100) for i in range(3)], NOW)
        jobprogress.history(self.cfg).save(force=True)
        jobprogress._histories.clear()
        self.assertEqual(self.one(job(id='quiet:new', started=iso(NOW - 50)))['progress']['layer'], 'estimate')
        other = self.one(job(id='quiet:n2', started=iso(NOW - 50), machine='nuc'))['progress']
        self.assertEqual(other['layer'], 'elapsed')

    def test_a_running_job_that_is_gone_counts_as_finished(self):
        h = jobprogress.history(self.cfg)
        for i in range(2):
            jobprogress.annotate(self.cfg, [job(id=f'proc:{i}', kind='script', title='nuc-test.sh t', started=iso(NOW - 300))], NOW)
            jobprogress.annotate(self.cfg, [], NOW + 10)
        self.assertEqual(h.durations('script nuc-test.sh @ local'), [300, 300])

    def test_a_container_is_not_learned_from_while_its_machine_was_not_probed(self):
        c = job(id='remote:nuc:c1', kind='remote', title='hcm-nuc-x-41 on nuc (started 5 minutes ago)', machine='nuc', pid=None)
        jobprogress.annotate(self.cfg, [dict(c)], NOW)
        jobprogress.annotate(self.cfg, [], NOW + 10)                      # no probe: machines unknown
        self.assertEqual(jobprogress.history(self.cfg).data['runs'], {})
        jobprogress.annotate(self.cfg, [dict(c)], NOW, machines=[{'name': 'nuc', 'reachable': True}])
        jobprogress.annotate(self.cfg, [], NOW + 10, machines=[{'name': 'nuc', 'reachable': True}])
        self.assertEqual(jobprogress.history(self.cfg).durations('remote hcm-nuc @ nuc'), [300])


class AgentTest(Base):
    def test_conductor_handoff_notes(self):
        prog = write(os.path.join(self.dir, 'p.md'), '# Progress\n\n## Done\n\n- a\n- b\n- c\n\n## Next\n\n- d\n')
        write(os.path.join(self.dir, 'state', 'conduct', 'current.json'), json.dumps(
            {'id': 'r1', 'goal': 'g', 'progress': prog, 'status': 'running', 'pid': 7, 'started': iso(NOW - 60),
             'agents': [{'agent': 'agy'}]}))
        j = jobprogress.conduct_job(self.cfg, NOW, lambda pid: True)
        p = self.one(j)['progress']
        self.assertEqual((p['layer'], p['pct'], p['done'], p['total']), ('agent', 75, 3, 4))
        self.assertIsNone(jobprogress.conduct_job(self.cfg, NOW, lambda pid: False))

    def test_agents_without_history_get_a_rough_labelled_estimate(self):
        p = self.one(job(kind='agent', title='agy in /x', started=iso(NOW - 600)))['progress']
        self.assertEqual((p['layer'], p['pct']), ('agent', 50))
        self.assertIn('rough', p['label'])
        p = self.one(job(kind='agent', title='agy in /x', started=iso(NOW - 6000)))['progress']
        self.assertEqual((p['pct'], p['late']), (95, True))
        self.assertIn('taking longer than a typical agent', p['label'])

    def test_services_and_unknown_kinds_show_elapsed_without_a_bar(self):
        p = self.one(job(kind='tunnel', started=iso(NOW - 7200)))['progress']
        self.assertEqual((p['determinate'], p['elapsed_seconds']), (False, 7200))
        self.assertIn('a service', p['label'])
        p = self.one(job(kind='job', title='make', started=iso(NOW - 30)))['progress']
        self.assertEqual((p['layer'], p['pct']), ('elapsed', None))


class FactsTest(Base):
    def test_tests_tokens_files_from_lines(self):
        self.assertEqual(jobfacts.tests(['== 12 passed, 1 failed in 3s ==']), {'passed': 12, 'failed': 1})
        self.assertEqual(jobfacts.tests(['Ran 10 tests in 1s', '', 'FAILED (failures=1, errors=2)']), {'passed': 7, 'failed': 3})
        self.assertEqual(jobfacts.tests([' Tests  12 passed | 1 failed (13)']), {'passed': 12, 'failed': 1})
        self.assertIsNone(jobfacts.tests(['nothing here']))
        self.assertEqual(jobfacts.tokens_in_lines(['Tokens: 12.3k in, 4.1k out']), {'in': 12300, 'out': 4100})
        self.assertEqual(jobfacts.tokens_in_lines(['12,345 input tokens', '400 output tokens']), {'in': 12345, 'out': 400})
        self.assertIsNone(jobfacts.tokens_in_lines(['3 out of 5 tests']))
        self.assertEqual(jobfacts.files_in_lines([' 2 files changed, 5 insertions(+), 1 deletion(-)']), {'files': 2, 'plus': 5, 'minus': 1})

    def test_transcript_tokens_count_each_message_once(self):
        lines = [{'timestamp': '2026-01-01T00:00:00Z', 'type': 'user'},
                 {'message': {'id': 'm1', 'usage': {'input_tokens': 2, 'cache_read_input_tokens': 98, 'output_tokens': 5}}},
                 {'message': {'id': 'm1', 'usage': {'input_tokens': 2, 'cache_read_input_tokens': 98, 'output_tokens': 10}},
                  'content': [{'type': 'tool_use'}]},
                 {'message': {'id': 'm2', 'usage': {'input_tokens': 1, 'output_tokens': 1}}}]
        path = write(os.path.join(self.dir, 'a.output'), '\n'.join(json.dumps(x, separators=(',', ':')) for x in lines) + '\n')
        self.assertEqual(jobfacts.transcript_tokens(path), {'in': 101, 'out': 11, 'calls': 1})
        self.assertEqual(jobfacts.transcript_started(path), jobfacts.seconds('2026-01-01T00:00:00+00:00'))
        self.assertIsNone(jobfacts.transcript_tokens(os.path.join(self.dir, 'missing')))

    def test_a_finished_job_has_no_bar_but_its_result(self):
        j = self.one(job(status='failed', rc=2, started=iso(NOW - 70), updated=iso(NOW - 10), tail=['3 passed, 2 failed'],
                         progress={'done': 1, 'total': 1, 'pct': 100, 'source': 'finished'}))
        self.assertIsNone(j['progress'])
        self.assertEqual({k: j['facts'][k] for k in ('result', 'rc', 'seconds', 'tests')},
                         {'result': 'failed', 'rc': 2, 'seconds': 60, 'tests': {'passed': 3, 'failed': 2}})

    def test_a_delegated_run_names_its_account_files_and_report(self):
        runs = os.path.join(self.dir, 'state', 'runs')
        log = write(os.path.join(runs, '1-2.log'), 'run: new worktree /x\n')
        stat = ' 2 files changed, 2 insertions(+), 2 deletions(-)'
        write(os.path.join(runs, '1-2', 'report.json'), json.dumps({'worker': 'agy', 'stat': stat, 'report': '/r.txt',
                                                                    'checks': [{'rc': 0}, {'rc': 1}]}))
        write(os.path.join(self.dir, 'state', 'runs.jsonl'), json.dumps({'id': '1-2', 'account': 'agy-claude', 'credits': 1.5}) + '\n')
        f = self.one(job(kind='run', title='run agy: x', log=log, status='done', rc=0))['facts']
        self.assertEqual((f['account'], f['credits'], f['files'], f['report'], f['checks']),
                         ('agy-claude', 1.5, {'files': 2, 'plus': 2, 'minus': 2}, '/r.txt', {'passed': 1, 'failed': 1}))

    def test_a_running_run_counts_its_worktree_changes(self):
        def run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout=' 3 files changed, 9 insertions(+)\n')
        self.assertEqual(jobfacts.diff_files(self.dir, clock=lambda: 0, run=run), {'files': 3, 'plus': 9, 'minus': 0})


class EveryRunningJobTest(Base):
    """A realistic state folder: every kind of job Tutti shows. Each running one gets a number (a percentage, or its elapsed
    time) and a label saying where it comes from; no finished one gets a bar."""

    def test_every_running_job_gets_a_number(self):
        s = os.path.join(self.dir, 'state')
        t = int(NOW - 120)
        write(os.path.join(s, 'quiet', f'{t}-101.log'), 'static 3/51 eslint\n')
        write(os.path.join(s, 'quiet', f'{t}-102.log'), 'compiling\n')                                   # nothing countable
        write(os.path.join(s, f'ship-{t}-103-1.log'), '== 4 passed in 1s ==\n')
        write(os.path.join(s, f'ship-{t}-103-1.log.rc'), '0\n')
        write(os.path.join(s, 'files', f'{t}-104.log'), 'sending\n')
        write(os.path.join(s, 'files', f'{t}-104.log.progress'), '512 1024 MB\n')
        write(os.path.join(s, 'runs', f'{t}-105.log'), 'run: new worktree /x\n')
        write(os.path.join(s, 'runs', f'{t}-105.log.cmd'), 'run agy: fix it\n')
        write(os.path.join(s, 'jobs', '106.json'), json.dumps({'pid': 106, 'title': 'deploy', 'log': None, 'started': NOW - 30,
                                                               'rc': None}))
        write(os.path.join(s, 'llm-busy-107'), '')
        write(os.path.join(s, 'tunnel-pc.pid'), '108')
        write(os.path.join(s, 'waits', '109.json'), json.dumps({'kind': 'pid', 'target': '1', 'started': NOW - 10}))
        prog = write(os.path.join(s, 'conduct', 'r', 'progress.md'), '## Done\n\n- a\n\n## Next\n\n- b\n')
        write(os.path.join(s, 'conduct', 'current.json'), json.dumps({'id': 'r', 'goal': 'g', 'progress': prog, 'status': 'running',
                                                                      'pid': 110, 'started': iso(NOW - 90)}))
        task = write(os.path.join(self.dir, 'tasks', 'a1.output'), json.dumps({'timestamp': iso(NOW - 200), 'message': {
            'id': 'm', 'usage': {'input_tokens': 10, 'output_tokens': 3}}}, separators=(',', ':')) + '\n')
        os.utime(task, (NOW - 5, NOW - 5))
        self.cfg = cfg_of(self.dir, task_globs=[os.path.join(self.dir, 'tasks', '*.output')])
        nuc = {'name': 'nuc', 'reachable': True, 'free': 1, 'slots': 3, 'status': 'free', 'containers': ['hcm-nuc-x-1 4 minutes ago']}
        probes = {'machines': [nuc], 'machines_at': NOW}
        from datetime import datetime, timezone
        out = state.assemble(self.cfg, probes, now=datetime.fromtimestamp(NOW, timezone.utc), is_alive=lambda pid: True)
        items = out['jobs']['items']
        running = [j for j in items if j['status'] in ('running', 'stalled')]
        kinds = {j['kind'] for j in running}
        every = {'quiet', 'files', 'run', 'job', 'local model', 'tunnel', 'wait', 'conduct', 'agent task', 'remote'}
        self.assertTrue(every <= kinds, kinds)
        for j in running:
            p = j['progress']
            self.assertTrue(p['pct'] is not None or p['elapsed_seconds'] is not None, j['id'])
            self.assertTrue(p['label'], j['id'])
            self.assertIn(p['layer'], ('exact', 'shape', 'estimate', 'agent', 'elapsed'))
            self.assertEqual(p['determinate'], p['pct'] is not None)
        by_kind = {j['kind']: j for j in running}
        self.assertEqual(by_kind['files']['facts']['copied'], {'done': 512, 'total': 1024, 'unit': 'MB'})
        self.assertEqual(by_kind['agent task']['facts']['tokens'], {'in': 10, 'out': 3, 'calls': 0})
        self.assertEqual(by_kind['run']['progress']['layer'], 'shape')
        self.assertEqual(by_kind['conduct']['progress']['pct'], 50)
        done = [j for j in items if j['status'] == 'done']
        self.assertTrue(done and all(j['progress'] is None and j['facts']['result'] == 'passed' for j in done))


class ChecksForTest(Base):
    def repo(self, name, files=()):
        top = os.path.join(self.dir, name)
        subprocess.run(['git', 'init', '-q', top], check=True)
        for f, text in files:
            write(os.path.join(top, f), text)
        return top

    def test_checks_belong_to_the_repository(self):
        self.cfg['ship']['checks'] = [{'run': 'npx tsc'}]
        self.cfg['projects'] = {'Map': {'match': 'map-app'}, 'Tool': {'match': 'tool', 'checks': [{'run': 'bash scripts/check.sh'}]}}
        self.assertEqual(delegate.checks_for(self.cfg, self.repo('tool'))[0], [{'run': 'bash scripts/check.sh'}])
        self.assertEqual(delegate.checks_for(self.cfg, self.repo('map-app'))[0], [{'run': 'npx tsc'}])   # no delegate.project: as before
        self.cfg['delegate']['project'] = 'Map'
        other = self.repo('elsewhere')
        chosen, why = delegate.checks_for(self.cfg, other)
        self.assertEqual(chosen, [])
        self.assertIn('the default checks are for Map', why)
        own = self.repo('withcfg', [('tutti.config.yaml', 'ship: {checks: [{run: make test}]}\n')])
        self.assertEqual(delegate.checks_for(self.cfg, own)[0], [{'run': 'make test'}])
        bad = self.repo('badcfg', [('tutti.config.yaml', 'ship: [1\n')])
        self.assertIn('not a valid config', delegate.checks_for(self.cfg, bad)[1])


class GhPagesTest(unittest.TestCase):
    def test_pages_back_to_back_without_slurp(self):
        self.assertEqual(review.documents('[1, 2]\n[3]\n'), [[1, 2], [3]])
        self.assertEqual(review.documents(''), [])
        with self.assertRaises(ValueError):
            review.documents('[1')


if __name__ == '__main__':
    unittest.main()
