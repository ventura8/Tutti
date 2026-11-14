"""Open pull requests on the dashboard's Projects panel: a release in progress with its CI and review state (fake gh)."""
import os
import tempfile
import unittest

from tutti import config, dashboard, releases

GH = [{'number': 11, 'title': 'feat: agy-claude conducts (0.1.10)', 'url': 'https://github.com/o/r/pull/11', 'branch': 'feat/x',
       'draft': False, 'decision': '',
       'checks': [{'name': 'check', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}, {'name': 'lint', 'status': 'IN_PROGRESS'}],
       'reviews': [{'author': 'copilot-pull-request-reviewer', 'state': 'COMMENTED'}]},
      {'number': 9, 'title': 'docs: typo', 'url': 'javascript:alert(1)', 'branch': 'docs', 'draft': True, 'decision': 'APPROVED',
       'checks': [{'context': 'ci/legacy', 'state': 'FAILURE'}], 'reviews': []}]


class ReleasesTest(unittest.TestCase):
    def setUp(self):
        releases._cache.clear()

    def test_a_release_comes_first_with_its_ci_and_reviews(self):
        rel, other = releases.pr_items(GH)
        self.assertEqual((rel['release'], rel['ci'], rel['ci_detail']), ('0.1.10', 'pending', '1 pending, 1 passed: lint still running'))
        self.assertEqual(rel['review'], 'copilot-pull-request-reviewer commented')
        self.assertEqual((other['release'], other['ci'], other['review'], other['url']), (None, 'failing', 'approved', None))
        self.assertIn('ci/legacy failed', other['ci_detail'])

    def test_no_checks_and_no_reviews_say_so(self):
        self.assertEqual(releases.ci_state([]), ('none', 'no checks reported'))
        self.assertEqual(releases.review_state('', []), 'no review yet')
        self.assertEqual(releases.ci_state([{'name': 'a', 'conclusion': 'SUCCESS'}]), ('passing', '1 passed'))

    def test_gh_is_cached_and_a_failure_keeps_the_last_items(self):
        calls = []

        def ok(repo):
            calls.append(repo)
            return GH

        def broken(repo):
            raise OSError('gh pr list -R o/r exited 1: HTTP 401 (is gh signed in?)')
        clock = iter([0, 5, 500]).__next__
        cfg = {'projects': {'Tutti': {'repo': 'o/r'}, 'Other': {'match': 'x'}}}
        first = releases.all_prs(cfg, ok, clock)
        self.assertEqual(list(first), ['Tutti'])
        releases.all_prs(cfg, ok, clock)
        self.assertEqual(calls, ['o/r'])
        later = releases.all_prs(cfg, broken, clock)['Tutti']
        self.assertEqual(len(later['items']), 2)
        self.assertIn('HTTP 401', later['error'])

    def test_attach_adds_an_idle_project_with_open_prs(self):
        state = {'items': [{'name': 'Busy', 'repos': []}]}
        releases.attach(state, {'Tutti': {'repo': 'o/r', 'items': releases.pr_items(GH), 'error': None},
                                'Quiet': {'repo': 'o/q', 'items': [], 'error': None}})
        self.assertEqual([p['name'] for p in state['items']], ['Busy', 'Tutti'])
        self.assertEqual(state['items'][1]['prs']['items'][0]['number'], 11)

    def test_the_dashboard_snapshot_carries_the_prs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = config.normalise({'machines': {'a': {'host': 'local'}}, 'projects': {'Tutti': {'repo': 'o/r'}},
                                'project': {'worktrees': tmp.name, 'state': os.path.join(tmp.name, 'state')}})
        cfg['_path'] = os.path.join(tmp.name, 'tutti.config.yaml')
        c = dashboard.Collector(cfg, probe_fn=lambda m: None, available_fn=lambda w: (True, ''), activity_fn=lambda m: {},
                                prs_fn=lambda cfg: {'Tutti': {'repo': 'o/r', 'items': releases.pr_items(GH), 'error': None}})
        c.probes = {'prs': c.prs_fn(cfg)}
        snap = c.snapshot()
        tutti = next(p for p in snap['projects']['items'] if p['name'] == 'Tutti')
        self.assertEqual(tutti['prs']['items'][0]['release'], '0.1.10')

    def test_a_bad_repo_is_named(self):
        cfg = config.normalise({'machines': {'a': {'host': 'a.example'}}, 'projects': {'X': {'repo': 'not a repo'}}})
        self.assertIn('projects.X.repo', '\n'.join(config.validate(cfg)))


if __name__ == '__main__':
    unittest.main()
