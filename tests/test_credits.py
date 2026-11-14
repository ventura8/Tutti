"""Copilot AI credits from the CLI's own session records, here and on other machines (fake machines, temp folders)."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from tutti import config, credits, state

SINCE = datetime(2026, 11, 1, tzinfo=timezone.utc)


def shutdown(started, nano):
    return json.dumps({'type': 'session.shutdown', 'data': {'accountingSnapshot': {'sessionStartTime': started, 'totalNanoAiu': nano}}})


class CreditsTest(unittest.TestCase):
    def setUp(self):
        credits._cache.clear()

    def test_shutdown_records_in_the_window_are_summed_once(self):
        lines = [shutdown('2026-11-14T11:33:52.560Z', 6835050000.0), shutdown('2026-11-14T11:33:52.560Z', 6835050000.0),
                 shutdown('2026-11-14T13:24:54.480Z', 9151260000.0), shutdown('2026-10-30T10:00:00Z', 5e9)]
        self.assertEqual(credits.shutdown_credits(lines, SINCE), 15.99)

    def test_other_and_broken_lines_count_nothing(self):
        lines = ['{"type":"session.usage_record"}', 'not json "session.shutdown"', '{"type":"session.shutdown","data":{}}']
        self.assertEqual(credits.shutdown_credits(lines, SINCE), 0)

    def test_local_sessions_or_none_without_a_copilot_folder(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, 's1'))
            with open(os.path.join(d, 's1', 'events.jsonl'), 'w') as f:
                f.write('{"type":"user.message"}\n' + shutdown('2026-11-14T11:00:00Z', 2e9) + '\n')
            self.assertEqual(credits.shutdown_credits(credits.local_lines(SINCE, d), SINCE), 2.0)
            self.assertIsNone(credits.local_lines(SINCE, os.path.join(d, 'missing')))

    def test_a_machine_is_cached_and_a_silent_one_named(self):
        calls = []

        def lines(m, since):
            calls.append(m['name'])
            if m['name'] == 'down':
                raise OSError('exit 255: Connection timed out')
            return [shutdown('2026-11-14T11:00:00Z', 3e9)]
        clock = iter([0, 10, 10_000]).__next__   # one look per call
        self.assertEqual(credits.machine_credits({'name': 'pc'}, SINCE, lines, clock), (3.0, None))
        self.assertEqual(credits.machine_credits({'name': 'pc'}, SINCE, lines, clock), (3.0, None))
        self.assertEqual(calls, ['pc'])
        value, problem = credits.machine_credits({'name': 'down'}, SINCE, lines, clock)
        self.assertIsNone(value)
        self.assertIn('down was not read (exit 255', problem)

    def test_scripts_read_only_the_shutdown_lines(self):
        self.assertIn('-newermt "2026-11-01 00:00:00 +0000"', credits.bash_script(SINCE))
        self.assertIn("Select-String", credits.windows_script(SINCE))
        self.assertIn("'2026-11-01T00:00:00Z'", credits.windows_script(SINCE))

    def test_the_account_sums_here_and_its_machines(self):
        cfg = {'project': {'state': '/nonexistent'}, 'machines': {'pc': {'host': 'pc.example', 'transport': 'wsl'}, 'off': {}}}
        acc = {'credits': True, 'credits_machines': ['pc', 'off']}
        s = {'window_start': '2026-11-01T00:00:00+00:00'}
        fake = {'pc': (7.49, None), 'off': (None, 'off was not read (exit 255)')}
        with mock.patch.object(credits, 'local_lines', return_value=[shutdown('2026-11-14T11:00:00Z', 6.84e9)]):
            out = state.with_credits(cfg, s, acc, machine_fn=lambda m, since: fake[m['name']])
        self.assertEqual((out['credits_used'], out['credits_where']), (14.33, 'here 6.84, pc 7.49'))
        self.assertEqual(out['credits_problem'], 'off was not read (exit 255)')
        self.assertNotIn('credits_used', state.with_credits(cfg, {'window_start': None}, acc))

    def test_credits_machines_must_be_machines(self):
        cfg = config.normalise({'machines': {'a': {'host': 'a.example'}}, 'accounts': {
            'ok': {'credits': True, 'credits_machines': ['a']}, 'bad': {'credits_machines': ['ghost']},
            'worse': {'credits_machines': 'a'}}})
        errs = '\n'.join(config.validate(cfg))
        self.assertNotIn('accounts.ok', errs)
        self.assertIn('accounts.bad.credits_machines names "ghost"', errs)
        self.assertIn('accounts.worse.credits_machines is', errs)


if __name__ == '__main__':
    unittest.main()
