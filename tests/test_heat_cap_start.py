"""A run on a machine with heat.cap_c starts with throttle_cpus when the machine is already hot (NUC at 103 C, 2026-11-23)."""
import os
import subprocess
import unittest

from tutti import remote


class HeatCapAtStart(unittest.TestCase):
    def test_no_cap_keeps_the_plain_number(self):
        self.assertEqual(remote.cpus_now({}, 8), '8')

    def test_the_expression_picks_by_the_hottest_zone(self):
        expr = remote.cpus_now({'heat': {'cap_c': 85, 'throttle_cpus': 2}}, 8)
        for temps, want in (('40000\\n90000', '2'), ('40000\\n50000', '8'), ('', '8'), ('85000', '2')):
            line = expr.replace('cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null', f"printf '{temps}'")
            out = subprocess.run(['bash', '-c', f'echo {line}'], capture_output=True, text=True, env={'PATH': os.environ['PATH']})
            self.assertEqual(out.stdout.strip(), want, temps)


    def test_default_throttle_is_one_and_never_above_the_runner(self):
        self.assertIn('echo 1 ||', remote.cpus_now({'heat': {'cap_c': 85}}, 8))
        self.assertIn('echo 4 ||', remote.cpus_now({'heat': {'cap_c': 85, 'throttle_cpus': 16}}, 4))

if __name__ == '__main__':
    unittest.main()
