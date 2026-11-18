"""Chat hand-over package: HANDOVER.md, Chat seed, main pick routing, transcript import, secrets redacted."""
import json
import os
import tempfile
import unittest

from tutti import chat, chathandover, conduct, config


def cfg_in(folder, **over):
    raw = {'project': {'worktrees': folder, 'state': os.path.join(folder, 'state')},
           'workers': {'agy': {'kind': 'cli', 'command': ['agy', '-p', '{prompt}']},
                       'cursor': {'kind': 'cli', 'command': ['cursor-agent', '-p', '{prompt}']},
                       'pc': {'kind': 'ollama', 'host': '127.0.0.1:1', 'model': 'm:1', 'machine': 'pc'}},
           'accounts': {'agy-claude': {'window': '5h', 'resets': '2026-01-01T00:00Z', 'worker': 'agy'},
                        'agy-gemini': {'window': '5h', 'resets': '2026-01-01T00:00Z', 'worker': 'agy'},
                        'cursor': {'window': 'monthly', 'resets': '2026-01-01T00:00Z', 'worker': 'cursor'}},
           'machines': {'pc': {'host': 'local'}},
           'main': {'agent': 'agy', 'fallback': ['cursor'], 'check_seconds': 0.05},
           'brief': {'rules': ['Priorities: Tutti first.', 'Never commit secrets.']}}
    raw = config.merge(raw, over)
    path = os.path.join(folder, 'tutti.config.json')
    with open(path, 'w', encoding='utf8') as f:
        json.dump(raw, f)
    return config.load(path)


class PackageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.cfg = cfg_in(self.dir)
        self.home = os.path.join(self.dir, 'home')
        os.makedirs(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_md_has_rules_tasks_and_no_secrets(self):
        md = chathandover.build_md('agy', 'cursor', 'agy-claude: panic',
                                   ['Never commit secrets.'],
                                   ['t1 [queued] tutti: fix chat'], {'tutti': '# tutti\n'})
        self.assertIn('MAIN AGENT ROLE: **cursor**', md)
        self.assertIn('Never commit secrets.', md)
        self.assertIn('t1 [queued] tutti: fix chat', md)
        self.assertIn('`tutti.md`', md)

    def test_secrets_in_turns_are_redacted(self):
        turn = chathandover.parse_jsonl_turn({
            'type': 'user', 'message': {'content': 'token=sk-ant-api03-VERYSECRETVALUE123456 and Bearer abcdefghijklmnop'}
        })
        self.assertEqual(turn[0], 'user')
        self.assertNotIn('VERYSECRET', turn[1])
        self.assertNotIn('abcdefghijklmnop', turn[1])

    def test_incremental_transcript_cursor(self):
        path = os.path.join(self.dir, 'a.jsonl')
        with open(path, 'w', encoding='utf8') as f:
            f.write(json.dumps({'type': 'user', 'message': {'content': 'hello'}}) + '\n')
        turns, off = chathandover.read_new_turns(path, 0)
        self.assertEqual(turns, [('user', 'hello')])
        with open(path, 'a', encoding='utf8') as f:
            f.write(json.dumps({'type': 'assistant', 'message': {'content': 'hi'}}) + '\n')
        more, off2 = chathandover.read_new_turns(path, off)
        self.assertEqual(more, [('assistant', 'hi')])
        self.assertGreater(off2, off)
        empty, _ = chathandover.read_new_turns(path, off2)
        self.assertEqual(empty, [])

    def test_incremental_cursor_does_not_skip_unread_tail(self):
        path = os.path.join(self.dir, 'b.jsonl')
        first = json.dumps({'type': 'user', 'message': {'content': 'first'}}) + '\n'
        second = json.dumps({'type': 'assistant', 'message': {'content': 'second'}}) + '\n'
        with open(path, 'w', encoding='utf8') as f:
            f.write(first + second)
        turns, off = chathandover.read_new_turns(path, 0, limit=len(first) + 5)
        self.assertEqual(turns, [('user', 'first')])
        self.assertEqual(off, len(first))
        more, off2 = chathandover.read_new_turns(path, off, limit=100)
        self.assertEqual(more, [('assistant', 'second')])
        self.assertEqual(off2, os.path.getsize(path))

    def test_deliver_seeds_chat_writes_desktop_and_sets_main_pick(self):
        note = chathandover.deliver(self.cfg, 'agy', 'cursor', 'agy-claude: panic', home=self.home)
        self.assertIn('MAIN AGENT ROLE handed from agy to cursor', note)
        root = chathandover.folder(self.cfg)
        self.assertTrue(os.path.isfile(os.path.join(root, 'HANDOVER.md')))
        store = chat.Store(os.path.join(self.dir, 'state'))
        data = store.load('local')
        conv = next(c for c in data['conversations'] if c['title'] == chathandover.CHAT_TITLE)
        self.assertIn('Hand-over for the next main agent', conv['messages'][0]['text'])
        self.assertEqual(data['defaults']['model'], 'main')
        desk = os.path.join(self.home, '.cursor', 'rules', 'tutti-handover.md')
        self.assertTrue(os.path.isfile(desk))
        with open(desk, encoding='utf8') as f:
            self.assertIn('MAIN AGENT ROLE', f.read())

    def test_deliver_exports_transcripts_to_chat_and_desktop_context(self):
        folder = os.path.join(self.home, '.claude', 'projects', 'demo')
        os.makedirs(folder, exist_ok=True)
        transcript = os.path.join(folder, 'one.jsonl')
        with open(transcript, 'w', encoding='utf8') as f:
            f.write(json.dumps({'type': 'user', 'message': {'content': 'What changed today?'}}) + '\n')
            f.write(json.dumps({'type': 'assistant', 'message': {'content': 'Main moved to cursor.'}}) + '\n')
        chathandover.deliver(self.cfg, 'agy', 'cursor', 'agy-claude: panic', home=self.home)
        exported = os.path.join(chathandover.folder(self.cfg), chathandover.TRANSCRIPTS_FILE)
        self.assertTrue(os.path.isfile(exported))
        with open(exported, encoding='utf8') as f:
            text = f.read()
        self.assertIn('What changed today?', text)
        self.assertIn('Main moved to cursor.', text)
        desktop_copy = os.path.join(self.home, '.cursor', 'rules', 'tutti-handover-transcripts.md')
        self.assertTrue(os.path.isfile(desktop_copy))
        with open(desktop_copy, encoding='utf8') as f:
            self.assertIn('Main moved to cursor.', f.read())

    def test_main_pick_follows_current_agent(self):
        conduct.choose(self.cfg, 'cursor')
        self.assertEqual(chat.resolve_pick('main', self.cfg), 'cursor')
        snap = {'machines': {'items': []}, 'presence': {'items': {}}, 'workers': {'items': [
            {'name': 'agy', 'ready': True, 'account': 'agy-claude'},
            {'name': 'cursor', 'ready': True, 'account': 'cursor'},
        ]}, 'usage': {'accounts': [
            {'name': 'agy-claude', 'used': 10, 'verdict': 'on pace', 'panic': False},
            {'name': 'cursor', 'used': 10, 'verdict': 'on pace', 'panic': False},
        ]}, 'controls': {'paused': []}}
        user = {'sub': 'local', 'role': 'admin'}
        cands, _ = chat.candidates(self.cfg, snap, user, pick='main')
        self.assertEqual([c['worker'] for c in cands], ['cursor'])

    def test_status_for_chat_names_the_main_agent(self):
        st = chathandover.status_for_chat(self.cfg)
        self.assertEqual(st['name'], 'agy')
        self.assertEqual(st['label'], 'agy (Antigravity)')
        self.assertIn('Antigravity', st['show'])


class RoleWithPackageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.cfg = cfg_in(self.dir)
        self.home = os.path.join(self.dir, 'home')
        os.makedirs(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def states(self, **rows):
        out = []
        for n, v in rows.items():
            if isinstance(v, dict):
                out.append({'name': n, 'panic_at': 95, 'used': 0, 'verdict': 'on pace', 'panic': False, **v})
            else:
                out.append({'name': n, 'panic': v >= 95, 'used': v, 'verdict': 'late', 'panic_at': 95})
        return out

    def test_panic_hands_role_packages_chat_and_routes_to_new_main(self):
        notes, connects = [], []
        line = conduct.hand_off_role(
            self.cfg, self.states(**{'agy-claude': 99, 'agy-gemini': 97, 'cursor': 10}),
            connect_fn=lambda path, names: connects.append(names) or [],
            chat_fn=lambda cfg, text: notes.append(text), home=self.home)
        self.assertIn('MAIN AGENT ROLE handed from agy to cursor', line)
        self.assertEqual(conduct.current(self.cfg), 'cursor')
        self.assertEqual(connects, [['cursor']])
        self.assertTrue(os.path.isfile(os.path.join(chathandover.folder(self.cfg), 'HANDOVER.md')))
        self.assertEqual(chat.resolve_pick('main', self.cfg), 'cursor')
        again = conduct.hand_off_role(
            self.cfg, self.states(**{'agy-claude': 99, 'agy-gemini': 97, 'cursor': 10}),
            connect_fn=lambda *a, **k: connects.append('x') or [],
            chat_fn=lambda cfg, text: notes.append(text), home=self.home)
        self.assertIsNone(again)
        self.assertEqual(notes, [line])
        self.assertEqual(connects, [['cursor']])

    def test_refresh_updates_desktop_transcript_when_markdown_unchanged(self):
        folder = os.path.join(self.home, '.claude', 'projects', 'demo')
        os.makedirs(folder, exist_ok=True)
        transcript = os.path.join(folder, 'one.jsonl')
        with open(transcript, 'w', encoding='utf8') as f:
            f.write(json.dumps({'type': 'user', 'message': {'content': 'first'}}) + '\n')
        conduct.hand_off_role(
            self.cfg, self.states(**{'agy-claude': 99, 'agy-gemini': 97, 'cursor': 10}),
            connect_fn=lambda *a, **k: [], home=self.home)
        desktop_copy = os.path.join(self.home, '.cursor', 'rules', 'tutti-handover-transcripts.md')
        with open(desktop_copy, encoding='utf8') as f:
            self.assertIn('first', f.read())
        with open(transcript, 'a', encoding='utf8') as f:
            f.write(json.dumps({'type': 'assistant', 'message': {'content': 'second'}}) + '\n')
        self.assertIsNone(conduct.hand_off_role(
            self.cfg, self.states(**{'agy-claude': 99, 'agy-gemini': 97, 'cursor': 10}), home=self.home))
        with open(desktop_copy, encoding='utf8') as f:
            self.assertIn('second', f.read())


if __name__ == '__main__':
    unittest.main()
