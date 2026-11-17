"""Automatic usage readings: CLI readers, limit messages in job output, the reset anchor and the dashboard's reading form."""
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta, timezone
from unittest import mock

from tutti import browserusage, cli, codexusage, config, dashboard, pacing, readers, routing, state, workers

# These tests read times in UTC: the host's detected zone is pinned so they pass anywhere (tests/test_tz.py covers zones)
_PIN = mock.patch('tutti.tz.host_zone', return_value='UTC')


def setUpModule():
    _PIN.start()


def tearDownModule():
    _PIN.stop()

T = pacing.parse_time
NOW = T('2026-01-06T12:00Z')


def bucket(id_, left, reset):
    return {'id': id_, 'remaining_fraction': left, 'reset_time': reset}


AGY = {'command': {'name': 'usage', 'data': {'groups': [
    {'name': 'Gemini Models', 'buckets': [bucket('gemini-5h', 0.75, '2026-01-06T15:00:00Z'),
                                          bucket('gemini-weekly', 1, '2026-01-12T00:00:00Z')]},
    {'name': 'Claude and GPT models', 'buckets': [bucket('3p-5h', 0.1, '2026-01-06T14:30:00Z')]},
]}}}
AGY_CMD = ['agy', '-p', '/usage', '--output-format', 'json']
ACCOUNTS = {
    'cursor': {'window': 'monthly', 'resets': '2026-01-01T00:00Z', 'resets_estimated': True, 'match': {'command': 'cursor-agent'},
               'plan': {'command': ['cursor-agent', 'about', '--format', 'json'], 'field': 'subscriptionTier'}},
    'agy-claude': {'window': '5h', 'match': {'command': r'\bagy\b', 'model': 'claude|sonnet|opus'},
                   'reader': {'command': AGY_CMD, 'bucket': '3p-5h'}},
    'agy-gemini': {'window': '5h', 'match': {'command': r'\bagy\b', 'model': 'gemini', 'fallback': True},
                   'reader': {'command': AGY_CMD, 'bucket': 'gemini-5h'}},
}


def cfg_of(folder, accounts=None, workers_=None):
    raw = {'project': {'worktrees': folder, 'state': os.path.join(folder, 'state')}, 'accounts': accounts or ACCOUNTS,
           'workers': workers_ or {}, 'usage': {'log': os.path.join(folder, 'usage.jsonl')}}
    cfg = config.normalise(raw)
    cfg['_path'] = os.path.join(folder, 'tutti.config.yaml')
    return cfg


