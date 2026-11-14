"""AI assist: off by default and every switch with it, the owner-style opt-in, local-only never reaching an online model,
the fallbacks when AI is off or every model is down, the self-analysis into tasks, failure classes that retry instead of
escalating, the brief's criteria and summary, auto-tuning's bounds, daily limit and undo, and spare-only research watches.
Models are fakes; nothing leaves the temp folder."""
import json
import os
import tempfile
import unittest

import yaml

from tutti import aiassist, autopilot, backlog, config, dashtelemetry, scouts, settings, telemetry, yamledit

NOW = 1_800_000_000.0
SUMMARY = {'events': 40, 'parked': [{'cause': 'no_progress', 'count': 4}], 'wasted': [{'who': 'cursor', 'runs': 6, 'minutes': 90.0}],
           'stalls': [{'cause': 'job', 'count': 6}], 'crashes': [], 'most_failing': [], 'slowest': []}


def fake_ask(answer):
    def ask(cfg, prompt):
        ask.prompts.append(prompt)
        return (answer, 'pc') if answer is not None else (None, 'no local model answered (pc down)')
    ask.prompts = []
    return ask


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, 'state')

    def tearDown(self):
        self.tmp.cleanup()

    def raw(self, **ai):
        r = {'project': {'state': self.state},
             'workers': {'pc': {'kind': 'ollama', 'host': 'pc.example:11434', 'model': 'm'},
                         'lap': {'kind': 'ollama', 'host': 'lap.example:11434', 'model': 'n', 'capabilities': ['analysis']},
                         'cursor': {'kind': 'cli', 'command': ['cursor-agent', '{prompt}']}},
             'autopilot': {'repos': {'tutti': {'path': self.tmp.name, 'check': 'true'}}, 'no_progress': 3},
             'dashboard': {'stall_minutes': 10},
             'projects': {'Tutti': {'match': 'tutti', 'repo': 'octo/tutti', 'self_improve': {'tasks': True, 'autopilot_repo': 'tutti'}}},
             'self_improve': {'home': 'Tutti'},
             'scouts': {'grok': {'kind': 'grok-bot-webhook', 'url_env': 'U', 'key_env': 'K',
                                 'skills': ['research'], 'reply': {'github_repo': 'octo/tutti'}}}}
        if ai:
            r['ai_assist'] = ai
        return r

    def cfg(self, **ai):
        raw = self.raw(**ai)
        path = os.path.join(self.tmp.name, 'tutti.config.yaml')
        with open(path, 'w', encoding='utf8') as f:
            yaml.safe_dump(raw, f)
        cfg = config.normalise(raw)
        self.assertEqual(config.validate(cfg), [])
        cfg['_path'] = path
        return cfg


class Switches(Base):
    def test_off_by_default_for_a_new_install_and_nothing_runs(self):
        cfg = self.cfg()
        self.assertFalse(aiassist.switch(cfg, 'enabled'))
        self.assertFalse(aiassist.switch(cfg, 'local'))   # every switch is off while enabled is
        ask = fake_ask('PROPOSAL: x')
        self.assertEqual(aiassist.tick(cfg, NOW, ask), [])
        self.assertEqual(ask.prompts, [])
        self.assertEqual(aiassist.watches(cfg, scouts.settings(cfg, 'grok')), {})
        self.assertFalse(config.DEFAULTS['ai_assist']['enabled'])

    def test_the_owner_opts_in_with_local_and_online(self):
        cfg = self.cfg(enabled=True, online=True)
        self.assertTrue(all(aiassist.switch(cfg, k) for k in ('enabled', 'local', 'online', 'auto_tune')))
        self.assertFalse(aiassist.may_send_online(cfg, 'code'))   # code stays local without online_code
        self.assertTrue(aiassist.may_send_online(cfg, 'telemetry'))
        self.assertTrue(aiassist.may_send_online(self.cfg(enabled=True, online=True, online_code=True), 'code'))

    def test_a_bad_value_is_named(self):
        cfg = config.normalise({**self.raw(), 'ai_assist': {'enabled': 'yes', 'nope': 1, 'local_worker': 'ghost'}})
        errs = ' '.join(config.validate(cfg))
        self.assertIn('ai_assist.enabled', errs)
        self.assertIn('nope', errs)
        self.assertIn('ghost', errs)

    def test_the_settings_tab_offers_it_as_an_opt_in(self):
        view = settings.view(self.cfg())
        optins = next(s for s in view['sections'] if s['id'] == 'optins')
        self.assertIn('ai_assist.enabled', [r['key'] for r in optins['rows']])


