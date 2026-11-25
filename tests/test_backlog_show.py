"""`tutti backlog show <id>` and the backlog tasks a `tutti run` task names, appended to the worker's brief."""
import contextlib
import io
import json
import types
import unittest

from tutti import backlog, cli


def data():
    return {'next': 4, 'tasks': [
        {'id': 't1', 'title': 'fix the thing', 'repo': 'app', 'size': 'M', 'status': 'needs-person', 'scope': ['src/a.py'],
         'check': 'make test', 'depends_on': ['t2'], 'notes': ['look at the second file'], 'description': 'LONG ' * 100,
         'attempts': [{'at': 1700000000, 'step': 'codex', 'ok': False, 'class': 'code', 'why': 'changed nothing'}]},
        {'id': 't2', 'title': 'other', 'repo': 'app', 'size': 'S', 'status': 'queued', 'attempts': [], 'description': 'x' * 20000},
    ]}


def show(task_id, as_json=False):
    out, err = io.StringIO(), io.StringIO()
    a = types.SimpleNamespace(repo=task_id, json=as_json)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.backlog_show(data(), a)
        except backlog.BacklogError as e:
            return 2, str(e)
    return rc, out.getvalue()


class Show(unittest.TestCase):
    def test_plain_text_has_every_field_and_the_attempt_reason(self):
        rc, out = show('t1')
        self.assertEqual(rc, 0)
        for want in ('id: t1', 'title: fix the thing', 'status: needs-person', 'repo: app', 'size: M', 'scope: src/a.py',
                     'check: make test', 'depends_on: t2', 'look at the second file', 'LONG LONG', 'changed nothing', 'codex failed'):
            self.assertIn(want, out)

    def test_json_is_the_record(self):
        rc, out = show('t1', as_json=True)
        self.assertEqual((rc, json.loads(out)['id']), (0, 't1'))

    def test_unknown_id_says_so(self):
        rc, out = show('t99')
        self.assertEqual(rc, 2)
        self.assertIn('no task "t99"', out)

    def test_no_id_says_what_to_type(self):
        rc, out = show(None)
        self.assertEqual(rc, 2)
        self.assertIn('tutti backlog show t123', out)


class Brief(unittest.TestCase):
    def test_named_tasks_are_appended_in_order(self):
        text = backlog.with_tasks('do t2 then t1 and t2 again', data())
        self.assertTrue(text.startswith('do t2 then t1 and t2 again'))
        self.assertLess(text.index('Backlog task t2 (full text):'), text.index('Backlog task t1 (full text):'))
        self.assertEqual(text.count('Backlog task t2'), 1)
        self.assertIn('look at the second file', text)

    def test_unknown_ids_and_lookalikes_are_ignored(self):
        text = 'do t99, not at5 or t1x'
        self.assertEqual(backlog.with_tasks(text, data()), text)

    def test_a_long_task_is_cut_with_a_pointer(self):
        text = backlog.with_tasks('t2', data())
        self.assertLess(len(text), 8000 + 300)
        self.assertIn('tutti backlog show t2', text)


if __name__ == '__main__':
    unittest.main()
