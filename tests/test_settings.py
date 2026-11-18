"""The Settings tab: the schema covers every key, a save keeps the file's comments, a bad value is refused, secrets never
reach the page, the dashboard's switches write the same keys, project priority shares the work out, and the tab renders."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import yaml

from tutti import autopilot, config, dashboard, priority, settings, yamledit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = 'https://k3y-n0t-for-the-page@sentry.example/42'
TEXT = """# my config: keep this comment
project:
  state: STATE        # logs here

workers:
  cursor: {kind: cli, command: [cursor-agent, -p, '{prompt}']}   # the cheap one
  copilot: {kind: cli, command: [copilot, -p, '{prompt}'], costs_money: true}

autopilot:
  enabled: false      # off until I say so
  max_parallel: 1
  quiet_hours: []

projects:
  app: {match: app, priority: 1}
  tool:
    match: tool       # the tool itself
    priority: 0

telemetry:
  sentry_dsn: SECRET
"""


def comments(text):
    return [line.split('#', 1)[1] for line in text.splitlines() if '#' in line and "'#" not in line]


class Collector:
    def __init__(self, path):
        self.cfg, self.env = config.load(path), {}

    def reload(self):
        self.cfg = config.load(self.cfg['_path'])
        return self.cfg


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, 'state')
        self.path = os.path.join(self.tmp.name, 'tutti.config.yaml')
        with open(self.path, 'w', encoding='utf8') as f:
            f.write(TEXT.replace('STATE', self.state).replace('SECRET', SECRET))
        self.c = Collector(self.path)

    def file_text(self):
        with open(self.path, encoding='utf8') as f:
            return f.read()

    def post_setting(self, body):
        return settings.save(self.c, json.dumps(body).encode())


def setting_keys(data, prefix=''):
    for k, v in data.items():
        key = settings.generic(f'{prefix}{k}')
        if isinstance(v, dict) and v and (not settings.covered(key) or key in settings.ENTRIES):
            yield from setting_keys(v, f'{prefix}{k}.')   # a mapping: its keys are the settings
        else:
            yield key


class SchemaTest(unittest.TestCase):
    def test_every_key_of_the_example_config_has_a_schema_line(self):
        with open(config.EXAMPLE, encoding='utf8') as f:
            example = yaml.safe_load(f)
        missing = sorted({k for k in setting_keys(example) if not settings.covered(k) and k not in settings.ENTRIES
                          and not any(k.startswith(e + '.') and k.count('.') == e.count('.') + 1 for e in settings.ENTRIES)})
        self.assertEqual(missing, [])

    def test_every_default_has_a_label_and_a_section(self):
        claimed = {p.split('.')[0] for _, _, _, parts in settings.SECTIONS for p in parts}
        people = {m for m in settings.MANAGED if '.' not in m}   # on the Manage tab (tutti/manage.py), never here
        self.assertEqual(sorted(set(config.DEFAULTS) - claimed - people - {'routing'}), [])
        for key in settings.DEFAULTS_FLAT:
            self.assertTrue(settings.known(key)['label'], key)

    def test_an_undocumented_key_shows_as_yaml_under_other(self):
        cfg = config.normalise({})
        cfg['_path'] = ''
        rows = settings.View(cfg, {'brand_new': {'a': 1}}).payload()['sections'][-1]['rows']
        self.assertEqual([(r['key'], r['control'], r['yaml']) for r in rows if r['key'] == 'brand_new'], [('brand_new', 'yaml', 'a: 1\n')])


class EditTest(unittest.TestCase):
    def test_a_change_touches_only_its_value_and_keeps_every_comment(self):
        cases = [(['autopilot', 'max_parallel'], 3), (['workers', 'cursor', 'kind'], 'ollama'),
                 (['autopilot', 'quiet_hours'], ['22:00-06:00']),
                 (['projects', 'tool', 'priority'], 4), (['autopilot', 'fill_room'], False), (['workers', 'copilot', 'enabled'], True),
                 (['review', 'timeout_minutes'], 5), (['autopilot', 'enabled'], yamledit.DELETE)]
        for path, value in cases:
            new = yamledit.edit(TEXT, path, value)
            gone = [' off until I say so'] if value is yamledit.DELETE else []   # a removed key takes its own line's comment
            self.assertEqual(comments(new), [c for c in comments(TEXT) if c not in gone], path)
            want = yamledit.changed(yaml.safe_load(TEXT), path, value)
            self.assertEqual(yaml.safe_load(new), want, path)
        self.assertIn('  max_parallel: 3\n', yamledit.edit(TEXT, ['autopilot', 'max_parallel'], 3))
        self.assertIn("{kind: cli, command: [copilot, -p, '{prompt}'], costs_money: true, enabled: true}",
                      yamledit.edit(TEXT, ['workers', 'copilot', 'enabled'], True))

    def test_the_example_config_takes_a_change_to_every_key(self):
        with open(config.EXAMPLE, encoding='utf8') as f:
            text = f.read()
        data = yaml.safe_load(text)
        for top, v in data.items():
            for k in (v or {}) if isinstance(v, dict) else []:
                yamledit.edit(text, [top, k], yamledit.DELETE)
                yamledit.edit(text, [top, k], 'x')

    def test_what_it_cannot_do_safely_is_refused(self):
        with self.assertRaises(yamledit.EditError):
            yamledit.edit('a: 1\n', ['a', 'b'], 2)   # a scalar cannot hold a key
        with self.assertRaises(yamledit.EditError):
            yamledit.edit('a: &x {b: 1}\nc: *x\n', ['a', 'b'], 2)   # the alias would change too


class SaveTest(Base):
    def test_a_save_keeps_comments_backs_up_and_applies_now(self):
        before = self.file_text()
        code, answer = self.post_setting({'path': ['autopilot', 'max_parallel'], 'value': 2})
        self.assertEqual((code, answer['applies']), (200, 'now'))
        self.assertEqual(self.c.cfg['autopilot']['max_parallel'], 2)
        self.assertEqual(comments(self.file_text()), comments(before))
        with open(answer['backup'], encoding='utf8') as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(self.post_setting({'path': ['dashboard', 'port'], 'value': 4800})[1]['applies'], 'on restart')
        self.assertEqual(self.post_setting({'path': ['autopilot', 'max_parallel'], 'reset': True})[0], 200)
        self.assertNotIn('max_parallel', self.file_text())

    def test_an_invalid_value_is_refused_and_the_file_is_untouched(self):
        before = self.file_text()
        for body, code in (({'path': ['autopilot', 'max_parallel'], 'value': 0}, 400),
                           ({'path': ['autopilot', 'max_parallel'], 'value': 'two'}, 400),
                           ({'path': ['autopilot', 'enabled'], 'value': 'yes'}, 400),
                           ({'path': ['routing', 'order'], 'value': ['ghost']}, 400),   # the config's own rules
                           ({'path': ['autopilot', 'quiet_hours'], 'value': ['soon']}, 400),
                           ({'path': ['autopilot', 'max_parallel'], 'value': 2, 'etag': 'stale'}, 409),
                           ({'path': 'autopilot.max_parallel', 'value': 2}, 400)):
            got, answer = self.post_setting(body)
            self.assertEqual(got, code, body)
            self.assertTrue(answer['error'])
        self.assertEqual(self.file_text(), before)

    def test_secrets_are_never_sent_nor_changed(self):
        env = {'TUTTI_GROK_KEY': 'grok-k3y-n0t-for-the-page'}
        page = settings.view(self.c.cfg, env)
        body = json.dumps(page)
        self.assertNotIn(SECRET, body)
        self.assertNotIn('k3y-n0t', body)
        row = next(r for s in page['sections'] for r in s['rows'] if r['key'] == 'telemetry.sentry_dsn')
        self.assertEqual((row['control'], row['set'], row['setup']), ('secret', True, 'tutti telemetry setup'))
        self.assertNotIn('value', row)
        self.assertEqual(self.post_setting({'path': ['telemetry', 'sentry_dsn'], 'value': 'x'})[0], 403)
        self.assertEqual(self.post_setting({'path': ['runner', 'env'], 'yaml': 'API_TOKEN: x'})[0], 403)
        status, raw, *_ = dashboard.get(self.c, '/api/settings', 'tok', {})
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET.encode(), raw)

    def test_the_dashboard_switches_write_the_same_keys(self):
        autopilot.set_enabled(self.c.cfg, True)   # an older switch in the state folder wins until saved here
        row = next(r for s in settings.view(self.c.cfg)['sections'] for r in s['rows'] if r['key'] == 'autopilot.enabled')
        self.assertTrue(row['override']['value'])
        self.assertEqual(dashboard.save_autopilot(self.c, b'{"enabled": false, "fill_room": false}')[0], 200)
        self.assertFalse(yaml.safe_load(self.file_text())['autopilot']['enabled'])
        self.assertNotIn('enabled', autopilot.read_state(self.c.cfg))
        self.assertEqual(dashboard.save_control(self.c, b'{"action": "optin", "kind": "worker", "name": "copilot", "on": true}')[0], 200)
        self.assertIn('copilot', self.c.cfg['workers'])
        self.assertEqual(dashboard.save_autocompact(self.c, b'{"threshold_pct": 45}')[0], 200)
        self.assertEqual(yaml.safe_load(self.file_text())['autocompact'], {'threshold_pct': 45})

    def test_priority_order_numbers_the_projects(self):
        row = next(r for s in settings.view(self.c.cfg)['sections'] for r in s['rows'] if r['control'] == 'order')
        self.assertEqual(row['value'], ['tool', 'app'])
        self.assertEqual(self.post_setting({'path': ['projects'], 'order': ['app', 'tool']})[0], 200)
        self.assertEqual((config.project_priority(self.c.cfg, 'app'), config.project_priority(self.c.cfg, 'tool')), (0, 1))
        self.assertEqual(self.post_setting({'path': ['projects'], 'order': ['app']})[0], 400)
        self.assertIn('# the tool itself', self.file_text())

    def test_settings_sit_behind_the_one_auth_check(self):
        with mock.patch.object(dashboard, 'authorize', return_value=(403, 'admins only')):
            self.assertEqual(dashboard.get(self.c, '/api/settings', 'tok', {})[0], 403)
            headers = {'Host': '127.0.0.1:4777', 'Origin': 'http://127.0.0.1:4777', 'Content-Type': 'application/json',
                       'X-Tutti-Token': 'tok', 'Content-Length': '2'}
            self.assertEqual(dashboard.post(self.c, headers, '/api/restart', 4777, 'tok', lambda n: b'{}')[0], 403)

    def test_restart_runs_the_same_command_again(self):
        ran = []
        timer = mock.Mock(side_effect=lambda d, fn: SimpleNamespace(start=fn))
        self.c.argv = ['/usr/bin/python3', 'tutti', 'dashboard']
        self.assertEqual(dashboard.restart_now(self.c, b'{}', execv=lambda *a: ran.append(a), timer=timer)[0], 200)
        self.assertEqual(ran, [('/usr/bin/python3', ['/usr/bin/python3', 'tutti', 'dashboard'])])
        self.assertEqual(dashboard.without_port(['py', 'tutti', 'dashboard', '--port', '4777', '--port=1', '--no-probe']),
                         ['py', 'tutti', 'dashboard', '--no-probe'])   # the saved dashboard.port applies after a restart
        self.assertIn(b'"started"', dashboard.route(self.c, '/api/health')[1])


class PriorityTest(unittest.TestCase):
    CFG = {'projects': {'tutti': {'priority': 0}, 'radio': {'priority': 1}, 'map': {'priority': 2}, 'loose': {}}}

    def test_the_helper_and_its_bad_values(self):
        self.assertEqual([config.project_priority(self.CFG, n) for n in ('tutti', 'map', 'loose', 'ghost')],
                         [0, 2, config.UNRANKED_PRIORITY, config.UNRANKED_PRIORITY])
        errs = config.validate(config.normalise({'projects': {'a': {'priority': -1}, 'b': {'priority': 'high'}}}))
        self.assertEqual(sum('priority' in e for e in errs), 2)

    def test_weighted_fair_share_serves_the_important_first_and_starves_no_one(self):
        items = [(p, i) for p in ('map', 'radio', 'tutti') for i in range(20)]
        order = [p for p, _ in priority.fair_order(self.CFG, items, lambda it: it[0])]
        self.assertEqual(order[0], 'tutti')
        first = order[:11]
        self.assertEqual((first.count('tutti'), first.count('radio'), first.count('map')), (6, 3, 2))
        self.assertIn('map', order[:6])   # the least important still gets an early turn
        busy = priority.fair_order(self.CFG, items, lambda it: it[0], served={'tutti': 5})
        self.assertEqual(busy[0][0], 'radio')   # what already runs counts as served

    def test_the_autopilot_and_fill_room_follow_it(self):
        cfg = {**self.CFG, 'autopilot': {'repos': {'r-map': {'path': '/src/map'}, 'r-tutti': {'path': '/src/tutti'}}}}
        cfg['projects'] = {'tutti': {'priority': 0, 'match': 'tutti'}, 'map': {'priority': 2, 'match': 'map'}}
        tasks = [{'repo': 'r-map', 'id': 1}, {'repo': 'r-map', 'id': 2}, {'repo': 'r-tutti', 'id': 3}]
        self.assertEqual([t['id'] for t in priority.by_repo(cfg, tasks, lambda t: t['repo'])], [3, 1, 2])
        from tutti import fill
        found = [('r-map', 'a', '', '', ''), ('r-map', 'b', '', '', ''), ('r-tutti', 'c', '', '', '')]
        self.assertEqual([x[1] for x in fill.pick({'tasks': []}, found, 2, cfg)], ['c', 'a'])
        self.assertEqual([x[1] for x in fill.pick({'tasks': []}, found, 2)], ['a', 'c'])   # no priorities: round-robin

    def test_task_priority_precedes_project_share_and_explicit_value_wins(self):
        cfg = self.CFG
        tasks = [{'id': 1, 'repo': 'one', 'title': 'P1: later', 'priority': 1},
                 {'id': 2, 'repo': 'two', 'title': 'P0: first', 'priority': 0},
                 {'id': 3, 'repo': 'one', 'title': 'P0: relabelled', 'priority': 2}]
        self.assertEqual([t['id'] for t in priority.tasks_by_priority(cfg, tasks)], [2, 1, 3])
        self.assertEqual(priority.task_level({'title': 'P0: not triaged'}), 5)
        self.assertEqual(priority.task_level({'title': 'unlabelled'}), 5)
        tasks[0]['urgent'] = True
        self.assertEqual([t['id'] for t in priority.tasks_by_priority(cfg, tasks)], [1, 2, 3])

    def test_projects_remain_fair_within_the_same_task_priority(self):
        cfg = {'projects': {'a': {'priority': 0, 'match': 'a'}, 'b': {'priority': 2, 'match': 'b'}},
               'autopilot': {'repos': {'r-a': {'path': '/src/a'}, 'r-b': {'path': '/src/b'}}}}
        tasks = [{'id': f'{repo}{i}', 'repo': repo, 'priority': 0} for repo in ('r-b', 'r-a') for i in range(10)]
        ordered = priority.tasks_by_priority(cfg, tasks)
        self.assertEqual(ordered[0]['repo'], 'r-a')
        self.assertIn('r-b', [t['repo'] for t in ordered[:5]])


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class RenderTest(Base):
    FAKE_DOM = """
