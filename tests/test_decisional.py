"""The decisional model (tutti/decisional.py, backlog t440): the best free model for Tutti's judgement steps on the strongest
suitable machine, config-driven (no machine or model name in the code), fetched when missing, asked first by the judgement
purposes through tutti/brain.py, and never forced on a busy, paused or unmeasured machine. Servers and models are fakes."""
import unittest

import tmpdirs

from tutti import brain, config, decisional, fleet, power


def model(name, params):
    return fleet.classify({'name': name, 'details': {'parameter_size': f'{params}B', 'quantization_level': 'Q4_K_M'}})


class Base(unittest.TestCase):
    def setUp(self):
        self.cfg = {'project': {'state': tmpdirs.mkdtemp()}, 'ai_assist': {'enabled': True, 'local': True, 'online': False},
                    'machines': {'laptop': {'transport': 'local'}, 'nuc': {'transport': 'ssh', 'host': 'nuc.example'},
                                 'pc': {'transport': 'ssh', 'host': 'pc.example'}, 'phone': {'transport': 'android'}},
                    'workers': {}}
        self.bench = {'pc': {'vram_gb': 32, 'mem_gb': 64}, 'nuc': {'vram_gb': 8, 'mem_gb': 32}, 'laptop': {'vram_gb': 0, 'mem_gb': 16}}
        self.fleet = {'machines': {}}

    def plan(self, **over):
        self.cfg['decisional'] = over
        return decisional.plan(self.cfg, self.fleet, self.bench)


class Pick(Base):
    def test_the_strongest_machine_takes_the_best_candidate_that_fits(self):
        t = self.plan()['target']
        self.assertEqual((t['machine'], t['model'], t['placement'], t['installed']), ('pc', 'qwen3:32b', 'gpu', False))
        self.assertEqual(t['need_gb'], 21.2)

    def test_a_bigger_card_gets_a_bigger_model_from_the_same_config(self):
        self.bench['pc']['vram_gb'] = 48
        self.assertEqual(self.plan()['target']['model'], 'llama3.3:70b')

    def test_the_candidates_are_the_owners_not_the_codes(self):
        t = self.plan(candidates=[{'model': 'house-judge', 'gb': 10}, 'tiny:3b'])['target']
        self.assertEqual((t['model'], t['need_gb']), ('house-judge', 10.0))
        self.assertEqual(self.plan(model='qwen3:8b')['target']['model'], 'qwen3:8b')

    def test_a_missing_biggest_model_is_the_target_while_an_installed_smaller_one_is_asked_now(self):
        self.fleet['machines']['pc'] = {'models': [model('qwen3:14b', 14)]}
        p = self.plan()
        self.assertEqual((p['target']['model'], p['target']['installed']), ('qwen3:32b', False))
        self.assertEqual((p['usable']['machine'], p['usable']['model']), ('pc', 'qwen3:14b'))
        self.fleet['machines']['pc']['models'].append(model('qwen3:32b', 32))
        self.assertEqual(self.plan()['usable']['model'], 'qwen3:32b')

    def test_a_latest_tag_is_the_same_model(self):
        self.fleet['machines']['pc'] = {'models': [{'name': 'house-judge:latest', 'details': {'parameter_size': '12B'}}]}
        p = self.plan(candidates=[{'model': 'house-judge', 'gb': 8}])
        self.assertTrue(p['usable']['installed'])

    def test_a_paused_machine_is_skipped_and_says_so(self):
        self.cfg['machines']['pc']['paused'] = True
        p = self.plan()
        self.assertEqual((p['target']['machine'], p['target']['model']), ('nuc', 'qwen3:8b'))
        self.assertIn('pc: paused by its owner', p['skipped'])

    def test_a_machine_without_the_graphics_memory_needs_the_ram_switch(self):
        self.bench = {'laptop': {'vram_gb': 0, 'mem_gb': 40}}
        p = self.plan()
        self.assertIsNone(p['target'])
        self.assertRegex(p['skipped'][0], r'laptop: no candidate fits 0 GB of graphics memory .*decisional\.allow_cpu')
        t = self.plan(allow_cpu=True)['target']
        self.assertEqual((t['machine'], t['model'], t['placement']), ('laptop', 'qwen3:32b', 'cpu'))
        self.assertIsNone(self.plan(allow_cpu=True, min_free_gb=40)['target'])

    def test_an_unmeasured_machine_only_counts_for_a_model_it_already_holds(self):
        self.bench = {}
        self.assertIsNone(self.plan()['target'])
        self.assertIn('not measured', self.plan()['skipped'][0])
        self.fleet['machines']['pc'] = {'models': [model('qwen3:14b', 14)]}
        t = self.plan()['target']
        self.assertEqual((t['machine'], t['model'], t['placement']), ('pc', 'qwen3:14b', 'unmeasured'))

    def test_the_owners_hardware_beats_the_benchmark_and_covers_a_machine_that_cannot_be_measured(self):
        t = self.plan(hardware={'pc': {'vram_gb': 96}})['target']
        self.assertEqual((t['machine'], t['model']), ('pc', 'gpt-oss:120b'))

    def test_pinning_a_machine_or_a_model_is_honoured_and_a_bad_pin_says_why(self):
        self.assertEqual(self.plan(host='nuc')['target']['machine'], 'nuc')
        p = self.plan(host='ghost')
        self.assertIsNone(p['target'])
        self.assertIn('decisional.host names "ghost"', p['skipped'][-1])
        self.assertIsNone(self.plan(model='llama3.3:70b')['target'])   # too big for 32 GB: never forced
        self.assertIsNone(self.plan(candidates=[])['target'])

    def test_phones_and_household_machines_never_host_it(self):
        self.cfg['machines']['member'] = {'transport': 'household'}
        self.bench['phone'] = self.bench['member'] = {'vram_gb': 99, 'mem_gb': 99}
        self.assertEqual(self.plan()['target']['machine'], 'pc')

    def test_a_size_less_name_fits_nothing_until_it_says_its_memory(self):
        self.assertEqual(decisional.entry_of('mystery')['need_gb'], decisional.UNKNOWN_GB)
        self.assertEqual(decisional.entry_of({'model': 'mystery', 'gb': 6})['need_gb'], 6.0)
        self.assertEqual(decisional.entry_of({'model': 'mystery', 'gb': -1})['need_gb'], decisional.UNKNOWN_GB)


