"""A source-build preflight against accidental dirty-tree packaging, not a boundary against concurrent mutation."""
import os
import re
import stat
from pathlib import Path

GENERATED = ('tutti_agents.egg-info/PKG-INFO', 'tutti_agents.egg-info/SOURCES.txt',
             'tutti_agents.egg-info/dependency_links.txt', 'tutti_agents.egg-info/entry_points.txt',
             'tutti_agents.egg-info/requires.txt', 'tutti_agents.egg-info/top_level.txt')
REQUIRED = {'MANIFEST.in', 'SDIST_ALLOWLIST', 'setup.py', 'pyproject.toml', 'build_guard.py'}
GENERATED_SETUP = '[egg_info]\ntag_build = \ntag_date = 0\n\n'
EXPECTED_TOOL = {'packages': ['tutti', 'tutti.scripts'], 'package-dir': {'tutti.scripts': 'scripts'},
                 'include-package-data': False, 'package-data': {'tutti': ['web/*', 'web/icons/*'], 'tutti.scripts': ['*.sh']},
                 'dynamic': {'version': {'attr': 'tutti.__version__'}}}


def validate_config(config):
    """Keep setuptools' discovery rules aligned with the candidates checked below."""
    project, tools = config.get('project') or {}, config.get('tool') or {}
    build = config.get('build-system') or {}
    if (build != {'requires': ['setuptools==84.1.0'], 'build-backend': 'setuptools.build_meta'} or
            project.get('name') != 'tutti-agents' or project.get('readme') != 'README.md' or project.get('license') != 'MIT' or
            project.get('license-files') != ['LICENSE'] or
            project.get('dynamic') != ['version'] or tools.get('setuptools') != EXPECTED_TOOL or 'distutils' in tools):
        raise RuntimeError('source build: packaging discovery settings changed; review and update the source guard before building')


def validate_backend(version):
    if version != '84.1.0':
        raise RuntimeError('source build: setuptools==84.1.0 is required; use an isolated PEP 517 build or install the pinned backend')


def _regular(root, name):
    current = root
    for part in name.split('/'):
        current /= part
        try:
            mode = current.lstat().st_mode
        except OSError as e:
            raise RuntimeError(f'source build: a required allowlisted path is unavailable ({e.strerror}); restore the source tree') from e
        if stat.S_ISLNK(mode):
            raise RuntimeError('source build: a required allowlisted path is a symlink; restore the checked-out file before building')
    if not stat.S_ISREG(mode) or current.stat().st_nlink != 1:
        raise RuntimeError('source build: a required allowlisted path is not a unique regular file; restore it before building')


def _generated(root, allowed):
    """Pre-existing generated outputs may not redirect writes or add stale wheel contents."""
    info = root / 'tutti_agents.egg-info'
    if info.exists() or info.is_symlink():
        if info.is_symlink() or not info.is_dir():
            raise RuntimeError('source build: the generated metadata directory is unsafe; remove or move it before building')
        for path in info.iterdir():
            name = path.relative_to(root).as_posix()
            if name not in GENERATED:
                raise RuntimeError('source build: unexpected generated metadata exists; remove or move it before building')
            _regular(root, name)
        sources = info / 'SOURCES.txt'
        if sources.exists() and any(name not in allowed | set(GENERATED) | {'setup.cfg'}
                                    for name in sources.read_text(encoding='utf8').splitlines()):
            raise RuntimeError('source build: generated SOURCES.txt lists unexpected paths; remove it before building')
    setup = root / 'setup.cfg'
    if setup.exists() or setup.is_symlink():
        _regular(root, 'setup.cfg')
        if setup.read_text(encoding='utf8') != GENERATED_SETUP:
            raise RuntimeError('source build: setup.cfg is unexpected; remove or move it before building')


def validate(root=None):
    """Reject unexpected packaging candidates and symlinks in a quiescent source tree before setuptools runs."""
    root = Path(root or os.getcwd())
    for name in ('SDIST_ALLOWLIST', 'MANIFEST.in', 'pyproject.toml', 'setup.py', 'build_guard.py'):
        _regular(root, name)
    names = (root / 'SDIST_ALLOWLIST').read_text(encoding='ascii').splitlines()
    if (names != sorted(set(names)) or not REQUIRED.issubset(names) or
            any(not re.fullmatch(r'[A-Za-z0-9_./+-]+', p) or any(x in ('', '.', '..') for x in p.split('/')) for p in names)):
        raise RuntimeError('source build: SDIST_ALLOWLIST is invalid; restore sorted literal source paths before building')
    expected = 'global-exclude *\n' + ''.join(f'include {p}\n' for p in [*names, *GENERATED])
    if (root / 'MANIFEST.in').read_text(encoding='ascii') != expected:
        raise RuntimeError('source build: MANIFEST.in does not match SDIST_ALLOWLIST; regenerate it before building')
    _generated(root, set(names))
    for name in names:
        _regular(root, name)
    allowed = set(names)
    # These match the existing setuptools package-data and package module discovery, including direct wheel builds.
    groups = ((root / 'tutti', '*.py'), (root / 'tutti/web', '*'), (root / 'tutti/web/icons', '*'), (root / 'scripts', '*.sh'))
    for folder, pattern in groups:
        for path in folder.glob(pattern):
            if path == root / 'tutti/web/icons' and path.is_dir() and not path.is_symlink():
                continue
            if path.relative_to(root).as_posix() not in allowed:
                raise RuntimeError('source build: an unexpected package candidate is present; remove it or update the source allowlist')
