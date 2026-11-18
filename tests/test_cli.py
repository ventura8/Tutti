import unittest
from tutti.cli import command_line

class TestCli(unittest.TestCase):
    def test_command_line_quoting(self):
        # Desired behavior:
        # command_line(['ls', '-l']) -> 'ls -l'
        # command_line(['--']) -> ':'
        # command_line(['bash', '--', 'arg']) -> "bash -- 'arg'"
        
        self.assertEqual(command_line(['ls', '-l']), 'ls -l')
        self.assertEqual(command_line(['--']), ":")
        self.assertEqual(command_line(['bash', '--', 'arg']), "bash -- arg")
        self.assertEqual(command_line(['bash', '--']), "bash")
        self.assertEqual(command_line(['bash', '--', '--']), "bash -- --")
        self.assertEqual(command_line([]), ":")
        self.assertEqual(command_line(['ls', '-l', '--']), "ls -l")


if __name__ == '__main__':
    unittest.main()