class LocalOnly(Base):
    def test_local_only_never_makes_an_online_watch_and_never_feeds_one(self):
        cfg = self.cfg(enabled=True)
        self.assertEqual(aiassist.watches(cfg, scouts.settings(cfg, 'grok')), {})
        self.assertFalse(any(n.startswith(aiassist.WATCH_PREFIX) for n in scouts.settings(cfg, 'grok')['watches']))

    def test_online_watches_are_spare_only_and_scrubbed(self):
        cfg = self.cfg(enabled=True, online=True)
        summary = {**SUMMARY, 'crashes': [{'where': 'KeyError in /home/someone/x.py', 'count': 2}]}
        w = aiassist.watches(cfg, scouts.settings(cfg, 'grok'), summary_fn=lambda c: summary)
        self.assertEqual(sorted(w), ['tutti-ai-design', 'tutti-ai-release-notes'])
        self.assertTrue(all(x['spare_only'] for x in w.values()))
        self.assertNotIn('/home/someone', w['tutti-ai-design']['task'])
        self.assertIn('cursor-agent', w['tutti-ai-release-notes']['task'])

    def test_a_spare_only_watch_waits_while_the_account_is_on_or_ahead_of_pace(self):
        sc = {'name': 'grok'}
        w = {'every': '7d', 'spare_only': True}
        self.assertFalse(scouts.due(sc, [], 'tutti-ai-design', w, {'verdict': 'ahead'}, NOW))
        self.assertFalse(scouts.due(sc, [], 'tutti-ai-design', w, None, NOW))
        self.assertTrue(scouts.due(sc, [], 'tutti-ai-design', w, {'verdict': 'behind'}, NOW))

    def answer(self):
        return {'requests': [{'id': 'q1', 'kind': 'watch', 'watch': 'tutti-ai-release-notes', 'status': 'answered',
                              'answer': 'notes\nTUTTI-TASK: Read the new usage API || check: a test'}]}

    def test_an_answered_research_watch_becomes_candidates_the_admin_accepts(self):
        cfg = self.cfg(enabled=True, online=True)
        data = self.answer()
        events = aiassist.feed(cfg, aiassist.answered(data), NOW)
        self.assertIn('1 task(s)', events[0])
        self.assertEqual(aiassist.answered(data), [])   # fed once
        t = backlog.load(cfg)['tasks'][0]
        self.assertEqual((t['repo'], t['source'], t['status']), ('tutti', 'insight', 'candidate'))   # outside text: never queued
        notes = ' '.join(t.get('notes') or [])   # audit 2026-11: the suggested command never reaches a worker
        self.assertIn('not passed on', notes)
        self.assertNotIn('Suggested check', notes)

    def test_an_answer_after_online_ai_was_turned_off_is_dropped(self):
        cfg = self.cfg(enabled=True, online=False)
        self.assertIn('online AI is off', aiassist.feed(cfg, aiassist.answered(self.answer()), NOW)[0])
        self.assertEqual(backlog.load(cfg)['tasks'], [])

    def test_task_titles_go_online_only_with_online_code(self):
        for code, seen in ((False, False), (True, True)):
            cfg = self.cfg(enabled=True, online=True, online_code=code)
            with backlog.locked(cfg) as data:
                t, _ = backlog.add(data, backlog.new_task('tutti', 'Secret plan for the parser'))
                t['status'] = 'needs-person'
            self.assertEqual('Secret plan' in aiassist.design_text(cfg, SUMMARY), seen, code)


