"""Open pull requests on the dashboard's Projects panel: a release in progress with its CI and review state (fake gh)."""
import os
import shutil
import subprocess
import tempfile
import unittest

from tutti import config, dashboard, releases

GH = [{'number': 11, 'title': 'fix: the open items of 0.1.8/0.1.9 (0.1.10)', 'url': 'https://github.com/o/r/pull/11', 'branch': 'feat/x',
       'draft': False, 'decision': '',
       'checks': [{'name': 'check', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}, {'name': 'lint', 'status': 'IN_PROGRESS'}],
       'reviews': [{'author': 'copilot-pull-request-reviewer', 'state': 'COMMENTED'}]},
      {'number': 9, 'title': 'docs: typo', 'url': 'javascript:alert(1)', 'branch': 'docs', 'draft': True, 'decision': 'APPROVED',
       'checks': [{'context': 'ci/legacy', 'state': 'FAILURE'}], 'reviews': []}]


class ReleaseScriptTest(unittest.TestCase):
    def run_release(self, merge_sha, parent=None, reject_push=False, advance_main=False, changelog='# Changelog\n'):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, 'scripts'))
            os.makedirs(os.path.join(root, 'bin'))
            template = os.path.join(root, 'template')
            os.makedirs(os.path.join(template, 'tutti'))
            os.makedirs(os.path.join(template, 'scripts'))
            source = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'scripts', 'release-patch.sh')
            shutil.copyfile(source, os.path.join(root, 'scripts', 'release-patch.sh'))
            for name, content in (('tutti/__init__.py', "__version__ = '0.1.0'\n"),
                                  ('package.json', '{"version": "0.1.0"}\n'), ('CHANGELOG.md', changelog)):
                with open(os.path.join(template, name), 'w', encoding='utf8') as f:
                    f.write(content)
            with open(os.path.join(template, 'scripts', 'release-assets.sh'), 'w', encoding='utf8') as f:
                f.write('#!/bin/bash\ntouch "$2/asset"\n')
            git = '''#!/bin/bash
printf 'git %s\\n' "$*" >> "$RELEASE_TEST_LOG"
if [[ "$1" = '-C' ]]; then shift 2; fi
if [[ "$1" = 'push' && "$*" = *'--force-with-lease='* && "$RELEASE_TEST_REJECT" = 1 ]]; then exit 1; fi
if [[ "$1" = 'commit' ]]; then cat CHANGELOG.md >> "$RELEASE_TEST_LOG"; fi
if [[ "$1" = 'show' && "$2" = *:CHANGELOG.md ]]; then cat "$RELEASE_TEST_TEMPLATE/CHANGELOG.md"; exit; fi
case "$1 $2" in
  'tag --list') echo v0.1.0;;
  'log --format=%s') echo 'fix: sample';;
  'log --format=- %s') echo '- fix: sample';;
  'rev-parse -q') exit 1;;
  'rev-parse HEAD') printf '%040d\\n' 1;;
  'rev-parse origin/main')
    if [[ "$RELEASE_TEST_ADVANCE" = 1 && -f "$RELEASE_TEST_SEEN" ]]; then printf '%040d\\n' 4
    else touch "$RELEASE_TEST_SEEN"; printf '%040d\\n' 3; fi;;
  'show -s') printf '%s\\n' "$RELEASE_TEST_PARENT";;
  'worktree add') mkdir -p "$6"; cp -R "$RELEASE_TEST_TEMPLATE/." "$6/";;
  'worktree remove') rm -rf -- "$4";;
esac
'''
            gh = '''#!/bin/bash
printf 'gh %s\\n' "$*" >> "$RELEASE_TEST_LOG"
if [[ "$1 $2" = 'pr checks' && "$*" = *'--json name'* ]]; then echo check; fi
if [[ "$1 $2" = 'pr view' ]]; then printf '%s\\n' "$RELEASE_TEST_MERGE"; fi
'''
            for name, content in (('git', git), ('gh', gh)):
                path = os.path.join(root, 'bin', name)
                with open(path, 'w', encoding='utf8') as f:
                    f.write(content)
                os.chmod(path, 0o755)
            log = os.path.join(root, 'commands.log')
            env = {**os.environ, 'PATH': os.path.join(root, 'bin') + os.pathsep + os.environ['PATH'],
                   'RELEASE_TEST_LOG': log, 'RELEASE_TEST_TEMPLATE': template, 'RELEASE_TEST_MERGE': merge_sha,
                   'RELEASE_TEST_PARENT': parent if parent is not None else f'{3:040d}',
                   'RELEASE_TEST_REJECT': '1' if reject_push else '0', 'RELEASE_TEST_ADVANCE': '1' if advance_main else '0',
                   'RELEASE_TEST_SEEN': os.path.join(root, 'main-seen')}
            result = subprocess.run(['bash', os.path.join(root, 'scripts', 'release-patch.sh')], env=env,
                                    capture_output=True, text=True, timeout=20, check=False)
            with open(log, encoding='utf8') as f:
                commands = f.read()
            return result, commands

    def test_tags_the_pr_merge_not_a_later_main_and_never_deletes_a_branch(self):
        merge = 'b' * 40
        result, commands = self.run_release(merge)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'git tag -a v0.1.1 {merge} -m Tutti 0.1.1', commands)
        self.assertIn('--match-head-commit', commands)
        self.assertIn('--force-with-lease=refs/heads/', commands)
        self.assertNotIn('git branch -D', commands)
        self.assertNotIn('origin --delete', commands)

    def test_missing_merge_commit_stops_before_tagging(self):
        result, commands = self.run_release('')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("merged PR's commit is unavailable", result.stderr)
        self.assertNotIn('git tag -a', commands)

    def test_merge_on_a_different_base_is_not_tagged(self):
        result, commands = self.run_release('b' * 40, parent='c' * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('merged on a different base', result.stderr)
        self.assertNotIn('git tag -a', commands)

    def test_existing_remote_release_branch_is_never_overwritten(self):
        result, commands = self.run_release('b' * 40, reject_push=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--force-with-lease=refs/heads/release/v0.1.1:', commands)
        self.assertNotIn('gh pr create', commands)
        self.assertNotIn('origin --delete', commands)

    def test_main_advancing_during_review_stops_before_merge(self):
        result, commands = self.run_release('b' * 40, advance_main=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('main advanced during review', result.stderr)
        self.assertNotIn('gh pr merge', commands)

    def test_release_moves_unreleased_entries_once_and_preserves_history(self):
        log = '# Changelog\n\n## Unreleased\n\n### Fixed\n\n- First fix\n\n## 0.1.0\n\n- Old fix\n'
        result, commands = self.run_release('b' * 40, changelog=log)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(commands.count('- First fix'), 1)
        self.assertNotIn('## Unreleased', commands)
        self.assertIn('## 0.1.1 (', commands)
        self.assertIn('## 0.1.0\n\n- Old fix', commands)

    def test_release_refuses_duplicate_unreleased_sections(self):
        log = '# Changelog\n\n## Unreleased\n\n- A\n\n## [Unreleased]\n\n- B\n'
        result, commands = self.run_release('b' * 40, changelog=log)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('multiple Unreleased sections', result.stderr)
        self.assertNotIn('gh pr create', commands)
        self.assertNotIn('git worktree add', commands)

    def test_fenced_headings_are_not_sections(self):
        log = ('# Changelog\n\n## Unreleased\n\n### Fixed\n\n```sh\n## example\n'
               'echo ok\n```\n\n## 0.1.0\n\n```md\n## Unreleased\n```\n')
        result, commands = self.run_release('b' * 40, changelog=log)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('```sh\n## example\necho ok\n```', commands)
        self.assertIn('## 0.1.0\n\n```md\n## Unreleased\n```', commands)

    def test_empty_categories_fall_back_to_commit_notes(self):
        result, commands = self.run_release('b' * 40, changelog='# Changelog\n\n## Unreleased\n\n### Fixed\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('### Changes\n\n- fix: sample', commands)

    def test_fence_with_info_does_not_close_an_open_fence(self):
        log = '# Changelog\n\n## Unreleased\n\n### Fixed\n\n```\n```python\n## example\n```\n\n## 0.1.0\n'
        result, commands = self.run_release('b' * 40, changelog=log)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('```\n```python\n## example\n```', commands)

    def test_unclosed_fence_stops_before_release_branch_creation(self):
        log = '# Changelog\n\n## Unreleased\n\n```\n## example\n'
        result, commands = self.run_release('b' * 40, changelog=log)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unclosed fenced block', result.stderr)
        self.assertNotIn('git worktree add', commands)

    def test_indented_heading_with_closing_hashes_is_moved(self):
        log = '# Changelog\n\n  ## Unreleased ##\n\n### Fixed\n\n- Fix\n\n  ## 0.1.0\n\n- Old\n'
        result, commands = self.run_release('b' * 40, changelog=log)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(commands.count('- Fix'), 1)
        self.assertNotIn('## Unreleased', commands)
        self.assertIn('  ## 0.1.0\n\n- Old', commands)


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
