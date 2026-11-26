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

    def test_report_counts_for_each_repository(self):
        scans = {name: {'slug': f'o/{name}', **depbots.read_repo(f'o/{name}', S, fake_gh())} for name in ('one', 'two')}
        result = depbots.summary(scans, 1)
        self.assertEqual([len(repo['prs']) for repo in result['repos']], [4, 4])
        self.assertEqual([repo['alerts'] for repo in result['repos']], [3, 3])
        self.assertEqual([sum(a['kind'] == 'dependabot' for a in scan['alerts']) for scan in scans.values()], [1, 1])

    def test_real_gh_pr_list_json_is_parsed(self):
        # Shape captured from `gh pr list --json number,title,headRefName,author,url,labels,statusCheckRollup`
        # (names and urls rewritten to example placeholders; CheckRun fields kept as gh emits them).
        raw = json.dumps([
            {'author': {'id': 'BOT_ID', 'is_bot': True, 'login': 'app/dependabot', 'name': 'dependabot'},
             'headRefName': 'dependabot/pip/ruff-0.17.4',
             'labels': [{'id': 'LA_1', 'name': 'dependencies', 'description': 'Pull requests that update a dependency file',
                         'color': '0366d6'}],
             'number': 41,
             'statusCheckRollup': [
                 {'__typename': 'CheckRun', 'completedAt': '2026-11-17T12:44:51Z', 'conclusion': 'SUCCESS',
                  'detailsUrl': 'https://github.com/o/r/actions/runs/1/job/1', 'name': 'test',
                  'startedAt': '2026-11-17T12:42:30Z', 'status': 'COMPLETED', 'workflowName': 'CI'}],
             'title': 'Bump ruff from 0.17.2 to 0.17.4', 'url': 'https://github.com/o/r/pull/41'},
            {'author': {'id': 'BOT_ID2', 'is_bot': True, 'login': 'cursor[bot]', 'name': 'cursor'},
             'headRefName': 'cursor/anybump-lodash', 'labels': [], 'number': 42,
             'statusCheckRollup': [
                 {'__typename': 'CheckRun', 'completedAt': '2026-11-17T12:45:04Z', 'conclusion': 'FAILURE',
                  'detailsUrl': 'https://github.com/o/r/actions/runs/2/job/2', 'name': 'lint',
                  'startedAt': '2026-11-17T12:42:29Z', 'status': 'COMPLETED', 'workflowName': 'CI'}],
             'title': 'Upgrade lodash to fix CVE-2026-1', 'url': 'https://github.com/o/r/pull/42'},
            {'author': {'id': 'USER_ID', 'is_bot': False, 'login': 'someone', 'name': 'Someone'},
             'headRefName': 'feat/docs', 'labels': [], 'number': 43, 'statusCheckRollup': [],
             'title': 'docs: clarify the security card', 'url': 'https://github.com/o/r/pull/43'},
        ])

        def gh_fn(args):
            if args[0] == 'pr':
                self.assertEqual(args[1:6], ['list', '-R', 'o/r', '--state', 'open'])
                return 0, raw, ''
            return fake_gh(dep=[])(args)

        scan = depbots.read_repo('o/r', S, gh_fn)
        self.assertEqual([p['number'] for p in scan['prs']], [41, 42])
        self.assertEqual([p['bot'] for p in scan['prs']], ['dependabot', 'cursor'])
        self.assertTrue(scan['prs'][1]['failing'])
        self.assertIn('prs', scan['read'])
        self.assertEqual(scan['problems'], [])
        self.assertEqual(sum(a['kind'] == 'dependabot' for a in scan['alerts']), 0)

    def test_non_list_pr_response_is_not_an_auth_error(self):
        def bad_pr(args):
            return (0, '{"message":"bad response"}', '') if args[0] == 'pr' else fake_gh()(args)

        scan = depbots.read_repo('o/r', S, bad_pr)
        self.assertNotIn('prs', scan['read'])
        self.assertIn('expected a list', scan['problems'][0])
        self.assertNotIn('gh auth', scan['problems'][0])
        parse_err = depbots.problem('pull requests', 'o/r', "invalid literal for int() with base 10: 'open'")
        self.assertIn('invalid literal', parse_err)
        self.assertNotIn('gh auth', parse_err)

    def test_unreadable_alerts_name_the_fix(self):
        scan = depbots.read_repo('o/r', S, fake_gh({'dependabot': 'HTTP 403: Dependabot alerts are disabled',
                                                   'code-scanning': 'HTTP 404: no analysis found'}))
        self.assertEqual(len(scan['problems']), 1)
        self.assertIn('https://github.com/o/r/settings/security_analysis', scan['problems'][0])
        self.assertFalse(scan['code_scanning'])
        self.assertIn('code-scanning', scan['read'])
        self.assertEqual(len(scan['alerts']), 1)  # the review finding; no code-scanning reports
        missing = depbots.read_repo('o/r', S, fake_gh({'code-scanning': 'HTTP 404: Not Found'}))
        self.assertNotIn('code-scanning', missing['read'])
        self.assertTrue(missing['problems'])

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
            found['r']['started'] = data['tasks'][-1]['added'] - 1   # an older read never drops a newer task
            depbots.apply(cfg, data, found)
            self.assertEqual([t for t in data['tasks'] if t['status'] == 'dropped'], [])
            found['r']['started'] = data['tasks'][-1]['added'] + 1
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
        self.assertNotIn('gh auth', scan['problems'][0])
        tidy = [{'user': {'login': 'cursor[bot]'}, 'body': 'A high-level note on the CVE-2026-2 handling', 'position': 1}]
        self.assertEqual(depbots.from_comments(tidy, S['scanners'])[0]['severity'], 2)   # "high-level" is not a label
        pr = {'title': 'Update the README', 'headRefName': 'cursor/update-readme', 'author': {'login': 'cursor[bot]'}}
        self.assertIsNone(depbots.classify_pr(pr, S['bots']))
        self.assertTrue(depbots.failing([{'conclusion': 'TIMED_OUT'}]))
        self.assertTrue(depbots.failing([{'state': 'ERROR'}]))
        self.assertFalse(depbots.failing([{'conclusion': 'SUCCESS'}]))
        note = depbots.pr_note({'bot': 'dependabot', 'url': 'u', 'branch': 'dependabot/x;id;#'}, '')
        self.assertIn("'dependabot/x;id;#'", note)   # quoted: never shell syntax

    def test_incomplete_reads_prove_nothing(self):
        many = [{**PRS[0], 'number': n} for n in range(depbots.PR_LIMIT)]
        self.assertNotIn('prs', depbots.read_repo('o/r', S, fake_gh(prs=many))['read'])
        self.assertNotIn('comments', depbots.read_repo('o/r', S, fake_gh(prs=many[:31]))['read'])
        def no_list(args):
            return (1, '', 'HTTP 502') if args[0] == 'pr' else (0, '[[]]', '')
        self.assertEqual(depbots.read_repo('o/r', S, no_list)['read'], ['dependabot', 'code-scanning'])

    def test_refs_are_per_repo_and_reports_rank_first(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['autopilot']['repos'] = {'a': {'path': '/a'}, 'b': {'path': '/b'}}
        alert = {'key': 'dep:npm:x', 'source': 'github-dependabot', 'kind': 'dependabot', 'severity': 4, 'title': 'x', 'url': 'u',
                 'path': '', 'hint': 'h', 'sources': ['github-dependabot'], 'urls': ['u'], 'kinds': ['dependabot']}
        both = {n: {'slug': f'o/{n}', 'prs': [], 'alerts': [alert], 'problems': [], 'read': list(depbots.SOURCES), 'started': 0}
                for n in ('a', 'b')}
        data = {'next': 1, 'tasks': []}
        depbots.apply(cfg, data, both)
        self.assertEqual(data['tasks'][0]['rank'], -4)
        for t in data['tasks']:
            t['added'] = 0
        both['a'] = {**both['a'], 'alerts': [], 'started': 1}
        depbots.apply(cfg, data, both)
        self.assertEqual([t['status'] for t in data['tasks']], ['dropped', 'queued'])   # b's alert does not keep a's task


if __name__ == '__main__':
    unittest.main()
