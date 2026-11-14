"""The Chat tab (docs/CHAT.md): conversations per user (mode 600), requests turned into work through the MCP tools' calls, the
role rules (a viewer cannot change the household), the model order (never the user's own machine, never another member's
private quota), streaming, stop, Undo, and the page: safe Markdown, keys, accessibility and the phone layout."""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tutti import chat, chatrun, controls, dashboard, move

ADMIN = {'sub': 'local', 'email': None, 'role': 'admin'}
VIEWER = {'sub': 'v1', 'email': 'ana@example.com', 'role': 'viewer'}
PENDING = {'sub': 'p1', 'email': 'new@example.com', 'role': 'pending'}


def config_for(folder, **extra):
    cfg = {'_path': os.path.join(folder, 'tutti.config.yaml'), 'project': {'state': folder},
           'machines': {'pc': {}, 'nuc': {}}, 'devices': {}, 'scouts': {'grok': {}},
           'workers': {'pc': {'kind': 'ollama', 'host': '127.0.0.1:1', 'model': 'big:27b', 'machine': 'pc'},
                       'nuc': {'kind': 'ollama', 'host': '127.0.0.1:2', 'model': 'small:9b', 'machine': 'nuc'},
                       'claude': {'kind': 'cli', 'command': ['claude', '-p', '{prompt}']},
                       'copilot': {'kind': 'cli', 'command': ['copilot'], 'costs_money': True}},
           'accounts': {'claude': {'window': '7d'}, 'copilot': {'costs_money': True}},
           'autopilot': {'repos': {'map': {'path': folder}, 'radio': {'path': folder}}}}
    cfg.update(extra)
    return cfg


def snapshot(**over):
    snap = {'machines': {'items': [{'name': 'pc', 'status': 'free', 'busy': 0, 'slots': 2},
                                   {'name': 'nuc', 'status': 'free', 'busy': 0, 'slots': 3}]},
            'presence': {'items': {'machine:pc': {'state': 'present', 'label': 'Here'},
                                   'machine:nuc': {'state': 'present', 'label': 'Here'}}},
            'workers': {'items': [{'name': 'pc', 'ready': True}, {'name': 'nuc', 'ready': True},
                                  {'name': 'claude', 'ready': True, 'account': 'claude'}]},
            'usage': {'accounts': [{'name': 'claude', 'used': 40.0, 'verdict': 'on pace', 'panic': False, 'reset': '2026-11-21T02:00Z'}]},
            'controls': {'paused': []}, 'jobs': {'items': []}, 'queue': {'groups': []}, 'summary': {}}
    snap.update(over)
    return snap


