"""The Settings tab: the schema covers every key, a save keeps the file's comments, a bad value is refused, secrets never
reach the page, the dashboard's switches write the same keys, project priority shares the work out, and the tab renders."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import yaml

from tutti import autopilot, chatrun, config, dashboard, priority, settings, yamledit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = 'https://k3y-n0t-for-the-page@sentry.example/42'
TEXT = """# my config: keep this comment
project:
  state: STATE        # logs here

workers:
  cursor: {kind: cli, command: [cursor-agent, -p, '{prompt}']}   # the cheap one
  copilot: {kind: cli, command: [copilot, -p, '{prompt}'], costs_money: true}

autopilot:
  enabled: false      # off until I say so
  max_parallel: 1
  quiet_hours: []

projects:
  app: {match: app, priority: 1}
  tool:
    match: tool       # the tool itself
    priority: 0

telemetry:
  sentry_dsn: SECRET
"""


def comments(text):
    return [line.split('#', 1)[1] for line in text.splitlines() if '#' in line and "'#" not in line]


class Collector:
    def __init__(self, path):
        self.cfg, self.env = config.load(path), {}

    def reload(self):
        self.cfg = config.load(self.cfg['_path'])
        return self.cfg


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, 'state')
        self.path = os.path.join(self.tmp.name, 'tutti.config.yaml')
        with open(self.path, 'w', encoding='utf8') as f:
            f.write(TEXT.replace('STATE', self.state).replace('SECRET', SECRET))
        self.c = Collector(self.path)

    def file_text(self):
        with open(self.path, encoding='utf8') as f:
            return f.read()

    def post_setting(self, body):
        return settings.save(self.c, json.dumps(body).encode())


def setting_keys(data, prefix=''):
    for k, v in data.items():
        key = settings.generic(f'{prefix}{k}')
        if isinstance(v, dict) and v and (not settings.covered(key) or key in settings.ENTRIES):
            yield from setting_keys(v, f'{prefix}{k}.')   # a mapping: its keys are the settings
        else:
            yield key


class SchemaTest(unittest.TestCase):
    def test_every_key_of_the_example_config_has_a_schema_line(self):
        with open(config.EXAMPLE, encoding='utf8') as f:
            example = yaml.safe_load(f)
        missing = sorted({k for k in setting_keys(example) if not settings.covered(k) and k not in settings.ENTRIES
                          and not any(k.startswith(e + '.') and k.count('.') == e.count('.') + 1 for e in settings.ENTRIES)})
        self.assertEqual(missing, [])

    def test_every_default_has_a_label_and_a_section(self):
        claimed = {p.split('.')[0] for _, _, _, parts in settings.SECTIONS for p in parts}
        people = {m for m in settings.MANAGED if '.' not in m}   # on the Manage tab (tutti/manage.py), never here
        self.assertEqual(sorted(set(config.DEFAULTS) - claimed - people - {'routing'}), [])
        for key in settings.DEFAULTS_FLAT:
            self.assertTrue(settings.known(key)['label'], key)

    def test_an_undocumented_key_shows_as_yaml_under_other(self):
        cfg = config.normalise({})
        cfg['_path'] = ''
        rows = settings.View(cfg, {'brand_new': {'a': 1}}).payload()['sections'][-1]['rows']
        self.assertEqual([(r['key'], r['control'], r['yaml']) for r in rows if r['key'] == 'brand_new'], [('brand_new', 'yaml', 'a: 1\n')])


class EditTest(unittest.TestCase):
    def test_a_change_touches_only_its_value_and_keeps_every_comment(self):
        cases = [(['autopilot', 'max_parallel'], 3), (['workers', 'cursor', 'kind'], 'ollama'),
                 (['autopilot', 'quiet_hours'], ['22:00-06:00']),
                 (['projects', 'tool', 'priority'], 4), (['autopilot', 'fill_room'], False), (['workers', 'copilot', 'enabled'], True),
                 (['review', 'timeout_minutes'], 5), (['autopilot', 'enabled'], yamledit.DELETE)]
        for path, value in cases:
            new = yamledit.edit(TEXT, path, value)
            gone = [' off until I say so'] if value is yamledit.DELETE else []   # a removed key takes its own line's comment
            self.assertEqual(comments(new), [c for c in comments(TEXT) if c not in gone], path)
            want = yamledit.changed(yaml.safe_load(TEXT), path, value)
            self.assertEqual(yaml.safe_load(new), want, path)
        self.assertIn('  max_parallel: 3\n', yamledit.edit(TEXT, ['autopilot', 'max_parallel'], 3))
        self.assertIn("{kind: cli, command: [copilot, -p, '{prompt}'], costs_money: true, enabled: true}",
                      yamledit.edit(TEXT, ['workers', 'copilot', 'enabled'], True))

    def test_the_example_config_takes_a_change_to_every_key(self):
        with open(config.EXAMPLE, encoding='utf8') as f:
            text = f.read()
        data = yaml.safe_load(text)
        for top, v in data.items():
            for k in (v or {}) if isinstance(v, dict) else []:
                yamledit.edit(text, [top, k], yamledit.DELETE)
                yamledit.edit(text, [top, k], 'x')

    def test_what_it_cannot_do_safely_is_refused(self):
        with self.assertRaises(yamledit.EditError):
            yamledit.edit('a: 1\n', ['a', 'b'], 2)   # a scalar cannot hold a key
        with self.assertRaises(yamledit.EditError):
            yamledit.edit('a: &x {b: 1}\nc: *x\n', ['a', 'b'], 2)   # the alias would change too


class SaveTest(Base):
    def test_a_save_keeps_comments_backs_up_and_applies_now(self):
        before = self.file_text()
        code, answer = self.post_setting({'path': ['autopilot', 'max_parallel'], 'value': 2})
        self.assertEqual((code, answer['applies']), (200, 'now'))
        self.assertEqual(self.c.cfg['autopilot']['max_parallel'], 2)
        self.assertEqual(comments(self.file_text()), comments(before))
        with open(answer['backup'], encoding='utf8') as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(self.post_setting({'path': ['dashboard', 'port'], 'value': 4800})[1]['applies'], 'on restart')
        self.assertEqual(self.post_setting({'path': ['autopilot', 'max_parallel'], 'reset': True})[0], 200)
        self.assertNotIn('max_parallel', self.file_text())

    def test_an_invalid_value_is_refused_and_the_file_is_untouched(self):
        before = self.file_text()
        for body, code in (({'path': ['autopilot', 'max_parallel'], 'value': 0}, 400),
                           ({'path': ['autopilot', 'max_parallel'], 'value': 'two'}, 400),
                           ({'path': ['autopilot', 'enabled'], 'value': 'yes'}, 400),
                           ({'path': ['routing', 'order'], 'value': ['ghost']}, 400),   # the config's own rules
                           ({'path': ['autopilot', 'quiet_hours'], 'value': ['soon']}, 400),
                           ({'path': ['autopilot', 'max_parallel'], 'value': 2, 'etag': 'stale'}, 409),
                           ({'path': 'autopilot.max_parallel', 'value': 2}, 400)):
            got, answer = self.post_setting(body)
            self.assertEqual(got, code, body)
            self.assertTrue(answer['error'])
        self.assertEqual(self.file_text(), before)

    def test_secrets_are_never_sent_nor_changed(self):
        env = {'TUTTI_GROK_KEY': 'grok-k3y-n0t-for-the-page'}
        page = settings.view(self.c.cfg, env)
        body = json.dumps(page)
        self.assertNotIn(SECRET, body)
        self.assertNotIn('k3y-n0t', body)
        row = next(r for s in page['sections'] for r in s['rows'] if r['key'] == 'telemetry.sentry_dsn')
        self.assertEqual((row['control'], row['set'], row['setup']), ('secret', True, 'tutti telemetry setup'))
        self.assertNotIn('value', row)
        self.assertEqual(self.post_setting({'path': ['telemetry', 'sentry_dsn'], 'value': 'x'})[0], 403)
        self.assertEqual(self.post_setting({'path': ['runner', 'env'], 'yaml': 'API_TOKEN: x'})[0], 403)
        status, raw, *_ = dashboard.get(self.c, '/api/settings', 'tok', {})
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET.encode(), raw)

    def test_the_dashboard_switches_write_the_same_keys(self):
        autopilot.set_enabled(self.c.cfg, True)   # an older switch in the state folder wins until saved here
        row = next(r for s in settings.view(self.c.cfg)['sections'] for r in s['rows'] if r['key'] == 'autopilot.enabled')
        self.assertTrue(row['override']['value'])
        self.assertEqual(dashboard.save_autopilot(self.c, b'{"enabled": false, "fill_room": false}')[0], 200)
        self.assertFalse(yaml.safe_load(self.file_text())['autopilot']['enabled'])
        self.assertNotIn('enabled', autopilot.read_state(self.c.cfg))
        self.assertEqual(dashboard.save_control(self.c, b'{"action": "optin", "kind": "worker", "name": "copilot", "on": true}')[0], 200)
        self.assertIn('copilot', self.c.cfg['workers'])
        self.assertEqual(dashboard.save_autocompact(self.c, b'{"threshold_pct": 45}')[0], 200)
        self.assertEqual(yaml.safe_load(self.file_text())['autocompact'], {'threshold_pct': 45})

    def test_priority_order_numbers_the_projects(self):
        row = next(r for s in settings.view(self.c.cfg)['sections'] for r in s['rows'] if r['control'] == 'order')
        self.assertEqual(row['value'], ['tool', 'app'])
        self.assertEqual(self.post_setting({'path': ['projects'], 'order': ['app', 'tool']})[0], 200)
        self.assertEqual((config.project_priority(self.c.cfg, 'app'), config.project_priority(self.c.cfg, 'tool')), (0, 1))
        self.assertEqual(self.post_setting({'path': ['projects'], 'order': ['app']})[0], 400)
        self.assertIn('# the tool itself', self.file_text())

    def test_settings_sit_behind_the_one_auth_check(self):
        with mock.patch.object(dashboard, 'authorize', return_value=(403, 'admins only')):
            self.assertEqual(dashboard.get(self.c, '/api/settings', 'tok', {})[0], 403)
            headers = {'Host': '127.0.0.1:4777', 'Origin': 'http://127.0.0.1:4777', 'Content-Type': 'application/json',
                       'X-Tutti-Token': 'tok', 'Content-Length': '2'}
            self.assertEqual(dashboard.post(self.c, headers, '/api/restart', 4777, 'tok', lambda n: b'{}')[0], 403)

    def test_restart_runs_the_same_command_again(self):
        ran = []
        timer = mock.Mock(side_effect=lambda d, fn: SimpleNamespace(start=fn))
        self.c.argv = ['/usr/bin/python3', 'tutti', 'dashboard']
        self.c.snapshot = lambda: {}
        self.assertEqual(dashboard.restart_now(self.c, b'{}', execv=lambda *a: ran.append(a), timer=timer)[0], 200)
        self.assertEqual(ran, [('/usr/bin/python3', ['/usr/bin/python3', 'tutti', 'dashboard'])])
        self.assertEqual(dashboard.without_port(['py', 'tutti', 'dashboard', '--port', '4777', '--port=1', '--no-probe']),
                         ['py', 'tutti', 'dashboard', '--no-probe'])   # the saved dashboard.port applies after a restart
        self.assertIn(b'"started"', dashboard.route(self.c, '/api/health')[1])

    def test_restart_refuses_a_live_answer_even_if_its_conversation_was_deleted(self):
        self.c.snapshot = lambda: {}
        engine = chatrun.engine_of(self.c)
        engine.jobs['m'] = chatrun.Job('user', 'deleted', {'id': 'm'})
        timer = mock.Mock()
        code, body = dashboard.restart_now(self.c, b'{}', timer=timer)
        self.assertEqual(code, 409)
        self.assertIn('answering', body['error'])
        timer.assert_not_called()

    def test_restart_defers_if_a_chat_starts_after_preflight(self):
        self.c.snapshot = lambda: {}
        engine = chatrun.engine_of(self.c)
        pending, ran = [], []
        def timer(_delay, fn):
            return SimpleNamespace(start=lambda: pending.append(fn))
        self.assertEqual(dashboard.restart_now(self.c, b'{}', execv=lambda *a: ran.append(a), timer=timer)[0], 200)
        engine.jobs['m'] = chatrun.Job('user', 'c', {'id': 'm'})
        pending[0]()
        self.assertEqual(ran, [])

    def test_restart_refuses_streaming_or_unreadable_persisted_state(self):
        self.c.snapshot = lambda: {}
        engine = chatrun.engine_of(self.c)
        conv = engine.store.create('user', messages=[{'id': 'm', 'role': 'assistant', 'status': 'streaming'}])
        self.assertEqual(dashboard.restart_now(self.c, b'{}')[0], 409)
        engine.store.delete('user', conv['id'])
        with open(engine.store.path('user'), 'w', encoding='utf8') as f:
            f.write('{broken')
        self.assertEqual(dashboard.restart_now(self.c, b'{}')[0], 409)
        with mock.patch.object(chatrun.auth, 'fcntl', None):
            self.assertEqual(dashboard.restart_now(self.c, b'{}')[0], 409)
        with mock.patch.object(chatrun.os, 'listdir', side_effect=PermissionError('denied')):
            self.assertEqual(dashboard.restart_now(self.c, b'{}')[0], 409)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO not supported')
    def test_restart_refuses_fifo_store_without_waiting_for_a_writer(self):
        self.c.snapshot = lambda: {}
        engine = chatrun.engine_of(self.c)
        os.makedirs(engine.store.folder, exist_ok=True)
        os.mkfifo(engine.store.path('user'))
        self.assertEqual(dashboard.restart_now(self.c, b'{}')[0], 409)

    def test_chat_send_waits_for_guarded_restart(self):
        self.c.snapshot = lambda: {}
        engine = chatrun.engine_of(self.c)
        entered, release, sent = threading.Event(), threading.Event(), threading.Event()
        def action():
            entered.set()
            self.assertTrue(release.wait(2))
        worker = threading.Thread(target=lambda: chatrun.guard_chat_restart(engine, action))
        with mock.patch.object(engine, '_send_locked', side_effect=lambda *a: sent.set()):
            worker.start()
            self.assertTrue(entered.wait(2))
            sender = threading.Thread(target=lambda: engine.send({'sub': 'user'}, 'c', 'hello'))
            sender.start()
            self.assertFalse(sent.wait(0.05))
            release.set()
            worker.join(2)
            sender.join(2)
        self.assertTrue(sent.is_set())
        self.assertFalse(worker.is_alive() or sender.is_alive())

    def test_concurrent_first_chat_requests_share_one_engine(self):
        self.c.snapshot = lambda: {}
        engines = []
        start = threading.Barrier(8)
        def obtain():
            start.wait(timeout=2)
            engines.append(chatrun.engine_of(self.c))
        workers = [threading.Thread(target=obtain) for _ in range(8)]
        with mock.patch.object(chatrun, 'Engine', wraps=chatrun.Engine) as make:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(2)
        self.assertEqual(len(engines), 8)
        self.assertTrue(all(engine is engines[0] for engine in engines))
        make.assert_called_once()

    def test_restart_refuses_inflight_confirm_until_its_result_is_saved(self):
        self.c.snapshot = lambda: {}
        engine = chatrun.engine_of(self.c)
        entered, release, saved = threading.Event(), threading.Event(), threading.Event()
        card = {'state': 'proposed', 'call': {'tool': 'backlog_add', 'args': {}}}
        calls = []
        def change(_sub, _cid, _mid, _kid, fn):
            calls.append(1)
            result = fn(card)
            if len(calls) > 1:
                saved.set()
            return result
        def execute(_user, _cfg, _call, result):
            entered.set()
            self.assertTrue(release.wait(2))
            result['state'] = 'done'
        with mock.patch.object(engine, '_card_change', side_effect=change), \
                mock.patch.object(chatrun.chat, 'validate', return_value=card['call']), \
                mock.patch.object(engine, 'execute', side_effect=execute):
            worker = threading.Thread(target=lambda: engine.confirm({'sub': 'u'}, 'c', 'm', 'k'))
            worker.start()
            self.assertTrue(entered.wait(2))
            with self.assertRaises(chatrun.chat.ChatError):
                chatrun.guard_chat_restart(engine, lambda: self.fail('restart during an action'))
            self.assertFalse(saved.is_set())
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(saved.is_set())
        self.assertEqual(chatrun.guard_chat_restart(engine, lambda: 'ok'), 'ok')

    def test_new_action_waits_for_restart_guard(self):
        self.c.snapshot = lambda: {}
        engine = chatrun.engine_of(self.c)
        entered, release, claimed = threading.Event(), threading.Event(), threading.Event()
        def guarded():
            entered.set()
            self.assertTrue(release.wait(2))
        def change(_sub, _cid, _mid, _kid, fn):
            claimed.set()
            return fn({'state': 'proposed'})
        worker = threading.Thread(target=lambda: chatrun.guard_chat_restart(engine, guarded))
        with mock.patch.object(engine, '_card_change', side_effect=change):
            worker.start()
            self.assertTrue(entered.wait(2))
            action = threading.Thread(target=lambda: engine.confirm({'sub': 'u'}, 'c', 'm', 'k', yes=False))
            action.start()
            self.assertFalse(claimed.wait(0.05))
            release.set()
            worker.join(2)
            action.join(2)
        self.assertFalse(worker.is_alive() or action.is_alive())
        self.assertTrue(claimed.is_set())

    def test_action_admission_is_released_when_saving_result_fails(self):
        self.c.snapshot = lambda: {}
        engine = chatrun.engine_of(self.c)
        card = {'state': 'proposed', 'call': {'tool': 'backlog_add', 'args': {}}}
        def execute(*_args):
            engine.action_admission.mark_started()
        with mock.patch.object(engine, '_card_change', side_effect=[card, OSError('save failed')]), \
                mock.patch.object(chatrun.chat, 'validate', return_value=card['call']), \
                mock.patch.object(engine, 'execute', side_effect=execute):
            with self.assertRaises(OSError):
                engine.confirm({'sub': 'u'}, 'c', 'm', 'k')
        self.assertEqual(engine.action_admission.active, 0)
        with self.assertRaisesRegex(chatrun.chat.ChatError, 'engine blocks guarded restarts'):
            chatrun.guard_chat_restart(engine, lambda: self.fail('restart with uncertain outcome'))

    def test_preflight_refusal_does_not_create_uncertain_action_hold(self):
        self.c.snapshot = lambda: {}
        engine = chatrun.engine_of(self.c)
        with mock.patch.object(engine, '_card_change', side_effect=chatrun.chat.ChatError('gone')):
            with self.assertRaises(chatrun.chat.ChatError):
                engine.confirm({'sub': 'u'}, 'c', 'm', 'k')
        self.assertEqual(chatrun.guard_chat_restart(engine, lambda: 'ok'), 'ok')

    def test_plan_and_undo_are_admitted_before_they_start(self):
        for method in ('run_plan', 'undo'):
            with self.subTest(method=method):
                self.c.snapshot = lambda: {}
                engine = chatrun.engine_of(self.c)
                entered, release = threading.Event(), threading.Event()
                def get_cfg():
                    entered.set()
                    self.assertTrue(release.wait(2))
                    raise chatrun.chat.ChatError('stopped before execution')
                def run():
                    try:
                        getattr(engine, method)({'sub': 'u'}, 'c', 'm', 'k')
                    except chatrun.chat.ChatError:
                        pass
                with mock.patch.object(engine, 'cfg_fn', side_effect=get_cfg):
                    worker = threading.Thread(target=run)
                    worker.start()
                    self.assertTrue(entered.wait(2))
                    with self.assertRaises(chatrun.chat.ChatError):
                        chatrun.guard_chat_restart(engine, lambda: self.fail('restart during an action'))
                    release.set()
                    worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(chatrun.guard_chat_restart(engine, lambda: 'ok'), 'ok')

    def test_separate_actions_can_be_admitted_together(self):
        admission = chatrun.ActionAdmission()
        both = threading.Barrier(3)
        def action():
            with admission.action():
                both.wait(timeout=2)
        workers = [threading.Thread(target=action) for _ in range(2)]
        for worker in workers:
            worker.start()
        both.wait(timeout=2)
        for worker in workers:
            worker.join(2)
        self.assertTrue(all(not worker.is_alive() for worker in workers))

    def test_automatic_action_is_covered_by_its_live_answer(self):
        self.c.snapshot = lambda: {}
        engine = chatrun.engine_of(self.c)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        call = {'tool': 'backlog_add', 'args': {}}
        def execute(_user, _cfg, _call, card, job=None):
            entered.set()
            self.assertTrue(release.wait(2))
            card['state'] = 'done'
            return card
        def answer(job, user, _prompt):
            try:
                engine._run_mutations(job, user, engine.cfg_fn(), [call], [])
            finally:
                job.emit('done')
                finished.set()
        with mock.patch.object(engine, '_answer', side_effect=answer), \
                mock.patch.object(engine, 'new_card', return_value={}), \
                mock.patch.object(engine, 'execute', side_effect=execute):
            engine.send({'sub': 'u'}, None, 'hello')
            self.assertTrue(entered.wait(2))
            with self.assertRaises(chatrun.chat.ChatError):
                chatrun.guard_chat_restart(engine, lambda: self.fail('restart during automatic action'))
            release.set()
            self.assertTrue(finished.wait(2))


class PriorityTest(unittest.TestCase):
    CFG = {'projects': {'tutti': {'priority': 0}, 'radio': {'priority': 1}, 'map': {'priority': 2}, 'loose': {}}}

    def test_the_helper_and_its_bad_values(self):
        self.assertEqual([config.project_priority(self.CFG, n) for n in ('tutti', 'map', 'loose', 'ghost')],
                         [0, 2, config.UNRANKED_PRIORITY, config.UNRANKED_PRIORITY])
        errs = config.validate(config.normalise({'projects': {'a': {'priority': -1}, 'b': {'priority': 'high'}}}))
        self.assertEqual(sum('priority' in e for e in errs), 2)

    def test_weighted_fair_share_serves_the_important_first_and_starves_no_one(self):
        items = [(p, i) for p in ('map', 'radio', 'tutti') for i in range(20)]
        order = [p for p, _ in priority.fair_order(self.CFG, items, lambda it: it[0])]
        self.assertEqual(order[0], 'tutti')
        first = order[:11]
        self.assertEqual((first.count('tutti'), first.count('radio'), first.count('map')), (6, 3, 2))
        self.assertIn('map', order[:6])   # the least important still gets an early turn
        busy = priority.fair_order(self.CFG, items, lambda it: it[0], served={'tutti': 5})
        self.assertEqual(busy[0][0], 'radio')   # what already runs counts as served

    def test_the_autopilot_and_fill_room_follow_it(self):
        cfg = {**self.CFG, 'autopilot': {'repos': {'r-map': {'path': '/src/map'}, 'r-tutti': {'path': '/src/tutti'}}}}
        cfg['projects'] = {'tutti': {'priority': 0, 'match': 'tutti'}, 'map': {'priority': 2, 'match': 'map'}}
        tasks = [{'repo': 'r-map', 'id': 1}, {'repo': 'r-map', 'id': 2}, {'repo': 'r-tutti', 'id': 3}]
        self.assertEqual([t['id'] for t in priority.by_repo(cfg, tasks, lambda t: t['repo'])], [3, 1, 2])
        from tutti import fill
        found = [('r-map', 'a', '', '', ''), ('r-map', 'b', '', '', ''), ('r-tutti', 'c', '', '', '')]
        self.assertEqual([x[1] for x in fill.pick({'tasks': []}, found, 2, cfg)], ['c', 'a'])
        self.assertEqual([x[1] for x in fill.pick({'tasks': []}, found, 2)], ['a', 'c'])   # no priorities: round-robin

    def test_task_priority_precedes_project_share_and_explicit_value_wins(self):
        cfg = self.CFG
        tasks = [{'id': 1, 'repo': 'one', 'title': 'P1: later', 'priority': 1},
                 {'id': 2, 'repo': 'two', 'title': 'P0: first', 'priority': 0},
                 {'id': 3, 'repo': 'one', 'title': 'P0: relabelled', 'priority': 2}]
        self.assertEqual([t['id'] for t in priority.tasks_by_priority(cfg, tasks)], [2, 1, 3])
        self.assertEqual(priority.task_level({'title': 'P0: not triaged'}), 5)
        self.assertEqual(priority.task_level({'title': 'unlabelled'}), 5)
        tasks[0]['urgent'] = True
        self.assertEqual([t['id'] for t in priority.tasks_by_priority(cfg, tasks)], [1, 2, 3])

    def test_projects_remain_fair_within_the_same_task_priority(self):
        cfg = {'projects': {'a': {'priority': 0, 'match': 'a'}, 'b': {'priority': 2, 'match': 'b'}},
               'autopilot': {'repos': {'r-a': {'path': '/src/a'}, 'r-b': {'path': '/src/b'}}}}
        tasks = [{'id': f'{repo}{i}', 'repo': repo, 'priority': 0} for repo in ('r-b', 'r-a') for i in range(10)]
        ordered = priority.tasks_by_priority(cfg, tasks)
        self.assertEqual(ordered[0]['repo'], 'r-a')
        self.assertIn('r-b', [t['repo'] for t in ordered[:5]])


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class RenderTest(Base):
    FAKE_DOM = """
