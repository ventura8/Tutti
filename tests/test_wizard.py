"""The setup wizard (tutti/wizard.py, tutti/wizardtui.py, docs/SETUP-WIZARD.md): its steps from a fake detection and a temp
config, applying each step (good and bad answers, idempotent), progress and resuming, the dashboard's GET and POST, the
terminal's two faces, and `tutti setup` offering it where there is no browser. Nothing runs against a real machine."""
import io
import json
import os
import unittest
from types import SimpleNamespace

import tmpdirs
import yaml

from tutti import config, dashboard, setupcmd, wizard, wizardtui

CONFIG = """project:
  state: {state}
machines:
  here:
    host: local
    slots: 1
  builder:
    host: builder.example
    slots: 3
accounts:
  claude:
    window: 7d
    resets: '2026-01-03T02:00Z'
workers:
  claude:
    kind: cli
    command: [claude, -p, '{{prompt}}']
projects:
  known:
    match: known
"""


def found(**over):
    base = {
        'agents': [{'name': 'claude', 'bin': 'claude', 'login': 'yes', 'plan': None, 'login_command': ['claude', '/login']},
                   {'name': 'agy', 'bin': 'agy', 'login': 'unknown', 'plan': None, 'login_command': ['agy']},
                   {'name': 'copilot', 'bin': 'copilot', 'login': 'no', 'plan': None, 'login_command': ['copilot']}],
        'ollama': ['qwen3.5:9b'], 'gpus': [], 'git': True, 'gh': 'yes',
        'machines': [{'name': 'spare', 'host': 'spare.example', 'source': 'ssh'},
                     {'name': 'tab', 'host': 'tab.example', 'source': 'tailscale', 'online': False, 'os': 'android'}],
        'devices': [{'serial': 'R58M00TEST01', 'state': 'unauthorized', 'model': 'Pixel'}],
        'repos': [{'name': 'known', 'path': '~/Projects/known', 'when': 2, 'priority': 0},
                  {'name': 'app', 'path': '~/Projects/app', 'when': 1, 'priority': 1}],
        'remote_repos': [{'name': 'lib', 'repo': 'someone/lib'}],
    }
    return {**base, **over}


class Ran:
    """A fake subprocess.run: records argv, answers 0."""

    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, argv, **_kw):
        self.calls.append(argv)
        return SimpleNamespace(returncode=self.code, stdout='', stderr='refused' if self.code else '')


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tmpdirs.mkdtemp()
        self.path = os.path.join(self.d, 'tutti.config.yaml')
        with open(self.path, 'w', encoding='utf8') as f:
            f.write(CONFIG.format(state=os.path.join(self.d, 'state')))
        self.found = found(home=self.d)

    def cfg(self):
        return config.load(self.path)

    def text(self):
        with open(self.path, encoding='utf8') as f:
            return f.read()

    def raw(self):
        with open(self.path, encoding='utf8') as f:
            return yaml.safe_load(f)

    def step(self, sid, admin=True):
        return next(s for s in wizard.view(self.cfg(), self.found, admin)['steps'] if s['id'] == sid)

    def apply(self, sid, data, **kw):
        return wizard.apply(self.cfg(), self.found, sid, data, **kw)


