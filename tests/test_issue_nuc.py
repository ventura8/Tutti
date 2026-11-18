import unittest

from tutti import cli


class TestNucIssue(unittest.TestCase):
    def test_command_line_quoting(self):
        # The goal is to see if '--' is quoted properly
        words = ['bash', '--', 'ls']
        cmd = cli.command_line(words)
        # We want to make sure '--' is treated as an argument and not an option to bash
        # If the command is 'bash -- ls', it might be interpreted as running the file '--'
        # Which is what we might want to avoid if it's not a file.

        # Actually, if we want it to be safe, maybe we should always quote everything
        # that looks like an option?

        # Let's see what it produces currently
        print(f"DEBUG: cmd = {cmd}")
        self.assertIn("'--'", cmd)


if __name__ == '__main__':
    unittest.main()
