"""An explicit `tutti run agy` runs on one of agy's accounts with that account's args (its --settings and --model), never on
the worker's bare defaults (2026-11-25: two agy runs had `npx vitest` refused by the worker's narrow allow-list)."""
import unittest
from datetime import datetime, timezone
from unittest import mock

from tutti import cli

NOW = datetime(2026, 11, 25, 20, tzinfo=timezone.utc)
NARROW, WIDE = '{"permissions":{"allow":["c"]}}', '{"permissions":{"allow":["command(*)"]}}'
CFG = {'accounts': {'agy-claude': {'worker': 'agy', 'args': ['--model', 'claude-x', '--settings', NARROW]},
                    'agy-gemini': {'worker': 'agy', 'args': ['--model', 'gemini-y', '--settings', WIDE]},
                    'codex': {'worker': 'codex'}},
       'workers': {'agy': {'kind': 'cli'}, 'codex': {'kind': 'cli'}}}


def states(*names):
    return lambda n, *a, **k: {'name': n, 'verdict': 'behind' if n in names else 'spent', 'panic': n not in names,
                               'used': 10.0 if n in names else 100.0, 'panic_at': 95}


class SharedWorkerArgs(unittest.TestCase):
    def pick(self, wanted=None, room=('agy-gemini',)):
        with mock.patch.object(cli.pacing, 'read_log', return_value=[]), \
                mock.patch.object(cli.pacing, 'account_state', side_effect=states(*room)), \
                mock.patch.object(cli.pacing, 'plan_args', return_value=()), mock.patch.object(cli, 'say'), \
                mock.patch.object(cli, 'usage_log_path', return_value='/nonexistent'):
            return cli.shared_worker_args(CFG, 'agy', wanted, NOW)

    def test_the_account_with_room_gives_its_args(self):
        args, name = self.pick()
        self.assertEqual(name, 'agy-gemini')
        self.assertIn('command(*)', args[args.index('--settings') + 1])
        self.assertEqual(args[:2], ['--model', 'gemini-y'])

    def test_account_names_the_account(self):
        self.assertEqual(self.pick('agy-claude')[1], 'agy-claude')

    def test_a_worker_with_one_account_or_none_is_unchanged(self):
        self.assertEqual(cli.shared_worker_args(CFG, 'codex', None, NOW), ([], None))
        self.assertEqual(cli.shared_worker_args(CFG, 'nobody', None, NOW), ([], None))


if __name__ == '__main__':
    unittest.main()
