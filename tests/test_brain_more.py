"""Tutti's own AI, 0.17.1: household members' shared machines as candidates (never pre-empted), Sentry triage and scout
drafting through brain.ask. Models and servers are fakes."""
import unittest

import tmpdirs

from tutti import aiassist, brain, fleet, scouts, triage


def tags(*names):
    return {'models': [{'name': n, 'details': {'parameter_size': n.split(':')[1].upper(), 'quantization_level': 'Q4_K_M'}}
                       for n in names]}


class Shared(unittest.TestCase):
    def setUp(self):
        brain._TAGS.clear()
        self.addCleanup(brain._TAGS.clear)
        self.cfg = {'project': {'state': tmpdirs.mkdtemp()}, 'ai_assist': {'enabled': True},
                    'machines': {'laptop': {'transport': 'local'},
                                 'ana-pc': {'transport': 'household', 'host': 'ana-pc.example', 'ollama_port': 11434}},
                    'workers': {'laptop-local': {'kind': 'ollama', 'host': 'l', 'model': 'small:4b', 'machine': 'laptop'}}}
        self.fleet = {'machines': {'laptop': {'models': [fleet.classify(tags('small:4b')['models'][0])]}}}

    def best(self, ps=None):
        brain.shared_workers(self.cfg, tags_fn=lambda h: tags('big:32b') if h == 'ana-pc.example:11434' else None)
        return brain.best_free(self.cfg, 'needs', lambda w: ps.get(w['host'], {}) if ps else {}, lambda m: None, self.fleet)

    def test_a_members_shared_machine_with_a_bigger_model_is_chosen(self):
        name, machine, model, _s, _p = self.best()
        self.assertEqual((name, machine, model), ('ana-pc-shared', 'ana-pc', 'big:32b'))

    def test_a_members_busy_machine_is_never_preempted(self):
        out = self.best({'ana-pc.example:11434': {'models': [{'name': 'other:7b'}]}})
        self.assertEqual((out[2], out[4]), ('small:4b', ''))

    def test_a_paused_shared_machine_is_skipped_and_one_without_a_model_server_is_no_candidate(self):
        self.cfg['machines']['ana-pc']['paused'] = {'by': 'ana'}
        self.assertEqual(self.best()[2], 'small:4b')
        del self.cfg['machines']['ana-pc']['ollama_port']
        self.assertEqual(brain.shared_workers(self.cfg), {})


class Drafting(unittest.TestCase):
    def setUp(self):
        self.cfg = {'project': {'state': tmpdirs.mkdtemp()}, 'ai_assist': {'enabled': True, 'online': True}, 'machines': {},
                    'workers': {}}

    def test_scout_focus_is_drafted_locally_and_added_to_the_design_review(self):
        asked = []

        def ask(cfg, prompt, purpose, online=True):
            asked.append((purpose, online))
            return 'Q: why do runs stall at night?\nQ: which step wastes quota?\nnoise', 'pc-local'
        self.assertEqual(len(aiassist.draft_focus(self.cfg, {'events': 3}, ask, 1)), 1)
        self.assertEqual(asked, [('scout', False)])
        self.assertIn('why do runs stall at night?', aiassist.design_text(self.cfg, {'events': 3}))

    def test_no_answer_drafts_nothing(self):
        self.assertEqual(aiassist.draft_focus(self.cfg, {'events': 3}, lambda *a, **k: (None, 'down'), 1), [])
        self.assertNotIn('Look first at', aiassist.design_text(self.cfg, {'events': 3}))

    def report(self):
        with scouts.locked(self.cfg) as data:
            data['requests'].append({'id': 's1', 'kind': 'watch', 'watch': triage.PREFIX + 'app', 'status': 'answered',
                                     'answer': 'Three crashes in the parser since 1.2.', 'fed': {'added': 0, 'why': 'no TUTTI-TASK line'}})

    def test_a_triage_report_without_task_lines_gets_them_drafted(self):
        self.report()
        ev = triage.draft_missing(self.cfg, ask=lambda c, p, purpose: ('TUTTI-TASK: guard the parser || sentry: ABC-1', 'pc-local'))
        self.assertIn('1 task line(s) drafted by pc-local', ev[0])
        r = scouts.load(self.cfg)['requests'][0]
        self.assertNotIn('fed', r)   # fed again next round
        self.assertEqual(triage.parse(triage.answer_of(r))[0]['sentry'], 'ABC-1')
        self.assertEqual(triage.draft_missing(self.cfg, ask=lambda *a: 1 / 0), [])   # never drafted twice

    def test_a_report_no_model_can_read_is_left(self):
        self.report()
        triage.draft_missing(self.cfg, ask=lambda c, p, purpose: (None, 'down'))
        r = scouts.load(self.cfg)['requests'][0]
        self.assertEqual((r['drafted'], r['fed']['why']), ('none', 'no TUTTI-TASK line'))


if __name__ == '__main__':
    unittest.main()
