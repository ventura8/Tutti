"""The dashboard's Queued list (tutti/waitlist.py, tutti/web/queue.js): everything waiting, grouped by project in priority
order, each with why it waits; an admin's actions; the page renders each item with its reason (run in node)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tutti import autopilot, backlog, config, dashboard, waitlist


class Waitlist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raw = {'project': {'state': os.path.join(self.tmp.name, 'state')},
               'workers': {'agy': {'kind': 'cli', 'command': ['agy', '{prompt}']}},
               'accounts': {'agy-gemini': {'worker': 'agy'}, 'agy-claude': {'worker': 'agy'}},
               'projects': {'Tutti': {'priority': 0, 'self_improve': {'autopilot_repo': 'tutti'}},
                            'Map': {'priority': 2, 'self_improve': {'autopilot_repo': 'map'}}},
               'autopilot': {'enabled': True, 'accounts': ['agy-gemini', 'agy-claude'], 'ladder': ['agy-gemini', 'agy-claude'],
                             'repos': {'tutti': {'path': self.tmp.name, 'check': 'true'}, 'map': {'path': self.tmp.name, 'check': 'true'}}}}
        self.cfg = config.normalise(raw)
        os.makedirs(self.cfg['project']['state'], exist_ok=True)
        with backlog.locked(self.cfg) as data:
            backlog.add(data, backlog.new_task('map', 'Fix the layer list'))
            backlog.add(data, backlog.new_task('tutti', 'Probe once per round'))
            backlog.add(data, backlog.new_task('tutti', 'Second Tutti task'))
        with autopilot.state_locked(self.cfg) as st:
            st['last'] = {'state': 'stopped', 'reason': 'no worker takes a task now',
                          'waiting': {'t1': 'agy-claude weekly binding until 12:55 (UTC+2)'}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_items_are_grouped_by_priority_with_their_reason_and_counts(self):
        v = waitlist.view(self.cfg, running=3)
        self.assertEqual(v['header'], '3 running · 3 queued')
        self.assertEqual([g['project'] for g in v['groups']], ['Tutti', 'Map'])
        t1 = v['groups'][1]['items'][0]
        self.assertEqual(t1['why'], 'agy-claude weekly binding until 12:55 (UTC+2)')
        self.assertEqual(t1['to'], 'agy-gemini')
        self.assertEqual(v['groups'][0]['items'][0]['why'], 'no worker takes a task now')
        self.assertIn('run', t1['actions'])
        self.assertEqual(waitlist.view(self.cfg, can_act=False)['groups'][0]['items'][0]['actions'], [])   # viewers: read-only

    def test_an_admin_reorders_moves_and_cancels_and_a_bad_action_is_named(self):
        self.assertEqual(waitlist.act(self.cfg, {'kind': 'task', 'id': 't3', 'action': 'up'})[0], 200)
        self.assertEqual(waitlist.act(self.cfg, {'kind': 'task', 'id': 't1', 'action': 'move', 'to': 'agy-claude'})[0], 200)
        tutti = waitlist.view(self.cfg)['groups'][0]['items']
        self.assertEqual([i['id'] for i in tutti], ['t3', 't2'])                         # priority up
        self.assertEqual(waitlist.view(self.cfg)['groups'][1]['items'][0]['to'], 'agy-claude')
        self.assertEqual(autopilot.candidates(self.cfg, backlog.find(backlog.load(self.cfg), 't1'))[0], 'agy-claude')
        self.assertEqual(waitlist.act(self.cfg, {'kind': 'task', 'id': 't2', 'action': 'cancel'})[0], 200)
        self.assertEqual(backlog.find(backlog.load(self.cfg), 't2')['status'], 'dropped')
        code, answer = waitlist.act(self.cfg, {'kind': 'task', 'id': 't1', 'action': 'move', 'to': 'nowhere'})
        self.assertEqual(code, 400)
        self.assertIn('agy-gemini', answer['error'])
        self.assertEqual(waitlist.act(self.cfg, {'kind': 'task', 'id': 't9', 'action': 'run'})[0], 404)
        self.assertIn('/api/queue', dashboard.ADMIN_PATHS)

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_page_renders_each_queued_item_with_its_reason(self):
        with open(os.path.join(dashboard.WEB, 'queue.js'), encoding='utf8') as f:
            src = f.read()
        stub = '''
const nodes = {};
function el(tag, attrs, ...kids) { return { tag, attrs: attrs || {}, kids: kids.flat() }; }
function text(n) { return typeof n === 'string' ? n : n ? [(n.attrs || {}).text || '', ...(n.kids || []).map(text)].join(' ') : ''; }
const $ = (id) => nodes[id] || (nodes[id] = { id, textContent: '', replaceChildren(...k) { this.kids = k; } });
let TOKEN = '', refresh = async () => {}, showBanner = () => {};
'''
        state = {'autopilot': {'ladder': ['agy-gemini']}, 'queue': waitlist.view(self.cfg, running=1)}
        show = 'console.log(JSON.stringify([nodes.queued.kids.map(text).join(" "), nodes["queued-meta"].textContent]));'
        js = f'{stub}\n{src}\nrenderQueue({json.dumps(state)});\n{show}'
        out = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
        self.assertIn('Waiting: agy-claude weekly binding until 12:55 (UTC+2)', out[0])
        self.assertIn('Tutti · priority 0 · 2 queued', out[0])
        self.assertIn('Run now', out[0])
        self.assertIn('easy · local', out[0])
        self.assertEqual(out[1], '1 running · 3 queued')

    def test_classification_defaults_overrides_and_ladder_routing(self):
        t_easy = backlog.new_task('tutti', 'Fix typo in README', scope=['README.md'], size='S')
        self.assertEqual((t_easy['difficulty'], t_easy['runner']), ('easy', 'local'))
        t_hard = backlog.new_task('tutti', 'Refactor routing architecture', size='L')
        self.assertEqual((t_hard['difficulty'], t_hard['runner']), ('hard', 'both'))
        t_online = backlog.new_task('tutti', 'Visual browser check', size='S')
        self.assertEqual(t_online['runner'], 'online')
        with self.assertRaises(backlog.BacklogError):
            backlog.new_task('tutti', 'Bad diff', difficulty='impossible')
        with self.assertRaises(backlog.BacklogError):
            backlog.new_task('tutti', 'Bad runner', runner='quantum')

        code, _ = waitlist.act(self.cfg, {'kind': 'task', 'id': 't1', 'action': 'classify', 'difficulty': 'hard', 'runner': 'online'})
        self.assertEqual(code, 200)
        t1 = waitlist.view(self.cfg)['groups'][1]['items'][0]
        self.assertEqual((t1['difficulty'], t1['runner']), ('hard', 'online'))
        self.assertEqual(waitlist.act(self.cfg, {'kind': 'task', 'id': 't1', 'action': 'classify', 'difficulty': 'nope'})[0], 400)

        local_cfg = config.normalise({
            'project': {'state': os.path.join(self.tmp.name, 'state')},
            'workers': {'agy': {'kind': 'cli', 'command': ['agy', '{prompt}']},
                        'pc': {'kind': 'ollama', 'model': 'qwen', 'url': 'http://127.0.0.1:11434'}},
            'accounts': {'agy-gemini': {'worker': 'agy'}},
            'autopilot': {'enabled': True, 'accounts': ['agy-gemini'], 'ladder': ['agy-gemini', 'pc'],
                          'repos': {'tutti': {'path': self.tmp.name, 'check': 'true'}}}})
        self.assertEqual(autopilot.candidates(local_cfg, t_easy)[0], 'pc')
        t_online_scoped = {**t_online, 'scope': ['README.md']}
        self.assertNotIn('pc', autopilot.candidates(local_cfg, t_online_scoped))


if __name__ == '__main__':
    unittest.main()
