"""--track resolves a run id, a track name or the full worktree name to one worktree (never <prefix><prefix><id>)."""
import unittest

from tutti import delegate


class TrackName(unittest.TestCase):
    def test_a_full_worktree_name_or_path_resolves_to_its_id(self):
        p = 'tutti-run-'
        self.assertEqual(delegate.track_name(p, 'tutti-run-1795542537-3544339'), '1795542537-3544339')
        self.assertEqual(delegate.track_name(p, '/x/hcm-wt/tutti-run-1795542537-3544339/'), '1795542537-3544339')
        self.assertEqual(delegate.track_name(p, 'tutti-run-tutti-run-17'), '17')

    def test_a_bare_id_or_name_is_kept(self):
        self.assertEqual(delegate.track_name('tutti-run-', '1795542537-3544339'), '1795542537-3544339')
        self.assertEqual(delegate.track_name('tutti-run-', 'search'), 'search')
        self.assertEqual(delegate.track_name('tutti-run-', 'tutti-run-'), 'tutti-run-')
        self.assertIsNone(delegate.track_name('tutti-run-', None))


if __name__ == '__main__':
    unittest.main()
