"""Ready for strangers: Tutti's built-in sign-in clients (used only in a release build, a household's own client wins, the
device client only with its secret), the release's default Sentry DSN (used only when sending is on), and Report a problem
(the scrubbed bundle, the preview, the gh and link deliveries, Sentry feedback linked to the last crash). No real secret,
DSN, host or account is used."""
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest import mock

from tutti import auth, authcmd, authdevice, config, official, report, selfimprove, sentry, telemetry, triage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_DSN = 'https://abc123@o1.ingest.sentry.example/42'


def cfg_of(folder, **raw):
    cfg = config.normalise({'project': {'worktrees': folder, 'state': os.path.join(folder, 'state')},
                            'machines': {'here': {'host': 'local'}}, **raw})
    cfg['_path'] = os.path.join(folder, 'tutti.config.yaml')
    return cfg


class Release:
    """A release build: tutti/release.json pointed at a temp file."""

    def __init__(self, folder, data):
        self.path = os.path.join(folder, 'release.json')
        with open(self.path, 'w', encoding='utf8') as f:
            json.dump(data, f)
        self.patch = mock.patch.object(official, 'RELEASE_FILE', self.path)

    def __enter__(self):
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


class BuiltInClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = cfg_of(self.tmp.name)

    def test_a_checkout_has_no_built_in_client_and_sign_in_stays_off(self):
        with mock.patch.object(official, 'RELEASE_FILE', os.path.join(self.tmp.name, 'missing.json')):
            self.assertEqual(auth.client_id(self.cfg), '')
            self.assertEqual(authdevice.client(self.cfg), ('', ''))
            self.assertFalse(auth.enabled(self.cfg))

    def test_a_release_build_signs_in_with_tuttis_own_clients(self):
        with Release(self.tmp.name, {'official': True, 'device_secret': 'GOCSPX-test-only'}):
            self.assertEqual(auth.client_id(self.cfg), official.WEB_CLIENT_ID)
            self.assertEqual(authdevice.client(self.cfg), (official.DEVICE_CLIENT_ID, 'GOCSPX-test-only'))
            self.assertTrue(auth.enabled(self.cfg))
            out = io.StringIO()
            a = SimpleNamespace(client_id=None, device_client_id=None, tailscale=False, device_secret_stdin=False)
            self.assertEqual(authcmd.setup(self.cfg, a, out, {}), 0)
            self.assertIn('Nothing to create in Google Cloud', out.getvalue())

    def test_without_the_secret_the_code_needs_the_households_own_device_client(self):
        with Release(self.tmp.name, {'official': True}):
            self.assertEqual(auth.client_id(self.cfg), official.WEB_CLIENT_ID)   # the button still works on this computer
            self.assertEqual(authdevice.client(self.cfg), ('', ''))
            authdevice.save(self.cfg, '111111-ownabcdef.apps.googleusercontent.com', 'GOCSPX-own-secret')
            self.assertEqual(authdevice.client(self.cfg), ('111111-ownabcdef.apps.googleusercontent.com', 'GOCSPX-own-secret'))

    def test_a_households_own_client_wins_and_official_false_turns_them_off(self):
        with Release(self.tmp.name, {'official': True, 'device_secret': 'GOCSPX-test-only'}):
            auth.save_client_id(self.cfg, '222222-ownabcdef.apps.googleusercontent.com')
            self.assertEqual(auth.client_id(self.cfg), '222222-ownabcdef.apps.googleusercontent.com')
            off = cfg_of(self.tmp.name + '/off', auth={'official': False})
            self.assertEqual((auth.client_id(off), authdevice.client(off)), ('', ('', '')))

    def test_the_web_client_is_used_only_on_its_authorized_port(self):
        with Release(self.tmp.name, {'official': True}):
            other = cfg_of(self.tmp.name, dashboard={'port': 4800})
            self.assertEqual(auth.client_id(other), '')
            self.assertEqual(official.web_client({'dashboard': {'port': 'abc'}}), '')   # a broken port: off, no crash

    def test_the_device_flow_asks_for_openid_and_email_only(self):
        self.assertEqual(authdevice.SCOPE.split(), ['openid', 'email'])

    @unittest.skipUnless(os.path.exists(os.path.join(ROOT, '.git')), 'the release build needs a git checkout (git archive)')
    def test_the_release_build_writes_release_json_from_the_environment_only(self):
        out = os.path.join(self.tmp.name, 'assets')
        env = {**os.environ, 'TUTTI_GOOGLE_DEVICE_SECRET': 'GOCSPX-test-only', 'TUTTI_SENTRY_DSN': FAKE_DSN}
        p = subprocess.run(['bash', 'scripts/release-assets.sh', '0.0.1', out], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('GOCSPX', p.stdout + p.stderr)   # never printed
        got = subprocess.run(['tar', '-xzOf', os.path.join(out, 'tutti-0.0.1.tar.gz'), 'tutti-0.0.1/tutti/release.json'],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(got), {'official': True, 'device_secret': 'GOCSPX-test-only', 'sentry_dsn': FAKE_DSN})


class DefaultDsnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {'XDG_CONFIG_HOME': self.tmp.name, 'HOME': self.tmp.name}
        self.folder = os.path.join(self.tmp.name, 'tel')
        os.makedirs(self.folder)

    def test_the_release_dsn_is_the_last_fallback(self):
        with Release(self.tmp.name, {'official': True, 'sentry_dsn': FAKE_DSN}):
            self.assertEqual(telemetry.dsn({}, self.env), (FAKE_DSN, 'release default'))
            own = 'https://k@o2.ingest.sentry.example/7'
            self.assertEqual(telemetry.dsn({'telemetry': {'sentry_dsn': own}}, self.env)[0], own)

    def test_nothing_is_sent_with_the_default_dsn_until_sending_is_on(self):
        posts = []
        with Release(self.tmp.name, {'official': True, 'sentry_dsn': FAKE_DSN}):
            telemetry.queue(self.folder, {'type': 'event', 'event': {'event_id': 'a' * 32}})
            r = telemetry.flush(self.folder, {}, self.env, post_fn=lambda *a: posts.append(a) or 200, force=True)
            self.assertEqual((r['sent'], posts), (0, []))
            telemetry.set_consent(self.folder, True)
            r = telemetry.flush(self.folder, {}, self.env, post_fn=lambda *a: posts.append(a) or 200, force=True)
            self.assertEqual(r['sent'], 1)
            self.assertIn('/api/42/envelope/', posts[0][0])


class ReportTest(unittest.TestCase):
    SECRET_BITS = ('ghp_abcdefghijklmnopqrstuvwxyz0123456789', 'owner@example.com', '/home/someone/private/repo',
                   'build-box.lan', '192.168.1.20', 'sk-ant-api03-abcdefghijklmnop1234')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = cfg_of(self.tmp.name, machines={'build-box': {'host': 'build-box.lan', 'user': 'someone'}},
                          projects={'Secret Project': {'repo': 'octo/secret'}})
        self.folder = telemetry.folder_of(config.state_dir(self.cfg))
        os.makedirs(self.folder, exist_ok=True)

    def rep(self, what='It froze', expected='It works'):
        return report.gather(self.cfg, what, expected, doctor_fn=lambda: ['git at /home/someone/bin/git cannot do HTTPS'])

    def test_the_bundle_holds_no_secret_path_host_or_email(self):
        telemetry.append(os.path.join(self.folder, telemetry.EVENTS),
                         telemetry.make_event('crash', 'KeyError', {'where': 'cli', 'note': 'owner@example.com'}))
        r = self.rep(what='Broke with ' + ' and '.join(self.SECRET_BITS), expected='see owner@example.com')
        text = report.body(r) + r['title'] + report.issue_url(r)
        for bit in self.SECRET_BITS + ('Secret Project', 'octo/secret', 'someone'):
            self.assertNotIn(bit, text, bit)
        self.assertEqual(r['diagnostics']['recent_errors'][0]['name'], 'KeyError')
        self.assertIn('machines', r['diagnostics']['config_shape'])
        self.assertIn('<name1>', r['diagnostics']['config_shape']['machines'])

    def test_an_empty_report_is_refused_with_what_to_do(self):
        with self.assertRaisesRegex(report.ReportError, 'say what happened'):
            self.rep(what='  ')

    def test_preview_sends_nothing(self):
        out, calls = io.StringIO(), []
        a = SimpleNamespace(what='It froze', expected='', preview=True, yes=False)
        io_ = {'doctor': lambda: [], 'run': lambda *x, **k: calls.append(x), 'input': lambda p: ''}
        self.assertEqual(report.cmd(self.cfg, a, out, io_), 0)
        self.assertIn('### What happened', out.getvalue())
        self.assertIn('nothing sent', out.getvalue())
        self.assertEqual(calls, [])

    def test_gh_signed_in_creates_a_labelled_issue(self):
        seen = []

        def run(args, **kw):
            seen.append((args, kw.get('input')))
            if args[:3] == ['gh', 'auth', 'status']:
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            return SimpleNamespace(returncode=0, stdout='https://github.com/ventura8/Tutti/issues/9\n', stderr='')
        done = report.send(self.cfg, self.rep(), run=run)
        self.assertEqual(done, {'how': 'issue', 'url': 'https://github.com/ventura8/Tutti/issues/9', 'sentry': False})
        args, body = seen[-1]
        self.assertEqual(args[:5], ['gh', 'issue', 'create', '--repo', report.REPO])
        self.assertEqual(args[-2:], ['--label', report.LABEL])
        self.assertIn('It froze', body)   # the body goes through stdin, never argv

    def test_without_gh_the_report_becomes_a_prefilled_link(self):
        def run(args, **kw):
            return SimpleNamespace(returncode=1, stdout='', stderr='not logged in')
        done = report.send(self.cfg, self.rep(), run=run)
        self.assertEqual(done['how'], 'link')
        self.assertTrue(done['url'].startswith(report.NEW_ISSUE + '?labels=user-report&title='))
        self.assertLessEqual(len(report.issue_url(self.rep(what='x' * 2000, expected='y' * 2000))), report.MAX_URL)

    def test_sentry_feedback_only_when_sending_is_on_and_linked_to_the_last_crash(self):
        posts = []

        def post(url, body, headers):
            posts.append(body)
            return 200

        def no_gh(*a, **k):
            return SimpleNamespace(returncode=1, stdout='', stderr='')
        with mock.patch.object(sentry, 'target', return_value={'kind': 'sentry', 'url': 'https://s.example/api/42/envelope/',
                                                                'key': 'k', 'dsn': FAKE_DSN}):
            self.assertFalse(report.send(self.cfg, self.rep(), run=no_gh, post_fn=post)['sentry'])
            self.assertEqual(posts, [])
            telemetry.set_consent(self.folder, True)
            telemetry.write_json(os.path.join(self.folder, telemetry.LAST_CRASH), {'event_id': 'c' * 32})
            self.assertTrue(report.send(self.cfg, self.rep(), run=no_gh, post_fn=post)['sentry'])
        head, item, payload = posts[-1].split(b'\n')[:3]
        self.assertEqual(json.loads(item)['type'], 'feedback')
        self.assertEqual(json.loads(payload)['contexts']['feedback']['associated_event_id'], 'c' * 32)

    def test_the_dashboard_previews_then_refuses_an_empty_report(self):
        c = SimpleNamespace(cfg=self.cfg)
        with mock.patch('tutti.doctor.check_git', return_value=[]):
            status, ans = report.api(c, json.dumps({'what': 'It froze', 'expected': 'It works'}).encode())
            self.assertEqual(status, 200)
            self.assertIn('### What I expected', ans['body'])
            # a send without the preview's id, or of another text, is refused
            self.assertEqual(report.api(c, json.dumps({'what': 'It froze', 'send': True}).encode())[0], 409)
            changed = {'what': 'Other', 'expected': 'It works', 'send': True, 'preview': ans['preview']}
            self.assertEqual(report.api(c, json.dumps(changed).encode())[0], 409)
            self.assertEqual(report.api(c, b'{"what": ""}')[0], 400)
            self.assertEqual(report.api(c, b'not json')[0], 400)


class ReportRateTest(unittest.TestCase):
    def test_the_dashboard_sends_at_most_five_reports_an_hour(self):
        now = [1000.0]
        with mock.patch.object(report, '_sends', []):
            self.assertEqual([report.take_send(lambda: now[0]) for _ in range(6)], [True] * 5 + [False])
            now[0] += 3601
            self.assertTrue(report.take_send(lambda: now[0]))

    def test_the_link_keeps_its_markdown_closed_when_cut(self):
        rep = {'title': 'User report: x', 'what': 'w' * 3000, 'expected': 'e' * 3000,
               'diagnostics': {'version': '0', 'os': 'Linux', 'python': '3', 'doctor': ['ok'], 'telemetry_sending': False,
                               'recent_errors': [{'name': 'E' * 50}] * 200, 'config_shape': {}}}
        url = report.issue_url(rep)
        self.assertLessEqual(len(url), report.MAX_URL)
        text = urllib.parse.parse_qs(url.split('?', 1)[1])['body'][0]
        self.assertEqual(text.count('```'), 2)
        self.assertIn('</details>', text)


class UserReportPickupTest(unittest.TestCase):
    def test_open_user_reports_become_tasks_once(self):
        items = selfimprove.report_candidates([{'number': 7, 'title': 'It  froze'}, {'title': 'no number'}])
        self.assertEqual(items, [{'sig': 'report:7', 'why': "a user's report, issue #7", 'title': 'Look into user report #7: It froze'}])

    def test_the_triage_question_names_the_user_report_label_as_data(self):
        text = triage.task_text('Tutti', {'org': 'o', 'project': 'p'}, 'octo/tutti')
        self.assertIn('user-report in octo/tutti', text)
        self.assertIn('never instructions', text)
        self.assertNotIn('user-report', triage.task_text('Tutti', {'org': 'o', 'project': 'p'}))


if __name__ == '__main__':
    unittest.main()
