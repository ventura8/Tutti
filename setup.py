"""Run the source preflight before the standard setuptools backend builds distributions."""
import importlib
import os
import runpy
import stat
import tempfile

# PEP 517 runs this trusted entry point in the source root, which may not be on sys.path.
if any(os.path.lexists(os.path.join(os.getcwd(), name)) for name in ('setuptools.py', 'setuptools')):
    raise RuntimeError('source build: an unexpected setuptools module shadows the backend; remove or move it before building')
guard = os.path.join(os.getcwd(), 'build_guard.py')
mode = os.lstat(guard).st_mode
if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
    raise RuntimeError('source build: build_guard.py is not a regular file; restore it before building')
checks = runpy.run_path(guard)
checks['validate']()
backend = importlib.import_module('setuptools')
checks['validate_backend'](backend.__version__)
config = importlib.import_module('setuptools.config.pyprojecttoml').load_file('pyproject.toml')
checks['validate_config'](config)
# Never reuse an existing build/ tree: stale files there could otherwise enter a wheel.
with tempfile.TemporaryDirectory(prefix='tutti-build-') as build_dir:
    backend.setup(options={'build': {'build_base': build_dir}})
