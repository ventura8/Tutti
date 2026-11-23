"""Task dependencies, groups and merges (t243): blocked tasks never start, the Queued list says why, the CLI sets and shows them."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from tutti import backlog, cli, taskdeps


def task(tid, status='queued', **kw):
    return {'id': tid, 'title': f'task {tid}', 'repo': 'app', 'status': status, **kw}


class Deps(unittest.TestCase):
    def test_a_task_waits_until_what_it_depends_on_lands(self):
        tasks = [task('t1', depends_on=['t2', 't3']), task('t2'), task('t3', status='done'), task('t4', depends_on=['t5']),
                 task('t5', status='dropped')]
        self.assertEqual(taskdeps.holds(tasks), {'t1': 'blocked until t2 lands (depends_on)'})
        tasks[1]['status'] = 'done'
        self.assertEqual(taskdeps.holds(tasks), {})

    def test_a_dependency_merged_into_another_waits_for_that_target(self):
        tasks = [task('t1', merged_into='t2'), task('t2'), task('t3', depends_on=['t1'])]
        self.assertEqual(taskdeps.waiting_on(tasks[2], tasks), ['t1'])
        tasks[1]['status'] = 'done'
        self.assertEqual(taskdeps.waiting_on(tasks[2], tasks), [])   # t1 landed with t2

    def test_loops_through_merges_are_refused_and_a_dense_graph_is_fast(self):
        tasks = [task('t1', merged_into='t2'), task('t2')]
        with self.assertRaisesRegex(taskdeps.DepError, 'loop'):
            taskdeps.link(tasks, 't2', merged_into='t1')
        tasks = [task('t1', depends_on=['t2']), task('t2')]
        with self.assertRaisesRegex(taskdeps.DepError, 'loop'):
            taskdeps.link(tasks, 't2', merged_into='t1')
        layers = [task(f'a{i}', depends_on=[f'a{i + 2}', f'a{i + 3}'] if i < 60 else []) for i in range(64)]
        self.assertEqual(taskdeps.cycle(layers, 'a0'), [])   # each task visited once: no exponential walk

    def test_a_merged_task_never_runs_on_its_own(self):
        holds = taskdeps.holds([task('t1', merged_into='t2'), task('t2')])
        self.assertIn('merged into t2', holds['t1'])
        self.assertNotIn('t2', holds)

    def test_groups_go_both_ways_and_the_board_has_three_lanes(self):
        tasks = [task('t1', together_with=['t2']), task('t2'), task('t3', together_with=['t2']), task('t4', depends_on=['t1'])]
        self.assertEqual(taskdeps.group_of(tasks[1], tasks), ['t1', 't2', 't3'])
        self.assertEqual(taskdeps.lanes(tasks), {'blocked': ['t4'], 'ready': ['t1', 't2', 't3'], 'grouped': ['t1', 't2', 't3']})

    def test_a_group_waits_while_any_member_is_blocked(self):
        tasks = [task('t1', together_with=['t2']), task('t2', depends_on=['t3']), task('t3'), task('t4')]
        h = taskdeps.holds(tasks)
        self.assertIn('waits for its group: t2', h['t1'])
        self.assertNotIn('t4', h)
        self.assertTrue(taskdeps.describe(tasks[0], tasks)[0].startswith('Not yet: it waits for its group: t2'))
        tasks[2]['status'] = 'done'
        self.assertEqual(taskdeps.holds(tasks), {})   # the blocker landed: the whole group is ready together

    def test_a_finished_task_does_not_tie_two_open_tasks_together(self):
        tasks = [task('t1', together_with=['t2'], depends_on=['t4']), task('t2', status='done'), task('t3', together_with=['t2']),
                 task('t4')]
        self.assertNotIn('t3', taskdeps.holds(tasks))
        self.assertIn('t1', taskdeps.holds(tasks))

    def test_the_board_lists_each_lane_with_links(self):
        tasks = [task('t1', depends_on=['t2']), task('t2', together_with=['t3']), task('t3'), task('t4', status='done')]
        b = taskdeps.board(tasks)
        lanes = {lane['name']: [c['id'] for c in lane['items']] for lane in b['lanes']}
        self.assertEqual(lanes, {'Blocked': ['t1'], 'Ready': ['t2', 't3'], 'Grouped': ['t2', 't3']})
        t2 = b['lanes'][1]['items'][0]
        self.assertEqual((t2['needed_by'], t2['group']), (['t1'], ['t3']))
        self.assertIn('blocked until t2', b['lanes'][0]['items'][0]['why'])
        self.assertEqual(b['linked'], 3)
        self.assertEqual(taskdeps.board([]), {'lanes': [{'name': n, 'items': []} for n in ('Blocked', 'Ready', 'Grouped')], 'linked': 0})

    def test_groups_are_found_in_one_pass_and_fast_on_a_big_backlog(self):
        import time
        tasks = [task(f't{i}') for i in range(3000)] + [task('a1', together_with=['a2']), task('a2'), task('a3', together_with=['a2'])]
        g = taskdeps.groups(tasks)
        self.assertEqual((g['a1'], g['t5']), (['a1', 'a2', 'a3'], ['t5']))
        start = time.monotonic()
        taskdeps.holds(tasks)
        taskdeps.lanes(tasks)
        self.assertLess(time.monotonic() - start, 1.0)   # was quadratic: the state snapshot took 7.8 s live (t241's self-test)

    def test_link_refuses_unknown_ids_itself_and_loops_and_clears_with_empty(self):
        tasks = [task('t1'), task('t2', depends_on=['t1'])]
        with self.assertRaisesRegex(taskdeps.DepError, 'not in the backlog'):
            taskdeps.link(tasks, 't1', depends_on=['t9'])
        with self.assertRaisesRegex(taskdeps.DepError, 'itself'):
            taskdeps.link(tasks, 't1', depends_on=['t1'])
        with self.assertRaisesRegex(taskdeps.DepError, r'loop \(t1 -> t2 -> t1\)'):
            taskdeps.link(tasks, 't1', depends_on=['t2'])
        self.assertNotIn('depends_on', tasks[0])   # a refused link leaves the task as it was
        taskdeps.link(tasks, 't2', depends_on=[])
        self.assertNotIn('depends_on', tasks[1])

    def test_describe_says_what_it_waits_for_and_who_needs_it(self):
        tasks = [task('t1', depends_on=['t2'], together_with=['t3']), task('t2'), task('t3'), task('t4', depends_on=['t1']),
                 task('t5', merged_into='t1')]
        lines = taskdeps.describe(tasks[0], tasks)
        self.assertEqual(lines, ['Depends on: t2 (queued)', 'Blocked until t2 land',
                                 'Chain before it starts: t2 (about 3 h of runs by size)', 'Goes together with: t3 (queued)',
                                 'Folded into it: t5', 'Needed by: t4 (queued)'])


class Queued(unittest.TestCase):
    def test_the_queued_list_says_a_blocked_task_waits_for_its_dependency_before_any_round(self):
        from tutti import config, waitlist
        cfg = config.load(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tutti.config.example.yaml'))
        tasks = [task('t1', depends_on=['t2'], attempts=[], notes=[]), task('t2', attempts=[], notes=[])]
        items = {i['id']: i for i in waitlist.task_items(cfg, tasks, {'reason': 'the round has room'})}
        self.assertEqual(items['t1']['why'], 'blocked until t2 lands (depends_on)')
        self.assertEqual(items['t2']['why'], 'the round has room')


class Cli(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.cfg = {'project': {'state': d.name}, 'autopilot': {'repos': {'app': {'path': d.name, 'check': 'true'}}}}
        data = backlog.load(self.cfg)
        for title in ('one', 'two', 'three'):
            backlog.add(data, backlog.new_task('app', title, check='true'))
        backlog.save(self.cfg, data)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        a = cli.parser().parse_args(['backlog', *argv])
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.cmd_backlog(self.cfg, a)
        return rc, out.getvalue() + err.getvalue()

    def test_link_deps_and_board(self):
        rc, out = self.run_cli('link', 't1', '--on', 't2', '--with', 't3')
        self.assertEqual(rc, 0, out)
        self.assertIn('Blocked until t2 land', out)
        rc, out = self.run_cli('deps', 't2')
        self.assertIn('Needed by: t1 (queued)', out)
        rc, out = self.run_cli('board')
        self.assertIn('Blocked (2): t1, t3', out)   # t3 goes with t1, so it waits too
        self.assertIn('Grouped (2): t1, t3', out)

    def test_a_bad_link_says_why_and_changes_nothing(self):
        rc, out = self.run_cli('link', 't1', '--on', 't7')
        self.assertEqual(rc, 2)
        self.assertIn('t7', out)
        self.assertNotIn('depends_on', backlog.find(backlog.load(self.cfg), 't1'))
        rc, out = self.run_cli('link', 't1')
        self.assertEqual(rc, 2)
        self.assertIn('say what to set', out)


if __name__ == '__main__':
    os.environ.setdefault('TUTTI_TELEMETRY', 'off')
    unittest.main()

    def test_the_chain_follows_every_level_and_estimates_by_size(self):
        tasks = [{'id': 't1', 'status': 'queued', 'depends_on': ['t2']},
                 {'id': 't2', 'status': 'queued', 'depends_on': ['t3', 't4'], 'size': 'L'},
                 {'id': 't3', 'status': 'queued', 'size': 'S', 'depends_on': ['t1']}, {'id': 't4', 'status': 'done'}]
        self.assertEqual(taskdeps.chain(tasks[0], tasks), (['t2', 't3'], 9))   # the loop back to t1 stops; done t4 is skipped
        self.assertIn('Chain before it starts: t2, t3 (about 9 h of runs by size)', taskdeps.describe(tasks[0], tasks))
        self.assertEqual(taskdeps.board(tasks)['lanes'][0]['items'][0]['chain'], ['t2', 't3'])

    def test_a_task_with_nothing_open_before_it_has_no_chain(self):
        tasks = [{'id': 't1', 'status': 'queued', 'depends_on': ['t2']}, {'id': 't2', 'status': 'dropped'}]
        self.assertEqual(taskdeps.chain(tasks[0], tasks), ([], 0))
        self.assertFalse(any(x.startswith('Chain') for x in taskdeps.describe(tasks[0], tasks)))
