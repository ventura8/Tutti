"""My schedule: each signed-in user's own working days, hours and away or vacation mode (docs/AUTH.md, *My schedule*).

Every user, viewers included, may change their own; an admin may change anyone's. Kept per Google `sub` in
<state>/schedules.json (mode 600), checked with the same rules as `pacing.schedule` and `pacing.mode` (tutti/owner.py).
The household reads a member's with `read(state_dir, sub)` and paces that member's own main agent with it."""
import os

from . import auth, owner

FILE = 'schedules.json'
KEYS = ('mode', 'days', 'window', 'hours')


def _path(folder):
    return os.path.join(folder, FILE)


def read(folder, sub, defaults=None):
    """{mode, days, window, hours} of this user: their own changes over `defaults` (the config's pacing)."""
    base = {**owner.DEFAULT, 'mode': 'auto', **(defaults or {})}
    own = (auth._read(_path(folder)).get(sub) or {}) if sub else {}
    return {k: own.get(k, base.get(k)) for k in KEYS}


def change(folder, sub, data, defaults=None):
    """(status, answer): the user's schedule changed after the config's own checks; null takes a key back to the default."""
    keys = [k for k in KEYS if isinstance(data, dict) and k in data]
    if not sub or not keys:
        return 400, {'error': 'send {"mode": "auto|active|away|vacation-until-YYYY-MM-DD"} and/or {"days": [...], '
                              '"window": "HH-HH", "hours": n} as JSON'}
    current = read(folder, sub, defaults)
    trial = {**current, **{k: data[k] for k in keys if data[k] is not None}}
    errs = owner.schedule_errors({'schedule': {k: trial[k] for k in ('days', 'window', 'hours')}, 'mode': trial['mode']})
    if errs:
        return 400, {'error': '; '.join(e.replace('pacing.', 'my ') for e in errs)}
    with auth.locked(folder):
        all_ = auth._read(_path(folder))
        mine = {**(all_.get(sub) or {}), **{k: data[k] for k in keys}}
        all_[sub] = {k: v for k, v in mine.items() if v is not None}
        auth._write(_path(folder), all_)
    return 200, {'ok': True, 'schedule': read(folder, sub, defaults)}
