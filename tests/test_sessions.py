"""Unit tests for always-on and cloud agent sessions (tutti/sessions.py)."""
import tempfile
import unittest

from tutti import sessions


def make_cfg(state_dir, machines=None, workers=None, sess=None):
    return {
        'state_dir': state_dir,
        'machines': machines if machines is not None else {
            'laptop': {'transport': 'local', 'laptop': True, 'slots': 2},
            'nuc': {'transport': 'ssh', 'host': 'nuc.example', 'slots': 4},
            'sandbox': {'transport': 'ssh', 'host': 'cloud.example', 'slots': 2,
                        'cloud': True, 'costs_money': True, 'cost_note': '$0.10/hour'},
        },
        'workers': workers if workers is not None else {
            'cursor': {'kind': 'cli', 'command': ['cursor-agent']},
            'paid-ai': {'kind': 'cli', 'command': ['paid-ai'], 'costs_money': True, 'cost_note': 'per-token billing'},
        },
        'sessions': sess or {},
        'off': [],
    }


class TestSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_start_picks_always_on_host_and_persists(self):
        cfg = make_cfg(self.tmp.name)
        s = sessions.start(cfg, 'fix flaky test', worker='cursor', device='laptop', state={'branch': 'fix/1'}, now=100)
        self.assertEqual(s['host'], 'nuc')
        self.assertEqual(s['host_kind'], 'always_on')
        self.assertEqual(s['status'], 'running')
        self.assertEqual(sessions.get_session(cfg, s['id'])['state'], {'branch': 'fix/1'})

    def test_sleep_handoff_and_wake_resume_preserve_state(self):
        cfg = make_cfg(self.tmp.name)
        s = sessions.start(cfg, 'refactor auth', host='laptop', device='laptop', state={'step': 2, 'files': ['a.py']}, now=100)
        moved = sessions.sleep_handoff(cfg, sleeping_host='laptop', device='laptop', now=200)
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]['host'], 'nuc')
        self.assertEqual(moved[0]['state'], {'step': 2, 'files': ['a.py']})
        # When no always-on host exists, sleep pauses and wake resumes with state intact
        only_laptop = make_cfg(self.tmp.name, machines={'laptop': {'transport': 'local', 'laptop': True, 'slots': 2}})
        s2 = sessions.start(only_laptop, 'docs update', host='laptop', device='laptop', state={'draft': True}, now=300)
        paused = sessions.sleep_handoff(only_laptop, sleeping_host='laptop', now=400)
        self.assertEqual(paused[0]['status'], 'paused')
        resumed = sessions.resume_after_wake(only_laptop, device='laptop', now=500)
        self.assertEqual([r['id'] for r in resumed], [s2['id']])
        self.assertEqual(resumed[0]['status'], 'running')
        self.assertEqual(resumed[0]['state'], {'draft': True})

    def test_remote_device_control_and_handover_preserve_state(self):
        cfg = make_cfg(self.tmp.name)
        code, res = sessions.api_post(cfg, {
            'action': 'start', 'task': 'add caching', 'device': 'phone', 'state': {'worktree': '/wt/t1'},
        })
        self.assertEqual(code, 200)
        sid = res['session']['id']
        code, steered = sessions.api_post(cfg, {
            'action': 'steer', 'id': sid, 'device': 'tablet', 'note': 'keep diff small', 'state': {'phase': 'review'},
        })
        self.assertEqual(code, 200)
        self.assertEqual(steered['session']['device'], 'tablet')
        self.assertEqual(steered['session']['state'], {'worktree': '/wt/t1', 'phase': 'review'})
        handed = sessions.control(cfg, sid, 'handover', device='desktop', to_host='nuc', note='taking over',
                                  state_patch={'cursor': 42}, now=600)
        self.assertEqual(handed['device'], 'desktop')
        self.assertEqual(handed['state'], {'worktree': '/wt/t1', 'phase': 'review', 'cursor': 42})
        self.assertEqual(handed['history'][-1]['action'], 'handover')
        self.assertEqual(len(sessions.api_get(cfg)['sessions']), 1)

    def test_quota_enforced(self):
        cfg = make_cfg(self.tmp.name, sess={'max_sessions': 1})
        s = sessions.start(cfg, 'task one', now=10)
        with self.assertRaises(sessions.SessionError) as ctx:
            sessions.start(cfg, 'task two', now=20)
        self.assertIn('quota', str(ctx.exception))
        sessions.control(cfg, s['id'], 'stop', now=30)
        s2 = sessions.start(cfg, 'task two', now=40)
        self.assertEqual(s2['status'], 'running')

    def test_privacy_blocks_cloud_when_local_only(self):
        cfg = make_cfg(self.tmp.name, sess={'privacy': 'local_only'})
        with self.assertRaises(sessions.SessionError) as ctx:
            sessions.start(cfg, 'secret work', host='sandbox', opt_in_cost=True)
        self.assertIn('privacy', str(ctx.exception))

    def test_cost_requires_explicit_opt_in_and_communicates_notice(self):
        cfg = make_cfg(self.tmp.name, sess={'privacy': 'cloud_allowed'})
        with self.assertRaises(sessions.SessionError) as ctx:
            sessions.start(cfg, 'cloud run', host='sandbox', opt_in_cost=False)
        self.assertIn('cost opt-in required', str(ctx.exception))
        self.assertIn('$0.10/hour', str(ctx.exception))
        s = sessions.start(cfg, 'cloud run', host='sandbox', opt_in_cost=True)
        self.assertEqual(s['host_kind'], 'cloud')
        self.assertTrue(s['cost_opted_in'])
        self.assertIn('$0.10/hour', s['cost_notice'])


if __name__ == '__main__':
    unittest.main()
