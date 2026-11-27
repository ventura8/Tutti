"""Household improvement loop (t250): proposals across routing, pacing and parallel with evidence, expected gain and
bounds; auto-apply under autonomy or propose for review; measure then keep or roll back; anonymised share opt-in
(default off). Nothing leaves the temp folder."""
import os
import tempfile
import unittest

import yaml

from tutti import config, improve, settings

NOW = 1_800_000_000.0
SUMMARY = {
    'events': 100, 'parked': [{'cause': 'no_progress', 'count': 5}], 'wasted': [{'who': 'a', 'minutes': 90.0}],
    'escalations': 8, 'stalls': [{'cause': 'quiet', 'count': 6}],
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, 'state')
        self.path = os.path.join(self.tmp.name, 'tutti.config.yaml')

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, ai=None, **im):
        raw = {
            'project': {'state': self.state},
            'autopilot': {'no_progress': 3, 'max_attempts': 12, 'repos': {'tutti': {'path': self.tmp.name, 'check': 'true'}}},
            'dashboard': {'stall_minutes': 10},
            'improvements': {**im},
        }
        raw['ai_assist'] = {'local': False} if ai is None else dict(ai)
        with open(self.path, 'w', encoding='utf8') as f:
            yaml.safe_dump(raw, f)
        cfg = config.normalise(raw)
        self.assertEqual(config.validate(cfg), [])
        cfg['_path'] = self.path
        return cfg

    def store(self):
        calls = []

        def store(cfg, changes):
            calls.append(changes)
            settings.store(cfg, changes)
        store.calls = calls
        return store


class Propose(Base):
    def test_three_aspects_carry_evidence_gain_and_bounds(self):
        items = improve.propose(self.cfg(), SUMMARY, min_count=3)
        self.assertEqual({i['aspect'] for i in items}, {'routing', 'pacing', 'parallel'})
        for i in items:
            self.assertTrue(i['evidence'])
            self.assertTrue(i['expected_gain'])
            self.assertLessEqual(i['lo'], i['value'])
            self.assertLessEqual(i['value'], i['hi'])
            self.assertGreater(i['step'], 0)

    def test_below_min_count_proposes_nothing(self):
        thin = {'parked': [], 'wasted': [], 'escalations': 0, 'stalls': []}
        self.assertEqual(improve.propose(self.cfg(), thin, min_count=3), [])

    def test_ai_review_can_refine_rules_inside_bounds(self):
        cfg = self.cfg(ai={'enabled': True, 'local': True})
        raw = [rule(cfg, SUMMARY, 3) for rule in improve.RULES]
        proposed = [item for item in raw if item and item['lo'] <= item['value'] <= item['hi']]

        def ask(_cfg, _prompt, *_a, **_kw):
            return ('REVIEW: routing || verdict: keep || value: 2 || gain: less waste\n'
                    'REVIEW: pacing || verdict: keep || value: 14 || evidence: many escalations\n'
                    'REVIEW: parallel || verdict: drop || value: same'), 'local-model'

        items = improve.review(cfg, SUMMARY, proposed, ask=ask)
        self.assertEqual({i['aspect'] for i in items}, {'routing', 'pacing'})
        self.assertEqual(next(i for i in items if i['aspect'] == 'routing')['value'], 2)
        self.assertEqual(next(i for i in items if i['aspect'] == 'pacing')['value'], 14)


