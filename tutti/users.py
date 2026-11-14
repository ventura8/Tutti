"""Who this Tutti works for: one household member (docs/HOUSEHOLD.md).

A user is a login: their main agent, accounts, readings, pacing and state are this login's config and state folder. A
single user is a household of one, so nothing here changes routing, pacing or the backlog until the user joins a
household (`tutti household join`, tutti/household.py) and shares a project or a machine.

- The name: `user.name` in the config, else the one saved by `tutti user name`, else the login's name.
- The id: random, made once in <state>/user.json; telemetry and the household view name a user only by its hash.
- `accounts.<name>.share_spare` (default false), or `tutti user share <account> on|off` (kept in user.json): room this
  account would lose at its reset may do the household's shared tasks. Never another user's account.
- The membership and its token: `~/.config/tutti/household.json`, mode 600 (a secret: never in the state folder, never
  printed).

Pure apart from the small JSON files read and written at the edge."""
import getpass
import hashlib
import json
import os
import re
import secrets

from . import config

FILE = 'user.json'
HOUSEHOLD_FILE = 'household.json'
NAME_RX = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}')
ROLES = ('alone', 'member', 'coordinator')
SECRET_MODE = 0o600


class UserError(Exception):
    """A user setting is wrong; the message names it and the fix."""


# ---- files


def _read(path):
    try:
        with open(path, encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path, data, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode or 0o644)
    with os.fdopen(fd, 'w', encoding='utf8') as f:
        json.dump(data, f, indent=1)
    if mode:
        os.chmod(tmp, mode)   # an existing tmp keeps its old mode with os.open: set it again
    os.replace(tmp, path)


def path(folder):
    return os.path.join(os.path.expanduser(folder), FILE)


def read(folder):
    return _read(path(folder))


def write(folder, **changes):
    """Saves name or share changes in user.json; a None value takes the key back to the config's."""
    data = read(folder)
    for k, v in changes.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    _write(path(folder), data)
    return data


def household_path(env=None):
    return os.path.join(config.xdg_tutti_dir(env), HOUSEHOLD_FILE)


def read_household(env=None):
    """The membership ({household, coordinator, user_id, name, token, role, ...}) or {} for a household of one."""
    return _read(household_path(env))


def write_household(data, env=None):
    """The membership, mode 600 (it holds the user's token)."""
    _write(household_path(env), data, SECRET_MODE)


def leave_household(env=None):
    try:
        os.remove(household_path(env))
        return True
    except FileNotFoundError:
        return False


# ---- identity


def id_hash(user_id):
    """The only way telemetry and other members' views name a user: 12 hex digits of SHA-256, never the name."""
    return hashlib.sha256(f'tutti-user:{user_id}'.encode()).hexdigest()[:12]


def login_name():
    try:
        return getpass.getuser()
    except (KeyError, OSError):   # no passwd entry (a container): the user names themself with `tutti user name`
        return 'me'


def name_error(name):
    if not isinstance(name, str) or not NAME_RX.fullmatch(name):
        return (f'the user name {name!r} will not do: 1 to 32 letters, digits, spaces, dots, dashes or underscores, '
                'starting with a letter or digit')
    return None


def user_id(folder):
    """This user's random id, made on first use and kept in user.json."""
    data = read(folder)
    if not data.get('id'):
        data = write(folder, id=secrets.token_hex(8))
    return data['id']


def name_of(cfg, saved):
    configured = ((cfg.get('user') or {}).get('name') or '').strip()
    return configured or saved.get('name') or login_name()


def shares(cfg, saved):
    """The accounts whose spare room the user shares with the household: user.json's switch over the config's key."""
    over = saved.get('share') or {}
    return sorted(n for n, a in cfg['accounts'].items()
                  if over.get(n, bool((a or {}).get('share_spare'))) and not (a or {}).get('costs_money'))


def role(membership):
    if not membership:
        return 'alone'
    return 'coordinator' if membership.get('role') == 'coordinator' else 'member'


def identity(cfg, env=None):
    """{name, id, hash, main, role, household, shares, weight}: who this Tutti works for."""
    folder = config.state_dir(cfg)
    saved = read(folder)
    uid = user_id(folder)
    membership = read_household(env)
    return {'name': name_of(cfg, saved), 'id': uid, 'hash': id_hash(uid), 'main': (cfg.get('main') or {}).get('agent'),
            'role': role(membership), 'household': membership.get('household') or None, 'shares': shares(cfg, saved),
            'weight': (cfg.get('user') or {}).get('weight', 1)}


def view(cfg, env=None):
    """The dashboard's and `tutti user`'s view: identity without the raw id, and the folders it lives in."""
    me = identity(cfg, env)
    me.pop('id')
    return {**me, 'config': cfg.get('_path'), 'state': config.state_dir(cfg), 'household_file': household_path(env)}


# ---- changes


def set_name(cfg, name):
    if name in (None, '', 'config'):
        write(config.state_dir(cfg), name=None)
        return
    why = name_error(name)
    if why:
        raise UserError(why)
    write(config.state_dir(cfg), name=name)


def set_share(cfg, account, on):
    if account not in cfg['accounts']:
        raise UserError(f'no account "{account}" in {cfg.get("_path")}: the accounts are {", ".join(cfg["accounts"]) or "(none)"}')
    if (cfg['accounts'][account] or {}).get('costs_money'):
        raise UserError(f'{account} costs money per use: its room is never shared (accounts.{account}.costs_money)')
    folder = config.state_dir(cfg)
    share = dict(read(folder).get('share') or {})
    share[account] = bool(on)
    write(folder, share=share)


# ---- config checks


def errors(cfg):
    """Config errors of `user:`, `household:` and `accounts.<n>.share_spare`, each naming the key and the fix."""
    errs = []
    u = cfg.get('user') or {}
    if not isinstance(u, dict):
        return [f'user is {u!r}: write {{name: <your name>, weight: 1}}, or remove the key']
    if u.get('name') and name_error(u['name']):
        errs.append(f'user.name: {name_error(u["name"])}')
    w = u.get('weight', 1)
    if isinstance(w, bool) or not isinstance(w, (int, float)) or not 0 < w <= 10:
        errs.append(f'user.weight is {w!r}: a number above 0 and up to 10 (1: an equal share of the household\'s machines)')
    h = cfg.get('household') or {}
    if not isinstance(h, dict):
        return errs + [f'household is {h!r}: write {{port: 4778}}, or remove the key']
    for key, lo, hi in (('port', 1, 65535), ('lease_minutes', 1, 1440), ('starve_minutes', 1, 1440),
                        ('share_spare_hours', 0, 168), ('timeout_seconds', 0.1, 60)):
        v = h.get(key, lo)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            errs.append(f'household.{key} is {v!r}: a number from {lo:g} to {hi:g}')
    for n, a in cfg['accounts'].items():
        v = (a or {}).get('share_spare', False)
        if not isinstance(v, bool):
            errs.append(f'accounts.{n}.share_spare is {v!r}: true or false (default false: its room is never shared)')
        elif v and (a or {}).get('costs_money'):
            errs.append(f'accounts.{n}.share_spare is true but it costs money per use: its room is never shared; remove share_spare')
    return errs
