"""Idle with work (t254): room somewhere while queued work waits is never silent; the queue's own reasons come with it."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tutti import dashboard, idlecheck


def snap(machines=(), accounts=(), tasks=(), autopilot=True):
    return {'machines': {'items': list(machines)}, 'usage': {'accounts': list(accounts)}, 'autopilot': {'enabled': autopilot},
            'queue': {'groups': [{'items': [{'kind': 'task', 'id': f't{i}', 'why': why} for i, why in enumerate(tasks)]
                                  + [{'kind': 'shard', 'id': 's1', 'why': 'a device'}]}]}}


FREE = {'name': 'nuc', 'status': 'free', 'free': 2}
BEHIND = {'name': 'cursor', 'verdict': 'behind', 'used': 12.0}


class Log(unittest.TestCase):
    def test_a_new_case_is_logged_once_and_a_repeat_is_not(self):
        found = idlecheck.find(snap([FREE], tasks=['a model is busy']))
        first = idlecheck.keep([], idlecheck.entry(found, 'Mon 10:00'))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]['idle'], ['nuc (machine)'])
        self.assertEqual(idlecheck.keep(first, idlecheck.entry(found, 'Mon 10:01')), first)   # same case: not again
        other = idlecheck.find(snap([FREE], [BEHIND], tasks=['a model is busy']))
        self.assertEqual(len(idlecheck.keep(first, idlecheck.entry(other, 'Mon 10:02'))), 2)
        ended = idlecheck.keep(first, None)   # nothing idle: the case ends, nothing new is logged
        self.assertEqual((len(ended), ended[-1]['ended']), (1, True))
        self.assertEqual(len(idlecheck.keep(ended, idlecheck.entry(found, 'Mon 11:00'))), 2)   # the same case again later is logged
        self.assertEqual(idlecheck.keep(['junk', 3, *first], None)[0]['sig'], first[0]['sig'])   # non-entries are dropped
        self.assertEqual(len(idlecheck.keep([{'sig': str(i)} for i in range(600)], None)), idlecheck.LOG_KEEP)

    def test_the_dashboard_keeps_the_log_and_survives_a_broken_file(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {'project': {'state': d}}
            with open(os.path.join(d, dashboard.IDLE_LOG), 'w') as f:
                f.write('not json')
            self.assertEqual(dashboard.idle_log(cfg, None), [])
            found = idlecheck.find(snap([FREE], tasks=['busy']))
            shown = dashboard.idle_log(cfg, found)
            self.assertEqual((len(shown), shown[0]['waiting'], 'sig' in shown[0]), (1, 1, False))
            self.assertEqual(len(dashboard.idle_log(cfg, found)), 1)


class Find(unittest.TestCase):
    def test_a_free_machine_or_an_account_behind_with_queued_tasks_is_reported_with_the_reasons(self):
        machines = [FREE, {'name': 'pi', 'status': 'busy', 'free': 0}]
        accounts = [BEHIND, {'name': 'agy', 'verdict': 'ahead', 'used': 90}]
        w = idlecheck.find(snap(machines, accounts, ['binding limit', 'binding limit', 'no check']))
        self.assertEqual([r['name'] for r in w['idle']], ['nuc', 'cursor'])
        self.assertEqual(w['idle'][0]['room'], '2 free slots')
        self.assertEqual(w['waiting'], 3)
        self.assertEqual(w['reasons'], [['binding limit', 2], ['no check', 1]])
        self.assertEqual(w['headline'], 'Idle with work: nuc, cursor could take some of the 3 queued tasks')

    def test_nothing_is_reported_without_room_or_without_queued_tasks(self):
        self.assertIsNone(idlecheck.find(snap([FREE], [], [])))
        self.assertIsNone(idlecheck.find(snap([{'name': 'pi', 'status': 'free', 'free': 0}], [{**BEHIND, 'panic': True}], ['x'])))
        self.assertIsNone(idlecheck.find({}))

    def test_the_autopilot_being_off_is_the_first_reason(self):
        w = idlecheck.find(snap([FREE], [], ['x'], autopilot=False))
        self.assertIn('the autopilot is off', w['reasons'][0][0])

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_page_shows_the_note_and_nothing_when_there_is_none(self):
        with open(os.path.join(dashboard.WEB, 'queue.js'), encoding='utf8') as f:
            src = f.read()
        stub = ('function el(tag, attrs, ...kids) { return { tag, attrs: attrs || {}, kids: kids.flat() }; }\n'
                "function text(n) { return typeof n === 'string' ? n : n ? "
                "[(n.attrs || {}).text || '', ...(n.kids || []).map(text)].join(' ') : ''; }\n")
        w = idlecheck.find(snap([FREE], [], ['binding limit']))
        out = json.loads(subprocess.run(['node', '-e', f'{stub}\n{src}\nconsole.log(JSON.stringify([text(idleNote({json.dumps(w)})), '
                                                       'idleNote(null)]))'], capture_output=True, text=True, check=True).stdout)
        self.assertIn('nuc (machine): 2 free slots', out[0])
        self.assertIn('1 waiting: binding limit', out[0])
        self.assertIsNone(out[1])


if __name__ == '__main__':
    unittest.main()
