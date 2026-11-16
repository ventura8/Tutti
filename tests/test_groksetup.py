"""`tutti scout setup grok`: the guided setup against a fake gh and webhook (post_fn), getpass mocked, a temp home."""
import io
import os
import stat
import unittest
from contextlib import redirect_stdout
from unittest import mock

import tmpdirs

from tutti import config, groksetup

KEY = 'secret-test-key-zz9'
URL = 'https://api2.cursor.sh/automations/webhook/abc'
CFG = 'project: {name: t}\nprojects:\n  app: {repo: octo/app}\n'


class FakeGh:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **_kw):
        self.calls.append(args)
        if args[:2] == ['issue', 'create']:
            return 0, 'https://github.com/octo/app/issues/7\n', ''
        if args[:2] == ['api', 'repos/octo/app/issues/7/comments']:
            return 0, '[{"body": "Hello, it is today", "user": {"login": "grok"}, "created_at": "2099-01-01T00:00:00Z"}]', ''
        return 0, '', ''


class GrokSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdirs.mkdtemp()
        self.env = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': os.path.join(self.tmp, 'cfg'), 'HOME': self.tmp,
                                                'TUTTI_STATE_DIR': os.path.join(self.tmp, 'state')}, clear=False)
        self.env.start()
        for k in (groksetup.URL_ENV, groksetup.KEY_ENV):
            os.environ.pop(k, None)
        self.cfg_path = os.path.join(self.tmp, 'tutti.config.yaml')
        with open(self.cfg_path, 'w', encoding='utf8') as f:
            f.write(CFG)
        self.profile = os.path.join(self.tmp, '.profile')

    def tearDown(self):
        self.env.stop()

    def run_setup(self, inputs, keys, posted):
        out = io.StringIO()
        def post(url, key, body, timeout=30):
            posted.append((url, key, body))
            return 200
        it = iter(inputs)
        ioh = {'isatty': lambda: True, 'input': lambda _p='': next(it), 'getpass': mock.Mock(side_effect=keys), 'out': out}
        with redirect_stdout(out), self.assertLogs(level='DEBUG') as logs:
            import logging
            logging.getLogger('t').debug('start')
            rc = groksetup.run(config.load(self.cfg_path), io=ioh, gh_fn=FakeGh(), post_fn=post, profile=self.profile,
                               proc=os.path.join(self.tmp, 'noproc'))
        return rc, out.getvalue(), '\n'.join(logs.output)

    def test_happy_path_saves_600_profile_once_config_and_test_answer(self):
        posted = []
        with mock.patch('tutti.scoutcmd.answer', return_value=0):
            rc, out, logs = self.run_setup(['', 'http://evil.example/x', 'https://evil.example/x', URL], ['', 'Bearer ' + KEY], posted)
        self.assertEqual(rc, 0)
        self.assertIn('octo/app', out)
        self.assertIn('Create a routine named Tutti task', out)
        self.assertIn('Done: the scout is set up.', out)
        self.assertNotIn(KEY, out + logs)
        p = groksetup.env_path()
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(groksetup.read_env_file(p), {groksetup.URL_ENV: URL, groksetup.KEY_ENV: KEY})
        self.assertEqual(posted[0][1], KEY)
        self.assertIn('grok', config.load(self.cfg_path)['scouts'])
        self.assertFalse(os.path.exists(self.profile))   # the key never goes into every shell's environment (t116)
        self.assertNotIn(groksetup.KEY_ENV, os.environ)

    def test_no_tty_prints_the_command(self):
        out = io.StringIO()
        rc = groksetup.run(config.load(self.cfg_path), io={'isatty': lambda: False, 'out': out})
        self.assertEqual(rc, 2)
        self.assertIn('tutti scout setup grok', out.getvalue())

    def test_old_profile_line_is_removed(self):
        p = groksetup.env_path()
        with open(self.profile, 'w', encoding='utf8') as f:
            f.write(f'x=1\n{groksetup.COMMENT}\n{groksetup.profile_line(p)}\n')
        self.assertTrue(groksetup.remove_from_profile(p, self.profile))
        with open(self.profile, encoding='utf8') as f:
            self.assertEqual(f.read(), 'x=1\n')
        self.assertFalse(groksetup.remove_from_profile(p, self.profile))
        self.assertFalse(groksetup.remove_from_profile(p, os.path.join(self.tmp, 'none')))

    def test_check_url(self):
        self.assertIsNone(groksetup.check_url(URL))
        self.assertIn('https://', groksetup.check_url('http://api2.cursor.sh/x'))
        self.assertIn('not Grok Bot', groksetup.check_url('https://notcursor.sh/x'))

    def test_restart_dashboard_by_pid(self):
        proc = os.path.join(self.tmp, 'proc')
        os.makedirs(os.path.join(proc, '42'))
        with open(os.path.join(proc, '42', 'cmdline'), 'wb') as f:
            f.write(b'python3\0/x/bin/tutti\0dashboard\0--port\0004777\0')
        os.symlink(self.tmp, os.path.join(proc, '42', 'cwd'))
        self.assertEqual(groksetup.dashboard_pids(proc), [42])
        killed, spawned = [], []
        child = mock.Mock(pid=43)
        def spawn(argv, **kw):
            spawned.append((argv, kw['cwd']))
            return child
        new = groksetup.restart_dashboard(42, proc=proc, kill=lambda p, s: killed.append(p), spawn=spawn, sleep=lambda _s: None)
        self.assertEqual((new, killed, spawned[0][1]), (43, [42], self.tmp))
        self.assertEqual(spawned[0][0][2], 'dashboard')


class SpendEstimate(unittest.TestCase):
    def test_a_sent_question_moves_the_meter_but_is_not_a_fresh_reading(self):
        from datetime import datetime, timedelta, timezone

        from tutti import pacing, scouts
        log = os.path.join(tmpdirs.mkdtemp(), 'usage.jsonl')
        now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
        pacing.append_log(log, 'grok-bot', 1, now - timedelta(hours=5), resets='2026-11-15T00:00Z')
        sc = {'name': 'grok', 'account': 'grok-bot', '_usage_log': log, '_task_pct': 2.0}
        e = scouts.spend_estimate(sc, {'id': 's1'}, now.timestamp())
        self.assertEqual((e['used'], e['source']), (3.0, 'estimate'))
        a = {'window': '7d', 'resets': '2026-11-15T00:00Z'}
        st = pacing.account_state('grok-bot', a, pacing.read_log(log), now)
        self.assertEqual(st['used'], 3.0)
        self.assertEqual(st['reading_at'], e['at'])
        later = datetime(2026, 11, 15, 1, tzinfo=timezone.utc)
        self.assertEqual(scouts.spend_estimate(sc, {'id': 's2'}, later.timestamp())['used'], 2.0)   # after the reset: from 0
        self.assertIsNone(scouts.spend_estimate({'name': 'x'}, {'id': 's3'}))

    def test_page_reading_reports_a_loading_page_and_saves_nothing(self):
        from tutti import cli
        cfg = {'accounts': {}}
        log = os.path.join(tmpdirs.mkdtemp(), 'usage.jsonl')
        with redirect_stdout(io.StringIO()), mock.patch.object(cli, 'say') as say:
            rc = cli.page_reading(cfg, log, '{"readings": [], "missing": ["Grok Bot"], "loading": true}', None)
        self.assertEqual(rc, 3)
        self.assertIn('still loading', say.call_args[0][0])
        self.assertFalse(os.path.exists(log))


if __name__ == '__main__':
    unittest.main()
