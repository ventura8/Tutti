"""Releases stuck: merged versions of Tutti that no GitHub release publishes (docs/INSTALL.md, *Releases*).

A version is merged when its heading is in this checkout's CHANGELOG.md; it is published when the latest GitHub release is
that version or newer. Every install updates only to published releases, so merged versions that never got a release (a tag
the repository's rules refused, or a tag with no release) leave every installed copy behind without a word. `tutti doctor`
and the dashboard say so. It applies only where Tutti runs from a git checkout of its own repository (the maintainer's);
a release install is the published version by definition.

Pure apart from reading CHANGELOG.md and one cached request for the latest release's SHA256SUMS."""
import os
import re
import threading
import time

HEADING = re.compile(r'^## (\d+\.\d+\.\d+) ', re.M)
CACHE_SECONDS = 3600
_cache = {'at': 0.0, 'value': None, 'busy': False}
_lock = threading.Lock()


def vt(v):
    return tuple(int(x) for x in v.split('.'))


def merged_versions(root):
    """The versions CHANGELOG.md names, newest first; [] when this is not a git checkout with a CHANGELOG."""
    if not os.path.isdir(os.path.join(root, '.git')) and not os.path.isfile(os.path.join(root, '.git')):
        return []
    try:
        with open(os.path.join(root, 'CHANGELOG.md'), encoding='utf8') as f:
            text = f.read()
    except OSError:
        return []
    return sorted(set(HEADING.findall(text)), key=vt, reverse=True)


def unpublished(merged, published):
    """The merged versions newer than the newest published one."""
    if not published:
        return []
    return [v for v in merged if vt(v) > vt(published)]


def message(stuck, published):
    """The warning, or '' when nothing is stuck."""
    if not stuck:
        return ''
    shown = ', '.join(sorted(stuck, key=vt)[:8]) + (' ...' if len(stuck) > 8 else '')
    return (f'releases stuck: {len(stuck)} merged version(s) unpublished ({shown}); the newest release is {published}, so every '
            'install stays there. Why: their v* tags were refused by the repository\'s tag rules or have no GitHub release. '
            'Fix: the maintainer pushes the tags (or allows them in Settings, Rules), then the release workflow publishes '
            'them; for a tag without a release: gh release create v<version> --verify-tag')


def latest_published(fetch=None):
    """The newest version the latest GitHub release lists, or None (offline)."""
    from . import install
    if os.environ.get('TUTTI_OFFLINE') and fetch is None:   # tests and scripts/check.sh: never the network
        return None
    try:
        return install.newest(install.parse_sums((fetch or install.fetch)(f'{install.RELEASES}/latest/download/{install.SUMS}')))
    except (OSError, ValueError):
        return None


def check(root, fetch=None):
    """The warning for this checkout ('' when nothing is stuck or it cannot be told)."""
    merged = merged_versions(root)
    if not merged:
        return ''
    published = latest_published(fetch)
    return message(unpublished(merged, published), published)


def cached(root, clock=time.time, fetch=None, background=True):
    """The last warning, refreshed in a background thread at most every CACHE_SECONDS: the dashboard never waits on GitHub."""
    with _lock:
        stale = clock() - _cache['at'] >= CACHE_SECONDS and not _cache['busy']
        if stale:
            _cache['busy'] = True

    def refresh():
        value = check(root, fetch)
        with _lock:
            _cache.update(at=clock(), value=value, busy=False)
    if stale:
        if background:
            threading.Thread(target=refresh, name='tutti-releases-stuck', daemon=True).start()
        else:
            refresh()
    return _cache['value'] or ''
