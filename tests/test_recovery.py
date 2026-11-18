"""Unit tests for start-up recovery in tutti/recovery.py."""

import os
import unittest
from unittest import mock

import tmpdirs

from tutti import config, durable, presence, recovery, state, telemetry


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.root = tmpdirs.mkdtemp()
        self.cfg = config.normalise({'project': {'state': self.root, 'worktrees': self.root}})

    def test_on_start_sweeps_temps_repairs_log_and_resumes_jobs(self):
        log = state.usage_log_path(self.cfg)
        with open(log, 'w', encoding='utf8') as f:
            f.write('{"at":"2026-11-14T00:00:00+00:00","account":"x","used":1}\n{"at":"2026-11')
        orphan = os.path.join(self.root, 'backlog.json.999999.tmp')
        with open(orphan, 'w', encoding='utf8') as f:
            f.write('{')
        presence.write(self.cfg, {'resume': [{'kind': 'run', 'spec': {'track': 't1'}}]})
        resume = mock.Mock(return_value=['t1'])

        with mock.patch.object(durable, 'pid_alive', return_value=False), \
                mock.patch.object(telemetry, 'record') as record:
            out = recovery.on_start(self.cfg, resume=resume)

        self.assertFalse(os.path.exists(orphan))
        self.assertEqual(len(durable.read_jsonl(log)), 1)
        self.assertIn(f'removed {orphan}: a write cut by a power-off (the previous file is whole)', out)
        self.assertIn(f'{log}: a torn last line (a power-off mid-write) was cut off', out)
        self.assertIn('1 job(s) stopped by a shutdown started again', out)
        resume.assert_called_once_with(self.cfg)
        record.assert_called_once_with('feature', 'recovery.start', repaired=3)

    def test_on_start_nothing_to_recover(self):
        # Setup: No temp files, log is fine, nothing to resume
        presence.write(self.cfg, {'resume': []})

        with mock.patch.object(durable, 'sweep', return_value=[]), \
                mock.patch.object(os.path, 'exists', return_value=False), \
                mock.patch.object(telemetry, 'record') as record:
            out = recovery.on_start(self.cfg)

        self.assertEqual(out, [])
        record.assert_called_once_with('feature', 'recovery.start', repaired=0)

    def test_on_start_raises_when_resuming_jobs_fails(self):
        presence.write(self.cfg, {'resume': [{'kind': 'run', 'spec': {'track': 't1'}}]})

        def fail(_cfg):
            raise RuntimeError('resume failed')

        with mock.patch.object(telemetry, 'record') as record:
            with self.assertRaisesRegex(RuntimeError, 'resume failed'):
                recovery.on_start(self.cfg, resume=fail)
        record.assert_not_called()

    def test_on_start_log_is_already_good(self):
        # Setup: Log exists but repair_jsonl returns False
        log = state.usage_log_path(self.cfg)
        with open(log, 'w', encoding='utf8') as f:
            f.write('{"at":"2026-11-14T00:00:00+00:00","account":"x","used":1}\n')
        presence.write(self.cfg, {'resume': []})

        with mock.patch.object(durable, 'sweep', return_value=[]), \
                mock.patch.object(durable, 'repair_jsonl', return_value=False), \
                mock.patch.object(telemetry, 'record') as record:
            out = recovery.on_start(self.cfg)

        self.assertEqual(out, [])
        record.assert_called_once_with('feature', 'recovery.start', repaired=0)

    def test_on_start_sweeps_temp_files_correctly(self):
        orphan = os.path.join(self.root, 'bad.999999.tmp')
        with open(orphan, 'w', encoding='utf8') as f:
            f.write('{}')
        my_pid = os.getpid()
        live_tmp = os.path.join(self.root, f'live.{my_pid}.tmp')
        with open(live_tmp, 'w', encoding='utf8') as f:
            f.write('{}')

        with mock.patch.object(durable, 'pid_alive', side_effect=lambda pid: pid == my_pid), \
                mock.patch.object(telemetry, 'record') as record:
            out = recovery.on_start(self.cfg)

        self.assertFalse(os.path.exists(orphan))
        self.assertTrue(os.path.exists(live_tmp))
        self.assertIn(f'removed {orphan}: a write cut by a power-off (the previous file is whole)', out)
        self.assertNotIn(f'removed {live_tmp}: a write cut by a power-off (the previous file is whole)', out)
        record.assert_called_once_with('feature', 'recovery.start', repaired=1)


if __name__ == '__main__':
    unittest.main()