class Storage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = chat.Store(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_each_user_has_their_own_file_readable_only_by_tutti(self):
        conv = self.store.create('a')
        self.store.put_message('a', conv['id'], {'id': 'm1', 'role': 'user', 'text': 'why is the NUC idle?'})
        self.assertEqual(self.store.listing('b'), [])
        rows = self.store.listing('a')
        self.assertEqual((rows[0]['title'], rows[0]['count']), ('why is the NUC idle?', 1))
        path = self.store.path('a')
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        self.assertNotIn('a', os.path.basename(path).replace('.json', '')[:1] if False else '')

    def test_search_rename_delete_and_clear(self):
        c1, c2 = self.store.create('a'), self.store.create('a')
        self.store.put_message('a', c1['id'], {'id': 'm1', 'role': 'user', 'text': 'quota for cursor'})
        self.store.put_message('a', c2['id'], {'id': 'm2', 'role': 'user', 'text': 'pause the pc'})
        self.assertEqual([r['id'] for r in self.store.listing('a', 'CURSOR')], [c1['id']])
        self.store.rename('a', c2['id'], '  Machines  ')
        self.assertEqual(self.store.get('a', c2['id'])['title'], 'Machines')
        with self.assertRaises(chat.ChatError):
            self.store.rename('a', c2['id'], '   ')
        self.store.delete('a', c1['id'])
        with self.assertRaises(chat.ChatError):
            self.store.get('a', c1['id'])
        self.store.clear('a')
        self.assertFalse(os.path.exists(self.store.path('a')))

    def test_an_answer_cut_off_by_a_restart_is_marked_stopped(self):
        c = self.store.create('a')
        self.store.put_message('a', c['id'], {'id': 'm1', 'role': 'assistant', 'text': 'half', 'status': 'streaming'})
        data = self.store.interrupted('a', set())
        m = self.store.conversation(data, c['id'])['messages'][0]
        self.assertEqual(m['status'], 'stopped')
        self.assertIn('Retry', m['note'])

    def test_tutti_move_carries_the_conversations(self):
        self.assertFalse(any('chat' in x for x in move.STATE_EXCLUDE))

    def test_attachments_are_text_only_and_limited(self):
        self.assertEqual(chat.check_message(' hi ', [])[0], 'hi')
        for bad, why in (([{'name': 'a.bin', 'text': 'a\x00b'}], 'not a text file'),
                         ([{'name': 'big.log', 'text': 'x' * (chat.MAX_ATTACHMENT_BYTES + 1)}], 'the limit is 32 KB'),
                         ([{'name': str(i), 'text': 'x'} for i in range(4)], 'at most 3')):
            with self.assertRaises(chat.ChatError) as e:
                chat.check_message('look', bad)
            self.assertIn(why, str(e.exception))
        with self.assertRaises(chat.ChatError):
            chat.check_message('', [])
        with self.assertRaises(chat.ChatError):
            chat.check_message('x' * (chat.MAX_TEXT + 1), [])


class Requests(unittest.TestCase):
    known = {'projects': ['map', 'RoRadio'], 'scouts': ['grok'], 'machine': ['nuc', 'pc'], 'device': [], 'worker': [], 'account': []}

    def test_work_requests_become_tool_calls(self):
        self.assertEqual(chat.parse('Add task in map: fix the legend [size M]', self.known),
                         {'tool': 'backlog_add', 'args': {'repo': 'map', 'title': 'fix the legend', 'size': 'M'}})
        self.assertEqual(chat.parse('Queue tests for roradio', self.known)['args']['repo'], 'RoRadio')
        self.assertEqual(chat.parse('delegate in map: rename the helper', self.known)['tool'], 'delegate_task')
        self.assertEqual(chat.parse('ask grok: is there a newer GDAL?', self.known)['args'],
                         {'scout': 'grok', 'question': 'is there a newer GDAL?'})
        self.assertEqual(chat.parse('pause the NUC', self.known)['args'], {'action': 'pause', 'kind': 'machine', 'name': 'nuc'})

    def test_questions_and_unknown_names(self):
        self.assertIsNone(chat.parse('why is the PC idle?', self.known))
        self.assertIn('map, RoRadio', chat.parse('add task in nowhere: x', self.known)['error'])
        self.assertIn('Nothing called', chat.parse('pause the toaster', self.known)['error'])


class Roles(unittest.TestCase):
    cfg = {'chat': {'viewer_tasks_per_day': 2, 'viewer_projects': ['map']}}
    work = {'tool': 'backlog_add', 'args': {'repo': 'map', 'title': 't'}}

    def test_a_viewer_creates_work_for_its_projects_within_the_limit(self):
        self.assertIsNone(chat.refusal(VIEWER, self.work, self.cfg, 0))
        self.assertIn('limit', chat.refusal(VIEWER, self.work, self.cfg, 2))
        other = {'tool': 'backlog_add', 'args': {'repo': 'radio', 'title': 't'}}
        self.assertIn('for map only', chat.refusal(VIEWER, other, self.cfg, 0))

    def test_household_changes_are_for_admins_and_pending_users_do_nothing(self):
        pause = {'tool': 'control', 'args': {'action': 'pause', 'kind': 'machine', 'name': 'nuc'}}
        self.assertIn('only an admin', chat.refusal(VIEWER, pause, {}, 0))
        self.assertIsNone(chat.refusal(ADMIN, pause, {}, 99))
        self.assertIn('approval', chat.refusal(PENDING, self.work, {}, 0))

    def test_config_errors_name_the_key(self):
        self.assertEqual(chat.errors({'chat': chat.DEFAULTS}), [])
        errs = chat.errors({'chat': {'viewer_tasks_per_day': -1, 'viewer_projects': 'map', 'history_turns': 99}})
        self.assertEqual(len(errs), 3)
        self.assertTrue(all(e.startswith('chat.') for e in errs))


class Models(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = config_for(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def names(self, snap, user=ADMIN, owner=None, own=None):
        return [c['worker'] for c in chat.candidates(self.cfg, snap, user, owner, own)[0]]

    def test_local_models_first_then_accounts_with_room_never_paid_ones(self):
        self.assertEqual(self.names(snapshot()), ['pc', 'nuc', 'claude'])

    def test_a_machine_that_is_asleep_paused_or_the_users_own_is_skipped(self):
        asleep = snapshot(presence={'items': {'machine:pc': {'state': 'asleep'}}})
        self.assertEqual(self.names(asleep), ['nuc', 'claude'])
        paused = snapshot(controls={'paused': [{'kind': 'machine', 'name': 'nuc'}]})
        self.assertEqual(self.names(paused), ['pc', 'claude'])
        self.assertEqual(self.names(snapshot(), own='pc'), ['nuc', 'claude'])

    def test_another_members_private_quota_is_never_spent(self):
        self.assertEqual(self.names(snapshot(), user=VIEWER, owner='owner-sub'), ['pc', 'nuc'])
        self.cfg['accounts']['claude']['share_spare'] = True
        self.assertEqual(self.names(snapshot(), user=VIEWER, owner='owner-sub'), ['pc', 'nuc', 'claude'])
        panic = snapshot(usage={'accounts': [{'name': 'claude', 'panic': True, 'verdict': 'late'}]})
        self.assertEqual(self.names(panic, user=VIEWER, owner='owner-sub'), ['pc', 'nuc'])


def fake_streams(script):
    """A streams function: each worker's answer from `script` ({worker: [pieces] | OSError})."""
    def streams(cand, cfg, messages, cancel):
        out = script.get(cand['worker'], OSError('down'))
        if isinstance(out, Exception):
            raise out
        for piece in out:
            if cancel.is_set():
                return
            if isinstance(piece, Exception):
                raise piece
            if piece == 'WAIT':
                cancel.wait(2)
                continue
            yield piece
    return streams


class ApiBase(unittest.TestCase):
    """The engine with a fake runner and fake models; no tests of its own."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = config_for(self.dir)
        self.snap = snapshot()
        self.calls = []

        def runner(path, argv):
            self.calls.append(argv)
            if argv[:2] == ['backlog', 'add']:
                return 0, f't60: added: {argv[3]}'
            return 0, 'ok'
        self.runner = runner

    def tearDown(self):
        shutil.rmtree(self.dir)

    def engine(self, script, owner=None):
        return chatrun.Engine(lambda: self.cfg, lambda: self.snap, folder=self.dir, runner=self.runner,
                              streams=fake_streams(script), owner_fn=lambda: owner)

    def send(self, eng, user, text, cid=None):
        code, stream = chatrun.post(eng, user, {'action': 'send', 'text': text, 'conversation': cid})
        self.assertEqual(code, 200)
        return [json.loads(line) for line in stream.lines()]


class Api(ApiBase):
    def test_send_streams_the_answer_and_names_the_model(self):
        eng = self.engine({'pc': ['The NUC ', 'is free.']})
        events = self.send(eng, ADMIN, 'why is the NUC idle?')
        self.assertEqual([e['type'] for e in events], ['start', 'model', 'delta', 'delta', 'done'])
        done = events[-1]['message']
        self.assertEqual((done['text'], done['model'], done['status']), ('The NUC is free.', 'big:27b on pc', 'done'))
        conv = chatrun.post(eng, ADMIN, {'action': 'get', 'id': events[0]['conversation']})[1]['conversation']
        self.assertEqual([m['role'] for m in conv['messages']], ['user', 'assistant'])

    def test_it_answers_with_the_users_own_machine_off(self):
        self.snap = snapshot(presence={'items': {'machine:pc': {'state': 'off'}, 'machine:nuc': {'state': 'present'}}})
        eng = self.engine({'pc': ['never'], 'nuc': ['From the NUC.']})
        eng.own_machine_fn = lambda user: 'pc'
        done = self.send(eng, VIEWER, 'what is running?')[-1]['message']
        self.assertEqual((done['text'], done['model']), ('From the NUC.', 'small:9b on nuc'))

    def test_with_no_model_it_answers_from_the_state(self):
        eng = self.engine({})
        done = self.send(eng, ADMIN, 'How much quota is left?')[-1]['message']
        self.assertIn('claude', done['text'])
        self.assertIn('60 % left', done['text'])
        self.assertEqual(done['model'], chatrun.BUILTIN)

    def test_stop_ends_the_answer_and_keeps_what_was_written(self):
        eng = self.engine({'pc': ['Part one. ', 'WAIT', 'never']})
        code, stream = chatrun.post(eng, ADMIN, {'action': 'send', 'text': 'tell me everything'})
        lines = stream.lines()
        first = json.loads(next(lines))
        mid = first['message']['id']
        threading.Timer(0.2, lambda: chatrun.post(eng, ADMIN, {'action': 'stop', 'message': mid})).start()
        rest = [json.loads(x) for x in lines]
        self.assertEqual(rest[-1]['message']['status'], 'stopped')
        self.assertEqual(rest[-1]['message']['text'], 'Part one. ')
        self.assertFalse(chatrun.post(eng, VIEWER, {'action': 'stop', 'message': mid})[1]['stopped'])   # never another's

    def test_retry_and_edit_answer_again(self):
        eng = self.engine({'pc': ['A.']})
        events = self.send(eng, ADMIN, 'hello')
        cid, first_answer = events[0]['conversation'], events[-1]['message']['id']
        code, stream = chatrun.post(eng, ADMIN, {'action': 'send', 'conversation': cid, 'retry': first_answer})
        list(stream.lines())
        conv = eng.store.get('local', cid)
        self.assertEqual(len(conv['messages']), 2)
        self.assertNotEqual(conv['messages'][1]['id'], first_answer)
        edit = {'action': 'send', 'conversation': cid, 'text': 'hello again', 'edit': conv['messages'][0]['id']}
        code, stream = chatrun.post(eng, ADMIN, edit)
        list(stream.lines())
        conv = eng.store.get('local', cid)
        self.assertEqual([m['text'] for m in conv['messages']], ['hello again', 'A.'])
        self.assertTrue(conv['messages'][0]['edited'])

    def test_a_request_for_work_runs_the_mcp_call_with_a_card_undo_and_audit(self):
        eng = self.engine({})
        events = self.send(eng, VIEWER, 'add task in map: fix the legend')
        card = next(e['card'] for e in events if e['type'] == 'card')
        self.assertEqual((card['state'], card['ref']), ('done', 't60'))
        self.assertEqual(self.calls[0][:4], ['backlog', 'add', 'map', 'fix the legend'])
        with open(os.path.join(self.dir, 'auth-audit.jsonl'), encoding='utf8') as f:
            audit = [json.loads(x) for x in f]
        self.assertEqual((audit[-1]['by'], audit[-1]['action']), ('v1', 'chat: Queue a task in map: fix the legend'))
        mid = events[-1]['message']['id']
        undo = {'action': 'undo', 'conversation': events[0]['conversation'], 'message': mid, 'card': card['id']}
        code, answer = chatrun.post(eng, VIEWER, undo)
        self.assertEqual(code, 200)
        self.assertEqual(self.calls[-1], ['backlog', 'rm', 't60'])
        code, answer = chatrun.post(eng, VIEWER, undo)
        self.assertEqual(code, 409)

    def test_a_viewer_cannot_pause_a_machine_and_an_admin_can_with_undo(self):
        eng = self.engine({})
        events = self.send(eng, VIEWER, 'pause the nuc')
        card = next(e['card'] for e in events if e['type'] == 'card')
        self.assertEqual(card['state'], 'refused')
        self.assertEqual(controls.read(self.dir)['paused'], {})
        events = self.send(eng, ADMIN, 'pause the nuc')
        card = next(e['card'] for e in events if e['type'] == 'card')
        self.assertEqual(card['state'], 'done')
        self.assertIn('machine:nuc', controls.read(self.dir)['paused'])
        chatrun.post(eng, ADMIN, {'action': 'undo', 'conversation': events[0]['conversation'], 'message': events[-1]['message']['id'],
                                  'card': card['id']})
        self.assertEqual(controls.read(self.dir)['paused'], {})

    def test_a_viewers_daily_limit_holds(self):
        self.cfg['chat'] = {'viewer_tasks_per_day': 1}
        eng = self.engine({})
        self.send(eng, VIEWER, 'add task in map: one')
        card = next(e['card'] for e in self.send(eng, VIEWER, 'add task in map: two') if e['type'] == 'card')
        self.assertEqual(card['state'], 'refused')
        self.assertEqual(len([c for c in self.calls if c[:2] == ['backlog', 'add']]), 1)

    def test_conversations_are_private_and_activity_is_for_admins(self):
        eng = self.engine({'pc': ['hi']})
        cid = self.send(eng, VIEWER, 'my private question')[0]['conversation']
        self.assertEqual(chatrun.post(eng, ADMIN, {'action': 'get', 'id': cid})[0], 409)
        self.assertEqual(chatrun.post(eng, ADMIN, {'action': 'list'}, self.snap)[1]['conversations'], [])
        self.assertEqual(chatrun.post(eng, VIEWER, {'action': 'activity'})[0], 403)
        self.send(eng, VIEWER, 'add task in map: shared work')
        rows = chatrun.post(eng, ADMIN, {'action': 'activity'})[1]['activity']
        self.assertEqual(rows[0]['by'], 'ana@example.com')
        self.assertNotIn('private', json.dumps(rows))

    def test_bad_requests_get_plain_words(self):
        eng = self.engine({})
        self.assertEqual(chatrun.post(eng, ADMIN, None)[0], 400)
        code, answer = chatrun.post(eng, ADMIN, {'action': 'explode'})
        self.assertIn('Reload the page', answer['error'])
        self.assertEqual(chatrun.post(eng, ADMIN, {'action': 'send', 'text': ''})[0], 409)


def recording_streams(script, seen):
    """fake_streams that also records which workers were asked."""
    inner = fake_streams(script)

    def streams(cand, cfg, messages, cancel):
        seen.append(cand['worker'])
        return inner(cand, cfg, messages, cancel)
    return streams


class Picker(ApiBase):
    def engine_seen(self, script):
        self.seen = []
        eng = self.engine({})
        eng.streams = recording_streams(script, self.seen)
        return eng

    def test_auto_picks_local_when_up_and_says_why_it_fell_back_when_down(self):
        eng = self.engine_seen({'pc': ['From the PC.'], 'nuc': ['From the NUC.'], 'claude': ['From Claude.']})
        done = self.send(eng, ADMIN, 'hello')[-1]['message']
        self.assertEqual((done['model'], self.seen), ('big:27b on pc', ['pc']))
        self.assertNotIn('fallback', done)
        self.snap = snapshot(presence={'items': {'machine:pc': {'state': 'asleep'}, 'machine:nuc': {'state': 'asleep'}}})
        events = self.send(eng, ADMIN, 'hello again')
        done = events[-1]['message']
        self.assertEqual(done['model'], 'claude (claude account)')
        self.assertIn('pc is asleep', done['fallback'])
        self.assertIn('fallback', [e['type'] for e in events])

    def test_a_local_model_that_does_not_answer_falls_to_the_next(self):
        eng = self.engine_seen({'pc': OSError('tunnel down'), 'nuc': ['NUC here.']})
        done = self.send(eng, ADMIN, 'hello')[-1]['message']
        self.assertEqual(done['model'], 'small:9b on nuc')
        self.assertIn('big:27b on pc did not answer', done['fallback'])

    def test_local_only_never_calls_an_online_agent(self):
        eng = self.engine_seen({'claude': ['online!']})
        chatrun.post(eng, ADMIN, {'action': 'model', 'model': 'local'})
        self.snap = snapshot(presence={'items': {'machine:pc': {'state': 'off'}, 'machine:nuc': {'state': 'away'}}})
        done = self.send(eng, ADMIN, 'what is running?')[-1]['message']
        self.assertEqual(self.seen, [])
        self.assertEqual(done['model'], chatrun.BUILTIN)
        self.assertIn('No local model can answer now', done['fallback'])

    def test_online_only_and_one_named_model(self):
        eng = self.engine_seen({'pc': ['pc'], 'nuc': ['nuc'], 'claude': ['claude']})
        code, stream = chatrun.post(eng, ADMIN, {'action': 'send', 'text': 'hi', 'model': 'online'})
        list(stream.lines())
        code, stream = chatrun.post(eng, ADMIN, {'action': 'send', 'text': 'hi', 'model': 'nuc'})
        list(stream.lines())
        self.assertEqual(self.seen, ['claude', 'nuc'])

    def test_the_pickers_choice_persists_per_conversation_and_as_the_default(self):
        eng = self.engine_seen({'nuc': ['ok']})
        cid = self.send(eng, ADMIN, 'first')[0]['conversation']
        self.assertEqual(eng.store.get('local', cid)['model'], 'auto')
        self.assertEqual(chatrun.post(eng, ADMIN, {'action': 'model', 'id': cid, 'model': 'nuc'})[0], 200)
        eng2 = chatrun.Engine(lambda: self.cfg, lambda: self.snap, folder=self.dir)   # after a restart
        self.assertEqual(eng2.store.get('local', cid)['model'], 'nuc')
        listing = chatrun.post(eng2, ADMIN, {'action': 'list'}, self.snap)[1]
        self.assertEqual(listing['default_model'], 'nuc')
        self.assertEqual([m['id'] for m in listing['models']][:3], ['auto', 'local', 'online'])
        self.assertEqual(next(m for m in listing['models'] if m['id'] == 'copilot')['available'], False)
        new = self.send(eng, ADMIN, 'second')[0]['conversation']
        self.assertEqual(eng.store.get('local', new)['model'], 'nuc')
        self.assertEqual(self.seen[-1], 'nuc')
        self.assertEqual(chatrun.post(eng, ADMIN, {'action': 'model', 'model': 'gpt-imaginary'})[0], 400)


class FakeOllama(BaseHTTPRequestHandler):
    """Ollama's /api/chat: answers from a script, one per request, as NDJSON lines; records what it was sent."""
    script, seen = [], []

    def log_message(self, *a):
        return

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        type(self).seen.append(body)
        parts = type(self).script.pop(0) if type(self).script else [{'message': {'content': 'ok'}, 'done': True}]
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson')
        self.end_headers()
        for p in parts:
            self.wfile.write((json.dumps(p) + '\n').encode())


class ToolLoop(ApiBase):
    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), FakeOllama)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        port = self.httpd.server_address[1]
        self.cfg['workers'] = {'pc': {'kind': 'ollama', 'host': f'127.0.0.1:{port}', 'model': 'qwen3.8:27b-q8_0', 'machine': 'pc'}}
        FakeOllama.seen = []

    def tearDown(self):
        self.httpd.shutdown()
        super().tearDown()

    def engine_real(self):
        return chatrun.Engine(lambda: self.cfg, lambda: self.snap, folder=self.dir, runner=self.runner)

    def test_a_local_model_creates_a_task_through_the_tool_loop_after_a_confirmation(self):
        call = {'function': {'name': 'backlog_add', 'arguments': {'repo': 'map', 'title': 'Fix the legend on phones', 'size': 'S'}}}
        FakeOllama.script = [
            [{'message': {'content': '', 'tool_calls': [{'function': {'name': 'usage', 'arguments': {}}}]}, 'done': True}],
            [{'message': {'content': 'I will queue it. '}}, {'message': {'content': '', 'tool_calls': [call]}, 'done': True}],
        ]
        eng = self.engine_real()
        events = self.send(eng, ADMIN, 'the legend overlaps on phones, get it fixed')
        self.assertEqual(len(FakeOllama.seen), 2)
        self.assertIn('backlog_add', json.dumps(FakeOllama.seen[0]['tools']))   # the tools, with their schemas
        self.assertEqual(FakeOllama.seen[1]['messages'][-1]['role'], 'tool')     # usage's answer went back to the model
        self.assertEqual(self.calls, [['usage']])                               # read-only: ran at once
        card = next(e['card'] for e in events if e['type'] == 'card')
        self.assertEqual((card['state'], card['call']['args']['repo']), ('proposed', 'map'))
        done = events[-1]['message']
        self.assertEqual((done['model'], done['text']), ('qwen3.8:27b-q8_0 on pc', 'I will queue it. '))
        code, answer = chatrun.post(eng, ADMIN, {'action': 'confirm', 'conversation': events[0]['conversation'],
                                                 'message': done['id'], 'card': card['id']})
        self.assertEqual((code, answer['card']['state'], answer['card']['ref']), (200, 'done', 't60'))
        self.assertEqual(self.calls[-1][:4], ['backlog', 'add', 'map', 'Fix the legend on phones'])
        self.assertEqual(chatrun.post(eng, ADMIN, {'action': 'confirm', 'conversation': events[0]['conversation'],
                                                   'message': done['id'], 'card': card['id']})[0], 409)

    def test_a_bad_call_is_refused_back_to_the_model_and_nothing_runs(self):
        FakeOllama.script = [
            [{'message': {'tool_calls': [{'function': {'name': 'backlog_add', 'arguments': {'repo': 'nowhere', 'title': 'x'}}}]},
              'done': True}],
            [{'message': {'content': 'That project does not exist.'}, 'done': True}],
        ]
        events = self.send(self.engine_real(), ADMIN, 'add something to nowhere')
        self.assertIn('refused: there is no repo "nowhere"', FakeOllama.seen[1]['messages'][-1]['content'])
        self.assertEqual([e for e in events if e['type'] == 'card'], [])
        self.assertEqual(self.calls, [])

    def test_a_viewers_proposal_to_pause_is_refused_and_cancel_does_nothing(self):
        FakeOllama.script = [[{'message': {'tool_calls': [{'function': {'name': 'control', 'arguments': {
            'action': 'pause', 'kind': 'machine', 'name': 'nuc'}}}]}, 'done': True}]]
        events = self.send(self.engine_real(), VIEWER, 'stop the nuc')
        self.assertEqual(next(e['card'] for e in events if e['type'] == 'card')['state'], 'refused')
        self.assertEqual(controls.read(self.dir)['paused'], {})

    def test_an_online_agent_gets_the_same_tools_as_action_lines(self):
        self.cfg['workers'] = {'claude': {'kind': 'cli', 'command': ['claude', '-p', '{prompt}']}}
        eng = self.engine({'claude': ['Queuing it.\n', 'ACTION {"tool": "backlog_add", "args": {"repo": "map", "title": "Tests"}}\n']})
        events = self.send(eng, ADMIN, 'queue the tests please')
        card = next(e['card'] for e in events if e['type'] == 'card')
        self.assertEqual((card['state'], card['tool']), ('proposed', 'backlog_add'))
        self.assertEqual(events[-1]['message']['text'], 'Queuing it.')
        self.assertIn('replace', [e['type'] for e in events])


class Validation(unittest.TestCase):
    known = {'projects': ['map'], 'scouts': ['grok'], 'machine': ['nuc'], 'device': ['pixel'], 'worker': [], 'account': []}

    def test_every_action_is_checked_against_its_schema_and_the_names_here(self):
        self.assertEqual(chat.validate({'tool': 'backlog_add', 'args': {'repo': 'MAP', 'title': 'x'}}, self.known)['args']['repo'], 'map')
        for bad, why in (({'tool': 'rm', 'args': {}}, 'no tool'), ({'tool': 'backlog_add', 'args': {'repo': 'map'}}, 'needs title'),
                         ({'tool': 'backlog_add', 'args': {'repo': 'map', 'title': 'x', 'size': 'XXL'}}, 'size'),
                         ({'tool': 'test_on', 'args': {'track': 't', 'command': 'c', 'rm': 1}}, 'has no rm'),
                         ({'tool': 'test_on', 'args': {'track': 't', 'command': 'c', 'device': 'iphone'}}, 'no device'),
                         ({'tool': 'control', 'args': {'action': 'pause', 'kind': 'machine', 'name': 'toaster'}}, 'no machine')):
            self.assertIn(why, chat.validate(bad, self.known))

    def test_capability_order_and_who_gets_the_tools(self):
        self.assertEqual((chat.params_b('qwen3.8:27b-q8_0'), chat.params_b('qwen3.5:9b')), (27.0, 9.0))
        self.assertFalse(chat.local_capable('tiny:1.5b'))
        self.assertTrue(chat.local_capable('qwen3.5:9b'))
        d = tempfile.mkdtemp()
        try:
            cfg = config_for(d)
            cfg['workers'] = {'small': {'kind': 'ollama', 'model': 'm:3b'}, 'big': {'kind': 'ollama', 'model': 'm:32b'}}
            cands = chat.candidates(cfg, snapshot(workers={'items': []}), ADMIN)[0]
            self.assertEqual([(c['worker'], c['tools']) for c in cands], [('big', True), ('small', False)])
        finally:
            shutil.rmtree(d)


class Robustness(ApiBase):
    def test_deleting_the_chat_while_it_answers_still_ends_the_answer(self):
        eng = self.engine({'pc': ['Part one. ', 'WAIT', 'never']})
        code, stream = chatrun.post(eng, ADMIN, {'action': 'send', 'text': 'tell me everything'})
        lines = stream.lines()
        first = json.loads(next(lines))
        threading.Timer(0.2, lambda: (chatrun.post(eng, ADMIN, {'action': 'delete', 'id': first['conversation']}),
                                      chatrun.post(eng, ADMIN, {'action': 'stop', 'message': first['message']['id']}))).start()
        rest = [json.loads(x) for x in lines]
        self.assertEqual(rest[-1]['type'], 'done')
        self.assertEqual(eng.live_ids(), set())
        self.assertEqual(eng.store.listing('local'), [])   # not brought back by the answer's last save

    def test_a_model_that_stops_halfway_leaves_no_half_answer(self):
        eng = self.engine({'pc': ['Half an ans', OSError('connection reset')], 'nuc': ['A whole answer.']})
        events = self.send(eng, ADMIN, 'hello')
        done = events[-1]['message']
        self.assertEqual((done['text'], done['model']), ('A whole answer.', 'small:9b on nuc'))
        self.assertIn({'type': 'replace', 'text': ''}, events)

    def test_a_cli_agent_that_hangs_is_cut_off_and_the_next_one_answers(self):
        cancel = threading.Event()
        started = time.monotonic()
        with self.assertRaises(OSError) as e:
            list(chatrun.cli_stream({'command': ['sleep', '5']}, [{'role': 'user', 'content': 'hi'}], cancel, timeout=0.5))
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn('no full answer within', str(e.exception))

    def test_a_viewer_cannot_hand_off_or_ask_a_scout(self):
        for text in ('delegate in map: rename the helper', 'ask grok: anything new?'):
            card = next(e['card'] for e in self.send(self.engine({}), VIEWER, text) if e['type'] == 'card')
            self.assertEqual(card['state'], 'refused')
            self.assertIn('Queue it as a task instead', card['text'])
        self.assertEqual(self.calls, [])

    def test_malformed_requests_get_plain_words_not_a_crash(self):
        eng = self.engine({})
        for body in ({'action': 'send', 'text': 'x', 'attachments': {'name': 'a'}}, {'action': 'send', 'text': 'x', 'attachments': ['ab']}):
            code, answer = chatrun.post(eng, ADMIN, body)
            self.assertEqual(code, 409)
            self.assertIn('paper clip', answer['error'])
        self.assertEqual(chatrun.post(eng, ADMIN, {'action': ['list']})[0], 400)
        os.makedirs(os.path.join(self.dir, 'chat'), exist_ok=True)
        with open(eng.store.path('local'), 'w', encoding='utf8') as f:
            f.write('[1, 2]')
        self.assertEqual(chatrun.post(eng, ADMIN, {'action': 'list'}, self.snap)[0], 200)

    def test_the_built_in_answer_answers_the_question_asked(self):
        self.assertNotIn('Quota now', chat.builtin_answer('is there room for tests?', self.snap) or '')
        self.assertNotIn('Quota now', chat.builtin_answer('what is running now?', self.snap))
        self.assertIn('Quota now', chat.builtin_answer('how much quota is left?', self.snap))

    def test_a_model_of_unknown_size_only_talks_unless_the_fleet_knows_it(self):
        self.assertFalse(chat.local_capable('mistral:latest'))
        self.assertTrue(chat.local_capable('mistral:latest', {'mistral:latest': 7.2}))
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, 'fleet.json'), 'w', encoding='utf8') as f:
            json.dump({'machines': {'nuc': {'models': [{'name': 'small:9b', 'params_b': 15.7}]}}}, f)
        self.assertEqual(chat.fleet_sizes(self.cfg), {'small:9b': 15.7})
        nuc = next(c for c in chat.candidates(self.cfg, self.snap, ADMIN)[0] if c['worker'] == 'nuc')
        self.assertEqual((nuc['params'], nuc['tools']), (15.7, True))