class View(Base):
    def test_a_fresh_install_is_a_first_run_on_its_welcome_step(self):
        v = wizard.view(self.cfg(), self.found)
        self.assertTrue(v['first_run'])
        self.assertEqual([s['id'] for s in v['steps']], [s[0] for s in wizard.STEPS])
        self.assertEqual(v['current'], 'welcome')

    def test_the_main_step_lists_config_and_ssh_machines_and_asks_ssh_for_the_rest(self):
        s = self.step('main')
        main = next(f for f in s['fields'] if f['key'] == 'main')
        self.assertEqual(main['value'], 'here')
        self.assertEqual([o['value'] for o in main['options']], ['here', 'builder', 'spare'])
        self.assertIn('3 slots', next(o['note'] for o in main['options'] if o['value'] == 'builder'))
        backup = next(f for f in s['fields'] if f['key'] == 'backup')
        self.assertEqual(backup['value'], 'auto')
        self.assertIn('ssh-copy-id tab.example', [r['command'] for r in s['rows']])

    def test_accounts_show_sign_in_states_with_the_tools_own_command(self):
        rows = {r['title']: r for r in self.step('accounts')['rows']}
        self.assertEqual(rows['Claude Code']['state'], 'ok')
        copilot = next(r for t, r in rows.items() if t.startswith('GitHub Copilot'))
        self.assertEqual((copilot['state'], copilot['command'], copilot['runnable']), ('todo', 'copilot', True))
        keys = [f['key'] for f in self.step('accounts')['fields']]
        self.assertEqual(keys, ['plan.claude', 'plan.agy', 'copilot', 'local'])

    def test_github_offers_new_local_repos_and_the_persons_github_repos(self):
        fields = {f['key']: f for f in self.step('github')['fields']}
        self.assertEqual(fields['projects']['value'], ['app'])   # known is managed already
        self.assertEqual([o['value'] for o in fields['clone']['options']], ['someone/lib'])

    def test_a_signed_out_gh_needs_the_person(self):
        self.found = found(home=self.d, gh='no', remote_repos=[])
        s = self.step('github')
        self.assertEqual(s['status'], 'needs-you')
        self.assertIn('gh auth login --web --git-protocol https', [r['command'] for r in s['rows']])

    def test_a_viewer_sees_household_steps_locked_without_their_details(self):
        v = wizard.view(self.cfg(), self.found, admin=False)
        main = next(s for s in v['steps'] if s['id'] == 'main')
        self.assertEqual((main['locked'], main['status'], main['rows'], main['fields']), (True, 'locked', [], []))
        self.assertFalse(v['first_run'])
        self.assertNotIn('builder', json.dumps(main))

    def test_integrations_are_placeholders_until_one_registers(self):
        rows = self.step('integrations')['rows']
        self.assertEqual([r['title'] for r in rows], ['Linear', 'Slack', 'Discord'])
        self.assertTrue(all(r['detail'].startswith('coming soon') for r in rows))
        got = []
        wizard.register('linear', 'Linear', lambda cfg: ('todo', 'not connected', 'tutti linear connect'),
                        lambda cfg, on: got.append(on) or 'Linear connected')
        self.addCleanup(wizard._PLUGINS.pop, 'linear', None)
        s = self.step('integrations')
        self.assertEqual(s['rows'][0]['command'], 'tutti linear connect')
        self.assertEqual(s['fields'][0]['key'], 'integration.linear')
        self.assertIn('Linear connected', self.apply('integrations', {'integration.linear': True}))
        self.assertEqual(got, [True])


