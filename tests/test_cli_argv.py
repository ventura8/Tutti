import unittest

from tutti import cli


class DispatcherArgvTest(unittest.TestCase):
    """The command lines the dispatcher builds (dispatch.test_argv) must parse: RoRadio's shards all failed with
    'unrecognized arguments: -- <command>' before they ran (2026-11-16)."""

    def test_a_lease_on_a_runner_keeps_the_command_after_the_options(self):
        a = cli.parse(['devices', 'lease', 'pixeltablet', 'googletv', '--runner', 'w5090-win', '--who', 'dispatch-RoRadio',
                       '--', 'powershell.exe -NoProfile -Command run'])
        self.assertEqual(a.runner, 'w5090-win')
        self.assertEqual(a.who, 'dispatch-RoRadio')
        self.assertEqual(a.names, ['pixeltablet', 'googletv', '--', 'powershell.exe -NoProfile -Command run'])

    def test_the_commands_own_flags_stay_in_the_command(self):
        a = cli.parse(['devices', 'lease', 'pixel', '--who', 's', '--', 'powershell.exe', '-NoProfile', '-x', '--minutes', '5'])
        self.assertEqual(a.minutes, 120)   # the command's --minutes is not Tutti's
        self.assertEqual(a.names, ['pixel', '--', 'powershell.exe', '-NoProfile', '-x', '--minutes', '5'])

    def test_both_orders_give_one_shape(self):
        before = cli.parse(['devices', 'lease', 'pixel', '--', 'echo', 'hi'])
        after = cli.parse(['devices', 'lease', 'pixel', '--who', 's', '--', 'echo', 'hi'])
        self.assertEqual(before.names, after.names)
        self.assertEqual(before.names, ['pixel', '--', 'echo', 'hi'])

    def test_test_drops_a_leading_double_dash(self):
        a = cli.parse(['test', '--on', 'w5090-win', 'RoRadio', '--', 'echo', 'hi'])
        self.assertEqual(a.track, 'RoRadio')
        self.assertEqual(a.command, ['echo', 'hi'])

    def test_test_keeps_the_commands_own_options(self):
        a = cli.parse(['test', 't', 'echo', '-n', 'x'])
        self.assertEqual(a.command, ['echo', '-n', 'x'])

    def test_stray_words_elsewhere_are_still_refused(self):
        with self.assertRaises(SystemExit):
            cli.parse(['devices', 'claim', 'pixel', '--who', 's', 'extra-word-after-options'])


if __name__ == '__main__':
    unittest.main()