class Autonomy(Base):
    def test_bounded_applies_within_step_and_propose_waits(self):
        cfg = self.cfg(autonomy='bounded')
        store = self.store()
        st = {}
        events = improve.enact(cfg, st, improve.propose(cfg, SUMMARY), SUMMARY, NOW, store)
        self.assertTrue(any('routing' in e and '->' in e for e in events))
        self.assertTrue(store.calls)
        applied = [e for e in st['log'] if e['status'] == 'applied']
        self.assertTrue(applied)
        self.assertTrue(all(e.get('measure_after') for e in applied))

    def test_propose_autonomy_never_writes_config(self):
        cfg = self.cfg(autonomy='propose')
        store = self.store()
        st = {}
        events = improve.enact(cfg, st, improve.propose(cfg, SUMMARY), SUMMARY, NOW, store)
        self.assertEqual(store.calls, [])
        self.assertTrue(all('proposed' in e for e in events))
        self.assertEqual(len(st['proposals']), 3)

    def test_admin_applies_or_dismisses_a_proposal(self):
        cfg = self.cfg(autonomy='propose')
        st = {}
        improve.enact(cfg, st, improve.propose(cfg, SUMMARY), SUMMARY, NOW, self.store())
        improve.write_state(cfg, st)
        path = st['proposals'][0]['path']
        self.assertTrue(improve.decide(cfg, path, False, now=NOW)[0])
        self.assertFalse(any(p['path'] == path for p in improve.read_state(cfg)['proposals']))
        self.assertEqual(improve.read_state(cfg)['log'][0]['outcome'], 'dismissed')

    def test_only_the_reviewed_proposal_can_be_approved(self):
        cfg = self.cfg(autonomy='propose')
        improve.tick(cfg, NOW, summary=SUMMARY)
        proposal = improve.read_state(cfg)['proposals'][0]
        path = proposal['path']
        self.assertFalse(improve.decide(cfg, path, True, now=NOW, proposal_id='old')[0])
        self.assertTrue(improve.decide(cfg, path, True, now=NOW, proposal_id=proposal['id'])[0])
        self.assertEqual(improve.read_state(cfg)['log'][-1]['status'], 'applied')


class Measure(Base):
    def test_kept_when_metric_improves_else_rolled_back(self):
        cfg = self.cfg()
        cfg['autopilot']['no_progress'] = 2
        store = self.store()
        entry = {
            'id': 'i1', 'at': NOW, 'aspect': 'routing', 'path': 'autopilot.no_progress', 'old': 3, 'new': 2,
            'metric': 'wasted_minutes', 'better': 'lower', 'status': 'applied', 'before': 90.0,
            'measure_after': NOW - 1, 'evidence': 'x', 'expected_gain': 'y', 'outcome': None,
        }
        st = {'log': [dict(entry)], 'share': False}
        events = improve.measure(cfg, st, {'wasted': [{'minutes': 40.0}]}, NOW, store)
        self.assertIn('kept', events[0])
        self.assertEqual(st['log'][0]['outcome'], 'kept')
        st = {'log': [dict(entry)], 'share': False}
        events = improve.measure(cfg, st, {'wasted': [{'minutes': 100.0}]}, NOW, store)
        self.assertIn('rolled back', events[0])
        self.assertEqual(st['log'][0]['status'], 'rolled_back')
        self.assertTrue(store.calls)

    def test_measurement_uses_only_the_trial_window(self):
        rows = [
            {'ts': improve.telemetry.now_iso(NOW + offset), 'name': 'autopilot.escalate'}
            for offset in (-1, 0, 1, 2)
        ]
        self.assertEqual(improve._window(rows, NOW - 2, NOW)['escalations'], 1)
        self.assertEqual(improve._window(rows, NOW, NOW + 2)['escalations'], 2)

    def test_empty_work_window_is_not_an_improvement(self):
        entry = {'before': 90, 'better': 'lower'}
        self.assertFalse(improve._trial_improved(entry, {'work_samples': 0}, 0))
        self.assertTrue(improve._trial_improved(entry, {'work_samples': 1}, 40))

    def test_less_work_alone_does_not_make_a_trial_successful(self):
        entry = {'before': 90, 'before_samples': 9, 'better': 'lower'}
        self.assertFalse(improve._trial_improved(entry, {'work_samples': 3}, 40))
        self.assertTrue(improve._trial_improved(entry, {'work_samples': 5}, 40))

    def test_unresolved_trial_blocks_new_change_after_daily_cooldown(self):
        entry = {'status': 'applied', 'at': NOW, 'path': 'autopilot.no_progress'}
        self.assertTrue(improve._recent_path([entry], entry['path'], NOW + 2 * improve.DAY))
        entry.update(status='rolled_back', measured_at=NOW + 2 * improve.DAY)
        self.assertFalse(improve._recent_path([entry], entry['path'], NOW + 3 * improve.DAY))

    def test_share_opt_in_records_anonymised_report_only_when_kept(self):
        cfg = self.cfg(share=False)
        cfg['autopilot']['no_progress'] = 2
        self.assertFalse(improve.share_on(cfg))
        improve.set_share(cfg, True)
        self.assertTrue(improve.share_on(cfg))
        entry = {
            'id': 'i1', 'at': NOW, 'aspect': 'routing', 'path': 'autopilot.no_progress', 'old': 3, 'new': 2,
            'metric': 'wasted_minutes', 'better': 'lower', 'status': 'applied', 'before': 90.0,
            'measure_after': NOW - 1, 'evidence': 'parks', 'expected_gain': 'less waste', 'outcome': None,
        }
        st = {'log': [entry], 'share': True}
        improve.measure(cfg, st, {'wasted': [{'minutes': 40.0}]}, NOW, self.store())
        rep = st['reports'][0]
        self.assertEqual(rep['aspect'], 'routing')
        self.assertEqual(rep['setting'], 'autopilot.no_progress')
        self.assertEqual(rep['gain']['after'], 40.0)
        self.assertNotIn('host', rep)
        self.assertNotIn('user', rep)
        self.assertNotIn('repo', rep)

    def test_rollback_skips_when_setting_changed_after_apply(self):
        cfg = self.cfg()
        store = self.store()
        cfg['autopilot']['no_progress'] = 1
        st = {'log': [{
            'id': 'i1', 'at': NOW, 'aspect': 'routing', 'path': 'autopilot.no_progress', 'old': 3, 'new': 2,
            'metric': 'wasted_minutes', 'better': 'lower', 'status': 'applied', 'before': 90.0,
            'measure_after': NOW - 1, 'evidence': 'x', 'expected_gain': 'y', 'outcome': None,
        }]}
        events = improve.measure(cfg, st, {'wasted': [{'minutes': 120.0}]}, NOW, store)
        self.assertIn('rollback skipped', events[0])
        self.assertEqual(improve.current(cfg, ('autopilot', 'no_progress')), 1)
        self.assertEqual(st['log'][0]['outcome'], 'skipped_changed')
        improve.write_state(cfg, st)
        self.assertEqual(improve.view(cfg, SUMMARY)['skipped_changed'], 1)


