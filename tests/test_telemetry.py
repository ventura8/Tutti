"""Telemetry: scrubbing above all (nothing personal ever leaves), local recording and rotation, consent, the DSN, sending to a
fake Sentry (a real HTTP server on 127.0.0.1: envelopes, batches, offline and refused sends), the CLI wrapper, the report and
the dashboard's endpoints. No real DSN, host or account is used."""
import io
import json
import os
import socket
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

from tutti import cli, config, dashtelemetry, insights, scrub, sentry, telemetry, telemetrycmd

HOME = os.path.expanduser('~')
SECRETS = [
    'ghp_abcdefghijklmnopqrstuvwxyz0123456789', 'sk-ant-api03-abcdefghijklmnop1234', 'AKIAABCDEFGHIJKLMNOP',
    'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopqrstuv', 'owner@example.com', 'me@box',
    '192.168.1.20', 'build-box.lan', 'api.example.com', 'deadbeefdeadbeefdeadbeefdeadbeef', 'hunter2-password',
]


class FakeSentry:
    """Sentry's envelope endpoint: records each request; answers `status` (a list: one per request, the last repeating)."""

    def __init__(self, status=(200,)):
        self.calls, self.status = [], list(status)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                fake.calls.append({'path': self.path, 'headers': dict(self.headers), 'body': body})
                code = fake.status.pop(0) if len(fake.status) > 1 else fake.status[0]
                self.send_response(code)
                self.end_headers()

            def log_message(self, *a):
                return

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def dsn(self, project=42):
        return f'http://publickey@127.0.0.1:{self.port}/{project}'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def envelopes(self):
        out = []
        for c in self.calls:
            head, item, payload = c['body'].split(b'\n')[:3]
            out.append((json.loads(head), json.loads(item), json.loads(payload)))
        return out


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, 'state')
        self.folder = telemetry.configure(self.state, {'machines': {'nuc': {'host': 'my-nuc', 'user': 'alice'}}}, 'test', env={})
        self.env = {'XDG_CONFIG_HOME': os.path.join(self.tmp.name, 'xdg')}
        # hermetic: a real DSN or opt-in on this machine (~/.config/tutti/telemetry.env, $TUTTI_SENTRY_DSN) is never read,
        # so no test can send to a real Sentry or fail because of one; every call gets self.env, never {} (which falls back
        # to the real ~/.config)
        clean = {k: v for k, v in os.environ.items() if not k.startswith('TUTTI_')}
        patcher = mock.patch.dict(os.environ, {**clean, **self.env}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        telemetry.unconfigure()
        self.tmp.cleanup()

    def cfg(self, dsn='', endpoint=''):
        return {'telemetry': {'sentry_dsn': dsn, 'endpoint': endpoint}, 'project': {'state': self.state}}

    def assert_clean(self, text):
        for s in SECRETS + [HOME, 'my-nuc', 'alice', socket.gethostname()]:
            if len(s) > 2:
                self.assertNotIn(s, text, f'{s!r} leaked')


class Scrubbing(Base):
    def test_free_text_loses_everything_personal(self):
        raw = (f'failed at {HOME}/Projects/secret-app/main.py for owner@example.com via https://api.example.com/v1?key=1 on '
               '192.168.1.20 and build-box.lan (me@box) token ghp_abcdefghijklmnopqrstuvwxyz0123456789 password=hunter2-password '
               'sk-ant-api03-abcdefghijklmnop1234 AKIAABCDEFGHIJKLMNOP id deadbeefdeadbeefdeadbeefdeadbeef '
               'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopqrstuv my-nuc alice')
        out = scrub.text(raw, hosts=('my-nuc', 'alice'))
        self.assert_clean(out)
        self.assertIn('~/Projects', out)

    def test_quoted_fragments_are_what_the_user_typed_and_go(self):
        out = scrub.text('no task "fix the login of Maria" in \'my private repo\' or `rm -rf x`')
        self.assertNotIn('Maria', out)
        self.assertNotIn('private', out)
        self.assertNotIn('rm -rf', out)

    def test_other_homes_and_windows_paths_go_too(self):
        out = scrub.text('/home/bob/x.py C:\\Users\\carol\\a.txt /Users/dave/b', quotes=False)
        for name in ('bob', 'carol', 'dave'):
            self.assertNotIn(name, out)

    def test_file_names_and_module_names_stay_readable(self):
        out = scrub.text('tutti.config.example.yaml and tutti.cli.main', quotes=False)
        self.assertIn('tutti.config.example.yaml', out)
        self.assertIn('tutti.cli.main', out)

    def test_a_private_key_block_goes(self):
        out = scrub.text('-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----')
        self.assertEqual(out, '<key>')

    def test_categories_only(self):
        self.assertEqual(scrub.category('autopilot.attempt'), 'autopilot.attempt')
        self.assertEqual(scrub.category(3), 3)
        self.assertIsNone(scrub.category(None))
        for bad in ('10.0.0.5', 'box.lan', 'api.example.com', 'fix the login bug',
                    'owner@example.com', '/home/x/y', 'ghp_abcdefghijklmnopqrstuvwxyz0123', 'x' * 65, 'a;b'):
            self.assertEqual(scrub.category(bad), scrub.OTHER, bad)

    def test_fields_drop_bad_names_and_text_values(self):
        out = scrub.fields({'outcome': 'ok', 'task text': 'x', 'title': 'Fix the bug in login', 'flags': ['--bg', 'a b']})
        self.assertEqual(out, {'outcome': 'ok', 'title': scrub.OTHER, 'flags': ['--bg', scrub.OTHER]})

    def test_a_page_url_keeps_only_its_path(self):
        self.assertEqual(scrub.url_path('http://127.0.0.1:4777/app.js?v=1#x'), '/app.js')
        self.assertEqual(scrub.url_path('https://evil.example/%2e%2e/x'), scrub.OTHER)

    def test_frame_paths(self):
        self.assertEqual(scrub.path(os.path.join(telemetry.PACKAGE, 'cli.py'), telemetry.PACKAGE), 'tutti/cli.py')
        self.assertNotIn(HOME, scrub.path(f'{HOME}/elsewhere/deep/lib.py', telemetry.PACKAGE))


class Recording(Base):
    def test_events_are_recorded_locally_with_categories_only(self):
        ev = telemetry.record('feature', 'scout.ask', skill='triage', question='what is the secret plan of owner@example.com')
        self.assertEqual(ev['question'], scrub.OTHER)
        with open(os.path.join(self.folder, telemetry.EVENTS), encoding='utf8') as f:
            text = f.read()
        self.assertIn('scout.ask', text)
        self.assert_clean(text)
        self.assertEqual(telemetry.read_events(self.folder)[0]['skill'], 'triage')

    def test_nothing_is_recorded_unless_configured_or_when_turned_off(self):
        telemetry.unconfigure()
        self.assertIsNone(telemetry.record('command', 'x'))
        self.assertIsNone(telemetry.configure(self.state, None, 'x', env={'TUTTI_TELEMETRY': 'off'}))
        self.assertIsNone(telemetry.record('command', 'x'))
        self.assertFalse(os.path.exists(os.path.join(self.folder, telemetry.EVENTS)))

    def test_the_file_rotates(self):
        path = os.path.join(self.folder, telemetry.EVENTS)
        for _ in range(4):
            telemetry.append(path, {'v': 1, 'pad': 'x' * 60})
            telemetry.rotate(path, max_bytes=100, keep=2)
        self.assertTrue(os.path.exists(path + '.1'))
        self.assertFalse(os.path.exists(path + '.3'))

    def test_the_outbox_fills_only_with_consent_and_is_capped(self):
        telemetry.record('command', 'a')
        self.assertFalse(os.path.exists(os.path.join(self.folder, telemetry.OUTBOX)))
        self.assertIsNone(telemetry.consent(self.folder)['send'])
        telemetry.set_consent(self.folder, True)
        for _ in range(5):
            telemetry.record('command', 'b')
        telemetry.cap(os.path.join(self.folder, telemetry.OUTBOX), limit=3, max_bytes=0)
        self.assertEqual(telemetry.status(self.folder)['queued'], 3)

    def test_the_install_id_is_random_and_kept(self):
        a = telemetry.consent(self.folder)['install_id']
        self.assertEqual(a, telemetry.consent(self.folder)['install_id'])
        self.assertRegex(a, r'^[0-9a-f]{32}$')

    def test_tool_and_machine_kinds_never_carry_a_made_up_name(self):
        self.assertEqual(telemetry.tool_of('agy-gemini'), 'agy')
        self.assertEqual(telemetry.tool_of('marias-box'), 'other')
        self.assertEqual(telemetry.machine_kind({'host': 'secret.lan', 'transport': 'wsl'}), 'wsl')


class Crashes(Base):
    def crash(self):
        try:
            raise ValueError(f'cannot read {HOME}/Projects/x/tutti.config.yaml for owner@example.com on my-nuc: "my task text"')
        except ValueError as e:
            return telemetry.crash_event(type(e), e, e.__traceback__, {'where': 'cli'})

    def test_a_crash_event_is_allowlisted_and_scrubbed(self):
        ev = self.crash()
        text = json.dumps(ev)
        self.assert_clean(text)
        self.assertNotIn('my task text', text)
        self.assertNotIn('server_name', ev)
        self.assertEqual(set(ev), {'event_id', 'timestamp', 'platform', 'level', 'logger', 'release', 'environment', 'tags', 'contexts',
                                   'user', 'exception'})
        frame = ev['exception']['values'][0]['stacktrace']['frames'][-1]
        self.assertEqual(set(frame), {'filename', 'function', 'lineno', 'in_app'})   # never a source line or a local
        self.assertEqual(ev['release'], f'tutti@{telemetry.__version__}')
        self.assertEqual(ev['level'], 'fatal')

    def test_a_crash_is_recorded_locally_and_queued_only_with_consent(self):
        telemetry.capture_exception(*self.crash_info())
        self.assertEqual(telemetry.read_events(self.folder)[-1]['kind'], 'crash')
        self.assertEqual(telemetry.status(self.folder)['queued'], 0)
        telemetry.set_consent(self.folder, True)
        telemetry.capture_exception(*self.crash_info())
        self.assertEqual(telemetry.status(self.folder)['queued'], 2)   # the event and its usage record

    def crash_info(self):
        try:
            raise KeyError('x')
        except KeyError as e:
            return type(e), e, e.__traceback__

    def test_a_failed_command_says_its_name_and_code_only(self):
        telemetry.set_consent(self.folder, True)
        ev = telemetry.capture_failure('run', 3, action='')
        self.assertEqual(ev['message']['formatted'], 'tutti run exited 3')
        self.assertEqual(ev['fingerprint'], ['tutti-exit', 'run', '3'])


class Dsn(Base):
    def test_a_dsn_gives_the_envelope_url_and_key(self):
        self.assertEqual(sentry.parse_dsn('https://abc@o1.ingest.sentry.io/123'), ('https://o1.ingest.sentry.io/api/123/envelope/', 'abc'))
        self.assertEqual(sentry.parse_dsn('https://abc@host.example/sub/7')[0], 'https://host.example/sub/api/7/envelope/')

    def test_bad_dsns_say_what_is_wrong(self):
        for bad, word in (('http://k@sentry.example/1', 'https'), ('https://sentry.example/1', 'public key'),
                          ('https://k@sentry.example/x', 'project id'), ('nonsense', 'https')):
            with self.assertRaises(sentry.DsnError) as e:
                sentry.parse_dsn(bad)
            self.assertIn(word, str(e.exception))

    def test_the_dsn_comes_from_env_then_the_file_then_the_config(self):
        cfg = self.cfg(dsn='https://c@cfg.example/3')
        self.assertEqual(telemetry.dsn(cfg, self.env)[1], 'telemetry.sentry_dsn')
        path = telemetry.save_dsn('https://f@file.example/2', self.env)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(telemetry.dsn(cfg, self.env), ('https://f@file.example/2', path))
        self.assertEqual(telemetry.dsn(cfg, {**self.env, 'TUTTI_SENTRY_DSN': 'https://e@env.example/1'})[1], 'TUTTI_SENTRY_DSN')

    def test_a_malformed_dsn_is_never_saved(self):
        with self.assertRaises(sentry.DsnError):
            telemetry.save_dsn('https://nokey.example/1', self.env)
        self.assertFalse(os.path.exists(telemetry.env_file(self.env)))

    def test_setup_reads_the_dsn_hidden_and_saves_it(self):
        said = []
        rc = telemetrycmd.setup(self.folder, read_secret=lambda _p: 'https://k@s.example/9', say=said.append, env=self.env)
        self.assertEqual(rc, 0)
        self.assertFalse(any('k@s.example' in s for s in said))   # the DSN is never echoed
        self.assertEqual(telemetrycmd.setup(self.folder, read_secret=lambda _p: 'bad', say=said.append, env=self.env), 2)

    def test_the_endpoint_must_be_https(self):
        self.assertEqual(sentry.endpoint_errors('https://collect.example/t'), [])
        self.assertEqual(sentry.endpoint_errors('http://127.0.0.1:9/t'), [])
        self.assertTrue(sentry.endpoint_errors('http://collect.example/t'))


class Sending(Base):
    def setUp(self):
        super().setUp()
        telemetry.set_consent(self.folder, True)

    def fake(self, *status):
        f = FakeSentry(status or (200,))
        self.addCleanup(f.close)
        return f

    def test_crashes_and_usage_reach_sentry_as_envelopes(self):
        f = self.fake()
        cfg = self.cfg(dsn=f.dsn())
        for i in range(3):
            telemetry.record('command', 'run', seconds=i)
        try:
            raise RuntimeError(f'boom at {HOME}/x')
        except RuntimeError as e:
            telemetry.capture_exception(type(e), e, e.__traceback__, where='cli')
        r = telemetry.flush(self.folder, cfg, env=self.env, force=True)
        self.assertEqual(r['left'], 0)
        self.assertEqual({c['path'] for c in f.calls}, {'/api/42/envelope/'})
        self.assertIn('sentry_key=publickey', f.calls[0]['headers']['X-Sentry-Auth'])
        kinds = [p.get('level') for _h, _i, p in f.envelopes()]
        self.assertIn('fatal', kinds)
        usage = [p for _h, _i, p in f.envelopes() if p['level'] == 'info'][0]
        self.assertEqual(len(usage['breadcrumbs']['values']), 4)   # 3 commands and the crash's local record
        self.assert_clean(b''.join(c['body'] for c in f.calls).decode())
        self.assertEqual(telemetry.status(self.folder)['queued'], 0)

    def test_offline_keeps_the_queue_for_the_next_try(self):
        telemetry.record('command', 'run')
        cfg = self.cfg(dsn=f'http://k@127.0.0.1:{free_port()}/1')
        r = telemetry.flush(self.folder, cfg, env=self.env, force=True)
        self.assertEqual((r['sent'], r['left']), (0, 1))
        self.assertTrue(telemetry.status(self.folder)['last_error'])

    def test_a_wrong_dsn_keeps_the_queue_and_says_so(self):
        f = self.fake(401)
        telemetry.record('command', 'run')
        r = telemetry.flush(self.folder, self.cfg(dsn=f.dsn()), env=self.env, force=True)
        self.assertEqual(r['left'], 1)
        self.assertIn('telemetry setup', telemetry.status(self.folder)['last_error'])

    def test_throttled_keeps_and_refused_drops(self):
        f = self.fake(429)
        telemetry.record('command', 'run')
        self.assertEqual(telemetry.flush(self.folder, self.cfg(dsn=f.dsn()), env=self.env, force=True)['left'], 1)
        f.status = [400]
        self.assertEqual(telemetry.flush(self.folder, self.cfg(dsn=f.dsn()), env=self.env, force=True)['left'], 0)

    def test_nothing_is_sent_without_consent_or_a_target(self):
        f = self.fake()
        telemetry.record('command', 'run')
        self.assertIn('setup', telemetry.flush(self.folder, self.cfg(), env=self.env, force=True)['why'])
        telemetry.set_consent(self.folder, False)
        self.assertIn('off', telemetry.flush(self.folder, self.cfg(dsn=f.dsn()), env=self.env, force=True)['why'])
        self.assertEqual(f.calls, [])

    def test_a_flush_waits_its_interval(self):
        f = self.fake()
        telemetry.record('command', 'run')
        telemetry.flush(self.folder, self.cfg(dsn=f.dsn()), env=self.env, force=True)
        telemetry.record('command', 'run')
        self.assertEqual(telemetry.flush(self.folder, self.cfg(dsn=f.dsn()), env=self.env)['why'], 'sent recently')

    def test_a_custom_endpoint_gets_json_batches(self):
        f = self.fake()
        telemetry.record('command', 'run')
        telemetry.flush(self.folder, self.cfg(endpoint=f'http://127.0.0.1:{f.port}/collect'), env=self.env, force=True)
        body = json.loads(f.calls[0]['body'])
        self.assertEqual(body['items'][0]['event']['name'], 'run')

    def test_send_test_posts_one_event(self):
        f = self.fake()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(telemetrycmd.send_test(self.cfg(dsn=f.dsn()), self.folder), 0)
        self.assertEqual(f.envelopes()[0][2]['message']['formatted'], 'tutti telemetry test')


class Wrapper(Base):
    def test_a_command_is_recorded_with_flag_names_never_values(self):
        cfg_path = os.path.join(self.tmp.name, 'tutti.config.yaml')
        with open(cfg_path, 'w', encoding='utf8') as f:
            f.write(f'project: {{state: {self.state}}}\n')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = telemetrycmd.run(cli.main, ['--config', cfg_path, 'wait', 'file', '/nonexistent/secret-name', '--timeout', '0.01',
                                             '--poll', '0.01'], env=self.env)
        self.assertEqual(rc, 124)
        ev = [e for e in telemetry.read_events(self.folder) if e['kind'] == 'command'][-1]
        self.assertEqual((ev['name'], ev['rc'], ev['outcome']), ('wait', 124, 'failed'))
        self.assertEqual(ev['flags'], ['--poll', '--timeout'])   # --config is a global option, read before the command
        self.assertNotIn('secret-name', json.dumps(telemetry.read_events(self.folder)))

    def test_a_crash_is_captured_and_raised_again(self):
        def boom(_argv):
            raise ZeroDivisionError('x')
        with self.assertRaises(ZeroDivisionError):
            telemetrycmd.run(boom, ['--config', '/nonexistent.yaml', 'backlog', 'add', 'r', 'title'], env=self.env)
        kinds = [e['kind'] for e in telemetry.read_events(telemetry._ctx['folder'])]
        self.assertIn('crash', kinds)

    def test_subcommand_and_action_come_from_the_parser(self):
        p = cli.parser()
        self.assertEqual(telemetrycmd.subcommand(p, ['telemetry', 'on']), ('telemetry', 'on'))
        self.assertEqual(telemetrycmd.subcommand(p, ['run', 'cursor', 'do the secret thing']), ('run', ''))
        self.assertEqual(telemetrycmd.subcommand(p, ['nonsense']), (scrub.OTHER, ''))
        self.assertEqual(telemetrycmd.flag_names(['--repo=/home/x', '-n', '--', '--not-a-flag']), ['--repo', '-n'])

    def test_the_consent_question_is_asked_once_on_a_terminal_only(self):
        tty = SimpleNamespace(isatty=lambda: True)
        pipe = SimpleNamespace(isatty=lambda: False)
        self.assertTrue(telemetrycmd.should_ask(self.folder, 'usage', {}, tty, tty))
        self.assertFalse(telemetrycmd.should_ask(self.folder, 'usage', {}, pipe, tty))
        self.assertFalse(telemetrycmd.should_ask(self.folder, 'usage', {'CI': '1'}, tty, tty))
        self.assertFalse(telemetrycmd.should_ask(self.folder, 'run', {}, tty, tty))
        self.assertFalse(telemetrycmd.ask_once(self.folder, read=lambda _q: '', say=lambda _m: None))
        self.assertFalse(telemetrycmd.should_ask(self.folder, 'usage', {}, tty, tty))
        self.assertTrue(telemetrycmd.ask_once(self.folder, read=lambda _q: 'y', say=lambda _m: None))

    def test_telemetry_on_off_status_report(self):
        cfg_path = os.path.join(self.tmp.name, 'tutti.config.yaml')
        with open(cfg_path, 'w', encoding='utf8') as f:
            f.write(f'project: {{state: {self.state}}}\n')
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': self.env['XDG_CONFIG_HOME']}):
            cli.main(['--config', cfg_path, 'telemetry', 'on'])
            cli.main(['--config', cfg_path, 'telemetry'])
            cli.main(['--config', cfg_path, 'telemetry', 'report'])
            cli.main(['--config', cfg_path, 'telemetry', 'off'])
        text = out.getvalue()
        self.assertIn('sending: on', text)
        self.assertIn('tutti telemetry setup', text)
        self.assertIn('Most used', text)
        self.assertFalse(telemetry.consent(self.folder)['send'])


class Report(unittest.TestCase):
    def ev(self, kind, name, **kw):
        return {'v': 1, 'ts': '2026-11-14T10:00:00+00:00', 'kind': kind, 'name': name, **kw}

    def test_the_summary_ranks_use_speed_failures_waste_and_stalls(self):
        events = [self.ev('command', 'run', seconds=s, rc=0, outcome='ok') for s in (1, 2, 30)]
        events += [self.ev('command', 'ship', seconds=100, rc=1, outcome='failed')] * 2
        events += [self.ev('feature', 'autopilot.attempt', outcome='failed', account='agy', minutes=12),
                   self.ev('feature', 'wait.stall', cause='run'), self.ev('feature', 'autopilot.park', cause='no_progress'),
                   self.ev('feature', 'autopilot.escalate'), self.ev('crash', 'KeyError', frame='main'),
                   self.ev('dashboard', 'usage', control='refresh'), {'ts': '2020-01-01', 'kind': 'command', 'name': 'old'}]
        from datetime import datetime, timezone
        s = insights.summary(events, 7, now=datetime(2026, 11, 15, tzinfo=timezone.utc))
        self.assertEqual(s['most_used'][0], {'name': 'run', 'runs': 3})
        self.assertEqual(s['slowest'][0]['name'], 'ship')
        self.assertEqual(s['most_failing'][0], {'name': 'ship', 'failed': 2, 'runs': 2})
        self.assertEqual(s['wasted'][0], {'who': 'agy', 'runs': 1, 'minutes': 12.0})
        self.assertEqual((s['stalls'][0]['cause'], s['parked'][0]['cause'], s['escalations']), ('run', 'no_progress', 1))
        self.assertEqual(s['crashes'][0]['where'], 'KeyError in main')
        self.assertEqual(s['dashboard'][0]['name'], 'usage refresh')
        self.assertIn('Most failing:', insights.lines(s))


class Dashboard(Base):
    def collector(self):
        return SimpleNamespace(cfg=config.normalise({'project': {'state': self.state}}))

    def test_a_page_error_is_scrubbed_on_the_server(self):
        telemetry.set_consent(self.folder, True)
        body = json.dumps({'error': {'type': 'TypeError', 'message': f'x of owner@example.com at {HOME}/a',
                                     'stack': 'at render (http://127.0.0.1:4777/app.js:12:5)\nat x (https://evil.example/a.js:1:1)'}})
        self.assertEqual(dashtelemetry.save(self.collector(), body.encode())[0], 200)
        with open(os.path.join(self.folder, telemetry.OUTBOX), encoding='utf8') as f:
            text = f.read()
        self.assert_clean(text)
        ev = [json.loads(x)['event'] for x in text.splitlines() if json.loads(x)['type'] == 'event'][0]
        self.assertEqual(ev['exception']['values'][0]['stacktrace']['frames'][-1]['filename'], '/app.js')

    def test_panel_use_consent_and_bad_bodies(self):
        c = self.collector()
        self.assertEqual(dashtelemetry.save(c, b'{"use": {"panel": "usage", "control": "refresh"}}')[0], 200)
        self.assertEqual(dashtelemetry.save(c, b'{"use": {"panel": "../etc", "control": "x"}}')[0], 400)
        self.assertTrue(dashtelemetry.save(c, b'{"consent": true}')[1]['send'])
        self.assertEqual(dashtelemetry.save(c, b'not json')[0], 400)
        self.assertEqual(dashtelemetry.save_self_improve(c, b'{"enabled": "yes"}')[0], 400)
        self.assertEqual(dashtelemetry.save_self_improve(c, b'{"enabled": false}')[0], 200)
        v = dashtelemetry.view(c.cfg)
        self.assertFalse(v['self_improve']['enabled'])
        self.assertTrue(v['status']['send'])
        self.assertNotIn('folder', v['status'])


if __name__ == '__main__':
    unittest.main()
