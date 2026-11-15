"""Temporary folders that never outlive their test. `mkdtemp()` registers the folder with the running test's cleanups
(tests/run.py says which test runs); outside that runner it is removed when the process ends. Tests use it, a
TemporaryDirectory, or addCleanup: tests/run.py fails any test that leaves something in its temp folder."""
import atexit
import shutil
import tempfile

_running = []   # the test tests/run.py is running now


def mkdtemp(**kw):
    kw.setdefault('prefix', 'tutti-t-')   # a Tutti temp folder is named so: the self-heal janitor knows it (t86)
    d = tempfile.mkdtemp(**kw)
    if _running:
        _running[-1].addCleanup(shutil.rmtree, d, True)
    else:
        atexit.register(shutil.rmtree, d, True)
    return d