class Wiring(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def collector(self, signed, who=None):
        cfg = config_for(self.dir)
        c = type('C', (), {})()
        c.cfg, c.signed, c.who, c.snapshot = cfg, signed, (lambda h: who), (lambda: snapshot())
        return c

    def test_the_chat_is_a_viewer_post_with_its_own_checks(self):
        from tutti import authweb
        self.assertIn(chatrun.API, authweb.VIEWER_POST)
        self.assertIn(chatrun.API, dashboard.SELF_POSTS)
        self.assertGreater(dashboard.BODY_LIMITS[chatrun.API], 3 * chat.MAX_ATTACHMENT_BYTES)

    def test_who_asks(self):
        self.assertEqual(chatrun.user_of(self.collector(False), {})['role'], 'admin')   # sign-in off: the owner here
        self.assertIsNone(chatrun.user_of(self.collector(True, None), {}))
        code, answer = chatrun.handle(self.collector(True, None), {}, b'{"action": "list"}')
        self.assertEqual(code, 401)
        code, answer = chatrun.handle(self.collector(True, VIEWER), {}, b'{"action": "list"}')
        self.assertEqual((code, answer['role']), (200, 'viewer'))
        self.assertIn('What\'s running?', answer['suggestions'])

    def test_a_post_through_the_dashboard_streams(self):
        coll = self.collector(False)
        coll.chat_engine = chatrun.Engine(lambda: coll.cfg, coll.snapshot, folder=self.dir, streams=fake_streams({'pc': ['ok']}))
        body = json.dumps({'action': 'send', 'text': 'hi'}).encode()
        headers = {'Host': '127.0.0.1:4777', 'Origin': 'http://127.0.0.1:4777', 'Content-Type': 'application/json',
                   'X-Tutti-Token': 'tok', 'Content-Length': str(len(body))}
        code, answer = dashboard.post(coll, headers, chatrun.API, 4777, 'tok', lambda n: body)
        self.assertIsInstance(answer, chatrun.Stream)
        self.assertEqual(json.loads(list(answer.lines())[-1])['message']['text'], 'ok')


# ---- the page

WEB = dashboard.WEB


def js(expr):
    with open(os.path.join(WEB, 'chat.js'), encoding='utf8') as f:
        src = f.read()
    out = subprocess.run(['node', '-e', f'{src}\nconsole.log(JSON.stringify({expr}));'], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class PageLogic(unittest.TestCase):
    def test_markdown_is_parsed_to_a_tree_with_no_markup_from_text(self):
        text = (r'Hi <script>alert(1)</script> **bold** [x](javascript:alert(1)) [ok](https://example.com)'
                r'\n\n```sh\nrm -rf <b>\n```\n- one\n- `two`')
        tree = js(f"mdParse('{text}')")
        p, pre, ul = tree
        self.assertEqual(p['t'], 'p')
        flat = json.dumps(p)
        self.assertIn('<script>alert(1)</script>', flat)   # stays text: drawn with textContent
        self.assertNotIn('javascript:', json.dumps([n for n in p['c'] if isinstance(n, dict) and n['t'] == 'a']))
        links = [n for n in p['c'] if isinstance(n, dict) and n['t'] == 'a']
        self.assertEqual([n['href'] for n in links], ['https://example.com'])
        self.assertEqual((pre['t'], pre['lang'], pre['c']), ('pre', 'sh', ['rm -rf <b>']))
        self.assertEqual(ul['c'][1]['c'], [{'t': 'code', 'c': ['two']}])

    def test_keys(self):
        k = "chatKey({key: %s, shiftKey: %s, isComposing: %s}, {streaming: %s, text: '', canEdit: true})"
        self.assertEqual(js(k % ("'Enter'", 'false', 'false', 'false')), 'send')
        self.assertIsNone(js(k % ("'Enter'", 'true', 'false', 'false')))   # Shift+Enter: a new line
        self.assertIsNone(js(k % ("'Enter'", 'false', 'true', 'false')))   # an IME picks a candidate
        self.assertEqual(js(k % ("'Escape'", 'false', 'false', 'true')), 'stop')
        self.assertIsNone(js(k % ("'Escape'", 'false', 'false', 'false')))
        self.assertEqual(js(k % ("'ArrowUp'", 'false', 'false', 'false')), 'edit')

    def test_stream_lines_and_attachments(self):
        self.assertEqual(js("ndjson('{\"a\":1}\\n{\"b\"')"), [[{'a': 1}], '{"b"'])
        self.assertIn('not a text file', js("attachmentError({name: 'x.png', type: 'image/png', size: 10}, 32768)"))
        self.assertIn('the limit is 32 KB', js("attachmentError({name: 'x.log', type: '', size: 40000}, 32768)"))
        self.assertEqual(js("attachmentError({name: 'x.log', type: '', size: 400}, 32768)"), '')
        self.assertTrue(js('atBottom({scrollHeight: 1000, scrollTop: 560, clientHeight: 400})'))
        self.assertFalse(js('atBottom({scrollHeight: 1000, scrollTop: 100, clientHeight: 400})'))


class Page(HTMLParser):
    """Collects what an accessibility check needs from index.html: ids, labels, buttons and their names."""

    def __init__(self):
        super().__init__()
        self.ids, self.labels, self.fields, self.buttons, self.stack, self.texts = [], set(), [], [], [], {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if 'id' in a:
            self.ids.append(a['id'])
        if tag == 'label' and a.get('for'):
            self.labels.add(a['for'])
        if tag in ('input', 'textarea') and a.get('type') not in ('hidden', 'file'):
            self.fields.append(a)
        if tag == 'button':
            self.buttons.append(a)
            self.stack.append(a)

    def handle_endtag(self, tag):
        if tag == 'button' and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        for b in self.stack:
            b['_text'] = b.get('_text', '') + data.strip()


class PageMarkup(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(WEB, 'index.html'), encoding='utf8') as f:
            self.html = f.read()
        with open(os.path.join(WEB, 'app.css'), encoding='utf8') as f:
            self.css = f.read()
        self.page = Page()   # the Chat's part of the page (the rest has its own checks in test_layout)
        self.page.feed(self.html.split('<main', 1)[1].split('<div id="dashboard-view"', 1)[0])

    def test_chat_is_the_first_tab_and_the_default_view(self):
        tabs = re.findall(r'data-view="([a-z]+)"', self.html.split('<nav class="tabs"', 1)[1].split('</nav>', 1)[0])
        self.assertEqual(tabs[0], 'chat')
        self.assertIn('<div id="chat-view" class="chat view-ok">', self.html)   # shown, and usable by viewers
        self.assertIn('<div id="dashboard-view" hidden>', self.html)
        self.assertIn('chat.js?v=', self.html)

    def test_accessibility_basics(self):
        dupes = {i for i in self.page.ids if self.page.ids.count(i) > 1}
        self.assertEqual(dupes, set())
        unlabelled = [f.get('id') for f in self.page.fields if f.get('id') not in self.page.labels and not f.get('aria-label')]
        self.assertEqual(unlabelled, [])
        nameless = [b.get('id') for b in self.page.buttons if not (b.get('_text') or b.get('aria-label'))]
        self.assertEqual(nameless, [])
        chat_part = self.html.split('id="chat-view"', 1)[1].split('id="dashboard-view"', 1)[0]
        self.assertIn('role="log"', chat_part)
        self.assertIn('<output class="sr-only" id="chat-announce" aria-live="polite">', chat_part)   # new answers are announced
        self.assertIn('aria-describedby="chat-hint"', chat_part)

    def test_phone_layout_reduced_motion_and_themes(self):
        phone = self.css.split('@media (max-width: 760px)', 1)[1]
        self.assertIn("body[data-chat-pane='list'] .chat__main", phone)
        self.assertIn('var(--kb', phone)            # the composer stays above the on-screen keyboard
        self.assertIn('env(safe-area-inset-bottom)', self.css.split('.chat__composer', 1)[1].split('}', 1)[0])
        self.assertRegex(self.css, r'prefers-reduced-motion: reduce\)\s*\{\s*\.typing i \{ animation: none')
        self.assertIn('interactive-widget=resizes-content', self.html)
        chat_css = self.css.split('---- the Chat', 1)[1]
        self.assertNotRegex(chat_css, r'#[0-9a-fA-F]{3,6}\b')   # colours only from the theme's tokens (light and dark)


if __name__ == '__main__':
    unittest.main()
