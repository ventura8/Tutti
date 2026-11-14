"""Files for other machines, the pure part: which paths are secrets, what an item holds (its manifest), where it lands,
what leaves a full cache first and how far a transfer is. The copying itself is in transfer.py."""
import fnmatch
import hashlib
import json
import os
import re
from collections import namedtuple

GB = 1024 ** 3
MB = 1024 ** 2
META = '.tutti-files'          # beside the items: <name>.sha256 (the manifest) and <name>.json (size, files, when)
PARTIAL = '.tutti-partial'     # rsync's resumable partial files, inside the destination
PART_SUFFIX = '.tutti-part'    # the stream fallback's partial file, beside the final one
DEFAULTS = {'cache': '~/.cache/tutti/files', 'budget_gb': 50, 'keep_free_gb': 2, 'bwlimit_kbps': 0, 'compress': False, 'refuse': []}
# Never copied, whatever the caller asks: names first (case-insensitive), then folders anywhere in the path.
SECRET_NAMES = ('.env*', '*.env', '*token*', '*secret*', '*service-account*.json', '*service_account*.json',
                '*credentials*.json', '*.pem', '*.key', '*.p12', '*.pfx', 'id_rsa*', 'id_ed25519*', 'id_ecdsa*', 'id_dsa*',
                '.netrc', '.npmrc', '.pypirc', '.git-credentials', 'known_hosts', 'authorized_keys')
SECRET_DIRS = ('.ssh', '.gnupg', '.aws', '.azure', '.kube', '.docker')
# A Google service-account key is a .json with any name; a small .json that holds a private key is refused too.
KEY_JSON = re.compile(rb'"(?:private_key|private_key_id|client_secret|refresh_token)"\s*:')
KEY_JSON_MAX = 256 * 1024
# rsync --info=progress2: "  1,234,567  45%   10.00MB/s    0:01:02"
RSYNC_PROGRESS = re.compile(r'([\d,]+)\s+(\d{1,3})%')

Entry = namedtuple('Entry', 'name bytes used_at')
Item = namedtuple('Item', 'name path is_dir files bytes dirs')   # files: [(relpath, size, sha256)], dirs: [relpath], relative to the parent
PIN_HOURS = 24   # a job's pin keeps its inputs from eviction and replacement; an older pin (a crashed job) no longer counts


def settings(cfg, machine):
    """The files settings for a machine: the config's `files:` with the machine's own `files:` over it."""
    out = {**DEFAULTS, **(cfg.get('files') or {})}
    out.update((machine or {}).get('files') or {})
    return out


def errors(cfg):
    """Config problems in `files:` and `machines.<m>.files:`, each naming the key and the fix."""
    errs = []
    blocks = [('files', cfg.get('files'))] + [(f'machines.{n}.files', m.get('files')) for n, m in cfg['machines'].items()]
    for key, f in blocks:
        if f is None:
            continue
        if not isinstance(f, dict):
            errs.append(f'{key} is {f!r}: write it as a mapping, e.g. {{cache: ~/.cache/tutti/files, budget_gb: 50}}')
            continue
        errs += _number_errors(key, f)
        if 'cache' in f and (not isinstance(f['cache'], str) or not f['cache'].strip()):
            errs.append(f'{key}.cache is {f["cache"]!r}: give a folder on the machine, e.g. ~/.cache/tutti/files')
        if not isinstance(f.get('refuse', []), list):
            errs.append(f'{key}.refuse is {f["refuse"]!r}: a list of extra file-name patterns never to copy, e.g. ["*.sqlite"]')
    return errs


def _number_errors(key, f):
    errs = []
    for k, low in (('budget_gb', 0), ('keep_free_gb', -1), ('bwlimit_kbps', -1)):
        v = f.get(k)
        if k in f and (isinstance(v, bool) or not isinstance(v, (int, float)) or v <= low):
            errs.append(f'{key}.{k} is {v!r}: use a number above {low}' + (' (0 = no limit)' if k == 'bwlimit_kbps' else ''))
    return errs


# ---- secrets


def secret_reason(path, extra=()):
    """Why `path` must never leave this computer, or None. Looks at the name and the folders above it, for the path as given
    and for where a symlink leads; a small .json is also scanned for a private key (its bytes are only matched, never shown)."""
    real = os.path.realpath(path)
    return _secret_reason(path, extra) or (_secret_reason(real, extra) if real != os.path.abspath(path) else None)


def _secret_reason(path, extra):
    parts = os.path.normpath(os.path.abspath(path)).split(os.sep)
    hidden = [d for d in parts[:-1] if d.lower() in SECRET_DIRS]
    if hidden:
        return f'it is inside {hidden[-1]}/, a folder of keys and credentials'
    base = parts[-1].lower()
    hit = next((p for p in (*SECRET_NAMES, *extra) if fnmatch.fnmatch(base, p.lower())), None)
    if hit:
        return f'its name matches "{hit}", a pattern of secret files'
    if base.endswith('.json') and _json_holds_key(path):
        return 'it is a .json that holds a private key or client secret (a service-account key)'
    return None


def _json_holds_key(path):
    try:
        if os.path.getsize(path) > KEY_JSON_MAX:
            return False
        with open(path, 'rb') as f:
            return bool(KEY_JSON.search(f.read()))
    except OSError:
        return False


