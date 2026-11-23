"""agy may read the guide files a brief points to (an earlier diff under .tutti-state), and nothing else outside its tree."""
import json
import os
import tempfile
import unittest

from tutti import workers


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

if __name__ == '__main__':
    unittest.main()
