"""Dependency bots and vulnerability reports as tasks (tutti/depbots.py): classification, grouping, dedupe and ranking,
the owner's messages, queueing and dropping. Fake gh calls only."""
import copy
import json
import tempfile
import unittest

from tutti import backlog, config, depbots

S = config.DEFAULTS['depbots']
PRS = [
    {'number': 1, 'title': 'Bump ruff from 0.17.2 to 0.17.4', 'headRefName': 'dependabot/pip/ruff-0.17.4',
     'author': {'login': 'app/dependabot'}, 'url': 'https://github.com/o/r/pull/1', 'statusCheckRollup': []},
    {'number': 2, 'title': 'Bump pillow from 12.3.0 to 13.0.0', 'headRefName': 'dependabot/pip/pillow-13.0.0',
     'author': {'login': 'app/dependabot'}, 'url': 'https://github.com/o/r/pull/2', 'statusCheckRollup': []},
    {'number': 3, 'title': 'Upgrade lodash to fix CVE-2026-1', 'headRefName': 'cursor/anybump-lodash',
     'author': {'login': 'cursor[bot]'}, 'url': 'https://github.com/o/r/pull/3', 'statusCheckRollup': [{'conclusion': 'FAILURE'}]},
    {'number': 4, 'title': 'Refactor the parser', 'headRefName': 'cursor/refactor', 'author': {'login': 'cursor[bot]'},
     'url': 'https://github.com/o/r/pull/4', 'statusCheckRollup': []},
    {'number': 5, 'title': 'Bump coverage from 7.16.1 to 7.16.2', 'headRefName': 'dependabot/pip/coverage',
     'author': {'login': 'app/dependabot'}, 'url': 'https://github.com/o/r/pull/5', 'statusCheckRollup': []},
    {'number': 6, 'title': 'feat: owner work', 'headRefName': 'feat/x', 'author': {'login': 'someone'}, 'url': 'u6',
     'statusCheckRollup': []},
]
DEP = [{'html_url': 'https://github.com/o/r/security/dependabot/1', 'dependency': {'package': {'ecosystem': 'npm', 'name': 'lodash'},
        'manifest_path': 'package-lock.json'},
        'security_advisory': {'severity': 'high', 'summary': 'Prototype pollution'},
        'security_vulnerability': {'first_patched_version': {'identifier': '4.17.22'}}}]
CODE = [{'html_url': 'https://github.com/o/r/security/code-scanning/7',
         'rule': {'id': 'py/sql-injection', 'security_severity_level': 'critical',
         'description': 'SQL injection'},
         'most_recent_instance': {'location': {'path': 'db.py', 'start_line': 9}}, 'tool': {'name': 'CodeQL'}}]
COMMENTS = [{'user': {'login': 'cursor[bot]'}, 'body': '**High: path traversal** in the upload handler',
             'path': 'up.py', 'html_url': 'c1', 'position': 3},
            {'user': {'login': 'greptile-apps[bot]'}, 'body': 'Consider renaming this variable', 'path': 'a.py',
             'html_url': 'c2', 'position': 1},
            {'user': {'login': 'greptile-apps[bot]'}, 'body': '**High: path traversal** in the upload handler',
             'path': 'up.py', 'html_url': 'c3', 'position': 3}]


def fake_gh(fail=None, prs=None, dep=None):
    def gh(args):
        if args[0] == 'pr':
            return 0, json.dumps(PRS if prs is None else prs), ''
        path = args[-1]
        for key, data in (('dependabot', DEP if dep is None else dep), ('code-scanning', CODE), ('/comments', COMMENTS)):
            if key in path:
                if fail and key in fail:
                    return 1, '', fail[key]
                if key == '/comments':   # one page per open pull request; the same finding on #1 only
                    return 0, json.dumps(COMMENTS if '/pulls/1/' in path else []), ''
                return 0, json.dumps([data]), ''   # --slurp: a list of pages
        return 1, '', 'unknown'
    return gh