class Node { constructor(tag) { this.tag = tag; this.kids = []; this.attrs = {}; this.textContent = ''; this.className = ''; }
  append(...k) { this.kids.push(...k.map((x) => (typeof x === 'string' ? { text: x } : x))); }
  setAttribute(k, v) { this.attrs[k] = v; } addEventListener() {} }
global.document = { createElement: (t) => new Node(t), querySelector: () => null };
const text = (n) => (n.text !== undefined ? n.text : `${n.textContent} ${(n.kids || []).map(text).join(' ')}`);
const tags = (n) => [n.tag, ...(n.kids || []).flatMap(tags)];
"""

    def test_the_tab_renders_every_section_its_controls_and_no_secret(self):
        data = settings.view(self.c.cfg, {})
        script = self.FAKE_DOM + f"""
const s = require({json.dumps(os.path.join(ROOT, 'tutti', 'web', 'settings.js'))});
const nodes = s.renderSettings({json.dumps(data)});
console.log(JSON.stringify({{ text: nodes.map(text).join(' '), tags: nodes.flatMap(tags) }}));
s.SET.changed = true;
console.log(JSON.stringify({{ text: s.renderSettings({json.dumps(data)}).map(text).join(' ') }}));
"""
        # through stdin: one argv string may not pass 128 KB (MAX_ARG_STRLEN), and the settings' data grows
        out = subprocess.run(['node', '-'], input=script, capture_output=True, text=True, check=True).stdout.splitlines()
        page, changed = json.loads(out[0]), json.loads(out[1])
        for title in ('Accounts and readings', 'Autopilot', 'Opt-ins', 'Dashboard', 'Secrets', 'Project priority',
                      'Runs at once'):
            self.assertIn(title, page['text'])
        self.assertIn('Reset to default', page['text'])
        self.assertNotIn(SECRET, page['text'])
        self.assertLessEqual({'input', 'select', 'details', 'textarea', 'ol'}, set(page['tags']))
        self.assertIn('Runs at once', page['text'])
        self.assertNotIn('Machine order', changed['text'])   # the filter shows only what differs from the defaults

    def test_the_page_has_the_tab_and_the_worker_caches_it(self):
        with open(os.path.join(dashboard.WEB, 'index.html'), encoding='utf8') as f:
            page = f.read()
        self.assertIn('href="#settings" data-view="settings"', page)
        self.assertIn('id="settings-view"', page)
        self.assertIn('settings.js?v=', page)
        with open(os.path.join(dashboard.WEB, 'sw.js'), encoding='utf8') as f:
            self.assertIn('`settings.js${V}`', f.read())


if __name__ == '__main__':
    unittest.main()


class ManagedKeysTest(unittest.TestCase):
    """Users, the household, a user's schedule and main agent are on the Manage tab only: nothing of it in Settings."""

    def test_settings_shows_no_people_keys(self):
        cfg = config.normalise({})
        cfg['_path'] = '/tmp/none.yaml'
        payload = settings.view(cfg, env={})
        keys = [r.get('key', '') for s in payload['sections'] for r in s['rows']]
        keys += [r.get('key', '') for s in payload['sections'] for g in s.get('groups', []) for e in g.get('entries', [])
                 for r in e.get('rows', [])]
        self.assertEqual([k for k in keys if settings.managed(k)], [])
        self.assertNotIn('Household membership', str(payload['secrets']))
        self.assertIn('main.fallback', keys)   # the system part of the main agent's section stays