class ParseTest(unittest.TestCase):
    def test_codex_rate_limits_match_the_configured_window(self):
        rates = {'primary': {'usedPercent': 28, 'windowDurationMins': 300, 'resetsAt': 1767708000},
                 'secondary': {'usedPercent': 64, 'windowDurationMins': 10080, 'resetsAt': 1768176000}}
        self.assertEqual(codexusage.window(rates, {'window': '7d'}, {}),
                         (64.0, T('2026-01-12T00:00Z')))
        self.assertEqual(codexusage.window(rates, {'window': '5h'}, {}),
                         (28.0, T('2026-01-06T14:00Z')))

    def test_codex_app_server_response_ignores_notifications_and_rejects_missing_limits(self):
        text = '{"method":"account/updated"}\n{"id":2,"result":{"rateLimits":{"primary":{"usedPercent":12}}}}\n'
        self.assertIn('primary', codexusage.response(text))
        with self.assertRaisesRegex(ValueError, 'no rateLimits'):
            codexusage.response('{"id":2,"result":{}}')

    def test_codex_usage_starts_the_configured_cli_app_server_with_a_read_only_request(self):
        rates = {'secondary': {'usedPercent': 45, 'windowDurationMins': 10080, 'resetsAt': 1768176000}}
        script = ('import json,sys\n'
                  'assert json.loads(input())["method"] == "initialize"\n'
                  'print(json.dumps({"id":1,"result":{}}), flush=True)\n'
                  'assert json.loads(input())["method"] == "initialized"\n'
                  'assert json.loads(input())["method"] == "account/rateLimits/read"\n'
                  f'print(json.dumps({{"id":2,"result":{{"rateLimits":{rates!r}}}}}), flush=True)\n')
        def popen(argv, **kwargs):
            self.assertEqual(argv, ['/opt/codex', 'app-server', '--listen', 'stdio://'])
            return subprocess.Popen([sys.executable, '-u', '-c', script], **kwargs)
        cfg = {'workers': {'codex': {'command': ['/opt/codex', 'exec', '{prompt}']}}}
        self.assertEqual(codexusage.read(cfg, {'worker': 'codex'}, popen=popen), rates)

    def test_codex_usage_reports_a_server_that_closes_early(self):
        def popen(argv, **kwargs):
            return subprocess.Popen([sys.executable, '-c', 'pass'], **kwargs)
        with self.assertRaisesRegex(ValueError, 'closed without a rate limits response'):
            codexusage.read({}, {}, popen=popen)

    def test_browser_usage_uses_the_scoped_visible_page_extractor(self):
        url = 'https://cursor.com/dashboard/spending'
        value = {'readings': [{'account': 'grok-bot', 'used': 31.5}], 'missing': [], 'loading': False}
        broker = mock.Mock()
        broker.page.return_value = {'value': value, 'signed_out': False}
        cfg = {'accounts': {'grok-bot': {'usage_url': url, 'helper_label': 'Grok Bot'}}}
        with mock.patch('tutti.browsercmd.broker', return_value=broker):
            self.assertEqual(browserusage.read(cfg, url)['readings']['grok-bot']['used'], 31.5)
        broker.page.assert_called_once_with('usage-reading', url, 'usage', 'tutti usage reader')

    def test_reset_times_in_limit_messages(self):
        self.assertEqual(readers.parse_reset('quota resets at 2026-01-06T15:30:00Z', NOW), T('2026-01-06T15:30Z'))
        self.assertEqual(readers.parse_reset('try again in 3h 12m', NOW), NOW + timedelta(hours=3, minutes=12))
        self.assertEqual(readers.parse_reset('it refreshes in 6 days, 12 hours', NOW), NOW + timedelta(days=6, hours=12))
        self.assertEqual(readers.parse_reset('resume in 45 minutes', NOW), NOW + timedelta(minutes=45))
        # agy's quota message writes the units together: 147h44m29s (it was read as no reset, so agy kept getting work)
        self.assertEqual(readers.parse_reset('Individual quota reached. Resets in 147h44m29s.', NOW),
                         NOW + timedelta(hours=147, minutes=44, seconds=29))
        self.assertIsNone(readers.parse_reset('back in 3 months', NOW))
        self.assertIsNone(readers.parse_reset('you have reached your usage limit', NOW))

    def test_clock_time_resets(self):
        local = NOW.astimezone(timezone.utc)   # the module pins the zone to UTC
        at = readers.parse_reset("Claude usage limit reached. Your limit resets 3pm", NOW).astimezone(local.tzinfo)
        self.assertEqual((at.hour, at.minute), (15, 0))
        self.assertTrue(NOW < at <= NOW + timedelta(days=1))
        at = readers.parse_reset('limit reached, available again at 09:30', NOW).astimezone(local.tzinfo)
        self.assertEqual((at.hour, at.minute), (9, 30))
        self.assertIsNone(readers.parse_reset('try again 3 times', NOW))
        self.assertIsNone(readers.parse_reset('resets at 27:00', NOW))

    def test_only_limit_lines_are_hits(self):
        lines = ['working', "You've hit your usage limit for this month.", 'It resets in 2 days.', 'Error: 429 is a test name']
        hits = readers.limit_hits(lines, NOW)
        self.assertEqual([(i, r) for i, r, _ in hits], [(1, NOW + timedelta(days=2))])
        self.assertEqual(readers.limit_hits(['all tests passed', 'Nominatim 429 handled'], NOW), [])

    def test_a_limit_message_goes_to_the_right_account(self):
        m = readers.match_account
        self.assertEqual(m(ACCOUNTS, 'cursor-agent -p task', 'usage limit'), 'cursor')
        self.assertEqual(m(ACCOUNTS, 'agy -p task --model claude-opus-5-5-high', 'quota exhausted'), 'agy-claude')
        self.assertEqual(m(ACCOUNTS, 'agy -p task', 'Claude Sonnet quota exhausted'), 'agy-claude')
        self.assertEqual(m(ACCOUNTS, 'agy -p task', 'quota exhausted'), 'agy-gemini')   # no model named: the fallback
        self.assertIsNone(m(ACCOUNTS, 'npm test', 'rate limit'))
        no_fallback = {k: {**v, 'match': {**v['match'], 'fallback': False}} for k, v in ACCOUNTS.items()}
        self.assertIsNone(m(no_fallback, 'agy -p task', 'quota exhausted'))

    def test_agy_usage_buckets(self):
        self.assertEqual(readers.agy_usage(AGY, '3p-5h'), (90.0, T('2026-01-06T14:30Z')))
        self.assertEqual(readers.agy_usage(AGY, 'gemini-5h')[0], 25.0)
        with self.assertRaisesRegex(ValueError, 'gemini-weekly'):
            readers.agy_usage(AGY, 'nope')
        with self.assertRaisesRegex(ValueError, 'command.data.groups'):
            readers.agy_usage({'response': 'text'}, '3p-5h')

    def test_a_disabled_bucket_without_reset_time_waits_for_the_weekly(self):
        live = {'command': {'name': 'usage', 'data': {'groups': [
            {'name': 'Gemini Models', 'buckets': [bucket('gemini-5h', 1, '2026-11-16T17:00:00Z'),
                                                  bucket('gemini-weekly', 1, '2026-11-21T12:00:00Z')]},
            {'name': 'Claude and GPT models', 'buckets': [
                {'id': '3p-5h', 'disabled': True, 'remaining_fraction': 1},
                bucket('3p-weekly', 0, '2026-11-21T12:09:39Z')]},
        ]}}}
        self.assertEqual(readers.agy_usage(live, '3p-5h'), (100.0, T('2026-11-21T12:09:39Z')))
        self.assertEqual(readers.agy_usage(live, 'gemini-5h')[0], 0.0)
        lone = {'command': {'data': {'groups': [{'buckets': [{'id': 'x', 'remaining_fraction': 0.5}]}]}}}
        self.assertEqual(readers.agy_usage(lone, 'x'), (50.0, None))
        live_no_reset = {'command': {'data': {'groups': [{'buckets': [{'id': 'x', 'remaining_fraction': 0.5},
                                                                     bucket('x-weekly', 1, '2026-11-21T12:00:00Z')]}]}}}
        self.assertEqual(readers.agy_usage(live_no_reset, 'x'), (50.0, None))
        with self.assertRaises(KeyError):
            readers.agy_usage({'command': {'data': {'groups': [{'buckets': [{'id': 'x'}]}]}}}, 'x')

    def test_the_most_specific_model_names_the_account(self):
        accounts = {**ACCOUNTS, 'agy-gemini-argon': {'window': 'weekly', 'match': {'command': r'\bagy\b', 'model': 'gemini-4-argon'}}}
        self.assertEqual(readers.match_account(accounts, 'agy -m gemini-4-argon -p x', 'limit'), 'agy-gemini-argon')
        self.assertEqual(readers.match_account(accounts, 'agy -m gemini-3-flash -p x', 'limit'), 'agy-gemini')
        tie = {**ACCOUNTS, 'agy-other': {'match': {'command': r'\bagy\b', 'model': 'gemini'}}}
        self.assertEqual(readers.match_account(tie, 'agy -m gemini -p x', 'limit'), 'agy-gemini')   # a tie: the fallback

    def test_a_reader_reading_overrides_a_newer_limit_message(self):
        with tempfile.TemporaryDirectory() as t:
            path = os.path.join(t, 'usage.jsonl')
            pacing.append_log(path, 'agy-gemini', 0, NOW - timedelta(minutes=30), source='reader')
            pacing.append_log(path, 'agy-gemini', 100, NOW - timedelta(minutes=10), source='limit message', ref='r1')
            reading = {'agy-gemini': {'used': 0.0, 'resets': NOW + timedelta(hours=4)}}
            self.assertEqual(len(readers.record(path, reading, [], NOW)), 1)
            self.assertEqual(pacing.latest(pacing.read_log(path), 'agy-gemini')['used'], 0.0)
            self.assertEqual(readers.record(path, reading, [], NOW + timedelta(minutes=1)), [])
            pacing.append_log(path, 'agy-gemini', 60, NOW + timedelta(minutes=2), 'by hand')
            self.assertEqual(readers.record(path, reading, [], NOW + timedelta(minutes=3)), [])   # a person's reading stays

    def test_a_limit_message_goes_to_the_cap_that_resets_then(self):
        argon = NOW + timedelta(days=4, hours=12)
        readings = {'agy-gemini': {'used': 0.0, 'resets': NOW + timedelta(hours=5)},
                    'agy-gemini-argon': {'used': 100.0, 'resets': argon}}
        self.assertEqual(readers.owner_by_reset('agy-gemini', argon + timedelta(seconds=30), readings), 'agy-gemini-argon')
        self.assertEqual(readers.owner_by_reset('agy-gemini', NOW + timedelta(hours=5), readings), 'agy-gemini')
        self.assertEqual(readers.owner_by_reset('agy-gemini', None, readings), 'agy-gemini')
        self.assertEqual(readers.owner_by_reset('agy-gemini', NOW + timedelta(days=2), readings), 'agy-gemini')
        self.assertEqual(readers.owner_by_reset('agy-x', argon, readings), 'agy-x')                  # no reading of its own
        stale = {**readings, 'agy-gemini': {'resets': NOW - timedelta(hours=1)}}
        self.assertEqual(readers.owner_by_reset('agy-gemini', argon, stale, NOW), 'agy-gemini')          # its reset is past
        two = {**readings, 'agy-other': {'resets': argon + timedelta(minutes=5)}}
        self.assertEqual(readers.owner_by_reset('agy-gemini', argon + timedelta(minutes=4), two), 'agy-other')  # the closest
        self.assertEqual(readers.owner_by_reset('agy-gemini', argon + timedelta(minutes=71), readings), 'agy-gemini')
        with tempfile.TemporaryDirectory() as t:
            path = os.path.join(t, 'usage.jsonl')
            readers.record(path, readings, [('agy-gemini', argon, 'run-agy-1:4', 'limit')], NOW)
            self.assertEqual(pacing.latest(pacing.read_log(path), 'agy-gemini')['used'], 0.0)
            hit = [e for e in pacing.read_log(path) if e.get('ref') == 'run-agy-1:4']
            self.assertEqual([(e['account'], e['used']) for e in hit], [('agy-gemini-argon', 100)])
            # a run's message (no fresh reading): the log's readings place it
            readers.record(path, {}, [('agy-gemini', argon, 'run-agy-2:4', 'limit')], NOW + timedelta(minutes=1))
            self.assertEqual(pacing.latest(pacing.read_log(path), 'agy-gemini')['used'], 0.0)
            self.assertEqual(pacing.latest(pacing.read_log(path), 'agy-gemini-argon')['ref'], 'run-agy-2:4')

    def test_an_hour_precise_reset_text_finds_its_cap(self):
        argon = T('2026-11-21T00:26:39Z')
        now = T('2026-11-16T11:47:17Z')
        said = readers.parse_reset('You have hit your weekly limit, it refreshes in 4 days, 13 hours.', now)
        self.assertEqual(said, T('2026-11-21T00:47:17Z'))   # 21 minutes past the cap's real reset
        readings = {'agy-gemini': {'resets': now + timedelta(hours=5)}, 'agy-gemini-argon': {'resets': argon}}
        self.assertEqual(readers.owner_by_reset('agy-gemini', said, readings, now), 'agy-gemini-argon')

    def test_a_window_not_started_yet_starts_now(self):
        self.assertEqual(readers.not_started(NOW + timedelta(hours=5, seconds=4), '5h', NOW), NOW + timedelta(hours=5))
        self.assertEqual(readers.not_started(NOW + timedelta(hours=2), '5h', NOW), NOW + timedelta(hours=2))
        self.assertEqual(readers.not_started(NOW + timedelta(hours=9), None, NOW), NOW + timedelta(hours=9))

    def test_json_field(self):
        self.assertEqual(readers.json_field({'subscriptionTier': 'Pro'}, 'subscriptionTier'), 'Pro')
        with self.assertRaises(ValueError):
            readers.json_field({}, 'subscriptionTier')


class ReadersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_of(self.tmp.name)
        self.calls = []
        self.t = [NOW.timestamp()]

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, answers):
        def run(cmd):
            self.calls.append(cmd[0])
            return answers[cmd[0]]
        return readers.Readers(self.cfg, run=run, clock=lambda: self.t[0])

    def test_one_command_serves_both_agy_windows_and_is_cached(self):
        r = self.fake({'agy': (AGY, None), 'cursor-agent': ({'subscriptionTier': 'Pro'}, None)})
        out = r.read_all()
        self.assertEqual(out['agy-claude']['used'], 90.0)
        self.assertEqual(out['agy-gemini']['resets'], T('2026-01-06T15:00Z'))
        self.assertEqual(out['cursor']['plan'], 'Pro')
        self.assertEqual(sorted(self.calls), ['agy', 'cursor-agent'])
        r.read_all()
        self.assertEqual(len(self.calls), 2)   # within every_minutes: no new run
        self.t[0] += 11 * 60
        r.read_all()
        self.assertEqual(len(self.calls), 4)

    def test_codex_app_server_is_read_automatically_and_cached(self):
        account = {'window': '7d', 'worker': 'codex', 'reader': {'format': 'codex-app-server', 'every_minutes': 15}}
        cfg = cfg_of(self.tmp.name, {'codex': account}, {'codex': {'command': ['codex', 'exec']}})
        rates = {'secondary': {'usedPercent': 64, 'windowDurationMins': 10080, 'resetsAt': 1768176000}}
        with mock.patch('tutti.codexusage.read', return_value=rates) as read:
            r = readers.Readers(cfg, clock=lambda: self.t[0])
            self.assertEqual(r.read_all()['codex']['used'], 64.0)
            r.read_all()
            self.assertEqual(read.call_count, 1)

    def test_existing_codex_accounts_get_an_automatic_reader_without_setup_migration(self):
        account = {'window': '7d', 'worker': 'codex'}
        cfg = cfg_of(self.tmp.name, {'codex': account}, {'codex': {'command': ['codex', 'exec']}})
        rates = {'secondary': {'usedPercent': 64, 'windowDurationMins': 10080, 'resetsAt': 1768176000}}
        with mock.patch('tutti.codexusage.read', return_value=rates) as read:
            result = readers.Readers(cfg, clock=lambda: self.t[0]).read_all()['codex']
        self.assertEqual(result['used'], 64.0)
        self.assertEqual(result['every_minutes'], 15)
        read.assert_called_once()

    def test_remote_codex_reader_uses_its_automatic_interval(self):
        account = {'window': '7d', 'worker': 'codex', 'read_on': 'laptop'}
        cfg = cfg_of(self.tmp.name, {'codex': account})
        response = {'plan': None, 'used': 64.0, 'resets': T('2026-01-12T00:00Z'), 'problem': None, 'every_minutes': 15}
        with mock.patch('tutti.bridge.read_account', return_value=response) as read:
            r = readers.Readers(cfg, clock=lambda: self.t[0])
            r.read_all()
            r.read_all()
        self.assertEqual(read.call_count, 1)

    def test_usage_page_accounts_are_read_automatically(self):
        account = {'window': '7d', 'usage_url': 'https://cursor.com/dashboard/spending', 'helper_label': 'Grok Bot'}
        cfg = cfg_of(self.tmp.name, {'grok-bot': account})
        answer = {'readings': {'grok-bot': {'used': 31.5, 'resets': '2026-01-12T00:00Z'}}}
        with mock.patch('tutti.browserusage.read', return_value=answer) as read:
            r = readers.Readers(cfg, clock=lambda: self.t[0])
            out = r.read_all()
            self.assertEqual((out['grok-bot']['used'], out['grok-bot']['resets']),
                             (31.5, T('2026-01-12T00:00Z')))
            r.read_all()
            self.assertEqual(read.call_count, 1)

    def test_an_unchanged_answer_backs_off_and_a_change_or_force_resets_it(self):
        answers = {'agy': (AGY, None), 'cursor-agent': ({'subscriptionTier': 'Pro'}, None)}
        r = self.fake(answers)
        spec = self.cfg['accounts']['agy-claude']['reader']
        base = float(r.every_minutes(spec)) * 60
        r.read_all()
        for wait in (1, 2, 4, 8, 8):   # the same answer each time: twice as long, up to BACKOFF_MAX intervals
            self.t[0] += base * wait * 0.5
            before = self.calls.count('agy')
            r.read_all()
            self.assertEqual(self.calls.count('agy'), before, f'asked again too early at x{wait}')
            self.t[0] += base * wait * 0.5
            r.read_all()
            self.assertEqual(self.calls.count('agy'), before + 1)
        self.assertEqual(r.interval_minutes(spec), round(float(r.every_minutes(spec)) * readers.BACKOFF_MAX, 2))
        answers['agy'] = ({**AGY, 'changed': True}, None)
        self.t[0] += base * readers.BACKOFF_MAX
        self.assertEqual(r.read_all()['agy-claude']['every_minutes'], r.every_minutes(spec))   # a change: its own interval
        r.read_all(force=True)
        self.assertEqual(r.interval_minutes(spec), r.every_minutes(spec))
        self.assertGreater(r.runs, 6)

    def test_a_failing_reader_never_backs_off(self):
        r = self.fake({'agy': (None, 'agy exited 1'), 'cursor-agent': ({'subscriptionTier': 'Pro'}, None)})
        spec = self.cfg['accounts']['agy-claude']['reader']
        for _ in range(3):
            r.read_all()
            self.t[0] += float(r.every_minutes(spec)) * 60
        self.assertEqual(self.calls.count('agy'), 3)

    def test_problems_are_named_per_account(self):
        r = self.fake({'agy': (None, 'agy is not on PATH: install it and sign in once'), 'cursor-agent': ({}, None)})
        out = r.read_all()
        self.assertIn('not on PATH', out['agy-claude']['problem'])
        self.assertIn('accounts.cursor.plan', out['cursor']['problem'])
        r2 = self.fake({'agy': ({'command': {'data': {'groups': []}}}, None), 'cursor-agent': ({'subscriptionTier': 'Pro'}, None)})
        self.assertIn('accounts.agy-claude.reader: no bucket', r2.read_all()['agy-claude']['problem'])
        r3 = self.fake({'agy': ({'command': {'data': {'groups': []}}}, None), 'cursor-agent': ({'subscriptionTier': 'Pro'}, None)})
        with mock.patch.object(r3, 'read', side_effect=lambda n, *a, **k: (_ for _ in ()).throw(RuntimeError('boom'))
                               if n == 'cursor' else {'problem': None}):
            out = r3.read_all()
        self.assertIn('boom', out['cursor']['problem'])
        self.assertIn('agy-gemini', out)
        self.assertEqual(readers.Readers.parse({'bucket': 'x'}, {'window': '5h'},
                                               {'command': {'data': {'groups': [{'buckets': [{'id': 'x', 'remaining_fraction': 1}]}]}}},
                                               NOW), (0.0, None))

    def test_record_keeps_only_readings_that_say_something_new_and_each_limit_once(self):
        path = self.cfg['usage']['log']
        reading = {'agy-claude': {'used': 90.0, 'resets': T('2026-01-06T14:30Z')}, 'cursor': {'used': None}}
        self.assertEqual(len(readers.record(path, reading, [], NOW)), 1)
        self.assertEqual(readers.record(path, reading, [], NOW + timedelta(minutes=10)), [])
        self.assertEqual(len(readers.record(path, reading, [], NOW + timedelta(hours=1))), 1)
        moved = {'agy-claude': {'used': 90.4, 'resets': T('2026-01-06T15:30Z')}}
        self.assertEqual(len(readers.record(path, moved, [], NOW + timedelta(hours=1, minutes=5))), 1)
        hit = [('cursor', None, 'job.log:3', 'usage limit')]
        self.assertEqual(readers.record(path, {}, hit, NOW)[0]['used'], 100)
        self.assertEqual(readers.record(path, {}, hit, NOW), [])
        entries = pacing.read_log(path)
        self.assertEqual(entries[-1]['source'], 'limit message')
        self.assertIn('window rule', entries[-1]['note'])

    def test_quiet_logs_of_agent_jobs_are_scanned_once(self):
        quiet = os.path.join(self.cfg['project']['state'], 'quiet')
        os.makedirs(quiet)
        for name, cmd, text in (('1-1.log', 'agy -p fix --model claude-sonnet-5-5', 'Quota exhausted. Resets in 2h.'),
                                ('2-2.log', 'bash scripts/ci/run-all-checks.sh', 'rate limit test passed'),
                                ('3-3.log', 'cursor-agent -p fix', 'done')):
            with open(os.path.join(quiet, name), 'w', encoding='utf8') as f:
                f.write(text + '\n')
            with open(os.path.join(quiet, name + '.cmd'), 'w', encoding='utf8') as f:
                f.write(cmd + '\n')
        now = pacing.parse_time(readers.utcnow())
        r = self.fake({})
        hits = r.scan_jobs(now)
        self.assertEqual([(h[0], h[2]) for h in hits], [('agy-claude', '1-1.log:1')])
        self.assertAlmostEqual((hits[0][1] - now).total_seconds(), 7200, delta=1)
        self.assertEqual(r.scan_jobs(now), [])


