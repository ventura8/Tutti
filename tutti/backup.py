"""Backups of Tutti itself (t246, docs/BACKUP.md): the config and the state folder in one dated archive, verified as it is
made, the newest `backup.keep` kept, daily from the dashboard by default.

What goes in: the config file and the state folder (backlog, scouts, users, chat, usage logs...). What stays out: work
copies that are rebuilt (the patch worktrees, test leftovers, locks) and every secret: secrets live in their own files under
~/.config/tutti and never in the state folder, so no secret is in an archive (Tutti does not store or send credentials).
Each archive carries MANIFEST.json: every file's size and SHA-256, so `verify` reads it back as a test restore would.

Pure: `members`, `manifest_ok`, `prune`; `create`, `verify`, `restore` and `record` do the I/O."""
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import time

PREFIX, SUFFIX = 'tutti-backup-', '.tar.gz'
SKIP_DIRS = ('patches', 'tests', 'quiet', '__pycache__')   # rebuilt work copies and leftovers, not the household's data
SKIP_SUFFIXES = ('.lock', '.tmp')
RECORD = 'backup.json'


class BackupError(Exception):
    pass


def dest_of(cfg):
    b = cfg.get('backup') or {}
    return os.path.expanduser(b.get('dest') or '~/.local/share/tutti/backups')


def members(state, walk=os.walk):
    """[relative path] of the state folder's files that go into a backup, sorted."""
    out = []
    for folder, dirs, files in walk(state):
        rel = os.path.relpath(folder, state)
        dirs[:] = [d for d in dirs if not (rel == '.' and d in SKIP_DIRS) and d != '__pycache__']
        for f in files:
            if not f.endswith(SKIP_SUFFIXES):
                out.append(os.path.normpath(os.path.join(rel, f)))
    return sorted(out)


def _sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def create(cfg, now=None, version=''):
    """Writes one archive into the destination and returns its path. Raises BackupError naming the step and the fix."""
    from . import config
    now = now or time.time()
    dest, state, cfg_path = dest_of(cfg), config.state_dir(cfg), cfg.get('_path')
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        raise BackupError(f'backup: cannot make {dest} ({e}): set backup.dest to a folder you may write (a mounted NAS works)') \
            from e
    name = PREFIX + time.strftime('%Y%m%d-%H%M%S', time.localtime(now)) + SUFFIX
    files, entries = {}, []
    if cfg_path and os.path.isfile(cfg_path):
        entries.append((cfg_path, 'config.yaml'))
    entries += [(os.path.join(state, rel), os.path.join('state', rel)) for rel in members(state)]
    tmp = os.path.join(dest, '.' + name + '.tmp')
    try:
        with tarfile.open(tmp, 'w:gz') as tar:
            for src, arc in entries:
                try:
                    files[arc] = {'size': os.path.getsize(src), 'sha256': _sha(src)}
                    tar.add(src, arcname=arc, recursive=False)
                except OSError:
                    files.pop(arc, None)   # a file that went away while the backup ran (a finished job's log)
            doc = json.dumps({'version': version, 'created': now, 'files': files}, indent=1).encode()
            info = tarfile.TarInfo('MANIFEST.json')
            info.size, info.mtime = len(doc), int(now)
            tar.addfile(info, io.BytesIO(doc))
        os.replace(tmp, os.path.join(dest, name))
    except OSError as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise BackupError(f'backup: writing {name} into {dest} failed ({e}): check the free space and that the folder is '
                          'mounted, then retry') from e
    return os.path.join(dest, name)


def manifest_ok(manifest, seen):
    """[problems]: a file the manifest names that is missing from the archive or differs from it."""
    out = []
    for arc, want in (manifest.get('files') or {}).items():
        got = seen.get(arc)
        if got is None:
            out.append(f'{arc} is missing')
        elif got != want['sha256']:
            out.append(f'{arc} differs from its checksum')
    return out


