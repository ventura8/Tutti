"""Token and runtime savings: compacted output for agents, shared ssh connections, automatic compaction of a context."""
import io
import os
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
        open(remote.mux_path(self.m), 'w').close()   # the master's socket
        opts = remote.mux_opts(self.m)
        self.assertIn('ControlMaster=no', opts)
        self.assertEqual(remote.argv(self.m, 'true')[-2:], ['box.example', 'true'])
        self.assertIn(f'ControlPath={remote.mux_path(self.m)}', remote.argv(self.m, 'true'))
        self.assertIn('ControlMaster=no', remote.rsh({}, self.m))
        self.assertFalse(remote.ensure_master(self.m, spawn=lambda argv, **kw: spawned.append(argv)))

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

    def test_running_jobs_show_their_context(self):
        jobs = [{'facts': {'tokens': {'in': 900, 'out': 1, 'context': 50000}}},
                {'facts': {'worker': 'cursor'}, 'tail': ['Context 42% used']}, {'facts': {}, 'tail': ['nothing']}]
        autocompact.tag_jobs(self.cfg, jobs)
        self.assertEqual([j['facts'].get('context_pct') for j in jobs], [25.0, 42.0, None])


class SimpleCollector:
    def __init__(self, cfg):
        self.cfg = cfg


if __name__ == '__main__':
    unittest.main()
