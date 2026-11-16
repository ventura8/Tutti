"""Tutti's own AI (tutti/brain.py, backlog t80): the best free local model across the household's machines, busy and yielding
ones skipped, a new bigger model picked up at once, pre-emption that parks other work and resumes it, the online fallback
within pacing, the use log, and a guard that no module calls a model directly. Models and servers are fakes."""
import json
import os
import re
import unittest

import tmpdirs

from tutti import aiassist, brain, fleet, local

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def model(name, params, quant='Q4_K_M'):
    return fleet.classify({'name': name, 'details': {'parameter_size': f'{params}B', 'quantization_level': quant}})


class Base(unittest.TestCase):
    def setUp(self):
        self.cfg = {'project': {'state': tmpdirs.mkdtemp()}, 'ai_assist': {'enabled': True, 'local': True, 'online': True},
                    'machines': {'laptop': {'transport': 'local'}, 'nuc': {'transport': 'ssh', 'host': 'nuc.example'},
                                 'pc': {'transport': 'ssh', 'host': 'pc.example'}},
                    'workers': {'laptop-local': {'kind': 'ollama', 'host': 'l', 'model': 'small:4b', 'machine': 'laptop'},
                                'nuc-local': {'kind': 'ollama', 'host': 'n', 'model': 'mid:14b', 'machine': 'nuc'},
                                'pc-local': {'kind': 'ollama', 'host': 'p', 'model': 'big:32b', 'machine': 'pc'}}}
        self.fleet = {'machines': {'laptop': {'models': [model('small:4b', 4)]}, 'nuc': {'models': [model('mid:14b', 14)]},
                                   'pc': {'models': [model('big:32b', 32)]}}}
        self.ps = {}   # host -> loaded model names; a host missing from `down` answers

    def ps_fn(self, w):
        return None if w['host'] in getattr(self, 'down', ()) else {'models': [{'name': n} for n in self.ps.get(w['host'], [])]}

    def ask(self, purpose='needs', **kw):
        calls = []

        def gen(cfg, w, prompt, **k):
            calls.append((w['host'], w['model'], brain.preempted_now(cfg, w['machine']) is not None))
            return f'from {w["model"]}'
        out = brain.ask(self.cfg, 'p', purpose, generate=gen, ps_fn=self.ps_fn, yield_fn=lambda m: None, fleet_state=self.fleet, **kw)
        return out, calls


class BestFree(Base):
    def test_the_largest_free_model_is_chosen_whatever_machine(self):
        (answer, who), _ = self.ask()
        self.assertEqual((answer, who), ('from big:32b', 'pc-local'))

    def test_a_down_or_yielding_machine_is_skipped_for_the_next_best(self):
        self.down = {'p'}
        (answer, _), _ = self.ask()
        self.assertEqual(answer, 'from mid:14b')
        out = brain.best_free(self.cfg, 'needs', self.ps_fn, lambda m: 'owner gaming' if m.get('host') == 'nuc.example' else None,
                              self.fleet)
        self.assertEqual(out[2], 'small:4b')

    def test_a_new_bigger_model_is_picked_up_at_once(self):
        self.fleet['machines']['nuc']['models'].append(model('huge:70b', 70))
        self.assertEqual(brain.best_free(self.cfg, 'needs', self.ps_fn, lambda m: None, self.fleet)[2], 'huge:70b')

    def test_quantisation_and_kind_rank_not_the_machine_name(self):
        self.assertGreater(brain.capability(model('a:14b', 14, 'Q8_0')), brain.capability(model('b:14b', 14, 'Q3_K_S')))
        self.assertEqual(brain.quant_factor('F16'), 1.0)
        self.assertEqual(brain.quant_factor(''), 0.9)

    def test_no_small_default_wins_over_a_bigger_free_model(self):
        self.cfg['ai_assist']['local_worker'] = 'laptop-local'   # an old pin: capability still decides
        self.assertEqual(brain.best_free(self.cfg, 'needs', self.ps_fn, lambda m: None, self.fleet)[2], 'big:32b')

    def test_off_never_asks(self):
        self.cfg['ai_assist']['local'] = False
        self.assertEqual(brain.ask(self.cfg, 'p', generate=lambda *a, **k: 1 / 0)[0], None)