class AnchorTest(unittest.TestCase):
    def test_a_reading_with_a_reset_beats_the_config(self):
        acc = {'window': '5h', 'resets': '2026-01-06T10:00Z', 'resets_estimated': True}
        s = pacing.account_state('agy', acc, [], NOW)
        self.assertEqual((s['reset'], s['reset_from']), (T('2026-01-06T15:00Z').isoformat(), 'estimate'))
        entries = [{'at': '2026-01-06T11:00:00+00:00', 'account': 'agy', 'used': 40, 'resets': '2026-01-06T14:30:00+00:00'}]
        s = pacing.account_state('agy', acc, entries, NOW)
        self.assertEqual((s['reset'], s['reset_from'], s['used']), (T('2026-01-06T14:30Z').isoformat(), 'reading', 40.0))
        s = pacing.account_state('agy', {'window': '5h'}, entries, NOW)   # no reset in the config at all
        self.assertEqual(s['state'], 'ok')
        self.assertEqual(pacing.account_state('agy', {}, entries, NOW)['state'], 'no reset')   # a window is still needed

    def test_config_checks_the_new_keys(self):
        self.assertEqual(config.validate(config.normalise({'accounts': ACCOUNTS})), [])
        bad = {'a': {'usage_url': 'http://x', 'reader': {'command': [], 'every_minutes': 0}, 'plan': 'x', 'match': {'command': '('}},
               'b': {'match': {'model': 'x'}}, 'c': {'match': {'command': 'x', 'model': '['}}}
        cfg = config.normalise({'accounts': bad})
        errs = '\n'.join(config.validate(cfg) + cfg['_warnings'])   # a reader it cannot read is a warning: skipped
        for key in ('a.usage_url', 'a.reader.command', 'a.reader.bucket', 'a.reader.every_minutes', 'a.plan is', 'a.match.command',
                    'b.match needs command', 'c.match.model'):
            self.assertIn(f'accounts.{key}', errs)


class ReadingPostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        acc = {**ACCOUNTS, 'cursor': {**ACCOUNTS['cursor'], 'usage_url': 'https://provider.example/usage'}}
        cls.cfg = cfg_of(cls.tmp.name, acc)
        collector = dashboard.Collector(cls.cfg, probe_fn=lambda m: routing.Probe(True, 1, 0.5, 4, False),
                                        available_fn=lambda w: (True, 'ok'), stored_fn=lambda m: None,
                                        reader=readers.Readers(cls.cfg, run=lambda cmd: ({'subscriptionTier': 'Pro'}, None)
                                                               if cmd[0] == 'cursor-agent' else (AGY, None)))
        collector.refresh()
        cls.httpd = dashboard.server(cls.cfg, 0, collector)
        cls.port = cls.httpd.server_address[1]
        cls.host = f'127.0.0.1:{cls.port}'
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, **headers):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        c.request(method, path, body=body, headers={'Host': self.host, **headers})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def post(self, data, path='/api/reading', **over):
        headers = {'Origin': f'http://{self.host}', 'Content-Type': 'application/json', 'X-Tutti-Token': self.httpd.token}
        headers.update({k.replace('_', '-'): v for k, v in over.items()})
        headers = {k: v for k, v in headers.items() if v is not None}
        code, body = self.request('POST', path, json.dumps(data).encode(), **headers)
        return code, json.loads(body)

    def test_the_page_carries_the_token_and_the_state_the_readers(self):
        code, body = self.request('GET', '/')
        self.assertIn(f'content="{self.httpd.token}"'.encode(), body)
        s = json.loads(self.request('GET', '/api/state')[1])
        acc = {a['name']: a for a in s['usage']['accounts']}
        self.assertEqual(acc['cursor']['plan'], 'Pro')
        self.assertEqual(acc['cursor']['usage_url'], 'https://provider.example/usage')
        self.assertEqual((acc['agy-claude']['used'], acc['agy-claude']['reset_from']), (90.0, 'reading'))
        self.assertEqual(acc['agy-gemini']['auto_every_minutes'], 0.5)

    def test_a_reading_from_the_page_is_saved(self):
        resets = (readers.utcnow() + timedelta(days=20)).isoformat()
        code, answer = self.post({'account': 'cursor', 'used': 37, 'resets': resets, 'note': 'from the usage page'})
        self.assertEqual((code, answer['ok']), (200, True))
        last = pacing.read_log(self.cfg['usage']['log'])[-1]
        self.assertEqual((last['account'], last['used'], last['source']), ('cursor', 37.0, 'dashboard'))
        self.assertEqual(T(last['resets']), T(resets))

    def test_refresh_readings_runs_the_readers_now(self):
        code, answer = self.post({}, path='/api/refresh')
        self.assertEqual((code, answer['ok']), (200, True))
        self.assertEqual(answer['read'], ['agy-claude', 'agy-gemini', 'cursor'])
        self.assertEqual(self.post({}, path='/api/refresh', X_Tutti_Token='stale')[0], 403)
        self.assertEqual(self.post({}, path='/api/refresh', Origin='http://evil.example')[0], 403)

    def test_cross_site_and_bad_readings_are_refused(self):
        self.assertEqual(self.post({'account': 'cursor', 'used': 1}, Origin='https://evil.example')[0], 403)
        self.assertEqual(self.post({'account': 'cursor', 'used': 1}, Origin=None)[0], 403)
        self.assertEqual(self.post({'account': 'cursor', 'used': 1}, Sec_Fetch_Site='cross-site')[0], 403)
        code, answer = self.post({'account': 'cursor', 'used': 1}, X_Tutti_Token='stale')
        self.assertEqual(code, 403)
        self.assertIn('reload the page', answer['error'])
        self.assertEqual(self.post({'account': 'cursor', 'used': 1}, Content_Type='text/plain')[0], 415)
        self.assertEqual(self.post({'account': 'nope', 'used': 1})[0], 400)
        self.assertEqual(self.post({'account': 'cursor', 'used': 120})[0], 400)
        self.assertEqual(self.post({'account': 'cursor', 'used': True})[0], 400)
        self.assertEqual(self.post({'account': 'cursor', 'used': 5, 'resets': 'soon'})[0], 400)
        self.assertEqual(self.post({'account': 'cursor', 'used': 5, 'resets': '2020-01-01T00:00Z'})[0], 400)
        self.assertEqual(self.post({'account': 'cursor', 'used': 5, 'note': 'x' * 300})[0], 400)
        self.assertEqual(self.post([1, 2])[0], 400)
        self.assertEqual(self.post({'account': 'cursor', 'used': 5, 'note': 'x' * 5000})[0], 413)
        code, _ = self.request('POST', '/api/state', b'{}', Origin=f'http://{self.host}')
        self.assertEqual(code, 404)
        code, _ = self.request('POST', '/api/reading', b'{}', Host='attacker.example')
        self.assertEqual(code, 403)