def verify(path):
    """[problems] reading the whole archive back as a restore would (every file against MANIFEST.json); [] when sound."""
    try:
        with tarfile.open(path, 'r:gz') as tar:
            manifest, seen = None, {}
            for m in tar:
                f = tar.extractfile(m) if m.isfile() else None
                if f is None:
                    continue
                data = f.read()
                if m.name == 'MANIFEST.json':
                    manifest = json.loads(data)
                else:
                    seen[m.name] = hashlib.sha256(data).hexdigest()
    except (OSError, tarfile.TarError, ValueError) as e:
        return [f'the archive cannot be read ({e})']
    if not isinstance(manifest, dict):
        return ['it has no MANIFEST.json: not a Tutti backup']
    return manifest_ok(manifest, seen)


def listing(cfg):
    """[{name, path, size, at}] of the destination's backups, newest first."""
    dest = dest_of(cfg)
    try:
        names = [n for n in os.listdir(dest) if n.startswith(PREFIX) and n.endswith(SUFFIX)]
    except OSError:
        return []
    out = [{'name': n, 'path': os.path.join(dest, n), 'size': os.path.getsize(os.path.join(dest, n)),
            'at': os.path.getmtime(os.path.join(dest, n))} for n in names]
    return sorted(out, key=lambda b: b['name'], reverse=True)


def prune(names, keep):
    """The archive names to delete: all but the newest `keep` (names sort by their date)."""
    keep = max(1, int(keep))
    return sorted(names, reverse=True)[keep:]


def run(cfg, now=None, version=''):
    """The daily backup: create, verify, prune, record. Returns the record; never raises (the record says what failed)."""
    now = now or time.time()
    rec = {'at': now, 'ok': False, 'path': '', 'size': 0, 'problems': []}
    try:
        path = create(cfg, now, version)
        rec.update(path=path, size=os.path.getsize(path), problems=verify(path))
        rec['ok'] = not rec['problems']
        if rec['ok']:
            for name in prune([b['name'] for b in listing(cfg)], (cfg.get('backup') or {}).get('keep', 14)):
                os.remove(os.path.join(dest_of(cfg), name))
    except BackupError as e:
        rec['problems'] = [str(e)]
    record(cfg, rec)
    return rec


def record(cfg, rec):
    from . import config
    try:
        with open(os.path.join(config.state_dir(cfg), RECORD), 'w', encoding='utf8') as f:
            json.dump(rec, f)
    except OSError:
        pass


def load(cfg):
    from . import config
    try:
        with open(os.path.join(config.state_dir(cfg), RECORD), encoding='utf8') as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None


def restore(cfg, path, now=None):
    """Puts an archive back: verified first, unpacked into a staging folder (no path may leave it), then the current state
    folder is kept aside as <state>.before-restore-<date> and the config backed up before it is replaced. Returns what was
    kept aside. Raises BackupError on any problem, changing nothing."""
    from . import config
    problems = verify(path)
    if problems:
        raise BackupError(f'backup: {os.path.basename(path)} did not verify ({"; ".join(problems[:3])}): restore another one')
    state = config.state_dir(cfg)
    stage = tempfile.mkdtemp(prefix='.restore-', dir=os.path.dirname(state.rstrip('/')) or '.')
    try:
        with tarfile.open(path, 'r:gz') as tar:
            tar.extractall(stage, filter='data')   # 'data': no absolute paths, no .., no links out, no devices
        stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now or time.time()))
        aside = f'{state.rstrip("/")}.before-restore-{stamp}'
        if os.path.exists(state):
            os.replace(state, aside)
        os.replace(os.path.join(stage, 'state'), state)
        cfg_path = cfg.get('_path')
        if cfg_path and os.path.isfile(os.path.join(stage, 'config.yaml')):
            if os.path.isfile(cfg_path):
                shutil.copy2(cfg_path, cfg_path + f'.before-restore-{stamp}')
            shutil.copy2(os.path.join(stage, 'config.yaml'), cfg_path)
        return aside
    except (OSError, tarfile.TarError) as e:
        raise BackupError(f'backup: restoring {os.path.basename(path)} failed ({e}); the state folder is unchanged unless '
                          f'{state}.before-restore-* exists') from e
    finally:
        shutil.rmtree(stage, ignore_errors=True)
