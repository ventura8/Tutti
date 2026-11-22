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
            self.assertEqual(len(allow), 3)

    def test_no_guide_means_only_the_tree(self):
        self.assertEqual(workers.guide_files('fix the bug in a.ts'), [])
        self.assertEqual(workers.guide_files(None), [])


if __name__ == '__main__':
    unittest.main()
