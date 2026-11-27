"""The unit tests with a temp-folder guard (scripts/check.sh runs this): each test gets its own temp folder (tempfile and
$TMPDIR point there), and a test that leaves anything in it fails, naming what it left. 17,000 folders of earlier runs once
filled /tmp's inodes and stopped every command on the laptop (2026-11-15)."""
import importlib
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]   # the tests' helpers, then the tutti package
tmpdirs = importlib.import_module('tmpdirs')   # the module the tests import (tests/ is on the path, as discover does)
os.environ['TUTTI_NO_DAEMONS'] = '1'   # no real keep-awake loop, tunnel or inhibitor from a test (tutti/procs.py)


# Python's own helpers of this runner (started once by the first multiprocessing test, they end with it)
RUNNER_HELPERS = ('multiprocessing.resource_tracker', 'multiprocessing.forkserver')


def left_children(root):
    """Pids of this user's live processes started under this test run (TUTTI_TEST_ROOT == root), never this runner."""
    from tutti import procs
    out = []
    for name in (n for n in os.listdir('/proc') if n.isdigit()):
        p = procs.info(name)
        if p and p['uid'] == os.getuid() and p['pid'] != os.getpid() and p['env'].get('TUTTI_TEST_ROOT') == root \
                and not any(h in ' '.join(p['argv']) for h in RUNNER_HELPERS):
            out.append(p)
    return out


def stop_children(rounds=5):
    """Every leftover stopped by exact pid, again until none is left: a loop killed between two scans may already have
    started its next child (a `sleep`, a fake `adb`), which carries the same marker and lived on until self-heal stopped it
    (about 129 a day, 0.17.2). The first round asks (SIGTERM), later rounds insist (SIGKILL). Returns the first round's."""
    first = []
    for n in range(rounds):
        kids = left_children(ROOT) if ROOT and os.path.isdir('/proc') else []
        if not kids:
            break
        first = first or kids
        for p in kids:
            try:
                os.kill(p['pid'], signal.SIGTERM if n == 0 else signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.05)
    return first


class Guarded(unittest.TextTestResult):
    def startTest(self, test):
        self._start_time = time.time()
        self._root = tempfile.mkdtemp(prefix='t-', dir=ROOT)
        self._saved = tempfile.tempdir, os.environ.get('TMPDIR')
        tempfile.tempdir = os.environ['TMPDIR'] = self._root
        tmpdirs._running.append(test)
        super().startTest(test)

    def stopTest(self, test):
        duration = time.time() - self._start_time
        if duration > 1.0:
            print(f'Slow test: {test} took {duration:.2f}s', file=sys.stderr)
        tmpdirs._running.pop()
        tempfile.tempdir, old = self._saved
        os.environ['TMPDIR'] = old if old is not None else ROOT
        left = sorted(n for n in os.listdir(self._root) if not n.startswith(KEPT))
        shutil.rmtree(self._root, ignore_errors=True)
        kids = stop_children()
        if kids:
            try:
                raise AssertionError(f'the test left {len(kids)} process(es) running: '
                                     + '; '.join(f'{p["pid"]} {" ".join(p["argv"])[:80]}' for p in kids[:3])
                                     + '; mock the background start or stop it in addCleanup')
            except AssertionError:
                self.addFailure(test, sys.exc_info())
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