class Fallbacks(Base):
    def test_no_model_up_answers_none_with_the_reason(self):
        cfg = self.cfg(enabled=True)
        answer, why = aiassist.ask_local(cfg, 'p', available=lambda w: (False, 'down'), generate=None)
        self.assertIsNone(answer)
        self.assertIn('pc down', why)

    def test_a_model_that_fails_hands_over_to_the_next_and_the_analyst_goes_first(self):
        cfg = self.cfg(enabled=True)
        self.assertEqual(aiassist.local_workers(cfg), ['lap', 'pc'])
        calls = []

        def gen(cfg, w, prompt, **kw):
            calls.append(w['model'])
            if w['model'] == 'n':
                raise RuntimeError('gone')
            return 'ok'
        self.assertEqual(aiassist.ask_local(cfg, 'p', available=lambda w: (True, ''), generate=gen), ('ok', 'pc'))
        self.assertEqual(calls, ['n', 'm'])

    def test_a_rate_limit_stays_real_whatever_the_model_says(self):
        cfg = self.cfg(enabled=True)
        t = {'attempts': [{'ok': False, 'tail': 'Error: 429 Too Many Requests: rate limit reached'}]}
        aiassist.examine_attempt(cfg, t, fake_ask('CLASS: flaky\nSUMMARY: try later'))
        self.assertEqual(t['attempts'][-1]['class'], 'real')

    def test_a_reply_that_is_not_json_hands_over_to_the_next_model(self):
        cfg = self.cfg(enabled=True)

        def gen(cfg, w, prompt, **kw):
            if w['model'] == 'n':
                raise ValueError('Expecting value: line 1 column 1')
            return 'ok'
        self.assertEqual(aiassist.ask_local(cfg, 'p', available=lambda w: (True, ''), generate=gen), ('ok', 'pc'))

    def test_a_config_false_beats_the_dashboards_switch(self):
        cfg = self.cfg(enabled=False)
        aiassist.set_state_switch(cfg, 'enabled', True)
        self.assertFalse(aiassist.switch(cfg, 'enabled'))
        cfg = self.cfg()
        aiassist.set_state_switch(cfg, 'enabled', True)
        self.assertTrue(aiassist.switch(cfg, 'enabled'))   # no key in the file: the dashboard's switch counts

    def test_a_round_with_no_model_waits_before_asking_again(self):
        cfg = self.cfg(enabled=True)
        with backlog.locked(cfg) as data:
            backlog.add(data, backlog.new_task('tutti', 'Fix the probe'))
        ask = fake_ask(None)
        self.assertEqual(aiassist.decisions(cfg, ask, NOW), ['ai-assist: 1 task(s) sharpened or failures examined'])
        self.assertEqual(aiassist.decisions(cfg, ask, NOW + 60), [])
        self.assertEqual(len(ask.prompts), 1)
        self.assertEqual(backlog.load(cfg)['tasks'][0]['sized'], 'heuristic')

    def test_off_never_asks_a_model(self):
        cfg = self.cfg(enabled=True, local=False)
        self.assertEqual(aiassist.ask_local(cfg, 'p', available=lambda w: 1 / 0), (None, 'local AI is off'))

    def test_quick_classes(self):
        self.assertEqual(aiassist.classify_quick('Error: connect ETIMEDOUT 10.0.0.1:443'), 'flaky')
        self.assertEqual(aiassist.classify_quick('bash: ruff: command not found'), 'environment')
        self.assertEqual(aiassist.classify_quick('OSError: [Errno 28] No space left on device'), 'environment')
        for real in ('AssertionError: 3 != 4', "AssertionError: 'room' != 'zoom'", 'test_retry_after_timeout FAILED',
                     'HTTP 429: rate limit reached', 'bloom filter killed the test'):
            self.assertEqual(aiassist.classify_quick(real), 'real', real)

    def test_the_class_comes_from_the_class_line_only(self):
        self.assertEqual(aiassist.parse_class('CLASS: flaky\nSUMMARY: a real bug'), 'flaky')
        self.assertIsNone(aiassist.parse_class('SUMMARY: a real bug in the planner'))

    def test_a_down_model_still_sizes_the_task_by_heuristic(self):
        cfg = self.cfg(enabled=True)
        t = {**backlog.new_task('tutti', 'Rework the planner, the pacing and the dashboard', ['a', 'b', 'c', 'd', 'e']), 'id': 't1'}
        self.assertTrue(aiassist.sharpen_task(cfg, t, fake_ask(None)))
        self.assertEqual((t['size'], t['sized'], t.get('criteria')), ('L', 'heuristic', None))   # criteria asked again later
        self.assertFalse(aiassist.sharpen_task(cfg, t, fake_ask(None)))
        aiassist.sharpen_task(cfg, t, fake_ask('- it works\nSIZE: M'))
        self.assertEqual((t['size'], t['sized'], t['criteria']), ('M', 'ai', ['it works']))

    def test_a_model_writes_criteria_and_a_size(self):
        cfg = self.cfg(enabled=True)
        t = {**backlog.new_task('tutti', 'Fix the probe'), 'id': 't1'}
        aiassist.sharpen_task(cfg, t, fake_ask('- the probe ends in 2 s\n- a test covers it\nSIZE: M'))
        self.assertEqual((t['size'], t['criteria']), ('M', ['the probe ends in 2 s', 'a test covers it']))
        text = autopilot.brief({**t, 'scope': [], 'attempts': []}, {'check': 'true'})
        self.assertIn('Done when:\n- the probe ends in 2 s', text)

    def test_a_failure_is_classified_and_summarised_for_the_retry(self):
        cfg = self.cfg(enabled=True)
        t = {'attempts': [{'ok': False, 'tail': 'boom', 'class': 'real', 'step': 'agy', 'why': 'failed'}], 'status': 'queued',
             'title': 'x', 'scope': [], 'check': ''}
        self.assertTrue(aiassist.examine_attempt(cfg, t, fake_ask('CLASS: flaky\nSUMMARY: the registry timed out')))
        self.assertEqual(t['attempts'][-1]['class'], 'flaky')
        self.assertFalse(aiassist.examine_attempt(cfg, t, fake_ask('CLASS: real')))   # once
        self.assertIn('the registry timed out', autopilot.brief(t, {}))
        t2 = {'attempts': [{'ok': False, 'tail': 'boom'}]}
        self.assertFalse(aiassist.examine_attempt(cfg, t2, fake_ask(None)))   # model down: the quick class stays


