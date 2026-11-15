"""The watchdog (docs/FAILOVER.md, *Light and self-healing*): restarts a crashed dashboard on the machine that serves, and
repairs corrupt state from the last good snapshot.

Every round, each state JSON file that parses is copied to `snapshots/` (only when it changed); one that no longer parses is
put back from its snapshot, and the broken copy kept beside it as `<name>.corrupt` for a look. Pure where it can be: the file
checks take a folder; restarting takes injected probes."""
import json
import os
import shutil

SNAPS = 'snapshots'
WATCHED = ('.json',)


def valid(path):
    try:
        with open(path, encoding='utf8') as f:
            json.load(f)
    except (OSError, ValueError):
        return False
    return True


def _same(a, b):
    try:
        return os.path.getsize(a) == os.path.getsize(b) and open(a, 'rb').read() == open(b, 'rb').read()
    except OSError:
        return False


def files(folder):
    try:
        # never failover*.json: an old term put back would undo the term protocol (tutti/failover.py fails closed instead)
        return sorted(n for n in os.listdir(folder) if n.endswith(WATCHED) and not n.startswith('failover')
                      and os.path.isfile(os.path.join(folder, n)))
    except OSError:
        return []


def round_files(folder):
    """Snapshots the good files and repairs the broken ones; returns lines for the log."""
    snaps = os.path.join(folder, SNAPS)
    os.makedirs(snaps, mode=0o700, exist_ok=True)
    out = []
    for n in files(folder):
        p, s = os.path.join(folder, n), os.path.join(snaps, n)
        if valid(p):
            if not _same(p, s):
                shutil.copy2(p, s + '.tmp')
                os.replace(s + '.tmp', s)
        elif os.path.exists(s) and valid(s):
            shutil.copy2(p, p + '.corrupt')
            shutil.copy2(s, p + '.tmp')
            os.replace(p + '.tmp', p)
            out.append(f'{n} was corrupt: put back from its last good snapshot (the broken copy is {n}.corrupt)')
        else:
            out.append(f'{n} is corrupt and has no good snapshot: look at it, or remove it so Tutti starts it again empty')
    return out


def round_services(serving, healthy, start):
    """Starts the dashboard when this machine serves and it does not answer. Returns lines."""
    if serving and not healthy():
        start()
        return ['the dashboard did not answer: started again']
    return []
