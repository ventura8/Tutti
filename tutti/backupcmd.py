"""`tutti backup [create|list|verify|restore]` (t246, docs/BACKUP.md): the config and the state folder in dated, verified
archives. Restore replaces the state folder (kept aside first) and needs --yes; stop the dashboard before it."""
import os
import time

from . import __version__, backup


def _size(n):
    return f'{n / 1048576:.1f} MB'


def cmd_backup(cfg, a, out=print):
    action = a.action or 'create'
    if action == 'list':
        rows = backup.listing(cfg)
        out('\n'.join(f'{b["name"]}  {_size(b["size"])}' for b in rows) or f'No backup in {backup.dest_of(cfg)} yet: '
                                                                            '`tutti backup` makes one.')
        return 0
    if action == 'verify':
        path = _pick(cfg, a.file)
        problems = backup.verify(path) if path else ['there is no backup to verify yet']
        out(f'{os.path.basename(path or "-")}: ' + ('sound: every file matches its checksum' if not problems
                                                     else 'NOT sound: ' + '; '.join(problems[:5])))
        return 0 if not problems else 1
    if action == 'restore':
        path = _pick(cfg, a.file)
        if not path:
            out('restore which? `tutti backup list` names them: tutti backup restore <file> --yes')
            return 2
        if not a.yes:
            out(f'This replaces the state folder (kept aside first) and the config with {os.path.basename(path)}. Stop the '
                'dashboard first (systemctl --user stop tutti-dashboard), then rerun with --yes.')
            return 2
        try:
            aside = backup.restore(cfg, path)
        except backup.BackupError as e:
            out(str(e))
            return 1
        out(f'Restored {os.path.basename(path)}. The state before it is kept at {aside}. Start the dashboard again.')
        return 0
    rec = backup.run(cfg, time.time(), __version__)
    if rec['ok']:
        out(f'{rec["path"]} ({_size(rec["size"])}), verified; the newest {(cfg.get("backup") or {}).get("keep", 14)} are kept')
        return 0
    out('backup failed: ' + '; '.join(rec['problems']))
    return 1


def _pick(cfg, name):
    rows = backup.listing(cfg)
    if not name:
        return rows[0]['path'] if rows else None
    if os.path.isfile(name):
        return name
    return next((b['path'] for b in rows if b['name'] == name), None)


def add_parser(add):
    s = add('backup', cmd_backup, 'back up the config and the state folder (create, the default), list them, verify one, or '
            'restore one (docs/BACKUP.md)')
    s.add_argument('action', nargs='?', choices=['create', 'list', 'verify', 'restore'])
    s.add_argument('file', nargs='?', help='verify, restore: a backup name from `tutti backup list` or a path (default: newest)')
    s.add_argument('--yes', action='store_true', help='restore: really replace the state folder and the config')
