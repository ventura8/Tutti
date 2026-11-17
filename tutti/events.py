"""A shared event hook: one place where Tutti says something happened that a person or another part may act on.

`emit(cfg, kind, **data)` appends the event to `<state>/events.jsonl` (kept short) and calls every hook that subscribed to
that kind (or to every kind, `'*'`). The continuity of a main session (tutti/continuity.py) emits `continuity.orphaned`,
`continuity.takeover`, `continuity.handback` and `needs-attention` (an ask-first action a conductor stopped at); the dashboard's
Needs attention list subscribes. A hook that fails never stops the emitter."""
import json
import os
import threading
import time

from . import config

FILE = 'events.jsonl'
KEEP = 500           # the newest events kept in the file
HOOKS = {}           # kind -> [callable(event)]
_lock = threading.Lock()


def subscribe(kind, fn):
    """Calls `fn(event)` for every event of `kind` (`'*'`: every kind). Returns `fn`, so it can be used as a decorator."""
    HOOKS.setdefault(kind, []).append(fn)
    return fn


def unsubscribe(kind, fn):
    if fn in HOOKS.get(kind, []):
        HOOKS[kind].remove(fn)


def path_of(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


def read(cfg, kinds=None, limit=50):
    """The newest events (newest last), only `kinds` when given."""
    try:
        with open(path_of(cfg), encoding='utf8') as f:
            rows = [json.loads(x) for x in f if x.strip()]
    except (OSError, ValueError):
        return []
    rows = [r for r in rows if isinstance(r, dict) and (not kinds or r.get('kind') in kinds)]
    return rows[-limit:]


def emit(cfg, kind, now=None, **data):
    """Records the event and calls its hooks. Returns the event."""
    event = {'kind': kind, 'at': now or time.time(), **data}
    p = path_of(cfg)
    with _lock:
        try:
            with open(p, encoding='utf8') as f:
                lines = [x for x in f if x.strip()]
        except OSError:
            lines = []
        lines = [*lines[-(KEEP - 1):], json.dumps(event) + '\n']
        tmp = f'{p}.{os.getpid()}.{threading.get_ident()}.tmp'
        with open(tmp, 'w', encoding='utf8') as f:
            f.writelines(lines)
        os.replace(tmp, p)
    for fn in [*HOOKS.get(kind, []), *HOOKS.get('*', [])]:
        try:
            fn(event)
        except Exception:   # a subscriber's bug never stops the one who emits
            continue
    return event