class Escalation(Base):
    def ladder(self, **ai):
        raw = {**self.raw(**ai), 'workers': {'a': {'kind': 'cli', 'command': ['a', '{prompt}']}},
               'accounts': {'one': {'worker': 'a'}, 'two': {'worker': 'a'}},
               'autopilot': {'accounts': ['one'], 'ladder': ['one', 'two'], 'repos': {'app': {'path': '.'}}}}
        cfg = config.normalise(raw)
        cfg['_path'] = os.path.join(self.tmp.name, 'tutti.config.yaml')
        return cfg

    def next_step(self, cfg, outputs):
        attempts = [{'step': 'one', 'ok': False, 'class': aiassist.attempt_class(cfg, False, out)} for out in outputs]
        return autopilot.candidates(cfg, {'repo': 'app', 'attempts': attempts})[0]

    def test_with_ai_assist_on_a_flaky_failure_retries_the_same_step_once_then_escalates(self):
        cfg = self.ladder(enabled=True)
        self.assertEqual(self.next_step(cfg, ['curl: (56) Connection reset by peer']), 'one')
        self.assertEqual(self.next_step(cfg, ['bash: npx: command not found']), 'one')
        self.assertEqual(self.next_step(cfg, ['AssertionError: 3 != 4']), 'two')
        self.assertEqual(self.next_step(cfg, ['Connection reset by peer', 'Connection reset by peer']), 'two')   # once only

    def test_with_ai_assist_off_the_ladder_is_unchanged(self):
        cfg = self.ladder()
        self.assertIsNone(aiassist.attempt_class(cfg, False, 'Connection reset by peer'))
        self.assertEqual(self.next_step(cfg, ['Connection reset by peer']), 'two')