class RouteByReadingsTest(unittest.TestCase):
    ROUTED = {'agy-claude': {'window': '5h', 'worker': 'agy', 'args': ['--model', 'claude-x']},
              'agy-gemini': {'window': '5h', 'worker': 'agy', 'args': ['--model', 'gemini-x']},
              'cursor': {'window': 'monthly', 'resets': '2026-01-01T00:00Z', 'worker': 'cursor'}}
    WORKERS = {'agy': {'kind': 'cli', 'command': ['agy', '-p', '{prompt}']},
               'cursor': {'kind': 'cli', 'command': ['cursor-agent', '{prompt}']}}

    def pick(self, entries, now=NOW, order=None):
        with tempfile.TemporaryDirectory() as folder:
            cfg = cfg_of(folder, self.ROUTED, self.WORKERS)
            cfg['routing']['workers'] = order or []
            accounts = [pacing.account_state(n, a, entries, now) for n, a in cfg['accounts'].items()]
            return state.next_worker(cfg, accounts)

    def r(self, account, used, resets):
        return {'at': (NOW - timedelta(minutes=5)).isoformat(), 'account': account, 'used': used, 'resets': resets}

    def test_a_near_cap_reading_moves_the_next_job_to_another_account(self):
        fresh = [self.r('agy-claude', 30, '2026-01-06T14:00Z'), self.r('agy-gemini', 10, '2026-01-06T16:00Z')]
        first = self.pick(fresh)
        self.assertEqual((first['worker'], first['account'], first['args']), ('agy', 'agy-claude', ['--model', 'claude-x']))
        near_cap = fresh + [{**self.r('agy-claude', 97, '2026-01-06T14:00Z'), 'at': NOW.isoformat()}]
        moved = self.pick(near_cap)
        self.assertEqual((moved['account'], moved['args']), ('agy-gemini', ['--model', 'gemini-x']))
        self.assertIn('agy-claude: panic', moved['reason'])
        spent = [{**self.r(n, 100, resets), 'at': NOW.isoformat()} for n, resets in (('agy-gemini', '2026-01-06T16:00Z'),
                                                                                      ('cursor', '2026-02-01T00:00Z'))]
        all_spent = near_cap + spent
        none = self.pick(all_spent)
        self.assertIsNone(none['worker'])
        self.assertIn('local model', none['reason'])

    def test_quota_that_would_be_lost_goes_first_and_order_breaks_ties(self):
        late = [self.r('agy-claude', 10, '2026-01-06T12:30Z'), self.r('agy-gemini', 0, '2026-01-06T16:59Z')]
        self.assertEqual(self.pick(late)['account'], 'agy-claude')   # 30 min to its reset with 90 % left: late
        self.assertEqual(self.pick([], order=['cursor', 'agy'])['account'], 'cursor')
        self.assertEqual(self.pick([], order=['agy'])['account'], 'agy-claude')

    def test_codex_joins_run_auto_when_left_off_routing_workers_and_cursor_is_ahead(self):
        """t297: routing.workers is order only; a behind Codex account on the laptop still wins while Cursor is ahead."""
        with tempfile.TemporaryDirectory() as folder:
            wk = {**self.WORKERS, 'codex': {'kind': 'cli', 'command': ['codex', 'exec', '--skip-git-repo-check', '{prompt}'],
                                            'machine': 'laptop'}}
            acc = {**self.ROUTED, 'codex': {'window': '7d', 'resets': '2026-01-13T00:00Z', 'worker': 'codex'}}
            cfg = cfg_of(folder, acc, wk)
            cfg['machines'] = {'laptop': {'host': 'local', 'slots': 1}}
            cfg['routing']['workers'] = ['agy', 'cursor']   # codex deliberately omitted: still must join
            cfg['autopilot'] = {'ladder': ['codex', 'cursor'], 'accounts': ['cursor', 'codex'],
                                'repos': {'app': {'path': folder, 'check': 'true'}}}
            entries = [self.r('cursor', 70, '2026-02-01T00:00Z'), self.r('codex', 5, '2026-01-13T00:00Z'),
                       self.r('agy-claude', 99, '2026-01-06T14:00Z'), self.r('agy-gemini', 99, '2026-01-06T16:00Z')]
            accounts = [pacing.account_state(n, a, entries, NOW) for n, a in cfg['accounts'].items()]
            by = {a['name']: a for a in accounts}
            self.assertEqual(by['cursor']['verdict'], 'ahead')
            self.assertEqual(by['codex']['verdict'], 'behind')
            pick = state.next_worker(cfg, accounts)
            self.assertEqual((pick['worker'], pick['account']), ('codex', 'codex'))
            self.assertIn('codex is behind', pick['reason'])
            from tutti import autopilot
            self.assertEqual(autopilot.on_machine(cfg, 'codex'), 'laptop')
            room, why = autopilot.step_room(cfg, 'codex', by, {})
            self.assertGreater(room, 0, why)
            room, why = autopilot.step_room(cfg, 'cursor', by, {})
            self.assertEqual(room, 0)
            self.assertIn('ahead', why)
            lent = [{'name': 'laptop', 'status': 'paused', 'free': 0, 'reason': 'yield flag is up'}]
            self.assertEqual(autopilot.step_room(cfg, 'codex', by, {}, lent)[0], 0)
            t = {'id': 't1', 'repo': 'app', 'title': 'x', 'status': 'queued', 'attempts': [], 'prefer': None, 'size': 'S',
                 'scope': ['a.py'], 'check': 'true'}
            self.assertEqual(autopilot.candidates(cfg, t)[:2], ['codex', 'cursor'])
            free = [{'name': 'laptop', 'status': 'free', 'free': 1, 'reason': 'fit'}]
            plan = autopilot.plan(cfg, [t], by, free, True, NOW)
            self.assertEqual(plan.get('dispatch'), [('t1', 'codex')], plan)

    def test_run_auto_uses_the_pick_and_refuses_when_nothing_has_room(self):
        with tempfile.TemporaryDirectory() as folder:
            script = os.path.join(folder, 'agent')
            with open(script, 'w', encoding='utf8') as f:
                f.write('import sys\nprint("args", sys.argv[2:])\n')
            wk = {'agy': {'kind': 'cli', 'command': [sys.executable, script, '{prompt}']}}
            acc = {'agy-claude': {'window': '5h', 'worker': 'agy', 'args': ['--model', 'claude-x']}}
            cfg = cfg_of(folder, acc, wk)
            path = os.path.join(folder, 'tutti.config.json')
            with open(path, 'w', encoding='utf8') as f:
                json.dump({k: cfg[k] for k in ('project', 'accounts', 'workers', 'usage')}, f)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(cli.main(['--config', path, 'run', 'auto', 'task']), 0)
            self.assertIn("'--model', 'claude-x'", out.getvalue())
            self.assertIn('run auto: agy --model claude-x', err.getvalue())
            soon = (readers.utcnow() + timedelta(hours=2)).isoformat()
            pacing.append_log(cfg['usage']['log'], 'agy-claude', 99, readers.utcnow(), resets=soon)
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                self.assertEqual(cli.main(['--config', path, 'run', 'auto', 'task']), 3)
            self.assertIn('no agent account has room', err.getvalue())

    def test_config_checks_worker_keys(self):
        bad = config.normalise({'routing': {'workers': ['ghost']}, 'accounts': {'a': {'worker': 'ghost', 'args': '--x'}}})
        errs = '\n'.join(config.validate(bad))
        for text in ('routing.workers names "ghost"', 'accounts.a.worker names "ghost"', 'accounts.a.args'):
            self.assertIn(text, errs)