class Apply(Base):
    def test_main_and_backup_are_written_once(self):
        msg = self.apply('main', {'main': 'here', 'backup': 'builder'})
        self.assertIn('backup main machine: builder', msg)
        self.assertEqual(self.raw()['failover'], {'main': 'here', 'backup': 'builder'})
        self.assertTrue(os.path.exists(self.path + '.bak'))
        self.assertEqual(self.apply('main', {'main': 'here', 'backup': 'builder'}), 'Main and backup machines: nothing to change')
        self.assertEqual(self.step('main')['status'], 'done')

    def test_a_discovered_ssh_host_picked_as_backup_is_added_first(self):
        self.apply('main', {'main': 'here', 'backup': 'spare'})
        raw = self.raw()
        self.assertEqual(raw['machines']['spare']['host'], 'spare.example')
        self.assertEqual(raw['failover']['backup'], 'spare')

    def test_the_failover_service_installs_only_when_asked(self):
        called = []
        self.apply('main', {'main': 'here', 'backup': 'builder'}, installer=lambda cfg, run: called.append(1) or 'installed')
        self.assertEqual(called, [])
        msg = self.apply('main', {'main': 'here', 'backup': 'builder', 'install': True},
                         installer=lambda cfg, run: called.append(1) or 'installed')
        self.assertEqual((called, msg.endswith('installed')), ([1], True))

    def test_bad_main_choices_are_refused_and_nothing_changes(self):
        before = self.text()
        for data, why in (({'main': 'here', 'backup': 'here'}, 'already the main'), ({'main': 'tab'}, 'ssh access first'),
                          ({'main': ''}, 'pick the machine'), ({'main': 'here', 'backup': 'nowhere'}, 'ssh access first')):
            with self.subTest(data=data), self.assertRaisesRegex(wizard.WizardError, why):
                self.apply('main', data)
        self.assertEqual(self.text(), before)

    def test_a_main_elsewhere_says_how_to_move_tutti_there(self):
        self.assertIn('tutti move builder', self.apply('main', {'main': 'builder', 'backup': 'none'}))

    def test_machine_shares_and_quiet_hours(self):
        self.apply('machines', {'machines.builder.use_pct': 60, 'machines.builder.quiet_hours': '22:00-07:00, 12:00-13:00',
                                'add': ['spare']})
        raw = self.raw()['machines']
        self.assertEqual((raw['builder']['use_pct'], raw['builder']['quiet_hours']), (60, ['22:00-07:00', '12:00-13:00']))
        self.assertIn('spare', raw)
        self.apply('machines', {'machines.builder.quiet_hours': ''})
        self.assertNotIn('quiet_hours', self.raw()['machines']['builder'])

    def test_bad_shares_and_hours_are_refused(self):
        for data in ({'machines.builder.use_pct': 150}, {'machines.builder.use_pct': True}, {'machines.here.quiet_hours': '25:00-07:00'}):
            with self.subTest(data=data), self.assertRaisesRegex(wizard.WizardError, 'machines\\.'):
                self.apply('machines', data)

    def test_accounts_add_plans_and_copilot_only_when_opted_in(self):
        self.apply('accounts', {'plan.agy': 'pro', 'copilot': False, 'local': True})
        raw = self.raw()
        self.assertEqual(raw['accounts']['agy']['subscription'], 'pro')
        self.assertNotIn('copilot', raw['workers'])
        self.assertEqual(raw['workers']['local']['model'], 'qwen3.5:9b')
        self.apply('accounts', {'plan.agy': 'ultra', 'copilot': True})
        raw = self.raw()
        self.assertEqual(raw['accounts']['agy']['subscription'], 'ultra')
        self.assertTrue(raw['workers']['copilot']['costs_money'])

    def test_a_plan_that_does_not_exist_is_refused(self):
        with self.assertRaisesRegex(wizard.WizardError, 'plan.agy: "gold"'):
            self.apply('accounts', {'plan.agy': 'gold'})

    def test_github_manages_picked_repos_and_clones_with_gh(self):
        run = Ran()
        msg = self.apply('github', {'projects': ['app'], 'clone': ['someone/lib']}, run=run)
        self.assertEqual(run.calls, [['gh', 'repo', 'clone', 'someone/lib', os.path.join(self.d, 'Projects', 'lib')]])
        self.assertIn('cloned someone/lib', msg)
        self.assertEqual(set(self.raw()['projects']), {'known', 'app', 'lib'})

    def test_a_clone_that_is_not_the_persons_or_fails_is_refused(self):
        with self.assertRaisesRegex(wizard.WizardError, 'not one of your GitHub repositories'):
            self.apply('github', {'clone': ['evil/repo; rm -rf ~']})
        with self.assertRaisesRegex(wizard.WizardError, 'gh repo clone someone/lib'):
            self.apply('github', {'clone': ['someone/lib']}, run=Ran(1))
        self.assertNotIn('lib', self.raw()['projects'])

    def test_a_folder_already_there_must_be_the_chosen_repository(self):
        os.makedirs(os.path.join(self.d, 'Projects', 'lib'))
        with self.assertRaisesRegex(wizard.WizardError, 'already exists and is not someone/lib'):
            self.apply('github', {'clone': ['someone/lib']}, run=lambda argv, **kw: SimpleNamespace(
                returncode=0, stdout='git@github.com:other/lib.git\n', stderr=''))
        self.assertNotIn('lib', self.raw()['projects'])
        msg = self.apply('github', {'clone': ['someone/lib']}, run=lambda argv, **kw: SimpleNamespace(
            returncode=0, stdout='https://github.com/someone/lib.git\n', stderr=''))
        self.assertIn('already at', msg)
        self.assertIn('lib', self.raw()['projects'])

    def test_a_malformed_field_is_refused_never_iterated(self):
        for sid, data, why in (('machines', {'add': 'spare'}, 'add: send a list'), ('github', {'projects': 'app'}, 'projects: send a list'),
                               ('github', {'clone': [1]}, 'clone: send a list'), ('main', {'main': ['here']}, 'main: send one name')):
            with self.subTest(data=data), self.assertRaisesRegex(wizard.WizardError, why):
                self.apply(sid, data)

    def test_a_failure_after_a_write_says_what_was_applied(self):
        def broken(cfg, run):
            raise wizard.WizardError('install: ssh refused')
        with self.assertRaisesRegex(wizard.WizardError, 'already applied: .*backup main machine: builder'):
            self.apply('main', {'main': 'here', 'backup': 'builder', 'install': True}, installer=broken)
        self.assertEqual(self.raw()['failover']['backup'], 'builder')
        self.assertNotEqual(self.step('main')['status'], 'done')

    def test_skip_review_and_resume(self):
        self.apply('welcome', {})
        self.assertIn('skipped', self.apply('devices', {'skip': True}))
        v = wizard.view(self.cfg(), self.found)
        self.assertFalse(v['first_run'])
        self.assertEqual(next(s for s in v['steps'] if s['id'] == 'devices')['status'], 'skipped')
        self.assertEqual(v['current'], 'main')   # resumes at the first step left
        with self.assertRaisesRegex(wizard.WizardError, 'cannot be skipped'):
            self.apply('welcome', {'skip': True})
        self.apply('review', {})
        self.assertTrue(wizard.view(self.cfg(), self.found)['finished'])

    def test_progress_holds_step_ids_and_times_only(self):
        self.apply('welcome', {})
        self.apply('accounts', {'plan.agy': 'pro'})
        with open(os.path.join(self.d, 'state', wizard.FILE), encoding='utf8') as f:
            self.assertEqual(set(json.load(f)), {'done', 'skipped', 'at'})

    def test_an_unknown_step_or_a_bad_body_is_refused(self):
        with self.assertRaisesRegex(wizard.WizardError, 'not one of'):
            self.apply('nope', {})
        with self.assertRaisesRegex(wizard.WizardError, 'JSON object'):
            self.apply('main', ['x'])


