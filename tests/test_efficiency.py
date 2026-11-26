"""Efficiency audit tests: scripts/audit.sh utilization reporting and insights scheduling gaps / wasted reruns."""
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone

from tutti import insights

AUDIT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts', 'audit.sh')


class AuditScriptTest(unittest.TestCase):
    def test_audit_reports_cpu_gpu_and_devices(self):
        with tempfile.TemporaryDirectory() as d:
            smi = os.path.join(d, 'smi')
            with open(smi, 'w') as f:
                f.write('#!/bin/sh\necho 42\n')
            os.chmod(smi, 0o755)
            dev = os.path.join(d, 'video0')
            open(dev, 'w').close()
            env = {**os.environ, 'NVIDIA_SMI': smi, 'AUDIT_DEVICE_GLOB': os.path.join(d, 'video*'), 'AUDIT_INTERVAL': '0.1'}
            p = subprocess.Popen(['bash', '-c', f'bash "{AUDIT}" $$ 0.1 & ap=$!; sleep 0.25; kill $ap 2>/dev/null; wait $ap 2>/dev/null'],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            out, _ = p.communicate(timeout=5)
            self.assertIn('AUDIT:', out)
            self.assertIn('host_cpu=', out)
            self.assertIn('gpu_npu=42%', out)
            self.assertIn('devices=1', out)

    def test_audit_rejects_missing_pid(self):
        r = subprocess.run(['bash', AUDIT], capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 2)
        self.assertIn('usage:', r.stderr)


class InsightsEfficiencyTest(unittest.TestCase):
    def test_scheduling_gaps_and_wasted_reruns(self):
        now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        ts = now.isoformat(timespec='seconds')
        events = [
            {'ts': ts, 'kind': 'feature', 'name': 'slot.wait', 'wait_minutes': 4.5},
            {'ts': ts, 'kind': 'feature', 'name': 'autopilot.gap', 'gap_minutes': 12.0},
            {'ts': ts, 'kind': 'feature', 'name': 'autopilot.attempt', 'task': 't223', 'outcome': 'failed', 'minutes': 3.0},
            {'ts': ts, 'kind': 'feature', 'name': 'autopilot.attempt', 'task': 't223', 'outcome': 'failed', 'minutes': 5.0},
            {'ts': ts, 'kind': 'feature', 'name': 'autopilot.attempt', 'task': 't999', 'outcome': 'failed', 'minutes': 2.0},
            {'ts': ts, 'kind': 'feature', 'name': 'autopilot.attempt', 'task': 't223', 'outcome': 'passed', 'minutes': 4.0},
        ]
        s = insights.summary(events, days=7, now=now)
        self.assertEqual(s['scheduling_gaps'], {'count': 2, 'minutes': 16.5, 'max_minutes': 12.0})
        self.assertEqual(s['wasted_reruns'], [{'target': 't223', 'reruns': 1, 'minutes': 8.0}])
        text = '\n'.join(insights.lines(s))
        self.assertIn('Scheduling gaps: 2 event(s), 16.5 min total, 12.0 min max', text)
        self.assertIn('t223: 1 rerun(s), 8.0 min', text)

    def test_empty_events_have_zero_gaps_and_no_reruns(self):
        s = insights.summary([], days=7, now=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(s['scheduling_gaps'], {'count': 0, 'minutes': 0.0, 'max_minutes': 0.0})
        self.assertEqual(s['wasted_reruns'], [])


if __name__ == '__main__':
    unittest.main()
