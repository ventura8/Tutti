"""The source preflight refuses accidental unsafe build inputs without assuming Git exists."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_guard as guard


class PackagingGuard(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = sorted(guard.REQUIRED | {'LICENSE', 'tutti/module.py', 'tutti/web/app.js', 'scripts/run.sh'})
        for name in self.paths:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture', encoding='utf8')
        (self.root / 'tutti/web/icons').mkdir()
        self.write_lists()

    def write_lists(self):
        (self.root / 'SDIST_ALLOWLIST').write_text(''.join(p + '\n' for p in self.paths), encoding='ascii')
        (self.root / 'MANIFEST.in').write_text('global-exclude *\n' + ''.join(
            'include ' + p + '\n' for p in [*self.paths, *guard.GENERATED]), encoding='ascii')

    def test_clean_literal_source_tree_passes_without_git(self):
        guard.validate(self.root)

    def test_untracked_package_data_and_python_modules_are_refused(self):
        for name in ('tutti/web/untracked_cafe\u0301.js', 'tutti/new_module.py', 'scripts/new.sh'):
            path = self.root / name
            path.write_text('synthetic fixture', encoding='utf8')
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'unexpected package candidate'):
                guard.validate(self.root)
            path.unlink()

    @unittest.skipUnless(hasattr(os, 'symlink'), 'symlinks unavailable')
    def test_symlink_at_allowlisted_asset_or_parent_is_refused(self):
        outside = self.root / 'outside'
        outside.write_text('synthetic fixture', encoding='utf8')
        asset = self.root / 'tutti/web/app.js'
        asset.unlink()
        try:
            asset.symlink_to(outside)
        except OSError:
            self.skipTest('symlink creation unavailable')
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            guard.validate(self.root)
        asset.unlink()
        asset.write_text('fixture')
        web = self.root / 'tutti/web'
        moved = self.root / 'moved-web'
        web.rename(moved)
        web.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            guard.validate(self.root)

    @unittest.skipUnless(hasattr(os, 'link'), 'hard links unavailable')
    def test_hard_link_at_allowlisted_asset_is_refused(self):
        outside = self.root / 'outside'
        outside.write_text('synthetic fixture', encoding='utf8')
        asset = self.root / 'tutti/web/app.js'
        asset.unlink()
        try:
            os.link(outside, asset)
        except OSError:
            self.skipTest('hard-link creation unavailable')
        with self.assertRaisesRegex(RuntimeError, 'unique regular file'):
            guard.validate(self.root)

    def test_traversal_duplicate_and_manifest_wildcard_are_refused(self):
        for name in ('../outside', '/absolute', 'tutti/web/*.js', 'tutti/module.py'):
            self.paths = sorted(self.paths + [name])
            self.write_lists()
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'SDIST_ALLOWLIST is invalid'):
                guard.validate(self.root)
            self.paths.remove(name)
        self.write_lists()
        with (self.root / 'MANIFEST.in').open('a', encoding='ascii') as stream:
            stream.write('include *.txt\n')
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            guard.validate(self.root)

    def test_missing_source_file_fails_closed(self):
        (self.root / 'tutti/module.py').unlink()
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            guard.validate(self.root)

    def test_direct_setup_refuses_backend_shadow_before_import(self):
        source = Path(__file__).resolve().parent.parent
        shutil.copyfile(source / 'setup.py', self.root / 'setup.py')
        (self.root / 'setuptools.py').write_text("from pathlib import Path\nPath('executed').write_text('fixture')\n")
        result = subprocess.run([sys.executable, 'setup.py', '--name'], cwd=self.root, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('shadows the backend', result.stderr)
        self.assertFalse((self.root / 'executed').exists())

    def test_generated_metadata_fails_closed_and_existing_build_tree_is_untouched(self):
        info = self.root / 'tutti_agents.egg-info'
        outside = self.root / 'outside'
        outside.mkdir()
        try:
            info.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation unavailable')
        with self.assertRaisesRegex(RuntimeError, 'metadata directory is unsafe'):
            guard.validate(self.root)
        info.unlink()
        info.mkdir()
        meta = info / 'PKG-INFO'
        target = self.root / 'target'
        target.write_text('fixture')
        meta.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            guard.validate(self.root)
        meta.unlink()
        try:
            os.link(target, meta)
        except OSError:
            self.skipTest('hard-link creation unavailable')
        with self.assertRaisesRegex(RuntimeError, 'unique regular file'):
            guard.validate(self.root)
        meta.unlink()
        meta.write_text('generated fixture')
        guard.validate(self.root)
        (info / 'unexpected').write_text('fixture')
        with self.assertRaisesRegex(RuntimeError, 'unexpected generated metadata'):
            guard.validate(self.root)
        (info / 'unexpected').unlink()
        sources = info / 'SOURCES.txt'
        sources.write_text('../outside\n')
        with self.assertRaisesRegex(RuntimeError, 'SOURCES.txt lists unexpected paths'):
            guard.validate(self.root)
        sources.write_text('tutti/module.py\n')
        guard.validate(self.root)
        (self.root / 'build').mkdir()
        (self.root / 'build/stale').write_text('fixture')
        guard.validate(self.root)  # setup.py selects a fresh temporary build base instead of reading this tree

    def test_setup_cfg_is_rejected_unless_it_is_generated_by_pinned_backend(self):
        setup = self.root / 'setup.cfg'
        setup.write_text('[metadata]\nname = unexpected\n')
        with self.assertRaisesRegex(RuntimeError, 'setup.cfg is unexpected'):
            guard.validate(self.root)
        setup.write_text(guard.GENERATED_SETUP)
        guard.validate(self.root)

    def test_literal_allowlist_matches_tracked_tree_when_git_is_available(self):
        source = Path(__file__).resolve().parent.parent
        if not (source / '.git').exists() or not shutil.which('git'):
            self.skipTest('Git or repository metadata is unavailable')
        result = subprocess.run(['git', 'ls-files', '-z'], cwd=source, capture_output=True, check=True)
        tracked = {p for p in result.stdout.decode().split('\0') if p}
        allowed = set((source / 'SDIST_ALLOWLIST').read_text(encoding='ascii').splitlines())
        self.assertEqual(allowed, tracked, 'update SDIST_ALLOWLIST and MANIFEST.in when tracked source paths change')

    def test_build_ignores_git_availability(self):
        with mock.patch('subprocess.run', side_effect=AssertionError('Git should not run')):
            guard.validate(self.root)

    def test_packaging_discovery_changes_fail_closed(self):
        import copy
        config = {'build-system': {'requires': ['setuptools==84.1.0'], 'build-backend': 'setuptools.build_meta'},
                  'project': {'name': 'tutti-agents', 'readme': 'README.md', 'license': 'MIT', 'license-files': ['LICENSE'],
                              'dynamic': ['version']},
                  'tool': {'setuptools': copy.deepcopy(guard.EXPECTED_TOOL)}}
        guard.validate_config(config)
        guard.validate_backend('84.1.0')
        with self.assertRaisesRegex(RuntimeError, 'setuptools==84.1.0 is required'):
            guard.validate_backend('80.9.0')
        for change in ('package-data', 'packages', 'license-files', 'distutils', 'name', 'backend'):
            broken = copy.deepcopy(config)
            if change == 'license-files':
                broken['project']['license-files'] = ['LICEN[CS]E*']
            elif change == 'distutils':
                broken['tool']['distutils'] = {'unsafe': True}
            elif change == 'name':
                broken['project']['name'] = 'other-project'
            elif change == 'backend':
                broken['build-system']['build-backend'] = 'other.backend'
            else:
                broken['tool']['setuptools'][change] = []
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, 'discovery settings changed'):
                guard.validate_config(broken)
