"""Pure helpers for dashboard card layout customization."""

CARD_IDS = (
    'usage',
    'machines',
    'attention',
    'household',
    'running',
    'queued',
    'main',
    'workers',
    'autopilot',
    'work-browser',
    'projects',
    'security',
    'scouts',
    'connect',
    'outside',
    'where',
    'setup',
    'controls',
    'selfimprove',
    'aiassist',
    'insights',
    'my-schedule',
    'my-questions',
    'my-github',
    'jobs',
    'vpn',
    'users',
)

LIMITS = {
    'default': {
        'min_width_cols': 1,
        'max_width_cols': 12,
        'min_height_px': 120,
        'max_height_px': 1200,
    },
}


def _int_or_none(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def clamp_size(width_cols, height_px, limits):
    """Clamp width/height to configured limits; fall back to defaults for bad values."""
    w = _int_or_none(width_cols)
    h = _int_or_none(height_px)
    min_w = int(limits.get('min_width_cols', LIMITS['default']['min_width_cols']))
    max_w = int(limits.get('max_width_cols', LIMITS['default']['max_width_cols']))
    min_h = int(limits.get('min_height_px', LIMITS['default']['min_height_px']))
    max_h = int(limits.get('max_height_px', LIMITS['default']['max_height_px']))
    if w is None:
        w = min_w
    if h is None:
        h = min_h
    w = max(min_w, min(max_w, w))
    h = max(min_h, min(max_h, h))
    return w, h


def normalize_order(order, known_ids):
    """Keep known ids in saved order, then append known ids that are missing."""
    known = tuple(known_ids)
    if not isinstance(order, list):
        order = []
    out = []
    for card_id in order:
        if card_id in known and card_id not in out:
            out.append(card_id)
    out.extend(card_id for card_id in known if card_id not in out)
    return out


def apply_layout(default_order, saved):
    """Return a sanitized layout payload with order and clamped card sizes."""
    known = tuple(default_order)
    payload = saved if isinstance(saved, dict) else {}
    order = normalize_order(payload.get('order'), known)
    raw_sizes = payload.get('sizes')
    raw_sizes = raw_sizes if isinstance(raw_sizes, dict) else {}
    sizes = {}
    for card_id, size in raw_sizes.items():
        if card_id not in known or not isinstance(size, dict):
            continue
        limits = {**LIMITS['default'], **(LIMITS.get(card_id) or {})}
        width, height = clamp_size(size.get('width_cols'), size.get('height_px'), limits)
        sizes[card_id] = {'width_cols': width, 'height_px': height}
    return {'order': order, 'sizes': sizes}


def reset_layout():
    """Return layout defaults for a fresh or reset dashboard customization."""
    return {'order': list(CARD_IDS), 'sizes': {}}
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
