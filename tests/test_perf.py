"""Token and runtime savings: compacted output for agents, shared ssh connections, automatic compaction of a context."""
import io
import os
import socket
import tempfile
import unittest
from unittest import mock

from tutti import autocompact, autopilot, compact, config, controls, dashboard, delegate, remote, workers


class CompactTest(unittest.TestCase):
    def test_noise_goes_and_every_distinct_line_stays(self):
        text = ('\x1b[32mstarting\x1b[0m\n'
                'tutti: w5090 is lent out: checking again in 60 s\nw5090 is lent out: checking again in 60 s\n'
                'tutti: w5090 is lent out: checking again in 61 s\nw5090 is lent out: checking again in 61 s\n'
                '\n10%\r50%\r100%\nFAILED test_x: assert 1 == 2\n')
        out = compact.compact(text)
        self.assertEqual(out.splitlines(), ['starting', 'w5090 is lent out: checking again in 61 s  (x2)', '100%',
                                            'FAILED test_x: assert 1 == 2'])
        self.assertLess(compact.tokens(out), compact.tokens(text))

    def test_a_long_output_keeps_the_errors_and_the_end(self):
        lines = [f'line {i} ok {"x" * (i % 7)}' for i in range(400)]
        lines[50] = 'Error: the build broke in step 3'
        out = compact.compact('\n'.join(lines), 600)
        self.assertLessEqual(len(out), 600)
        self.assertIn('Error: the build broke in step 3', out)
        self.assertIn('line 399', out)
        self.assertIn('left out', out)

    def test_empty_and_short_output(self):
        self.assertEqual(compact.compact(''), '')
        self.assertEqual(compact.compact(None), '')
        self.assertEqual(compact.compact('one line'), 'one line')
        self.assertEqual(compact.compact('a\r\nb\r\n1%\r2%\r\n'), 'a\nb\n2%')   # Windows line ends are not redraws

    def test_a_retry_brief_carries_the_reason_once(self):
        task = {'title': 'fix it', 'scope': ['a.py'], 'check': 'make test', 'notes': [],
                'attempts': [{'step': 'agy', 'why': 'check failed: E1\nE1\nE1', 'stat': ' a.py | 2 +-',
                              'tail': 'tutti: wait\nwait\n' + 'E1 boom\n' * 50}]}
        text = autopilot.brief(task, {})
        self.assertIn('failed: check failed: E1\n', text)
        self.assertIn('E1 boom  (x50)', text)
        self.assertEqual(text.count('tutti: wait'), 0)


class SharedSshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(remote.tempfile, 'gettempdir', return_value=self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.m = {'name': 'box', 'transport': 'ssh', 'host': 'box.example'}

    @unittest.skipIf(os.name == 'nt', 'no shared ssh connections on Windows')
    def test_a_master_is_started_once_and_calls_join_it(self):
        spawned = []
        self.assertEqual(remote.mux_opts(self.m), [])   # no master yet: a call connects on its own
        self.assertTrue(remote.ensure_master(self.m, spawn=lambda argv, **kw: spawned.append(argv)))
        self.assertIn('-N', spawned[0])
        self.assertIn('ControlMaster=yes', spawned[0])
        self.assertFalse(remote.ensure_master(self.m, spawn=lambda argv, **kw: spawned.append(argv)))   # within MUX_RETRY
        self.assertEqual(len(spawned), 1)
        master = socket.socket(socket.AF_UNIX)   # the master's socket, listening
        master.bind(remote.mux_path(self.m))
        master.listen(16)
        self.addCleanup(master.close)
        opts = remote.mux_opts(self.m)
        self.assertIn('ControlMaster=no', opts)
        self.assertEqual(remote.argv(self.m, 'true')[-2:], ['box.example', 'true'])
        self.assertIn(f'ControlPath={remote.mux_path(self.m)}', remote.argv(self.m, 'true'))
        self.assertIn('ControlMaster=no', remote.rsh({}, self.m))
        self.assertFalse(remote.ensure_master(self.m, spawn=lambda argv, **kw: spawned.append(argv)))

    @unittest.skipIf(os.name == 'nt', 'no unix sockets for ssh on Windows')
    def test_a_stale_socket_is_removed_and_not_joined(self):
        p = remote.mux_path(self.m)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        s = socket.socket(socket.AF_UNIX)
        s.bind(p)   # bound, never listening: what a killed master leaves
        s.close()
        self.assertEqual(remote.mux_opts(self.m), [])
        self.assertFalse(os.path.exists(p))
        live = socket.socket(socket.AF_UNIX)
        live.bind(p)
        live.listen(16)
        self.addCleanup(live.close)
        self.assertIn('ControlMaster=no', remote.mux_opts(self.m))

    def test_the_retry_claim_is_atomic_and_expires(self):
        lock = os.path.join(self.tmp.name, 'x.try')
        self.assertTrue(remote.claim(lock, clock=lambda: 1000.0))
        self.assertFalse(remote.claim(lock, clock=lambda: os.lstat(lock).st_mtime + 1))   # within MUX_RETRY
        self.assertTrue(remote.claim(lock, clock=lambda: os.lstat(lock).st_mtime + remote.MUX_RETRY + 1))

    @unittest.skipIf(os.name == 'nt', 'POSIX modes')
    def test_a_socket_folder_others_may_open_is_not_used(self):
        d = os.path.join(self.tmp.name, 'open')
        os.makedirs(d)
        os.chmod(d, 0o777)
        self.assertFalse(remote.private_dir(d))
        os.chmod(d, 0o700)
        self.assertTrue(remote.private_dir(d))
        link = os.path.join(self.tmp.name, 'link')
        os.symlink(d, link)
        self.assertFalse(remote.private_dir(link))

    @unittest.skipIf(os.name == 'nt', 'no shared ssh connections on Windows')
    def test_git_gets_a_quoted_command_with_the_shared_socket(self):
        with mock.patch.object(remote, 'mux_opts', return_value=['-o', 'ControlPath=/tmp/a;b']):
            self.assertEqual(remote.rsh({}, self.m), "ssh -o 'ControlPath=/tmp/a;b'")
            self.assertEqual(remote.rsh({}, {**self.m, 'transport': 'android', 'port': 8022}),
                             "ssh -p 8022 -o 'ControlPath=/tmp/a;b'")

    def test_off_for_local_and_when_the_config_says_so(self):
        spawned = []
        for m in ({'transport': 'local'}, {**self.m, 'ssh_reuse': False}):
            self.assertFalse(remote.ensure_master(m, spawn=lambda argv, **kw: spawned.append(argv)))
            self.assertEqual(remote.mux_opts(m), [])
        self.assertEqual(spawned, [])
        self.assertEqual(remote.rsh({}, {**self.m, 'ssh_reuse': False}), 'ssh')



class AutocompactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.normalise({'project': {'state': self.tmp.name}, 'workers': {
            'claude': {'kind': 'cli', 'command': ['claude', '-p', '{prompt}']},
            'cursor': {'kind': 'cli', 'command': ['cursor-agent', '-p', '{prompt}'], 'context_tokens': 1000},
            'agy': {'kind': 'cli', 'command': ['agy', '-p', '{prompt}']},
            'laptop': {'kind': 'ollama', 'url': 'http://laptop.example:11434', 'model': 'm', 'context_tokens': 400}}})

    def test_each_worker_gets_its_own_way(self):
        self.assertEqual({n: autocompact.mode(w) for n, w in self.cfg['workers'].items()},
                         {'claude': 'native', 'cursor': 'handover', 'agy': 'handover', 'laptop': 'tutti'})
        self.assertEqual(autocompact.env_for(self.cfg, self.cfg['workers']['claude']), {'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE': '60'})
        self.assertEqual(autocompact.env_for(self.cfg, self.cfg['workers']['agy']), {})

    def test_the_dashboard_value_beats_the_config_and_null_takes_it_back(self):
        c = SimpleCollector(self.cfg)
        self.assertEqual(dashboard.save_autocompact(c, b'{"threshold_pct": 45}')[0], 200)
        self.assertEqual(autocompact.threshold(self.cfg), 45)
        self.assertEqual(autocompact.view(self.cfg)['from'], 'dashboard')
        self.assertEqual(autocompact.env_for(self.cfg, self.cfg['workers']['claude'])['CLAUDE_AUTOCOMPACT_PCT_OVERRIDE'], '45')
        for bad in (b'{"threshold_pct": 5}', b'{"threshold_pct": "60"}', b'{}', b'nope', b'{"threshold_pct": true}'):
            code, answer = dashboard.save_autocompact(c, bad)
            self.assertEqual(code, 400, bad)
            self.assertIn('threshold_pct', answer['error'])
        self.assertEqual(dashboard.save_autocompact(c, b'{"threshold_pct": null}')[1]['threshold_pct'], 60)
        self.assertEqual(controls.read(self.tmp.name)['settings'], {})

    def test_bad_config_values_are_named(self):
        cfg = config.normalise({'autocompact': {'threshold_pct': 120, 'context_tokens': -1},
                                'workers': {'w': {'kind': 'cli', 'command': ['w'], 'context_tokens': 'big'}}})
        errs = '\n'.join(config.validate(cfg))
        for key in ('autocompact.threshold_pct', 'autocompact.context_tokens', 'workers.w.context_tokens'):
            self.assertIn(key, errs)
        self.assertIn('autocompact is', autocompact.errors({**cfg, 'autocompact': 'x'})[0])

    def test_a_cli_that_shows_its_context_is_watched(self):
        w = autocompact.Watch(self.cfg, self.cfg['workers']['cursor'])
        self.assertFalse(w.line('editing a.py'))
        self.assertFalse(w.line('context: 300 tokens'))   # 30 % of its 1000-token window
        self.assertEqual(w.pct, 30.0)
        self.assertTrue(w.line('Context 61% used'))
        self.assertFalse(autocompact.Watch(self.cfg, self.cfg['workers']['claude']).line('Context 99% used'))   # native: its own
        self.assertEqual(autocompact.seen_percent('context: 1.5k tokens', self.cfg, None), 0.8)

    def test_a_run_over_the_threshold_restarts_with_a_handoff(self):
        calls = []

        def run(cfg, name, task, files, cwd=None, out=None, extra=(), log=None):
            calls.append(task)
            return (workers.COMPACT_RC, ['Context 70% used']) if len(calls) == 1 else (0, ['done'])
        out = io.StringIO()
        rc, lines = delegate.run_compacting(self.cfg, 'cursor', 'fix the bug', [], self.tmp.name, out, (), None, run=run)
        self.assertEqual((rc, lines, len(calls)), (0, ['done'], 2))
        self.assertIn('Continue from this handoff (your context was compacted', calls[1])
        self.assertIn('Context 70% used', calls[1])
        self.assertIn('compacted', out.getvalue())

    def test_a_cli_over_the_threshold_is_stopped(self):
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, 'w.sh')
            with open(script, 'w', encoding='utf8') as f:
                f.write('echo working; echo "Context 80% used"; sleep 20; echo never\n')
            cfg = {**self.cfg, 'workers': {'w': {'kind': 'cli', 'command': ['bash', script, '{prompt}']}}}
            rc, lines = workers.run(cfg, 'w', 'task', cwd=d, out=io.StringIO())
            self.assertEqual(rc, workers.COMPACT_RC)
            self.assertNotIn('never', lines)
    def test_restarts_stop_after_the_limit(self):
        n = []

        def run(*a, **kw):
            n.append(1)
            return workers.COMPACT_RC, []
        rc, _ = delegate.run_compacting(self.cfg, 'cursor', 'task', [], self.tmp.name, io.StringIO(), (), None, run=run)
        self.assertEqual((rc, len(n)), (workers.COMPACT_RC, autocompact.RESTARTS + 1))

    def test_a_local_models_conversation_is_compacted_by_tutti(self):
        w = self.cfg['workers']['laptop']   # a 400-token window: 60 % is 240 tokens
        msgs = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'the goal'}]
        small = msgs + [{'role': 'assistant', 'content': 'RUN: tutti usage'}, {'role': 'user', 'content': 'exit 0'}]
        self.assertIs(autocompact.compact_messages(self.cfg, w, small), small)
        for i in range(4):
            msgs += [{'role': 'assistant', 'content': f'RUN: tutti runs show {i}'}, {'role': 'user', 'content': f'exit 0\n{"x" * 300}'}]
        out = autocompact.compact_messages(self.cfg, w, msgs)
        self.assertEqual(out[:2], msgs[:2])
        self.assertEqual(out[-4:], msgs[-4:])
        self.assertIn('RUN: tutti runs show 0', out[2]['content'])
        self.assertLess(autocompact.message_tokens(out), autocompact.message_tokens(msgs))
        self.assertIs(autocompact.compact_messages({**self.cfg, 'autocompact': {'enabled': False}}, w, msgs), msgs)

    def test_gzip_only_when_the_browser_takes_it(self):
        for accept, ok in (('gzip', True), ('gzip, deflate', True), ('br;q=1, gzip;q=0.5', True), ('*', True),
                           ('gzip;q=0, identity;q=1', False), ('identity', False), ('', False), (None, False),
                           ('*;q=0', False), ('gzip;q=x', False)):
            self.assertEqual(dashboard.takes_gzip(accept), ok, accept)

    def test_the_etag_ignores_the_assembly_time(self):
        t = [0.0]
        c = dashboard.StateCache(clock=lambda: t[0])
        snaps = iter([{'generated_at': 'a', 'n': 1}, {'generated_at': 'b', 'n': 1}, {'generated_at': 'c', 'n': 2}])
        fake = SimpleCollector(None)
        fake.snapshot = lambda: next(snaps)
        first = c.get(fake)[1]
        t[0] += dashboard.STATE_FRESH
        self.assertEqual(c.get(fake)[1], first)
        t[0] += dashboard.STATE_FRESH
        self.assertNotEqual(c.get(fake)[1], first)

    def test_a_reader_answer_it_cannot_parse_never_backs_off(self):
        from tutti import readers
        cfg = {**self.cfg, 'accounts': {'a': {'window': '5h', 'reader': {'command': ['agy', '-p', '/usage'], 'bucket': 'x'}}},
               'dashboard': {**self.cfg['dashboard']}}
        r = readers.Readers(cfg, run=lambda cmd: ({}, None), clock=lambda: 0.0)
        t = [0.0]
        r.clock = lambda: t[0]
        for _ in range(3):
            self.assertIn('accounts.a.reader', r.read_all()['a']['problem'])
            t[0] += 31
        self.assertEqual(r.runs, 3)

    def test_a_gzip_body_is_never_sent_under_a_newer_etag(self):
        c = dashboard.StateCache(clock=lambda: 0.0)
        c.body, c.etag, c.at = b'x' * 2000, 'W/"new"', 0.0
        self.assertIsNone(c.gzipped('W/"old"'))
        self.assertIsNotNone(c.gzipped('W/"new"'))

    def test_a_non_object_answer_does_not_list_copilot(self):
        from tutti import review
        for out in ('null', '[]', '{"requested_reviewers": [null, "x"]}', ''):
            self.assertFalse(review.copilot_listed(out))

    def test_running_jobs_show_their_context(self):
        jobs = [{'facts': {'tokens': {'in': 900, 'out': 1, 'context': 50000, 'model': 'claude-haiku-4-5'}}},
                {'facts': {'worker': 'cursor'}, 'tail': ['Context 42% used']}, {'facts': {}, 'tail': ['nothing']}]
        autocompact.tag_jobs(self.cfg, jobs)
        self.assertEqual([j['facts'].get('context_pct') for j in jobs], [25.0, 42.0, None])

    def test_an_agent_task_context_is_the_newest_turn_against_its_model_window(self):
        # 733k tokens in a 1M-window model is 73 %, not 366 % of a 200k default
        jobs = [{'facts': {'tokens': {'in': 9_000_000, 'context': 733_000, 'model': 'claude-opus-5-5'}}},
                {'facts': {'tokens': {'in': 9_000_000, 'context': 733_000}}},                          # no model, no worker: unknown
                {'facts': {'tokens': {'in': 9_000_000, 'context': 300_000, 'model': 'claude-haiku-4-5'}}}]  # over the window: unknown
        autocompact.tag_jobs(self.cfg, jobs)
        self.assertEqual([j['facts'].get('context_pct') for j in jobs], [73.3, None, None])

    def test_model_windows_by_id_dated_ids_included(self):
        for model, size in (('claude-opus-5-5', 1_000_000), ('claude-sonnet-4-6', 1_000_000), ('claude-haiku-5-5', 1_000_000),
                            ('claude-opus-4-20250514', 200_000), ('claude-sonnet-4-5', 200_000), ('claude-haiku-4-5', 200_000),
                            ('claude-3-5-sonnet-20241022', 200_000), ('gpt-x', None), ('claude-new', None)):
            self.assertEqual(autocompact.model_window(model), size, model)

    def test_the_newest_turn_is_read_even_when_an_earlier_id_repeats(self):
        import json
        import tempfile

        from tutti import jobfacts
        lines = [{'message': {'id': 'a', 'model': 'claude-opus-5-5', 'usage': {'input_tokens': 10}}},
                 {'message': {'id': 'b', 'model': 'claude-opus-5-5', 'usage': {'input_tokens': 500}}},
                 {'message': {'id': 'a', 'model': 'claude-opus-5-5', 'usage': {'input_tokens': 10}}}]
        with tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False) as f:
            f.write('\n'.join(json.dumps(x) for x in lines) + '\n')
        self.addCleanup(os.unlink, f.name)
        out = jobfacts.transcript_tokens(f.name)
        self.assertEqual((out['context'], out['model']), (10, 'claude-opus-5-5'))


class SimpleCollector:
    def __init__(self, cfg):
        self.cfg = cfg


if __name__ == '__main__':
    unittest.main()