class ProgressTest(unittest.TestCase):
    def test_progress_from_log_lines_never_a_guess(self):
        p = state.progress_of_line
        self.assertEqual(p('  ✓ [150/177] e2e/admin.spec.ts')['pct'], 85)
        self.assertEqual(p('tests/test_a.py ....  [ 13%]')['pct'], 13)
        self.assertEqual(p('stage 2 of 4: tests')['pct'], 50)
        self.assertEqual(p('12/40 tests')['pct'], 30)
        for line in ('coverage 90%', 'on 11/14 at noon', '[9/3]', '[ 140%]', 'stage 5 of 0'):
            self.assertIsNone(p(line), line)

    def test_jobs_carry_progress_and_tutti_progress_reports_it(self):
        with tempfile.TemporaryDirectory() as folder:
            quiet = os.path.join(folder, 'state', 'quiet')
            os.makedirs(quiet)
            log = os.path.join(quiet, '1700000000-999999.log')
            with open(log, 'w', encoding='utf8') as f:
                f.write('[3/10] building\n[4/10] building\nwarning: 50% of x\n')
            cfg = cfg_of(folder)
            cfg['dashboard']['stall_minutes'] = 10
            item = state.jobs_state(cfg, 1700000100, is_alive=lambda pid: True)['items'][0]
            self.assertEqual(item['progress'], {'done': 4, 'total': 10, 'pct': 40, 'source': 'log'})
            path = os.path.join(folder, 'tutti.config.json')
            with open(path, 'w', encoding='utf8') as f:
                json.dump({'project': cfg['project']}, f)
            self.assertEqual(cli.main(['--config', path, 'progress', '7/10', '--job', 'quiet:1700000000-999999.log']), 0)
            item = state.jobs_state(cfg, 1700000100, is_alive=lambda pid: True)['items'][0]
            self.assertEqual((item['progress']['pct'], item['progress']['source']), (70, 'reported'))
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(cli.main(['--config', path, 'progress', '11/10', '--job', log]), 2)
                os.environ.pop('TUTTI_PROGRESS_FILE', None)
                self.assertEqual(cli.main(['--config', path, 'progress', '1/2']), 2)
            self.assertIn('<done>/<total>', err.getvalue())
            self.assertIn('TUTTI_PROGRESS_FILE', err.getvalue())
            with open(log + '.rc', 'w', encoding='utf8') as f:
                f.write('0\n')
            item = state.jobs_state(cfg, 1700000100, is_alive=lambda pid: False)['items'][0]
            self.assertEqual(item['progress']['pct'], 100)
            os.remove(log + '.progress')
            with open(log, 'w', encoding='utf8') as f:
                f.write('no numbers here\n')
            os.remove(log + '.rc')
            self.assertIsNone(state.jobs_state(cfg, 1700000100, is_alive=lambda pid: True)['items'][0]['progress'])


