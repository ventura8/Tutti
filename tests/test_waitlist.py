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

    def test_board_preserves_empty_lanes_and_handles_non_task_work(self):
        with backlog.locked(self.cfg) as data:
            backlog.find(data, 't1')['depends_on'] = ['t2']
            backlog.find(data, 't2')['depends_on'] = ['t3']
        where = {'current': {'status': 'running', 'log': ['waiting for checks']}}
        v = waitlist.view(self.cfg, where=where, can_act=False)
        self.assertEqual([lane['lane'] for lane in v['board']], ['Blocked', 'Ready', 'Grouped'])
        self.assertEqual(v['queued'], 4)
        blocked = {item['id']: item for item in v['board'][0]['items']}
        self.assertEqual(blocked['t1']['eta'], 'after t2, t3 land')
        self.assertEqual(blocked['t1']['actions'], [])
        self.assertEqual(v['graph']['t2']['depends_on'], ['t3'])
        self.assertEqual(v['board'][2]['items'], [])

    def test_board_shows_a_group_and_moves_it_to_blocked_when_a_member_waits(self):
        with backlog.locked(self.cfg) as data:
            backlog.find(data, 't1')['together_with'] = ['t2']
        board = waitlist.view(self.cfg)['board']
        self.assertEqual({item['id'] for item in board[2]['items']}, {'t1', 't2'})
        with backlog.locked(self.cfg) as data:
            backlog.find(data, 't2')['depends_on'] = ['t3']
        board = waitlist.view(self.cfg)['board']
        self.assertEqual({item['id']: item['eta'] for item in board[0]['items']}, {'t1': 'after t3 land', 't2': 'after t3 land'})
        self.assertEqual(board[2]['items'], [])

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
        with backlog.locked(self.cfg) as data:
            backlog.find(data, 't2')['depends_on'] = ['t3']
        state = {'autopilot': {'ladder': ['agy-gemini']}, 'queue': waitlist.view(self.cfg, running=1)}
        show = '''
const projects = nodes.queued.kids.map(text).join(" ");
queueBoard = true;
renderQueue(state);
console.log(JSON.stringify([projects, nodes["queued-meta"].textContent, nodes.queued.kids.map(text).join(" ")]));
'''
        js = f'{stub}\n{src}\nconst state = {json.dumps(state)}; renderQueue(state);\n{show}'
        out = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
        self.assertIn('Waiting: agy-claude weekly binding until 12:55 (UTC+2)', out[0])
        self.assertIn('Tutti · priority 0 · 2 queued', out[0])
        self.assertIn('Run now', out[0])
        self.assertEqual(out[1], '1 running · 3 queued')
        for label in ('Blocked', 'Ready', 'Grouped', 'Dependency graph', 't2 → t3 (queued)'):
            self.assertIn(label, out[2])


if __name__ == '__main__':
    unittest.main()