class Round(Base):
    def test_tick_is_on_by_default_and_logs_the_view(self):
        cfg = self.cfg()
        self.assertTrue(improve.enabled(cfg))
        events = improve.tick(cfg, NOW, self.store(), SUMMARY)
        self.assertTrue(events)
        v = improve.view(cfg, SUMMARY)
        self.assertEqual(v['aspects'], ['routing', 'pacing', 'parallel', 'crashes'])
        self.assertGreater(v['applied'] + v['waiting'], 0)
        self.assertIn('log', v)
        self.assertIn('skipped_changed', v)

    def test_disabled_tick_changes_nothing(self):
        cfg = self.cfg(enabled=False)
        improve.set_enabled(cfg, False)
        self.assertEqual(improve.tick(cfg, NOW, self.store(), SUMMARY), [])

    def test_disabling_proposals_still_resolves_an_open_trial(self):
        cfg = self.cfg(measure_hours=1)
        improve.tick(cfg, NOW, summary=SUMMARY)
        improve.set_enabled(cfg, False)
        events = improve.tick(cfg, NOW + 3600, summary=SUMMARY)
        self.assertTrue(any('rolled back' in line for line in events))
        self.assertEqual(config.load(self.path)['autopilot']['no_progress'], 3)

    def test_errors_reject_bad_autonomy_and_unknown_keys(self):
        cfg = self.cfg()
        cfg['improvements'] = {'autonomy': 'open', 'nope': 1}
        errs = improve.errors(cfg)
        self.assertTrue(any('autonomy' in e for e in errs))
        self.assertTrue(any('nope' in e for e in errs))


if __name__ == '__main__':
    unittest.main()