class CliTest(unittest.TestCase):
    def test_usage_log_with_a_reset_anchors_the_window(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'tutti.config.json')
            with open(path, 'w', encoding='utf8') as f:
                json.dump({'project': {'state': folder}, 'usage': {'log': os.path.join(folder, 'u.jsonl')},
                           'accounts': {'cursor': {'window': 'monthly'}}}, f)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.main(['--config', path, 'usage', 'log', 'cursor', '40', '--resets', '2026-01-20T00:00Z',
                               '--at', '2026-01-06T00:00Z'])
            self.assertEqual(rc, 0)
            self.assertIn('Tue 20 Jan 00:00 (UTC)', out.getvalue())
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--config', path, 'usage', 'log', 'cursor']), 2)
            self.assertIn('tutti usage log <account> <percent>', err.getvalue())
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--config', path, 'usage', 'read']), 0)
            self.assertIn('0 account(s) with a reader', err.getvalue())

    def test_a_worker_that_hits_its_limit_is_logged_as_spent(self):
        with tempfile.TemporaryDirectory() as folder:
            script = os.path.join(folder, 'fake-cursor-agent')
            with open(script, 'w', encoding='utf8') as f:
                f.write("import sys\nprint('working')\nprint('You have hit your usage limit. Try again in 3 hours.')\nsys.exit(1)\n")
            accounts = {'cursor': {'window': 'monthly', 'match': {'command': 'fake-cursor-agent'}}}
            cfg = cfg_of(folder, accounts, {'cursor': {'kind': 'cli', 'command': [sys.executable, script, '{prompt}']}})
            out = io.StringIO()
            rc, lines = workers.run(cfg, 'cursor', 'task', out=out)
            self.assertEqual(rc, 1)
            self.assertIn('usage limit', out.getvalue())
            self.assertEqual(lines[0], 'working')
            hits = readers.scan_text(cfg['accounts'], ' '.join(cfg['workers']['cursor']['command']), lines, NOW, 'run')
            self.assertEqual([(h[0], h[1]) for h in hits], [('cursor', NOW + timedelta(hours=3))])
            path = os.path.join(folder, 'tutti.config.json')
            with open(path, 'w', encoding='utf8') as f:
                json.dump({k: cfg[k] for k in ('project', 'accounts', 'workers', 'usage')}, f)
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--config', path, 'run', 'cursor', 'task']), 1)
            self.assertIn('cursor is logged as spent until', err.getvalue())
            self.assertEqual(pacing.read_log(cfg['usage']['log'])[-1]['source'], 'limit message')

    def test_workers_find_their_account_by_worker_key(self):
        cfg = config.normalise({'accounts': {'agy-claude': {'worker': 'agy'}, 'cursor': {}}})
        self.assertEqual(state.account_of_worker(cfg, 'agy'), 'agy-claude')
        self.assertEqual(state.account_of_worker(cfg, 'cursor'), 'cursor')
        self.assertIsNone(state.account_of_worker(cfg, 'pc'))


if __name__ == '__main__':
    unittest.main()