class Analysis(Base):
    def test_proposals_become_ranked_tasks_with_evidence_and_a_test_first_brief(self):
        cfg = self.cfg(enabled=True, auto_tune=False)
        folder = telemetry.folder_of(config.state_dir(cfg))
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, telemetry.EVENTS), 'w', encoding='utf8') as f:
            f.write(json.dumps({'kind': 'command', 'name': 'wait', 'outcome': 'failed', 'ts': '2099-01-01T00:00:00+00:00'}) + '\n')
        answer = ('1. PROPOSAL: Make `tutti wait` end on a dead job || why: 5 of 6 runs failed || test: a dead pid ends the wait\n'
                  'PROPOSAL: Cache the probe || why: p90 12 s\nnoise')
        ask = fake_ask(answer)
        events = aiassist.tick(cfg, NOW, ask)
        self.assertIn('proposed 2 improvement(s), 2 new task(s)', ' '.join(events))
        tasks = backlog.load(cfg)['tasks']
        self.assertTrue(tasks[0]['sig'].startswith('ai:'))
        self.assertIn('5 of 6 runs failed', tasks[0]['notes'][0])
        self.assertIn('regression test', tasks[0]['notes'][1])
        self.assertEqual(aiassist.view(cfg, {})['proposals'], 2)
        again = aiassist.tick(cfg, NOW + 60, ask)   # the analysis is not due again; the new tasks get criteria and a size
        self.assertEqual(again, ['ai-assist: 2 task(s) sharpened or failures examined'])
        self.assertEqual(sum('telemetry of' in p for p in ask.prompts), 1)

    def test_parsers(self):
        self.assertEqual(len(aiassist.parse_proposals('PROPOSAL: a\nPROPOSAL: b\nPROPOSAL: c', 2)), 2)
        tunes = aiassist.parse_tunes('TUNE: dashboard.stall_minutes = 15 || why: stalls\nTUNE: runner.cpus = 99\n'
                                     'TUNE: autopilot.no_progress = 2.5')
        self.assertEqual(tunes, [{'path': ('dashboard', 'stall_minutes'), 'value': 15, 'why': 'stalls'}])