class Errors(Base):
    def errors(self, **over):
        self.cfg['decisional'] = {**config.DEFAULTS['decisional'], **over}
        return decisional.errors(self.cfg)

    def test_the_defaults_and_a_good_config_are_valid(self):
        self.assertEqual(self.errors(), [])
        self.assertEqual(self.errors(host='pc', model='qwen3:32b', hardware={'pc': {'vram_gb': 32}}, auto_pull=True), [])
        self.cfg.pop('decisional')
        self.assertEqual(decisional.errors(self.cfg), [])

    def test_each_mistake_names_its_key_and_the_fix(self):
        for over, key in ((dict(host='ghost'), 'decisional.host names "ghost"'), (dict(model='a b'), 'decisional.model is'),
                          (dict(candidates='qwen3:32b'), 'decisional.candidates is'), (dict(candidates=['mystery']), 'shows its size'),
                          (dict(candidates=['a b:3b']), 'decisional.candidates has'), (dict(purposes='x'), 'decisional.purposes is'),
                          (dict(hardware={'ghost': {'vram_gb': 8}}), 'decisional.hardware.ghost is not under machines'),
                          (dict(hardware={'pc': {'vram': 8}}), "decisional.hardware.pc.vram"),
                          (dict(hardware={'pc': 8}), 'decisional.hardware.pc is 8'), (dict(hardware=[1]), 'decisional.hardware is'),
                          (dict(min_free_gb=-1), 'decisional.min_free_gb is'), (dict(pull_timeout_minutes='x'), 'pull_timeout_minutes is'),
                          (dict(auto_pull='yes'), 'decisional.auto_pull is')):
            errs = self.errors(**over)
            self.assertTrue(any(key in e for e in errs), (over, errs))
        self.cfg['decisional'] = 'on'
        self.assertIn('decisional is', decisional.errors(self.cfg)[0])


