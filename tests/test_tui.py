"""The TUI and the shared task details (tutti/tui.py, tutti/taskdetail.py): Enter expands a task or job to all its details."""
import curses
import unittest

from tutti import autopilot, config, taskdetail, tui

TASK = {'id': 't7', 'kind': 'task', 'title': 'Fix it', 'repo': 'tutti', 'size': 'M', 'check': 'bash scripts/check.sh',
        'scope': ['a.py', 'b.py'], 'notes': ['n1'], 'attempts': [{'run': 'r1', 'ok': False, 'why': 'limit', 'log': 'l.log', 'tail': 'end'}]}


class Details(unittest.TestCase):
    def test_recorded_attributes_are_listed(self):
        facts = {label: value for label, value, *_ in taskdetail.facts(TASK)}
        self.assertEqual(facts['ID'], 't7')
        self.assertEqual(facts['Scope paths'], 'a.py, b.py')
        self.assertNotIn('Branch', facts)
        self.assertIn('failed: limit (log: l.log) | tail: end', facts['Attempts and logs (1)'])
        self.assertEqual(facts['Notes history (1)'], 'n1')

    def test_task_details_include_current_job_measurements_and_historical_duration(self):
        task = {**TASK, 'started': 100, 'closed_at': 110}
        job = {'machine': 'build.example', 'facts': {'tokens': 240}, 'progress': {'eta_seconds': 45}}
        facts = dict(autopilot.detail_facts(config.normalise({}), {**task, 'attempts': []}, job))
        self.assertEqual(facts['Duration (seconds)'], '10')
        self.assertEqual(facts['Tokens'], '240')
        self.assertEqual(facts['ETA'], '45')
        self.assertEqual(facts['Machine'], 'build.example')


class Keys(unittest.TestCase):
    def test_enter_expands_and_collapses_the_selected_item(self):
        t = tui.Tui([TASK, {**TASK, 'id': 't8', 'kind': 'job'}])
        self.assertFalse(any('Acceptance check' in ln for ln in t.render()))
        t.on_key('enter')
        shown = '\n'.join(t.render())
        self.assertIn('Acceptance check: bash scripts/check.sh', shown)
        self.assertIn('Repo / Project: tutti', shown)
        t.on_key('enter')
        self.assertFalse(any('Acceptance check' in ln for ln in t.render()))

    def test_enter_expands_a_job_and_page_down_reaches_wrapped_log_output(self):
        item = {'id': 'job:1', 'kind': 'job', 'title': 'Run checks', 'status': 'done', 'tail': ['x' * 55 + ' end of log']}
        tui_view = tui.Tui([item])
        tui_view.handle_key(curses.KEY_ENTER, 3)
        words = []
        for _ in range(5):
            words.extend(tui_view.viewport(3, 24))
            tui_view.handle_key(curses.KEY_NPAGE, 3)
        self.assertIn('end of log', ' '.join(words))
        tui_view.handle_key(curses.KEY_ENTER, 3)
        self.assertFalse(tui_view.expanded)

    def test_arrows_move_within_the_list_and_an_empty_list_ignores_enter(self):
        t = tui.Tui([TASK, {**TASK, 'id': 't8'}])
        t.on_key('down')
        t.on_key('down')
        self.assertEqual(t.selected, 1)
        t.on_key('enter')
        self.assertEqual(t.expanded, {'t8'})
        empty = tui.Tui([])
        empty.on_key('enter')
        self.assertEqual(empty.render()[-1], '  No tasks or jobs.')


if __name__ == '__main__':
    unittest.main()
