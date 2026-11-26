"""The probe round: one ssh call per machine carries heat, activity and power."""
import unittest
from types import SimpleNamespace
from unittest import mock

from tutti import energy, projects, remote


def _machine(**extra):
    m = {'name': 'pc', 'host': 'pc.example', 'transport': 'ssh', 'yield_file': '~/y', 'slots': 2, 'lock_prefix': '/tmp/x-'}
    m.update(extra)
    return m


ROUND = (
    'C tutti-1 2 minutes\n'
    'Z Europe/Bucharest\n'
    'W\t/home/u/proj\tclaude\n'
    'R\t/home/u/proj\t/home/u/proj\tmain\tgit@example.com:p.git\t/home/u/proj/.git\n'
    'G 10 100 8000\n'
    '== nvidia\n'
    'NVIDIA GeForce, 100.0, 50, 450.0\n'
    '== rocm\n\n== xpu\n== intel\n\n'
    '== a\nt 1000000000\nrapl -\n== b\nt 2000000000\nrapl -\n'
    '== end\n'
    '2 0.5 8 0 45000 55 3.2 -\n'
)


class ProbeRound(unittest.TestCase):
    def setUp(self):
        remote._probe_round.clear()
        self.addCleanup(remote._probe_round.clear)

    def test_probe_script_includes_activity_and_power(self):
        script = remote.probe_script(_machine())
        self.assertIn('== nvidia', script)
        self.assertIn('== end");', script)  # power subshell ends, then the summary echo (not glued to it)
        self.assertIn('sed "s/^/G /"', script)
        self.assertIn('flock -n', script)
        self.assertIn('(export PATH=', script)  # activity in a subshell so its exit cannot skip the summary

    def test_probe_script_reads_remote_ollama_when_set(self):
        script = remote.probe_script(_machine(), '127.0.0.1:11434')
        self.assertIn('== ps', script)
        self.assertIn('== tags', script)

    def test_one_ssh_call_per_machine_carries_activity_and_power(self):
        calls = []

        def sh(m, script, **kw):
            calls.append(script)
            return SimpleNamespace(returncode=0, stdout=ROUND, stderr='')

        m = _machine()
        with mock.patch.object(remote, 'sh', sh):
            p = remote.probe(m)
            power = remote.power_readings(m)
            activity = projects.probe_activity(m)
        self.assertEqual(len(calls), 1)
        self.assertTrue(p.reachable)
        self.assertEqual((p.free, p.temp, p.gpu_temp), (2, 45.0, 55.0))
        self.assertEqual(p.containers, ['tutti-1 2 minutes'])
        self.assertEqual(p.zone, 'Europe/Bucharest')
        self.assertEqual(activity['procs'], [('/home/u/proj', 'claude')])
        self.assertEqual(activity['gpu']['util_pct'], 10.0)
        self.assertEqual(power['gpu_w'], 100.0)
        self.assertEqual(energy.from_probe(m)['gpu_w'], 100.0)
        self.assertIs(p.power, power)
        self.assertEqual(p.activity['procs'], activity['procs'])

    def test_one_ssh_call_when_the_machine_does_not_answer(self):
        calls = []

        def sh(m, script, **kw):
            calls.append(script)
            return SimpleNamespace(returncode=255, stdout='', stderr='refused')

        m = _machine()
        with mock.patch.object(remote, 'sh', sh):
            p = remote.probe(m)
            power = remote.power_readings(m)
            activity = projects.probe_activity(m)
        self.assertEqual(len(calls), 1)
        self.assertFalse(p.reachable)
        self.assertIsNone(power)
        self.assertIn('exited 255', activity['error'])


if __name__ == '__main__':
    unittest.main()