class Pull(Base):
    def setUp(self):
        super().setUp()
        self.cfg['workers'] = {'pc-local': {'kind': 'ollama', 'host': '127.0.0.1:11450', 'model': 'x:1b', 'machine': 'pc'}}
        self.calls = []

    def post(self, reply):
        def fake(host, payload, timeout, path='/api/generate'):
            self.calls.append((host, payload, timeout, path))
            if isinstance(reply, Exception):
                raise reply
            return reply
        return fake

    def target(self):
        return self.plan()['target']

    def test_a_missing_model_is_fetched_through_the_machines_model_server(self):
        ok, text = decisional.pull(self.cfg, self.target(), self.post({'status': 'success'}), now=100.0)
        self.assertTrue(ok)
        self.assertEqual(text, 'qwen3:32b is now on pc')
        self.assertEqual(self.calls, [('127.0.0.1:11450', {'model': 'qwen3:32b', 'stream': False}, 7200.0, '/api/pull')])
        self.assertTrue(decisional.read_state(self.cfg)['pull']['ok'])

    def test_an_installed_model_is_not_fetched_again(self):
        self.fleet['machines']['pc'] = {'models': [model('qwen3:32b', 32)]}
        ok, text = decisional.pull(self.cfg, self.target(), self.post({'status': 'success'}))
        self.assertEqual((ok, text, self.calls), (True, 'qwen3:32b is already on pc', []))

    def test_a_machine_without_ollama_gets_the_operator_step(self):
        self.cfg['workers'] = {}
        ok, text = decisional.pull(self.cfg, self.target(), self.post({'status': 'success'}))
        self.assertFalse(ok)
        self.assertIn('install it there once (https://ollama.com/download', text)
        self.assertIn('tutti decisional pull', text)
        self.assertEqual(self.calls, [])

    def test_a_failed_fetch_says_what_to_check_and_is_remembered(self):
        ok, text = decisional.pull(self.cfg, self.target(), self.post(OSError('disk full')), now=5.0)
        self.assertFalse(ok)
        self.assertIn('was not fetched on pc (OSError: disk full): check its model server and disk', text)
        ok, text = decisional.pull(self.cfg, self.target(), self.post({'error': 'pull failed'}), now=6.0)
        self.assertFalse(ok)
        self.assertIn('Ollama answered', text)
        self.assertFalse(decisional.read_state(self.cfg)['pull']['ok'])

    def test_no_target_is_a_clear_refusal(self):
        self.bench = {}
        ok, text = decisional.pull(self.cfg, self.plan()['target'], self.post({'status': 'success'}))
        self.assertFalse(ok)
        self.assertIn('no machine fits a candidate', text)


class AutoPull(Base):
    def setUp(self):
        super().setUp()
        self.cfg['workers'] = {'pc-local': {'kind': 'ollama', 'host': 'p', 'model': 'x:1b', 'machine': 'pc'}}
        self.started = []
        self.addCleanup(decisional._running.clear)

    def measured(self, bench):
        self.cfg['project']['state'] = tmpdirs.mkdtemp()
        for name, b in bench.items():
            power.save_one(self.cfg, name, b, 1.0)

    def tick(self, quiet=False, **over):
        self.cfg['decisional'] = over
        return decisional.tick(self.cfg, 1000.0, start=self.started.append, pull_fn=lambda *a, **k: (True, ''),
                               quiet_fn=lambda cfg: {'quiet': quiet})

    def test_it_pulls_only_when_the_owner_allowed_it(self):
        self.measured(self.bench)
        self.assertEqual(self.tick(), 'decisional.auto_pull is off, or this is a check or a test')
        self.assertEqual(self.started, [])

    def test_with_the_switch_on_a_missing_model_starts_one_background_pull(self):
        self.measured(self.bench)
        self.assertEqual(self.tick(auto_pull=True), '')
        self.assertEqual(self.tick(auto_pull=True), 'a pull is already running')
        self.assertEqual(len(self.started), 1)
        self.started[0]()
        self.assertEqual(decisional._running, set())

    def test_a_person_at_this_computer_holds_a_pull_to_it(self):
        self.measured({'laptop': {'vram_gb': 0, 'mem_gb': 40}})
        self.assertEqual(self.tick(quiet=True, auto_pull=True, allow_cpu=True), 'a person is at this computer')
        self.assertEqual(self.started, [])
        self.assertEqual(self.tick(quiet=False, auto_pull=True, allow_cpu=True), '')

    def test_blockers_each_have_a_reason(self):
        t = {'machine': 'pc', 'model': 'qwen3:32b', 'installed': False}
        s = {'enabled': True, 'auto_pull': True}
        self.assertEqual(decisional.pull_blocker(s, t, None, 1.0), '')
        self.assertIn('auto_pull is off', decisional.pull_blocker({**s, 'auto_pull': False}, t, None, 1.0))
        self.assertIn('no machine fits', decisional.pull_blocker(s, None, None, 1.0))
        self.assertIn('already installed', decisional.pull_blocker(s, {**t, 'installed': True}, None, 1.0))
        failed = {'machine': 'pc', 'model': 'qwen3:32b', 'ok': False, 'at': 100.0}
        self.assertIn('failed less than 6 hours ago', decisional.pull_blocker(s, t, failed, 200.0))
        self.assertEqual(decisional.pull_blocker(s, t, failed, 100.0 + decisional.RETRY_S + 1), '')
        self.assertEqual(decisional.pull_blocker(s, t, {**failed, 'model': 'other'}, 200.0), '')

    def test_only_a_pull_to_this_computer_waits_for_its_person(self):
        def quiet(cfg):
            return {'quiet': True}
        self.assertTrue(decisional.person_here(self.cfg, {'machine': 'laptop'}, quiet))
        self.assertFalse(decisional.person_here(self.cfg, {'machine': 'pc'}, quiet))