def walk(path):
    """Every regular file of an item (the path itself for a file), sorted, as absolute paths. Symlinks are not followed."""
    if not os.path.isdir(path):
        return [path]
    out = []
    for root, dirs, names in os.walk(path):
        dirs.sort()
        out += [os.path.join(root, n) for n in sorted(names) if os.path.isfile(os.path.join(root, n))
                and not os.path.islink(os.path.join(root, n))]
    return out


def secrets_in(paths, extra=()):
    """[(path, reason)] for every secret among the paths and inside the folders among them."""
    found = []
    for p in paths:
        top = secret_reason(p, extra) if os.path.isdir(p) else None
        if top:
            found.append((p, top))
            continue
        found += [(f, r) for f in walk(p) if (r := secret_reason(f, extra))]
    return found


# ---- manifests


def sha256(path, cache=None):
    """The file's SHA-256; `cache` (a dict keyed by path, size and mtime) saves reading a big archive again."""
    st = os.stat(path)
    key = f'{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}'
    if cache is not None and key in cache:
        return cache[key]
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(4 * MB), b''):
            h.update(block)
    if cache is not None:
        cache[key] = h.hexdigest()
    return h.hexdigest()


def item_of(path, cache=None):
    """One item to send: a file or a folder, with every file's path relative to the item's parent, size and SHA-256."""
    path = os.path.abspath(path.rstrip(os.sep) or os.sep)
    parent = os.path.dirname(path)
    files = [(os.path.relpath(f, parent), os.path.getsize(f), sha256(f, cache)) for f in walk(path)]
    dirs = [os.path.relpath(r, parent) for r, _, _ in os.walk(path)] if os.path.isdir(path) else []
    return Item(os.path.basename(path), path, os.path.isdir(path), files, sum(s for _, s, _ in files), sorted(dirs))


def manifest(item):
    """The item's sha256sum file: what `sha256sum -c` checks on the machine. Folders, empty ones too, are listed as
    `dir  <path>/` lines in the digest only (meta_json), so a changed folder layout is a changed item."""
    return ''.join(f'{h}  {rel}\n' for rel, _, h in item.files)


def identity(item):
    """What makes two sends of an item the same: its files' checksums and its folders."""
    return manifest_digest(manifest(item) + ''.join(f'dir  {d}/\n' for d in item.dirs))


def extras(listed, item):
    """Files on the machine under the item that the item no longer has (a stream copy does not delete like rsync --delete)."""
    keep = {rel for rel, _, _ in item.files}
    return sorted(p for p in listed if p and p not in keep and not p.endswith(PART_SUFFIX) and f'/{PARTIAL}/' not in f'/{p}')


def manifest_digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def meta_json(item, now, origin):
    return json.dumps({'name': item.name, 'bytes': item.bytes, 'files': len(item.files), 'sent_at': now, 'from': origin,
                       'manifest': identity(item)})


# ---- the cache's budget


def plan_eviction(entries, budget, incoming, keep=()):
    """Which cached items to remove, least recently used first, so that what stays plus `incoming` fits `budget` (bytes).
    Returns (names, fits): fits is False when even an empty cache would not hold it."""
    keep = set(keep)
    used = sum(e.bytes for e in entries)
    out = []
    for e in sorted((e for e in entries if e.name not in keep), key=lambda e: (e.used_at or 0, e.name)):
        if used + incoming <= budget:
            break
        out.append(e.name)
        used -= e.bytes
    return out, used + incoming <= budget


def parse_listing(text):
    """{name: (Entry, meta)} from the machine's `ITEM<tab>name<tab>mtime<tab>json` lines; other and broken lines are skipped."""
    out = {}
    for line in text.splitlines():
        parts = line.split('\t', 3)
        if len(parts) != 4 or parts[0] != 'ITEM':
            continue
        try:
            meta = json.loads(parts[3])
            out[parts[1]] = (Entry(parts[1], int(meta.get('bytes') or 0), float(parts[2])), meta)
        except (ValueError, TypeError, AttributeError):
            continue
    return out


def valid_name(name):
    """An item's name is one path component that is not ours: no slash, not . or .., not the meta or partial folders."""
    return bool(name) and '/' not in name and name not in ('.', '..', META, PARTIAL) and not name.endswith(PART_SUFFIX)


# ---- progress


def rsync_progress(chunk):
    """Bytes done from the last rsync --info=progress2 figure in a chunk of its output, or None."""
    hits = RSYNC_PROGRESS.findall(chunk)
    return int(hits[-1][0].replace(',', '')) if hits else None


def progress_line(done, total):
    """What a transfer writes to <log>.progress: done and total in MB, the unit after them (the dashboard shows it)."""
    t = max(1, round(total / MB))
    return f'{t if done >= total else min(t, round(done / MB))} {t} MB\n'


def human(n):
    for unit, size in (('GB', GB), ('MB', MB), ('KB', 1024)):
        if n >= size:
            return f'{n / size:.1f} {unit}'
    return f'{n} B'


def parse_pins(text):
    """The items a running job has pinned (`PIN<tab>name` lines)."""
    return {line.split('\t', 1)[1] for line in text.splitlines() if line.startswith('PIN\t') and '\t' in line}


def parse_number(text, key):
    return next((int(x[len(key) + 1:]) for x in text.splitlines() if x.startswith(key + '=') and x[len(key) + 1:].isdigit()), None)
