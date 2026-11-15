"""The unit tests with a temp-folder guard (scripts/check.sh runs this): each test gets its own temp folder (tempfile and
$TMPDIR point there), and a test that leaves anything in it fails, naming what it left. 17,000 folders of earlier runs once
filled /tmp's inodes and stopped every command on the laptop (2026-11-15)."""
import importlib
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]   # the tests' helpers, then the tutti package
tmpdirs = importlib.import_module('tmpdirs')   # the module the tests import (tests/ is on the path, as discover does)


class Guarded(unittest.TextTestResult):
    def startTest(self, test):
        self._root = tempfile.mkdtemp(prefix='t-', dir=ROOT)
        self._saved = tempfile.tempdir, os.environ.get('TMPDIR')
        tempfile.tempdir = os.environ['TMPDIR'] = self._root
        tmpdirs._running.append(test)
        super().startTest(test)

    def stopTest(self, test):
        tmpdirs._running.pop()
        tempfile.tempdir, old = self._saved
        os.environ['TMPDIR'] = old if old is not None else ROOT
        left = sorted(n for n in os.listdir(self._root) if not n.startswith(KEPT))
        shutil.rmtree(self._root, ignore_errors=True)
        if left:
            try:
                raise AssertionError(f'the test left {len(left)} entr(y/ies) in its temp folder: {", ".join(left[:5])}; '
                                     'use tmpdirs.mkdtemp(), a TemporaryDirectory or addCleanup')
            except AssertionError:
                self.addFailure(test, sys.exc_info())
        super().stopTest(test)


KEPT = ('tutti-ssh-',)   # ssh's shared connection folder (tutti/remote.py): one per user, meant to outlive a run
ROOT = os.environ.get('TUTTI_TEST_ROOT', '')

if __name__ == '__main__':
    # made here, not on import: a test's spawned child imports this file again
    ROOT = os.environ['TUTTI_TEST_ROOT'] = os.environ['TMPDIR'] = tempfile.mkdtemp(prefix='tutti-tests-')
    suite = unittest.defaultTestLoader.discover(HERE, top_level_dir=HERE)
    result = unittest.TextTestRunner(resultclass=Guarded).run(suite)
    shutil.rmtree(ROOT, ignore_errors=True)
    sys.exit(0 if result.wasSuccessful() else 1)
