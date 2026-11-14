"""Room on the disks Tutti writes to: the temp folder (/tmp) and the work disk (the worktrees and the state folder).
Under 10 % free file slots (inodes), or 10 % free space with less than ROOMY_GB left, `tutti doctor` and the dashboard
warn and no new work starts: on 2026-11-15 /tmp ran out of file slots and every command on the laptop failed. Pure over
`statvfs`, so tests pass a fake."""
import os
import tempfile

from . import config

LOW_PCT = 10
ROOMY_GB = 20   # a big disk under 10 % free with this much left still holds a day's work: space is low only under both


def paths(cfg):
    """{label: path}: the temp folder and the work disk, each once per disk."""
    out = {'the temp folder': tempfile.gettempdir()}
    for label, key in (('the work disk', 'worktrees'), ('the state folder', 'state')):
        p = config.expand((cfg.get('project') or {}).get(key) or '')
        while p and not os.path.exists(p):
            p = os.path.dirname(p)
        if p:
            out.setdefault(label, p)
    return out


def reading(path, statvfs=os.statvfs):
    """(free space %, free file slots %, device, free GB) of the disk holding `path`, or None when it cannot be read."""
    try:
        s = statvfs(path)
        dev = os.stat(path).st_dev
    except OSError:
        return None
    space = 100.0 * s.f_bavail / s.f_blocks if s.f_blocks else 100.0
    slots = 100.0 * s.f_favail / s.f_files if s.f_files else 100.0   # 0 files: the filesystem has no inode limit
    return space, slots, dev, s.f_bavail * s.f_frsize / 1e9


def problems(cfg, statvfs=os.statvfs):
    """Plain sentences, one per disk under LOW_PCT % free space or free file slots: what, why it matters, the fix."""
    out, seen = [], set()
    for label, path in paths(cfg).items():
        r = reading(path, statvfs)
        if not r or r[2] in seen:
            continue
        seen.add(r[2])
        space, slots, _, free_gb = r
        if slots < LOW_PCT:
            out.append(f'{label} ({path}) has only {slots:.0f} % of its file slots left: soon no new file can be made there '
                       'and every command fails, so Tutti starts no new work. Fix: delete old folders there (left-over test '
                       'and build folders first), then Tutti resumes by itself.')
        if space < LOW_PCT and free_gb < ROOMY_GB:
            out.append(f'{label} ({path}) has only {space:.0f} % free space ({free_gb:.1f} GB): Tutti starts no new work. '
                       'Fix: free space there '
                       '(old worktrees, caches, logs), then Tutti resumes by itself.')
    return out