class Preempt(Base):
    def test_busy_best_model_parks_other_work_runs_then_resumes(self):
        self.ps = {'p': ['coder:7b']}   # the PC runs a delegated task's model
        (answer, _), calls = self.ask()
        self.assertEqual(answer, 'from big:32b')
        self.assertEqual(calls, [('p', 'big:32b', True)])   # the marker held while Tutti's job ran
        self.assertIsNone(brain.preempted_now(self.cfg, 'pc'))   # lowered after: the other work resumes
        line = brain.summary(brain.read_log(self.cfg), brain.read_log(self.cfg)[-1]['at'])['parks'][0]
        self.assertRegex(line, r"^parked coder:7b work on pc for Tutti's own job \(\d")

    def test_other_work_waits_between_requests_and_loses_nothing(self):
        brain.park_others(self.cfg, 'pc', 'needs')
        slept = []

        def sleep(s):
            slept.append(s)
            brain.resume_others(self.cfg, 'pc')
        self.assertEqual(local.wait_for_preempt(self.cfg, self.cfg['workers']['pc-local'], sleep=sleep), 2)
        self.assertEqual(slept, [2])

    def test_admin_turns_it_off_or_limits_it(self):
        self.ps = {'p': ['coder:7b']}
        self.cfg['ai_assist']['preempt'] = False
        self.assertEqual(self.ask()[0][0], 'from mid:14b')
        self.cfg['ai_assist']['preempt'] = ['welcome']
        self.assertEqual(self.ask('needs')[0][0], 'from mid:14b')
        self.assertEqual(self.ask('welcome')[0][0], 'from big:32b')
        self.assertTrue(any('preempt' in e for e in aiassist.errors({**self.cfg, 'ai_assist': {'preempt': 3}})))

    def test_a_stale_marker_does_not_hold_work_forever(self):
        brain.park_others(self.cfg, 'pc', 'needs', now=1000)
        self.assertIsNone(brain.preempted_now(self.cfg, 'pc', now=1000 + brain.PREEMPT_MAX_S + 1))


class Online(Base):
    def test_every_local_down_goes_online_within_pacing_and_is_logged(self):
        self.down = {'p', 'n', 'l'}
        (answer, who), _ = self.ask(ask_online_fn=lambda c, p: ('online answer', 'agy'))
        self.assertEqual((answer, who), ('online answer', 'agy'))
        self.assertEqual(brain.read_log(self.cfg)[-1]['where'], 'online')

    def test_auto_picks_the_first_subscription_worker_with_room(self):
        self.cfg['ai_assist']['online_worker'] = 'auto'
        self.cfg['workers'].update({'paid': {'kind': 'cli', 'command': ['x'], 'costs_money': True},
                                    'full': {'kind': 'cli', 'command': ['x']}, 'free': {'kind': 'cli', 'command': ['x']}})
        pick = brain.online_pick(self.cfg, available=lambda w: (True, ''),
                                 room=lambda n: (None, 'ahead of pace') if n == 'full' else (['--a'], ''))
        self.assertEqual(pick, ('free', ['--a']))

    def test_no_room_anywhere_says_why(self):
        self.cfg['ai_assist']['online_worker'] = 'auto'
        self.cfg['workers']['full'] = {'kind': 'cli', 'command': ['x']}
        name, why = brain.online_pick(self.cfg, available=lambda w: (True, ''), room=lambda n: (None, 'ahead of pace'))
        self.assertIsNone(name)
        self.assertIn('full: ahead of pace', why)


class Card(Base):
    def test_the_card_names_the_best_free_model(self):
        with open(fleet.path(self.cfg), 'w', encoding='utf8') as f:
            json.dump(self.fleet, f)
        self.assertEqual(brain.view(self.cfg, ps_fn=self.ps_fn, yield_fn=lambda m: None)['line'],
                         "Tutti's own AI: big:32b on pc (best free)")


class Guard(unittest.TestCase):
    """Every internal model use goes through brain.ask: no other module calls a model itself. The agent harnesses (the
    conductor and chat, which run the agent the owner chose, `tutti run`'s local edits and `tutti translate`, a project's job
    on the workers its config names) are the listed exceptions."""
    ALLOWED = {'brain.py', 'local.py', 'chatrun.py', 'conduct.py', 'translate.py'}
    DIRECT = re.compile(r"local\.generate\(|ollama_url\([^)]*/api/(generate|chat)|\bask_online\(")

    def test_no_module_calls_a_model_directly(self):
        bad = []
        for name in sorted(os.listdir(os.path.join(ROOT, 'tutti'))):
            if name.endswith('.py') and name not in self.ALLOWED:
                with open(os.path.join(ROOT, 'tutti', name), encoding='utf8') as f:
                    bad += [f'{name}:{i}' for i, line in enumerate(f, 1) if self.DIRECT.search(line)]
        self.assertEqual(bad, [], 'call tutti/brain.py ask() instead')


if __name__ == '__main__':
    unittest.main()
