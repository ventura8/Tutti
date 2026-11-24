"""agy may read the guide files a brief points to (an earlier diff under .tutti-state), and nothing else outside its tree."""
import json
import os
import tempfile
import unittest
from unittest import mock

from tutti import delegate, workers


class GuideReads(unittest.TestCase):
    def test_an_existing_guide_file_gets_a_read_grant_only(self):
        with tempfile.TemporaryDirectory() as d:
            runs = os.path.join(d, '.tutti-state', 'runs')
            os.makedirs(runs)
            patch = os.path.join(runs, 'redo-t1.patch')
            open(patch, 'w').close()
            task = f'do it again; the earlier diff, {patch}, is a guide. Also {runs}/missing.patch and {d}/config.yaml'
            self.assertEqual(workers.guide_files(task), [patch])
            argv = workers.grant_tree(['agy', '{"permissions":{"allow":[]}}'], d, workers.guide_files(task))
            allow = json.loads(argv[1])['permissions']['allow']
            self.assertIn(f'read_file({patch})', allow)
            self.assertNotIn(f'write_file({patch})', allow)
            self.assertEqual(sorted(allow), sorted([f'read_file({d})', f'write_file({d})', f'read_file({patch})']))

    def test_no_guide_means_only_the_tree(self):
        self.assertEqual(workers.guide_files('fix the bug in a.ts'), [])
        self.assertEqual(workers.guide_files(None), [])


    def test_traversal_and_symlinks_out_of_tutti_state_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            runs = os.path.join(d, '.tutti-state', 'runs')
            os.makedirs(runs)
            outside = os.path.join(d, 'notes.txt')
            open(outside, 'w').close()
            link = os.path.join(runs, 'link.txt')
            os.symlink(outside, link)
            self.assertEqual(workers.guide_files(f'see {runs}/../../notes.txt and {link}'), [])

    def test_a_tilde_path_in_the_brief_is_granted_as_its_real_file(self):
        with tempfile.TemporaryDirectory() as d:
            runs = os.path.join(d, 'hcm', '.tutti-state', 'runs')
            os.makedirs(runs)
            log = os.path.join(runs, 'ship-1.log')
            open(log, 'w').close()
            with mock.patch.dict(os.environ, {'HOME': d}):
                self.assertEqual(workers.guide_files('see ~/hcm/.tutti-state/runs/ship-1.log and ~/hcm/.tutti-state/runs/no.log'), [log])
                self.assertEqual(workers.guide_files('see ~/../etc/.tutti-state/x.log'), [])

    def test_extra_reads_are_added_to_the_allow_list_as_reads_only(self):
        argv = workers.grant_tree(['agy', '{"permissions":{"allow":["command(git)"]}}'], '/w/t', ['/s/runs'])
        allow = json.loads(argv[1])['permissions']['allow']
        self.assertIn('read_file(/s/runs)', allow)
        self.assertNotIn('write_file(/s/runs)', allow)


class ReadDenialRetry(unittest.TestCase):
    def test_only_a_file_read_refusal_is_retried(self):
        self.assertTrue(delegate.read_denied(['Print mode: soft-denying tool confirmation "ReadFile" at step 2']))
        self.assertTrue(delegate.read_denied(['a tool required the "read_file" permission ... auto-denied']))
        self.assertFalse(delegate.read_denied(['soft-denying tool confirmation "Bash" at step 2']))   # a command is never widened
        self.assertFalse(delegate.read_denied([]))

    def test_a_read_refusal_is_retried_once_with_the_runs_folder_readable(self):
        calls = []

        def fake(cfg, name, task, files, wt, log, extra, worker_log, check_list, reads=()):
            calls.append(reads)
            return 0, ['soft-denying tool confirmation "ReadFile" at step 2'] if not reads else ['done']

        cfg = {'workers': {'agy': {'denied': 'soft-denying'}}}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(delegate, 'run_worker', fake), \
                mock.patch.object(delegate, 'runs_dir', lambda c: '/s/runs'):
            self.assertEqual(delegate.run_worker_retrying(cfg, 'agy', 't', [], d, None, (), os.path.join(d, 'w.log'), None, print),
                             (0, ['done']))
        self.assertEqual(calls, [(), ['/s/runs']])

    def test_a_command_refusal_is_not_retried(self):
        calls = []

        def fake(cfg, name, task, files, wt, log, extra, worker_log, check_list, reads=()):
            calls.append(reads)
            return 0, ['soft-denying tool confirmation "Bash" at step 2']

        cfg = {'workers': {'agy': {'denied': 'soft-denying'}}}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(delegate, 'run_worker', fake):
            delegate.run_worker_retrying(cfg, 'agy', 't', [], d, None, (), os.path.join(d, 'w.log'), None, print)
        self.assertEqual(calls, [()])


if __name__ == '__main__':
    unittest.main()
