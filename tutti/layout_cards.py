"""Dashboard card layout (t447): order and sizes the owner chooses for the big cards on #dashboard.

Pure: clamp, normalize, apply, reset. The dashboard persists a copy per signed-in user under
`<state>/dashboard-layout.json` (not git). Limits keep a card usable on a phone and on a wide screen."""
import json
import os

from . import durable

FILE = 'dashboard-layout.json'

# Direct children of `.grid` that the owner may move and resize (stable ids).
DEFAULT_ORDER = (
    'usage', 'machines', 'attention', 'household', 'jobs-stack', 'side-stack',
    'autopilot', 'work-browser', 'projects', 'security', 'scouts', 'connect',
    'outside', 'where', 'setup', 'controls', 'selfimprove', 'aiassist', 'insights',
    'my-schedule', 'my-questions', 'my-github', 'jobs', 'vpn', 'users',
)

# Column span on the 12-column desktop grid, and min height in CSS pixels.
DEFAULT_SIZES = {
    'usage': {'cols': 12, 'height': 0},
    'machines': {'cols': 12, 'height': 0},
    'attention': {'cols': 12, 'height': 0},
    'household': {'cols': 12, 'height': 0},
    'jobs-stack': {'cols': 7, 'height': 0},
    'side-stack': {'cols': 5, 'height': 0},
    'autopilot': {'cols': 12, 'height': 0},
    'work-browser': {'cols': 12, 'height': 0},
    'projects': {'cols': 12, 'height': 0},
    'security': {'cols': 12, 'height': 0},
    'scouts': {'cols': 12, 'height': 0},
    'connect': {'cols': 12, 'height': 0},
    'outside': {'cols': 12, 'height': 0},
    'where': {'cols': 12, 'height': 0},
    'setup': {'cols': 12, 'height': 0},
    'controls': {'cols': 12, 'height': 0},
    'selfimprove': {'cols': 12, 'height': 0},
    'aiassist': {'cols': 12, 'height': 0},
    'insights': {'cols': 12, 'height': 0},
    'my-schedule': {'cols': 12, 'height': 0},
    'my-questions': {'cols': 12, 'height': 0},
    'my-github': {'cols': 12, 'height': 0},
    'jobs': {'cols': 12, 'height': 0},
    'vpn': {'cols': 12, 'height': 0},
    'users': {'cols': 12, 'height': 0},
}

COLS_MIN, COLS_MAX = 4, 12
HEIGHT_MIN, HEIGHT_MAX = 140, 1200   # 0 height means "content size" (no fixed min-height)


def known_ids():
    return list(DEFAULT_ORDER)


def clamp_cols(cols):
    try:
        n = int(cols)
    except (TypeError, ValueError):
        return COLS_MAX
    return max(COLS_MIN, min(COLS_MAX, n))


def clamp_height(height):
    """0 = auto (content). Any other value is clamped to HEIGHT_MIN..HEIGHT_MAX."""
    try:
        n = int(height)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    return max(HEIGHT_MIN, min(HEIGHT_MAX, n))


def clamp_size(size):
    size = size if isinstance(size, dict) else {}
    return {'cols': clamp_cols(size.get('cols', COLS_MAX)), 'height': clamp_height(size.get('height', 0))}


def normalize_order(order, known=None):
    """Keep known ids in the given order; append any missing ones in default order; drop unknowns."""
    known = list(known or DEFAULT_ORDER)
    seen, out = set(), []
    for name in order or []:
        if name in known and name not in seen:
            out.append(name)
            seen.add(name)
    for name in known:
        if name not in seen:
            out.append(name)
    return out


def normalize_sizes(sizes, known=None):
    known = list(known or DEFAULT_ORDER)
    base = {k: dict(DEFAULT_SIZES.get(k) or {'cols': 12, 'height': 0}) for k in known}
    if not isinstance(sizes, dict):
        return {k: clamp_size(v) for k, v in base.items()}
    out = {}
    for k in known:
        out[k] = clamp_size(sizes[k] if k in sizes else base[k])
    return out


def apply_layout(saved=None):
    """Sanitize a saved layout into {order, sizes}. None or junk → defaults."""
    saved = saved if isinstance(saved, dict) else {}
    return {
        'order': normalize_order(saved.get('order')),
        'sizes': normalize_sizes(saved.get('sizes')),
    }


def reset_layout():
    return apply_layout(None)


def path(folder):
    return os.path.join(os.path.expanduser(folder), FILE)


def read(folder):
    try:
        with open(path(folder), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def for_user(folder, user_key):
    """The sanitized layout for one user key (login hash or 'local')."""
    data = read(folder)
    return apply_layout(data.get(user_key) if user_key else None)


def save_user(folder, user_key, layout):
    """Write one user's layout; returns the sanitized layout that was stored."""
    if not user_key:
        raise ValueError('user_key is empty: name the signed-in user (or "local") before saving a layout')
    clean = apply_layout(layout)
    folder = os.path.expanduser(folder)
    os.makedirs(folder, exist_ok=True)
    data = read(folder)
    data[user_key] = clean
    durable.dump_json(path(folder), data, indent=1)
    return clean