class Collector:
    def __init__(self, cfg, fnd, admin=True):
        self.cfg, self.wizard_detect, self.reloaded = cfg, lambda: fnd, 0
        self.authorize = lambda headers, need: None if admin else (403, 'viewer')

    def reload(self):
        self.reloaded += 1
        self.cfg = config.load(self.cfg['_path'])


class Api(Base):
    def test_get_and_post_for_an_admin(self):
        c = Collector(self.cfg(), self.found)
        code, body, ctype, _cache, _extra = dashboard.get(c, wizard.API, 'tok', {})
        self.assertEqual((code, ctype), (200, dashboard.JSON_TYPE))
        self.assertTrue(json.loads(body)['first_run'])
        code, answer = wizard.post(c, {}, json.dumps({'step': 'machines', 'data': {'machines.builder.use_pct': 40}}).encode())
        self.assertEqual(code, 200)
        self.assertIn('1 setting', answer['message'])
        self.assertEqual(c.reloaded, 1)
        self.assertIn(wizard.API, dashboard.SELF_POSTS)

    def test_a_viewer_and_bad_bodies_are_refused(self):
        self.assertEqual(wizard.post(Collector(self.cfg(), self.found, admin=False), {}, b'{"step": "welcome"}')[0], 403)
        c = Collector(self.cfg(), self.found)
        self.assertEqual(wizard.post(c, {}, b'not json')[0], 400)
        code, answer = wizard.post(c, {}, b'{"step": "machines", "data": {"machines.here.use_pct": -1}}')
        self.assertEqual(code, 409)
        self.assertIn('machines.here.use_pct', answer['error'])

    def test_with_sign_in_the_signed_in_role_decides(self):
        c = SimpleNamespace(cfg=self.cfg(), wizard_detect=lambda: self.found, signed=True)
        c.who = lambda h: {'sub': 'g', 'role': 'viewer'}
        self.assertFalse(wizard.get(c, {})[1]['admin'])
        self.assertEqual(wizard.post(c, {}, b'{"step": "welcome"}')[0], 403)
        c.who = lambda h: None   # nobody signed in: never the owner by default
        self.assertFalse(wizard.is_admin(c, {}))
        c.who = lambda h: {'sub': 'o', 'role': 'admin'}
        self.assertTrue(wizard.get(c, {})[1]['admin'])

    def test_detection_is_cached_and_detect_runs_it_again(self):
        calls = []
        c = SimpleNamespace(cfg=self.cfg(), wizard_detect=lambda: calls.append(1) or self.found, authorize=None)
        wizard.get(c, {})
        wizard.get(c, {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(wizard.post(c, {}, b'{"action": "detect"}')[0], 200)
        self.assertEqual(len(calls), 2)


def keys(form, *ks):
    out = None
    for k in ks:
        out = form.key(k)
    return out


class Tui(Base):
    def test_a_form_changes_a_choice_a_bool_and_a_number_by_keys(self):
        step = self.step('main')
        form = wizardtui.Form(step)
        keys(form, 'right')
        self.assertEqual(form.values['main'], 'builder')
        keys(form, 'down', 'down', 'space')
        self.assertTrue(form.values['install'])
        self.assertEqual(keys(form, 'down', 'enter'), 'apply')
        self.assertEqual(keys(form, 'down', 'enter'), 'skip')
        self.assertTrue(any(on and text.startswith('< Skip') for text, on in form.lines()))
        m = wizardtui.Form(self.step('machines'))
        keys(m, 'down', 'down', 'enter', 'backspace', 'backspace', 'backspace', '4', '5', 'enter')
        self.assertEqual(m.values['machines.builder.use_pct'], 45)

    def test_a_locked_step_offers_only_back(self):
        form = wizardtui.Form(self.step('main', admin=False))
        self.assertEqual(form.controls(), [('action', 'back')])
        self.assertIn('Only an admin', '\n'.join(t for t, _ in form.lines()))

    def test_the_plain_face_applies_a_step_and_reports_a_bad_answer(self):
        answers = iter(['1', '', '3', '', '25:00-01:00', '', '', '', '', 'a', 'q'])
        out = io.StringIO()
        session = wizardtui.Session(self.path, detect_fn=lambda: self.found, run=Ran())
        self.assertEqual(wizardtui.plain(session, read=lambda _q: next(answers), out=out), 0)
        text = out.getvalue()
        self.assertIn('Welcome: done', text.replace('Welcome: Start', ''))
        self.assertIn('Not applied: machines.here.quiet_hours', text)

    def test_the_plain_face_runs_a_sign_in_only_when_asked(self):
        run = Ran()
        session = wizardtui.Session(self.path, detect_fn=lambda: self.found, run=run)
        answers = iter(['4', 'y', '', '', '', '', 'b', 'q'])
        wizardtui.plain(session, read=lambda _q: next(answers), out=io.StringIO())
        self.assertEqual(run.calls, [['copilot']])

    def test_has_browser(self):
        self.assertTrue(wizardtui.has_browser({}, 'win32'))
        self.assertTrue(wizardtui.has_browser({'DISPLAY': ':0'}, 'linux'))
        self.assertFalse(wizardtui.has_browser({'SSH_CONNECTION': 'x'}, 'linux'))

    def test_setup_offers_the_wizard_only_without_a_browser(self):
        out = io.StringIO()
        self.assertTrue(setupcmd.offer_tui(out, read=lambda _q: '', browser=False))
        self.assertFalse(setupcmd.offer_tui(out, read=lambda _q: 'n', browser=False))
        self.assertFalse(setupcmd.offer_tui(out, read=lambda _q: self.fail('asked'), browser=True))


if __name__ == '__main__':
    unittest.main()