class Node { constructor(tag) { this.tag = tag; this.kids = []; this.attrs = {}; this.textContent = ''; this.className = ''; }
  append(...k) { this.kids.push(...k.map((x) => (typeof x === 'string' ? { text: x } : x))); }
  setAttribute(k, v) { this.attrs[k] = v; } addEventListener() {} }
global.document = { createElement: (t) => new Node(t), querySelector: () => null };
const text = (n) => (n.text !== undefined ? n.text : `${n.textContent} ${(n.kids || []).map(text).join(' ')}`);
const tags = (n) => [n.tag, ...(n.kids || []).flatMap(tags)];
"""

    def test_the_tab_renders_every_section_its_controls_and_no_secret(self):
        data = settings.view(self.c.cfg, {})
        script = self.FAKE_DOM + f"""
const s = require({json.dumps(os.path.join(ROOT, 'tutti', 'web', 'settings.js'))});
const nodes = s.renderSettings({json.dumps(data)});
console.log(JSON.stringify({{ text: nodes.map(text).join(' '), tags: nodes.flatMap(tags) }}));
s.SET.changed = true;
console.log(JSON.stringify({{ text: s.renderSettings({json.dumps(data)}).map(text).join(' ') }}));
"""
        # through stdin: one argv string may not pass 128 KB (MAX_ARG_STRLEN), and the settings' data grows
        out = subprocess.run(['node', '-'], input=script, capture_output=True, text=True, check=True).stdout.splitlines()
        page, changed = json.loads(out[0]), json.loads(out[1])
        for title in ('Accounts and readings', 'Autopilot', 'Opt-ins', 'Dashboard', 'Secrets', 'Project priority',
                      'Runs at once'):
            self.assertIn(title, page['text'])
        self.assertIn('Reset to default', page['text'])
        self.assertNotIn(SECRET, page['text'])
        self.assertLessEqual({'input', 'select', 'details', 'textarea', 'ol'}, set(page['tags']))
        self.assertIn('Runs at once', page['text'])
        self.assertNotIn('Machine order', changed['text'])   # the filter shows only what differs from the defaults

    def test_the_page_has_the_tab_and_the_worker_caches_it(self):
        with open(os.path.join(dashboard.WEB, 'index.html'), encoding='utf8') as f:
            page = f.read()
        self.assertIn('href="#settings" data-view="settings"', page)
        self.assertIn('id="settings-view"', page)
        self.assertIn('settings.js?v=', page)
        with open(os.path.join(dashboard.WEB, 'sw.js'), encoding='utf8') as f:
            self.assertIn('`settings.js${V}`', f.read())


if __name__ == '__main__':
    unittest.main()


class ManagedKeysTest(unittest.TestCase):
    """Users, the household, a user's schedule and main agent are on the Manage tab only: nothing of it in Settings."""

    def test_settings_shows_no_people_keys(self):
        cfg = config.normalise({})
        cfg['_path'] = '/tmp/none.yaml'
        payload = settings.view(cfg, env={})
        keys = [r.get('key', '') for s in payload['sections'] for r in s['rows']]
        keys += [r.get('key', '') for s in payload['sections'] for g in s.get('groups', []) for e in g.get('entries', [])
                 for r in e.get('rows', [])]
        self.assertEqual([k for k in keys if settings.managed(k)], [])
        self.assertNotIn('Household membership', str(payload['secrets']))
        self.assertIn('main.fallback', keys)   # the system part of the main agent's section stays
