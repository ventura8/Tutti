"""Dependency bots and vulnerability reports as tasks (tutti/depbots.py): classification, grouping, dedupe and ranking,
the owner's messages, queueing and dropping. Fake gh calls only."""
import copy
import json
import os
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


class ReportIngestionTest(unittest.TestCase):
    FINDINGS = {'findings': [
        {'findingId': 'c1', 'severity': 'HIGH', 'title': 'Auth bypass', 'location': 'auth.py:10', 'repository': 'github.com/o/r'},
        {'findingId': 'c1', 'severity': 'critical', 'title': 'Auth bypass', 'location': 'auth.py:10'},
        {'findingId': 'c2', 'severity': 'low', 'title': 'Old bug', 'path': 'old.py', 'status': 'Fixed'},
        {'findingId': 'c3', 'severity': 'low', 'title': 'Not real', 'path': 'x.py', 'status': 'agent-refuted'},
        {'findingId': 'c4', 'severity': 'high', 'title': 'Elsewhere', 'path': 'y.py', 'repository': 'https://github.com/o/other'},
        {'severity': 'medium', 'title': 'High: path traversal', 'file': 'up.py'}], 'nextCursor': None}

    def save(self, d, data, name='cursor.json'):
        path = os.path.join(d, name)
        with open(path, 'w', encoding='utf8') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def test_findings_dedupe_by_id_and_skip_closed_or_other_repos(self):
        got = depbots.dedupe(depbots.from_report(depbots.report_items(self.FINDINGS), 'cursor', 'o/r'))
        self.assertEqual([a['key'] for a in got], ['cursor:c1', 'scan:up.py:high: path traversal'])
        self.assertEqual((got[0]['severity'], got[0]['kinds'], got[0]['sources']), (4, ['cursor-report'], ['cursor']))
        self.assertEqual(got[0]['title'], 'Auth bypass (auth.py)')
        self.assertIsNone(depbots.report_items({'error': 'forbidden'}))   # an error page is not an empty report

    def test_a_report_finding_and_its_review_comment_are_one(self):
        report = depbots.from_report([{'severity': 'critical', 'title': 'SQL injection in the query builder', 'path': 'db.py'}],
                                     'cursor', 'o/r')
        comment = {'user': {'login': 'greptile-apps[bot]'}, 'body': '## SQL injection in the query builder\n\nUse parameters.',
                   'path': 'db.py', 'html_url': 'c9', 'position': 4}
        got = depbots.dedupe(report + depbots.from_comments([comment], S['scanners']))
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]['severity'], got[0]['sources']), (4, ['cursor', 'greptile']))
        self.assertEqual(got[0]['kinds'], ['comments', 'cursor-report'])

    def test_severity_on_the_common_scale(self):
        for given, want in (('critical', 4), ('HIGH', 3), ('error', 3), ('moderate', 2), ('warning', 1), ('info', 0),
                            ('Severity: High', 3), (7, 4), (None, 0), ('unheard-of', 0)):
            self.assertEqual(depbots.rank(given), want, given)

    def test_saved_report_becomes_tasks_and_the_card(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = copy.deepcopy(config.DEFAULTS)
            cfg['project']['state'] = d
            reports = [{'scanner': 'cursor', 'path': self.save(d, self.FINDINGS)}]
            cfg['autopilot']['repos'] = {'r': {'path': d, 'github': 'o/r', 'check': 'make check', 'reports': reports}}
            self.assertEqual(depbots.errors(cfg), [])
            data = {'next': 1, 'tasks': []}
            found = depbots.gather(cfg, 1.0, fake_gh(prs=[], dep=[]), force=True)
            self.assertIn('cursor-report', found['r']['read'])
            depbots.apply(cfg, data, found)
            task = [t for t in data['tasks'] if t['depbot_ref'] == 'cursor:c1'][0]
            self.assertEqual((task['title'], task['size']), ('Fix critical vulnerability: Auth bypass (auth.py)', 'M'))
            self.assertEqual((task['source'], task['depbot_src']), ('security:cursor', ['cursor-report']))
            depbots.save(cfg, found, 1.0)
            card = depbots.view(cfg)
            self.assertEqual(card['worst'], 'critical')
            self.assertIn({'title': 'Auth bypass (auth.py)', 'url': 'https://github.com/o/r/security',
                           'sources': ['cursor'], 'severity': 'critical'}, card['repos'][0]['top'])
            os.remove(reports[0]['path'])   # an unreadable report keeps its tasks and says so
            found = depbots.gather(cfg, 2.0, fake_gh(prs=[], dep=[]), force=True)
            self.assertNotIn('cursor-report', found['r']['read'])
            self.assertIn('the cursor report', found['r']['problems'][0])
            task['added'] = 0
            depbots.apply(cfg, data, found)
            self.assertEqual(task['status'], 'queued')
            self.save(d, {'findings': []})   # read and gone: fixed
            depbots.apply(cfg, data, depbots.gather(cfg, 3.0, fake_gh(prs=[], dep=[]), force=True))
            self.assertEqual(task['status'], 'dropped')

    def test_a_report_that_is_not_findings_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            for text in ('not json', '{"message": "Forbidden"}'):
                scan = depbots.read_repo('o/r', S, fake_gh(prs=[]), [{'scanner': 'cursor', 'path': self.save(d, text)}])
                self.assertNotIn('cursor-report', scan['read'])
                self.assertEqual(len([p for p in scan['problems'] if 'cursor report' in p]), 1)

    def test_scanners_without_reports_are_refused_in_config(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['autopilot']['repos'] = {'r': {'path': '/r', 'reports': [{'scanner': 'greptile', 'path': '/g.json'},
                                                                    {'scanner': 'sonar', 'path': '/s.json'}, {'scanner': 'cursor'}]},
                                     'q': {'path': '/q', 'reports': 'cursor.json'}}
        errs = depbots.errors(cfg)
        self.assertEqual(len(errs), 4)
        self.assertIn('greptile has no API, webhook, export or email digest', errs[0])
        self.assertIn('use one of cursor, greptile', errs[1])
        self.assertIn('needs scanner and path', errs[2])
        self.assertIn('autopilot.repos.q.reports', errs[3])


if __name__ == '__main__':
    unittest.main()