class Routing(Base):
    """tutti/brain.py asks the decisional model first for the judgement purposes, and only for them."""

    def setUp(self):
        super().setUp()
        self.cfg['workers'] = {'nuc-local': {'kind': 'ollama', 'host': 'n', 'model': 'big:70b', 'machine': 'nuc'},
                               'pc-local': {'kind': 'ollama', 'host': 'p', 'model': 'qwen3:32b', 'machine': 'pc'}}
        self.fleet = {'machines': {'nuc': {'models': [model('big:70b', 70)]}, 'pc': {'models': [model('qwen3:32b', 32)]}}}
        self.ps = {}
        for name, b in self.bench.items():
            power.save_one(self.cfg, name, b, 1.0)

    def best(self, purpose, **over):
        self.cfg['decisional'] = over
        return brain.best_free(self.cfg, purpose, self.ps_fn, lambda m: None, self.fleet, preempt=False)[2]

    def ps_fn(self, w):
        return {'models': [{'name': n} for n in self.ps.get(w['host'], [])]}

    def test_judgement_purposes_ask_the_decisional_model_before_a_larger_one(self):
        self.assertEqual(self.best('other'), 'big:70b')
        for purpose in ('unblock', 'triage', 'scout', 'needs', 'welcome', 'routing'):
            self.assertEqual(self.best(purpose), 'qwen3:32b', purpose)

    def test_other_purposes_and_a_switched_off_decisional_keep_the_old_order(self):
        self.assertEqual(self.best('sizing'), 'big:70b')
        self.assertEqual(self.best('unblock', enabled=False), 'big:70b')
        self.assertEqual(self.best('unblock', purposes=['welcome']), 'big:70b')

    def test_a_busy_decisional_model_falls_back_to_the_next_free_one(self):
        self.ps = {'p': ['coder:7b']}
        self.assertEqual(self.best('unblock'), 'big:70b')

    def test_routing_advice_is_local_only_and_carries_the_report(self):
        seen = []
        out = decisional.advise(self.cfg, 'cursor: behind pace\nclaude: panic', lambda cfg, prompt, purpose, **kw: (
            seen.append((prompt, purpose, kw)) or ('send to cursor', 'pc-local')))
        self.assertEqual(out, ('send to cursor', 'pc-local'))
        prompt, purpose, kw = seen[0]
        self.assertEqual((purpose, kw), ('routing', {'online': False}))
        self.assertIn('claude: panic', prompt)
        self.assertIn('none to an account in panic', prompt)


class Describe(Base):
    def test_the_status_names_the_pick_the_step_and_the_skips(self):
        self.fleet['machines']['pc'] = {'models': [model('qwen3:14b', 14)]}
        text = decisional.describe(self.plan(), {'why': 'qwen3:32b was not fetched on pc'})
        self.assertIn('decisional model: qwen3:32b on pc (gpu, needs about 21.2 GB), missing: run `tutti decisional pull`', text)
        self.assertIn('asked now: qwen3:14b on pc', text)
        self.assertIn('last pull: qwen3:32b was not fetched on pc', text)
        self.assertIn('skipped laptop:', text)

    def test_none_fits_and_no_ollama_say_what_to_do(self):
        self.bench = {}
        self.assertTrue(decisional.describe(self.plan()).startswith('decisional model: none fits'))
        self.bench = {'pc': {'vram_gb': 32, 'mem_gb': 64}}
        self.assertIn('install it there once', decisional.describe(self.plan()))


if __name__ == '__main__':
    unittest.main()
