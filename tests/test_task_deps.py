"""Task dependencies: a blocked, merged or group-blocked task never starts; `backlog deps` and the Board lanes say why."""
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tutti import backlog, cli


def task(tid, status='queued', **kw):
    return {'id': tid, 'status': status, 'title': f'task {tid}', 'repo': 'r', **kw}


class Deps(unittest.TestCase):
    def test_blocked_until_dependency_is_done(self):
        tasks = [task('t1'), task('t2', depends_on=['t1'])]
        self.assertTrue(backlog.is_blocked(tasks[1], tasks))
        tasks[0]['status'] = 'done'
        self.assertFalse(backlog.is_blocked(tasks[1], tasks))

    def test_merged_task_never_starts(self):
        tasks = [task('t1'), task('t2', merged_into='t1')]
        self.assertTrue(backlog.is_blocked(tasks[1], tasks))
        self.assertFalse(backlog.is_blocked(tasks[0], tasks))

    def test_group_waits_for_a_blocked_member(self):
        tasks = [task('t1'), task('t2', together_with=['t1'], depends_on=['t3']), task('t3'),
                 task('t4', together_with=['t2'])]
        tasks[0]['together_with'] = ['t2']
        self.assertTrue(backlog.is_blocked(tasks[0], tasks))
        self.assertTrue(backlog.is_blocked(tasks[3], tasks))
        self.assertFalse(backlog.is_blocked(tasks[2], tasks))

    def test_seeded_prerequisites_must_both_land(self):
        tasks = [task('t242', depends_on=['t211', 't241'], notes=['#606', 'device move (feat/adb-via-device-host)']),
                 task('t211', 'done'), task('t241', 'review')]
        self.assertEqual(backlog.blockers(tasks[0], {t['id']: t for t in tasks}), ['t241'])
        tasks[2]['status'] = 'done'
        self.assertFalse(backlog.is_blocked(tasks[0], tasks))
        self.assertIn('Depends on: t211, t241', backlog.deps({'tasks': tasks}, 't242'))

    def test_seeded_groups_are_independent_and_transitive(self):
        members = [('t199', 't200', 't201', 't202', 't187'), ('t233', 't212', 't235'), ('t181', 't185', 't239')]
        tasks = [task(tid, together_with=list(group[i + 1:i + 2])) for group in members for i, tid in enumerate(group)]
        groups = backlog.group_index(tasks)
        self.assertEqual(groups, {tid: frozenset(group) for group in members for tid in group})

    def test_missing_and_cyclic_merge_targets_stay_blocked(self):
        tasks = [task('t1', depends_on=['t2'])]
        self.assertTrue(backlog.is_blocked(tasks[0], tasks))
        tasks.extend([task('t2', merged_into='t3'), task('t3', merged_into='t2')])
        self.assertTrue(backlog.is_blocked(tasks[0], tasks))
        tasks[2].update(status='done', merged_into=None)
        self.assertFalse(backlog.is_blocked(tasks[0], tasks))

    def test_link_validates_before_changing_existing_links(self):
        data = {'tasks': [task('t1'), task('t2')]}
        backlog.link(data, 't1', {'depends_on': ['t2']})
        with self.assertRaises(backlog.BacklogError):
            backlog.link(data, 't1', {'depends_on': ['t1']})
        self.assertEqual(data['tasks'][0]['depends_on'], ['t2'])

    def test_terminal_lookup_recovers_from_an_unknown_id(self):
        data = {'tasks': [task('t1', depends_on=['t2']), task('t2', status='done')]}
        out = io.StringIO()
        with mock.patch('builtins.input', side_effect=['t9', 't1', '']), mock.patch.object(backlog, 'load', return_value=data), \
                mock.patch.object(cli, 'say') as say, redirect_stdout(out):
            self.assertEqual(cli.cmd_ui({}, None), 0)
        self.assertIn('no task "t9"', say.call_args.args[0])
        self.assertIn('Depends on: t2', out.getvalue())
        self.assertIn('t2 (done)', out.getvalue())

    def test_deps_text_and_unknown_id(self):
        data = {'tasks': [task('t1', depends_on=['t2'], together_with=['t3'], merged_into='t4')]}
        out = backlog.deps(data, 't1')
        for part in ('Depends on: t2', 'Together with: t3', 'Merged into: t4'):
            self.assertIn(part, out)
        with self.assertRaises(backlog.BacklogError):
            backlog.deps(data, 't9')


if __name__ == '__main__':
    unittest.main()
