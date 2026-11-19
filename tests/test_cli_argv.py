"""Device lease command boundaries, including the dispatched command's own options."""
import unittest

from tutti import cli


class DeviceLeaseArgumentsTest(unittest.TestCase):
    def test_options_and_command_are_kept_separate(self):
        args = ['devices', 'lease', 'phone', 'tv', '--runner', 'pc', '--who', 'session', '--',
                'runner', '--minutes', '5', '--', '-x']
        for prefix in ([], ['--config', '/tmp/example.yaml'], ['--config=/tmp/example.yaml']):
            a = cli.parse(prefix + args)
            self.assertEqual((a.runner, a.who, a.minutes), ('pc', 'session', 120))
            self.assertEqual(a.names, ['phone', 'tv', '--', 'runner', '--minutes', '5', '--', '-x'])

    def test_non_lease_unrecognised_arguments_are_rejected(self):
        with self.assertRaises(SystemExit):
            cli.parse(['devices', 'claim', 'phone', '--unknown'])

    def test_unknown_lease_option_is_rejected(self):
        with self.assertRaises(SystemExit):
            cli.parse(['devices', 'lease', 'phone', '--typo', '--', 'echo'])