class AutoTune(Base):
    def store(self):
        def store(cfg, changes):
            store.calls.append(changes)
            return settings.store(cfg, changes)
        store.calls = []
        return store

    def test_small_changes_apply_larger_ones_wait_and_undo_puts_them_back(self):
        cfg = self.cfg(enabled=True)
        st, store = {}, self.store()
        tunes = aiassist.rule_tunes(cfg, SUMMARY) + [{'path': ('dashboard', 'probe_seconds'), 'value': 120, 'why': 'big'}]
        events = aiassist.tune(cfg, st, tunes, SUMMARY, NOW, store)
        self.assertEqual(len(events), 2)
        with open(cfg['_path'], encoding='utf8') as f:
            text = yaml.safe_load(f)
        self.assertEqual((text['autopilot']['no_progress'], text['dashboard']['stall_minutes']), (2, 15))
        self.assertEqual([p['path'] for p in st['tune_proposals']], ['dashboard.probe_seconds'])   # 30 -> 120 is past the step
        aiassist.write_state(cfg, st)
        ok, _ = aiassist.undo(cfg, 'u1', store)
        self.assertTrue(ok)
        with open(cfg['_path'], encoding='utf8') as f:
            self.assertEqual(yaml.safe_load(f)['autopilot']['no_progress'], 3)
        self.assertFalse(aiassist.undo(cfg, 'u1', store)[0])   # once

    def test_the_admin_applies_or_dismisses_a_larger_change(self):
        cfg = self.cfg(enabled=True)
        st = {}
        aiassist.tune(cfg, st, [{'path': ('dashboard', 'probe_seconds'), 'value': 90, 'why': 'slow probes'}], SUMMARY, NOW, self.store())
        aiassist.write_state(cfg, st)
        self.assertFalse(aiassist.decide(cfg, 'dashboard.nope', True)[0])
        self.assertTrue(aiassist.decide(cfg, 'dashboard.probe_seconds', True, self.store(), NOW)[0])
        with open(cfg['_path'], encoding='utf8') as f:
            self.assertEqual(yaml.safe_load(f)['dashboard']['probe_seconds'], 90)
        st = aiassist.read_state(cfg)
        self.assertEqual((st['tune_proposals'], st['tuned'][-1]['by']), ([], 'admin'))
        st['tune_proposals'] = [{'path': 'dashboard.stall_minutes', 'value': 40, 'old': 10, 'why': 'x', 'at': NOW}]
        aiassist.write_state(cfg, st)
        self.assertTrue(aiassist.decide(cfg, 'dashboard.stall_minutes', False)[0])
        self.assertEqual(aiassist.read_state(cfg)['tune_proposals'], [])

    def test_a_failed_apply_keeps_the_proposal_and_old_ones_go(self):
        cfg = self.cfg(enabled=True)
        aiassist.write_state(cfg, {'tune_proposals': [{'path': 'dashboard.stall_minutes', 'value': 40, 'old': 10, 'why': 'x', 'at': NOW},
                                                      {'path': 'dashboard.probe_seconds', 'value': 90, 'old': 30, 'why': 'y',
                                                       'at': NOW - 8 * aiassist.DAY}]})

        def refuse(cfg, changes):
            raise settings.SettingsError(409, 'read-only')
        ok, why = aiassist.decide(cfg, 'dashboard.stall_minutes', True, refuse, NOW)
        self.assertFalse(ok)
        self.assertIn('read-only', why)
        st = aiassist.read_state(cfg)
        self.assertEqual(len(st['tune_proposals']), 2)
        self.assertEqual([p['path'] for p in aiassist.live_proposals(st, NOW)], ['dashboard.stall_minutes'])

    def test_at_most_two_a_day_and_never_out_of_bounds(self):
        cfg = self.cfg(enabled=True)
        self.assertEqual(aiassist.judge(cfg, {'path': ('autopilot', 'no_progress'), 'value': 1}), 'propose')
        self.assertEqual(aiassist.judge(cfg, {'path': ('autopilot', 'no_progress'), 'value': 3}), 'drop')
        st = {'tuned': [{'by': 'auto', 'at': NOW, 'path': 'x'}, {'by': 'auto', 'at': NOW, 'path': 'y'}]}
        store = self.store()
        aiassist.tune(cfg, st, aiassist.rule_tunes(cfg, SUMMARY), SUMMARY, NOW, store)
        self.assertEqual(store.calls, [])
        self.assertNotIn('tune_proposals', st)   # small changes past the day's room wait for the next round

    def test_auto_tune_off_changes_nothing(self):
        cfg = self.cfg(enabled=True, auto_tune=False)
        self.assertEqual(aiassist.tune(cfg, {}, aiassist.rule_tunes(cfg, SUMMARY), SUMMARY, NOW, self.store()), [])

    def test_undo_of_a_key_that_was_not_in_the_file_removes_it(self):
        cfg = self.cfg(enabled=True)
        aiassist.write_state(cfg, {'tuned': [{'id': 'u1', 'path': 'dashboard.probe_seconds', 'old': None, 'new': 45}]})
        seen = []
        self.assertTrue(aiassist.undo(cfg, 'u1', lambda c, ch: seen.append(ch))[0])
        self.assertIs(seen[0][0][1], yamledit.DELETE)

    def test_gains_compare_the_metric_since_the_change(self):
        st = {'tuned': [{'metric': 'wasted_minutes', 'before': 90.0, 'path': 'autopilot.no_progress'}]}
        g = aiassist.gains(None, st, {'wasted': [{'minutes': 45.0}]})
        self.assertEqual(g, ['wasted minutes 90 -> 45 (-50 %) since autopilot.no_progress was tuned'])


class Dashboard(Base):
    def test_the_switch_and_a_bad_request(self):
        cfg = self.cfg()
        collector = type('C', (), {'cfg': cfg, 'reload': lambda self: cfg})()
        code, _ = aiassist.save(collector, b'{"switch": "nope", "on": true}')
        self.assertEqual(code, 400)
        code, ans = aiassist.save(collector, b'{"switch": "enabled", "on": true}')
        self.assertEqual(code, 200, ans)
        with open(cfg['_path'], encoding='utf8') as f:
            self.assertTrue(yaml.safe_load(f)['ai_assist']['enabled'])
        self.assertEqual(aiassist.save(collector, b'{"undo": "u9"}')[0], 409)

    def test_the_card_counts_done_tasks(self):
        cfg = self.cfg(enabled=True)
        with backlog.locked(cfg) as data:
            t, _ = backlog.add(data, {**backlog.new_task('tutti', 'x'), 'sig': 'ai:abc'})
            t['status'] = 'done'
        self.assertEqual(aiassist.view(cfg, {})['done'], 1)
        self.assertIn('ai_assist', dashtelemetry.view(cfg))


if __name__ == '__main__':
    unittest.main()