class DepbotsTest(unittest.TestCase):
    def test_classify_bots_and_kinds(self):
        got = [depbots.classify_pr(p, S['bots']) for p in PRS]
        self.assertEqual(got, ['dependabot', 'dependabot', 'cursor', None, 'dependabot', None])
        self.assertEqual([depbots.bump_kind(p['title']) for p in PRS[:3]], ['patch', 'major', 'unknown'])
        self.assertEqual(depbots.bump_kind('Bump a from 1.2.0 to 1.3.0'), 'minor')
        self.assertEqual(depbots.slug_of('git@github.com:o/r.git'), 'o/r')
        self.assertEqual(depbots.slug_of('https://gitlab.com/o/r'), '')

    def test_read_dedupes_and_ranks(self):
        scan = depbots.read_repo('o/r', S, fake_gh())
        self.assertEqual([p['number'] for p in scan['prs']], [1, 2, 3, 5])
        self.assertTrue(scan['prs'][2]['failing'])
        self.assertEqual([a['severity'] for a in scan['alerts']], [4, 3, 3])   # worst first; greptile's code review left out
        traversal = [a for a in scan['alerts'] if a['key'].startswith('scan:')][0]
        self.assertEqual(traversal['sources'], ['cursor', 'greptile'])         # one finding reported twice is one task
        self.assertEqual(scan['problems'], [])

    def test_unreadable_alerts_name_the_fix(self):
        scan = depbots.read_repo('o/r', S, fake_gh({'dependabot': 'HTTP 403: Dependabot alerts are disabled',
                                                   'code-scanning': 'HTTP 404: no analysis found'}))
        self.assertEqual(len(scan['problems']), 1)
        self.assertIn('https://github.com/o/r/settings/security_analysis', scan['problems'][0])
        self.assertFalse(scan['code_scanning'])

    def test_tasks_group_trivial_and_drop_when_gone(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = copy.deepcopy(config.DEFAULTS)
            cfg['project']['state'] = d
            cfg['autopilot']['repos'] = {'r': {'path': d, 'check': 'make check', 'github': 'o/r'}}
            found = depbots.gather(cfg, 1000.0, fake_gh(), force=True)
            data = {'next': 1, 'tasks': []}
            events = depbots.apply(cfg, data, found)
            titles = [t['title'] for t in data['tasks']]
            self.assertIn('Land the open patch and minor dependency bumps', titles)
            self.assertIn('Dependency update: Bump pillow from 12.3.0 to 13.0.0 (#2)', titles)
            self.assertTrue(any(t.startswith('Fix critical vulnerability: SQL injection') for t in titles))
            self.assertEqual(len(events), len(titles))
            group = [t for t in data['tasks'] if t['source'] == 'depbot:group'][0]
            self.assertIn('git fetch origin dependabot/pip/ruff-0.17.4', group['notes'][0])
            self.assertEqual(depbots.apply(cfg, data, found), [])   # a second read queues nothing twice
            found['r']['prs'].append({**found['r']['prs'][0], 'number': 9, 'url': 'https://github.com/o/r/pull/9'})
            depbots.apply(cfg, data, found)   # a bump joins the group: the same task, its notes follow
            self.assertEqual(group['status'], 'queued')
            self.assertEqual(len(group['notes']), 3)
            found['r']['alerts'] = []
            depbots.apply(cfg, data, found)
            dropped = [t for t in data['tasks'] if t['status'] == 'dropped']
            self.assertEqual(len(dropped), 3)
            depbots.save(cfg, found, 1000.0)
            v = depbots.view(cfg)
            self.assertEqual((v['prs'], v['alerts'], v['worst']), (5, 0, None))
            self.assertEqual(backlog.key_of(data['tasks'][0])[0], 'r')

    def test_unread_repo_keeps_its_tasks_and_bad_config(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['autopilot']['repos'] = {'r': {'path': '/nowhere'}}
        data = {'next': 2, 'tasks': [{**backlog.new_task('r', 'Fix high vulnerability: x'), 'id': 't1', 'depbot_ref': 'k'}]}
        depbots.apply(cfg, data, {'r': {'slug': '', 'prs': [], 'alerts': [], 'problems': ['r: no GitHub repository']}})
        self.assertEqual(data['tasks'][0]['status'], 'queued')
        cfg['depbots'] = {'bots': [{'name': 'x'}], 'every_minutes': 1}
        self.assertEqual(len(depbots.errors(cfg)), 2)


class PartialReadTest(unittest.TestCase):
    def cfg(self, d):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['project']['state'] = d
        cfg['autopilot']['repos'] = {'r': {'path': d, 'github': 'o/r'}}
        return cfg

    def test_a_failed_alert_read_keeps_its_tasks(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, data = self.cfg(d), {'next': 1, 'tasks': []}
            depbots.apply(cfg, data, depbots.gather(cfg, 1.0, fake_gh(), force=True))
            dep = [t for t in data['tasks'] if t['depbot_ref'].startswith('dep:')][0]
            found = depbots.gather(cfg, 2.0, fake_gh({'dependabot': 'HTTP 403'}), force=True)
            self.assertNotIn('dependabot', found['r']['read'])
            depbots.apply(cfg, data, found)
            self.assertEqual(dep['status'], 'queued')   # unread is not fixed
            depbots.apply(cfg, data, depbots.gather(cfg, 3.0, fake_gh(dep=[]), force=True))
            self.assertEqual(dep['status'], 'dropped')  # read and gone: fixed

    def test_bad_output_and_wording(self):
        def broken(args):
            return (0, 'not json', '') if args[0] == 'pr' else (0, '[[]]', '')
        scan = depbots.read_repo('o/r', S, broken)
        self.assertNotIn('prs', scan['read'])
        self.assertIn('unreadable', scan['problems'][0])
        tidy = [{'user': {'login': 'cursor[bot]'}, 'body': 'A high-level note on the CVE-2026-2 handling', 'position': 1}]
        self.assertEqual(depbots.from_comments(tidy, S['scanners'])[0]['severity'], 2)   # "high-level" is not a label
        pr = {'title': 'Update the README', 'headRefName': 'cursor/update-readme', 'author': {'login': 'cursor[bot]'}}
        self.assertIsNone(depbots.classify_pr(pr, S['bots']))
        self.assertTrue(depbots.failing([{'conclusion': 'TIMED_OUT'}]))
        self.assertTrue(depbots.failing([{'state': 'ERROR'}]))
        self.assertFalse(depbots.failing([{'conclusion': 'SUCCESS'}]))


if __name__ == '__main__':
    unittest.main()
