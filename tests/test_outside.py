import os
import shutil
import subprocess
import unittest


class TestOutside(unittest.TestCase):
    def test_outside(self):
        if not shutil.which('node'):
            self.skipTest("node not installed")

        # Ensure we run from the project root
        result = subprocess.run(
            ['node', 'tests/test_outside.js'],
            capture_output=True,
            text=True,
            check=False,
            cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
        )

        self.assertEqual(result.returncode, 0, f"JS test failed: {result.stderr}")
        self.assertIn('Good case passed', result.stdout)
        self.assertIn('Bad case passed', result.stdout)
        self.assertIn('Null case passed', result.stdout)

if __name__ == '__main__':
    unittest.main()
