"""The dashboard's Settings tab: every config key with its value, its default and where it comes from (GET /api/settings),
and one change at a time saved into the owner's tutti.config.yaml (POST /api/settings), comments and order kept.

A save is checked by the config's own rules before anything is written, the file is backed up first, and the dashboard
reloads the config so the change applies at once (a few keys say "takes effect on restart"). A dashboard or CLI switch
kept in the state folder (the autopilot's toggle, the compaction %, the pacing mode) shows as the value in effect; saving
the key here writes the file and takes that switch back, so the value lives in one place.

Secrets are never read into the answer: a DSN, token or key shows only as set or not set, with the `tutti … setup`
command that sets it."""
import copy
import glob
import hashlib
import json
import math
import os
import re
import shutil
import time

import yaml

from . import autopilot, config, configone, controls, groksetup, owner, telemetry, yamledit
from .settings_schema import DAYS, ENTRIES, ENTRY_DEFAULTS, FIELDS, MANAGED, MISSING, RESTART, SECTIONS

SECRET_RX = re.compile(r'(dsn|token|secret|password|passwd|api_?key|private_?key|credential)', re.I)
HIDDEN = '(hidden: a secret)'
BACKUPS = 'config-backups'
KEEP_BACKUPS = 20
MAX_DEPTH = 8
COLLECTION_FIELDS = {'scouts': ['watches']}   # a nested collection drawn as one YAML field of its entry
FILL_ROOM = 'autopilot.fill_room'
PAUSABLE = {'machines': 'machine', 'devices': 'device', 'workers': 'worker', 'accounts': 'account'}


class SettingsError(Exception):
    def __init__(self, status, message, errors=None):
        super().__init__(message)
        self.status, self.errors = status, errors or []


def is_secret(key):
    return isinstance(key, str) and not key.endswith('_env') and bool(SECRET_RX.search(key))


def has_secret(value):
    if isinstance(value, dict):
        return any(is_secret(k) or has_secret(v) for k, v in value.items())
    if isinstance(value, list):
        return any(has_secret(v) for v in value)
    return False


def redact(value):
    if isinstance(value, dict):
        return {k: (HIDDEN if is_secret(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def get(data, path, default=MISSING):
    for k in path:
        if not isinstance(data, dict) or k not in data:
            return default
        data = data[k]
    return data


def etag(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ---- the schema of one key

def generic(dotted):
    """The schema key of a path: an entry's own name becomes `*` (accounts.claude.share -> accounts.*.share)."""
    parts = dotted.split('.')
    out, i = [], 0
    while i < len(parts):
        out.append(parts[i])
        if '.'.join(out) in ENTRIES and i + 1 < len(parts):
            out.append('*')
            i += 1
        i += 1
    return '.'.join(out)


def flat_defaults(d=None, prefix=''):
    """{dotted key: default} of every leaf of config.DEFAULTS (an entry collection and an empty mapping are leaves)."""
    out = {}
    for k, v in (config.DEFAULTS if d is None else d).items():
        key = f'{prefix}{k}'
        if isinstance(v, dict) and v and key not in ENTRIES and key not in FIELDS:
            out.update(flat_defaults(v, key + '.'))
        else:
            out[key] = v
    return out


DEFAULTS_FLAT = flat_defaults()


def infer_control(v):
    if isinstance(v, bool):
        return 'switch'
    if isinstance(v, (int, float)):
        return 'number'
    if isinstance(v, list):
        return 'list'
    if isinstance(v, dict):
        return 'yaml'
    return 'text'


def auto_label(key):
    words = key.split('.')[-1].replace('_', ' ')
    return words[:1].upper() + words[1:]


def default_of(key):
    """The default of a schema key: the field's own, else config.DEFAULTS', else the entry defaults'."""
    f = FIELDS.get(key) or {}
    if 'default' in f:
        return f['default']
    if key in DEFAULTS_FLAT:
        return DEFAULTS_FLAT[key]
    parts = key.split('.')
    if len(parts) == 3 and parts[1] == '*' and parts[0] in ENTRY_DEFAULTS:
        return ENTRY_DEFAULTS[parts[0]].get(parts[2], MISSING)
    return MISSING


def known(key):
    """The schema line of a key, an automatic one for a key in config.DEFAULTS, or None (an undocumented key)."""
    if key in FIELDS:
        return FIELDS[key]
    if key in DEFAULTS_FLAT:
        return {'label': auto_label(key), 'help': '', 'control': infer_control(DEFAULTS_FLAT[key])}
    return None


def covered(key):
    """True when the key or one of its parents has a schema line drawn as one value (a YAML or list field)."""
    parts = key.split('.')
    return any(known('.'.join(parts[:i])) for i in range(1, len(parts) + 1))


# ---- the view

class View:
    def __init__(self, cfg, raw, env=None):
        self.cfg, self.raw, self.env = cfg, raw if isinstance(raw, dict) else {}, os.environ if env is None else env
        self.folder = config.state_dir(cfg)
        self.over = overrides(cfg, self.folder)

    def names(self, section):
        return sorted((self.raw.get(section) or {}).keys()) if isinstance(self.raw.get(section), dict) else []

    def row(self, path, spec, key):
        spec = spec or {'label': auto_label(key), 'help': 'Not described yet: shown as YAML.', 'control': 'yaml'}
        value = get(self.raw, path)
        default = default_of(key)
        control = spec.get('control') or infer_control(value if default is MISSING else default)
        out = {'path': list(path), 'key': '.'.join(path), 'label': spec['label'], 'help': spec['help'], 'control': control,
               'in_file': value is not MISSING, 'restart': key in RESTART,
               **{k: spec[k] for k in ('unit', 'min', 'max', 'step', 'choices', 'item', 'suggest', 'setup') if k in spec}}
        if 'choices_from' in spec:
            out['choices'] = self.names(spec['choices_from'])
        if control == 'secret' or is_secret(path[-1]):
            return {**out, 'control': 'secret', 'set': self.secret_set(path, value), 'setup': spec.get('setup') or setup_for(path)}
        if value is MISSING:
            value = None if default is MISSING else default
        out = yaml_row(out, value, default) if control == 'yaml' else {**out, 'value': value}
        if default is not MISSING and control != 'yaml':
            out['default'] = default
        if self.over.get(out['key']):
            out['override'] = self.over[out['key']]
        return out

    def secret_set(self, path, value):
        if path == ['telemetry', 'sentry_dsn']:
            return bool(telemetry.dsn(self.cfg, self.env)[0])
        return value not in (MISSING, None, '')

    def rows_under(self, prefix):
        """The rows of a fixed part of the config (not a named entry): schema lines, automatic ones and the file's own keys."""
        keys = [k for k in list(FIELDS) + list(DEFAULTS_FLAT) if (k == prefix or k.startswith(prefix + '.')) and '*' not in k
                and not managed(k)]
        rows, seen = [], set()
        for k in keys:
            if k in seen or k in ENTRIES:
                continue
            seen.add(k)
            rows.append(self.row(k.split('.'), known(k), k))
        rows += [self.row(p, None, '.'.join(p)) for p in self.unknown(prefix.split('.'), self.raw)]
        return rows

    def unknown(self, path, data):
        """Paths in the file under `path` that no schema line covers (shown as raw YAML)."""
        node = get(data, path)
        if not isinstance(node, dict):
            return []
        out = []
        for k, v in node.items():
            p = [*path, str(k)]
            key = generic('.'.join(p))
            if covered(key) or key in ENTRIES:
                continue
            if isinstance(v, dict) and v and any(f.startswith(key + '.') for f in FIELDS):
                out += self.unknown(p, data)
            else:
                out.append(p)
        return out

    def entry(self, coll, name):
        path = [*coll.split('.'), name]
        prefix = f'{coll}.*.'
        fields = [k[len(prefix):] for k in FIELDS if k.startswith(prefix) and '*' not in k[len(prefix):]]
        for k in COLLECTION_FIELDS.get(coll, []):
            fields = [f for f in fields if not f.startswith(k + '.')]
        rows = [self.row([*path, *f.split('.')], FIELDS[prefix + f], prefix + f) for f in fields if not self.parent_is_field(prefix, f)]
        node = get(self.raw, path)
        known_tops = {f.split('.')[0] for f in fields}
        for k in (node if isinstance(node, dict) else {}):
            if k not in known_tops:
                rows.append(self.row([*path, str(k)], None, f'{prefix}{k}'))
        if coll in PAUSABLE:
            p = (controls.read(self.folder)['paused'].get(f'{PAUSABLE[coll]}:{name}'))
            rows.append({'path': [*path, 'paused'], 'key': '.'.join([*path, 'paused']), 'label': 'Paused',
                         'help': 'Takes nothing new while paused; what runs now finishes. Kept on this computer, not in the file.',
                         'control': 'switch', 'value': bool(p), 'default': False, 'in_file': False, 'runtime': True,
                         'restart': False, **({'note': controls.paused_note({'paused': p})} if p else {})})
        return {'path': path, 'key': '.'.join(path), 'name': name, 'rows': rows}

    def parent_is_field(self, prefix, f):
        parts = f.split('.')
        return any(prefix + '.'.join(parts[:i]) in FIELDS for i in range(1, len(parts)))

    def section(self, sid, title, help_text, parts):
        rows, groups = [], []
        for p in parts:
            if p in ENTRIES:
                names = sorted((get(self.raw, p.split('.'), {}) or {}).keys()) if isinstance(get(self.raw, p.split('.'), {}), dict) else []
                groups.append({'key': p, 'title': p.split('.')[-1], 'entries': [self.entry(p, n) for n in names]})
            else:
                rows += self.rows_under(p)
        return {'id': sid, 'title': title, 'help': help_text, 'scope': 'file', 'rows': rows, 'groups': groups}

    def priority_row(self):
        names = self.names('projects')
        ranked = sorted(names, key=lambda n: (config.project_priority(self.raw, n), n))
        return {'path': ['projects'], 'key': 'projects.*.priority', 'label': 'Project priority', 'control': 'order',
                'help': 'Most important first. The autopilot, its retries and fill_room share the work out by this order; '
                        'a lower project still gets its turn.', 'value': ranked, 'in_file': any(
                    isinstance((self.raw.get('projects') or {}).get(n), dict) and 'priority' in self.raw['projects'][n] for n in names),
                'restart': False}

    def optins(self):
        rows = []
        for coll, kind in (('workers', 'worker'), ('accounts', 'account'), ('machines', 'machine')):
            for name in self.names(coll):
                e = (self.raw.get(coll) or {}).get(name) or {}
                why = controls.opt_in_reason(kind, e) if isinstance(e, dict) else ''
                if why:
                    r = self.row([coll, name, 'enabled'], {'label': f'{name} ({kind})', 'help': f'Off until you turn it on: '
                                 f'{controls.OPT_IN_WHY[why]}.', 'control': 'switch', 'default': False}, f'{coll}.*.enabled')
                    rows.append(r)
        rows.append(self.row(['autopilot', 'fill_room'], FIELDS[FILL_ROOM], FILL_ROOM))
        return rows

    def other(self):
        claimed = {p.split('.')[0] for _, _, _, parts in SECTIONS for p in parts} | {m.split('.')[0] for m in MANAGED if '.' not in m}
        keys = [k for k in self.raw if k not in claimed and k not in config.DEFAULTS]
        auto = sorted({k.split('.')[0] for k in DEFAULTS_FLAT} - claimed)
        rows = [r for k in auto for r in self.rows_under(k)]
        return rows + [self.row([str(k)], None, str(k)) for k in keys]

    def payload(self):
        sections = []
        for sid, title, help_text, parts in SECTIONS:
            s = self.section(sid, title, help_text, parts)
            if sid == 'optins':
                s['rows'] = self.optins()
            if sid == 'projects' and self.names('projects'):
                s['rows'].insert(0, self.priority_row())
            sections.append(s)
        sections.append({'id': 'other', 'title': 'Other', 'help': 'Keys Tutti has no description for yet, as YAML.', 'scope': 'file',
                         'rows': self.other(), 'groups': []})
        return {'path': self.cfg.get('_path') or '', 'writable': writable(self.cfg), 'sections': sections,
                'secrets': secrets_view(self.cfg, self.env)}


def managed(key):
    """A key about people (a user, the household, a user's schedule or main agent): on the Manage tab, not in Settings."""
    return any(key == m or key.startswith(m + '.') for m in MANAGED)


def yaml_row(out, value, default):
    """A structured value as YAML text, a secret in it hidden (and the row read-only then)."""
    shown = redact(value)
    out = {**out, 'yaml': '' if shown is None else yaml.safe_dump(shown, sort_keys=False, allow_unicode=True, width=120),
           'readonly': has_secret(value)}
    if isinstance(default, (str, int, float, bool)):
        out['default'] = default
    return out


def setup_for(path):
    if path and path[0] == 'telemetry':
        return 'tutti telemetry setup'
    if path and path[0] == 'scouts':
        return 'tutti scout setup grok'
    if path and path[0] in ('user', 'household'):
        return 'tutti household join'
    return 'an environment variable, never this file'


def secrets_view(cfg, env):
    """[{label, set, where, setup}]: whether each secret Tutti uses is set, and which command sets it. Never a value."""
    dsn, source = telemetry.dsn(cfg, env)
    out = [{'label': 'Sentry DSN (crash reports)', 'set': bool(dsn), 'where': source or telemetry.ENV_DSN,
            'setup': 'tutti telemetry setup'}]
    in_file = set(groksetup.read_env_file(groksetup.env_path(env)))
    for name, sc in sorted((cfg.get('scouts') or {}).items()):
        for k, what in (('url_env', 'webhook URL'), ('key_env', 'webhook key')):
            var = (sc or {}).get(k)
            if var:
                out.append({'label': f'Scout {name}: {what}', 'set': bool(env.get(var, '').strip()) or var in in_file, 'where': var,
                            'setup': 'tutti scout setup grok'})
    return out   # the household membership is on the Manage tab


def overrides(cfg, folder):
    """{key: {value, by}} of the switches kept in the state folder that win over the file."""
    out = {}
    st = autopilot.read_state(cfg)
    for k in ('enabled', 'fill_room'):
        if st.get(k) is not None:
            cmd = 'tutti autopilot fill on|off' if k == 'fill_room' else 'tutti autopilot on|off'
            out[f'autopilot.{k}'] = {'value': bool(st[k]), 'by': f'`{cmd}` (or an older dashboard switch)'}
    data = controls.read(folder)
    pct = data['settings'].get('autocompact_pct')
    if pct is not None:
        out['autocompact.threshold_pct'] = {'value': pct, 'by': 'the dashboard'}
    for k, v in data['enabled'].items():
        kind, _, name = k.partition(':')
        section = controls.SECTIONS.get(kind)
        if section:
            out[f'{section}.{name}.enabled'] = {'value': bool(v.get('on')), 'by': v.get('by') or 'a switch'}
    saved = owner.read(folder)
    if saved.get('mode'):
        out['pacing.mode'] = {'value': saved['mode'], 'by': 'the Usage card or `tutti owner`'}
    for k in ('days', 'window', 'hours'):
        if k in saved:
            out[f'pacing.schedule.{k}'] = {'value': saved[k], 'by': 'the Usage card'}
    return out


def clear_override(cfg, folder, path):
    key = '.'.join(path)
    if key in ('autopilot.enabled', FILL_ROOM):
        with autopilot.state_locked(cfg) as data:
            data.pop(path[1], None)
    elif key == 'autocompact.threshold_pct':
        controls.set_setting(folder, 'autocompact_pct', None)
    elif key == 'pacing.mode':
        owner.write(folder, mode=None)
    elif key.startswith('pacing.schedule.') and path[-1] in ('days', 'window', 'hours'):
        owner.write(folder, **{path[-1]: None})
    elif len(path) == 3 and path[2] == 'enabled' and path[0] in PAUSABLE:
        with controls.changed(folder) as data:
            data['enabled'].pop(f'{PAUSABLE[path[0]]}:{path[1]}', None)


def writable(cfg):
    p = cfg.get('_path') or ''
    return bool(p) and p.endswith(('.yaml', '.yml')) and os.access(p, os.W_OK)


def read_file(cfg):
    with open(cfg['_path'], encoding='utf8') as f:
        return f.read()


def view(cfg, env=None):
    raw = {}
    if cfg.get('_path') and os.path.isfile(cfg['_path']):
        raw = config.parse(read_file(cfg), cfg['_path']) or {}
    out = View(cfg, raw, env).payload()
    out['etag'] = etag(read_file(cfg)) if out['path'] and os.path.isfile(out['path']) else ''
    out['copies'] = configone.diverging(cfg) if cfg.get('_path') else []   # other config files that drifted apart
    return out


def merge_copies(collector):
    """(status, answer) for POST /api/settings {"action": "merge-configs"}: Settings' Merge into one (docs/CONFIG.md)."""
    try:
        done = configone.merge(collector.cfg)
    except (config.ConfigError, OSError, yamledit.EditError) as e:
        return 409, {'error': f'the copies were not merged: {e}; run `tutti doctor --merge-configs` in a terminal to see more'}
    collector.reload()
    return 200, {'ok': True, **done}


# ---- saving

def spec_for(path):
    key = generic('.'.join(path))
    return known(key) or (FIELDS.get(f'{key.rsplit(".", 1)[0]}.*.enabled') if path[-1] == 'enabled' else None) or {}, key


def number_error(spec, v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return 'use a number'
    if 'min' in spec and v < spec['min']:
        return f'use {spec["min"]} or more'
    if 'max' in spec and v > spec['max']:
        return f'use {spec["max"]} or less'
    return None


def value_error(spec, v, nullable=False):
    """Why the value does not fit its control, or None. The config's own rules check the rest."""
    c = spec.get('control')
    if v is None and nullable:
        return None
    if c == 'switch' and not isinstance(v, bool):
        return 'use on or off (true or false)'
    if c == 'number':
        return number_error(spec, v)
    if c == 'select' and spec.get('choices') and v not in spec['choices']:
        return f'use one of {", ".join(map(str, spec["choices"]))}'
    if c in ('list', 'timespans') and (not isinstance(v, list) or not all(isinstance(x, str) for x in v)):
        return 'use a list of words'
    if c == 'days' and (not isinstance(v, list) or not set(v) <= set(DAYS)):
        return 'use days as mon, tue, wed, thu, fri, sat, sun'
    if c in ('text', 'textarea', 'hours') and not isinstance(v, (str, int, float)):
        return 'use text'
    return None


def yaml_value(path, text):
    if not isinstance(text, str):
        raise SettingsError(400, 'yaml must be text')
    try:
        value = yaml.safe_load(text) if text.strip() else None
    except yaml.YAMLError as e:
        raise SettingsError(400, f'{".".join(path)}: that is not valid YAML ({str(e).splitlines()[0]})') from e
    if has_secret(value) or value == HIDDEN:
        raise SettingsError(403, f'{".".join(path)} holds a secret: change it in the file, or with its setup command')
    return value


def parse_change(body):
    """(path, value or DELETE) from a POST body; a SettingsError says what to send instead."""
    if not isinstance(body, dict):
        raise SettingsError(400, 'send {"path": [...], "value": ...} (or "yaml": text, or "reset": true) as JSON')
    path = body.get('path')
    if not isinstance(path, list) or not 0 < len(path) <= MAX_DEPTH or not all(isinstance(k, str) and k for k in path):
        raise SettingsError(400, 'path must be the key as a list of names, e.g. ["autopilot", "max_parallel"]')
    if any(is_secret(k) for k in path):
        raise SettingsError(403, f'{".".join(path)} is a secret: it is never shown or changed here; {setup_for(path)} sets it')
    spec, key = spec_for(path)
    if spec.get('control') == 'secret':
        raise SettingsError(403, f'{".".join(path)} is a secret: {spec.get("setup") or setup_for(path)} sets it')
    if body.get('reset') is True:
        return path, yamledit.DELETE, key
    if 'yaml' in body:
        return path, yaml_value(path, body['yaml']), key
    if 'value' not in body:
        raise SettingsError(400, 'send "value", "yaml" or "reset": true')
    why = value_error(spec, body['value'], nullable=default_of(key) in (None, MISSING))
    if why:
        raise SettingsError(400, f'{spec.get("label") or ".".join(path)} ({".".join(path)}): {why}')
    return path, body['value'], key


def check(cfg, text):
    """The config's own errors for the new text, the way `config.load` would see it (the state folder's switches applied)."""
    try:
        raw = config.parse(text, cfg['_path'])
    except Exception as e:
        return [f'the file would no longer parse: {e}']
    state = ((raw or {}).get('project') or {}).get('state') if isinstance(raw, dict) else None
    raw = controls.apply(copy.deepcopy(raw), controls.read(state or config.DEFAULTS['project']['state']))
    try:
        return config.validate(config.normalise(raw))
    except config.ConfigError as e:
        return [str(e)]
    except (TypeError, AttributeError, KeyError, ValueError) as e:
        return [f'the config would not load: {e}']


def backup(cfg, folder, clock=time.time):
    d = os.path.join(folder, BACKUPS)
    os.makedirs(d, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(clock()))
    dest = os.path.join(d, f'{os.path.basename(cfg["_path"])}.{stamp}')
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(d, f'{os.path.basename(cfg["_path"])}.{stamp}-{n}')
        n += 1
    shutil.copy2(cfg['_path'], dest)
    for old in sorted(glob.glob(os.path.join(d, os.path.basename(cfg['_path']) + '.*')), key=os.path.getmtime)[:-KEEP_BACKUPS]:
        os.remove(old)
    return dest


def write_text(path, text):
    config.write_text(path, text)   # through a symlink into the real file, atomically


def store(cfg, changes, tag=None):
    """Writes [(path, value or DELETE)] into the config file in one go, after the config's own check and a backup; the state
    folder's switches for those keys are taken back. Returns {backup, etag, restart}; SettingsError when nothing was written."""
    if not writable(cfg):
        raise SettingsError(409, f'{cfg.get("_path") or "the config"} cannot be saved from here: only a YAML file you may write '
                                 '(tutti.config.yaml) is; change it in the file')
    text = read_file(cfg)
    if tag and tag != etag(text):
        raise SettingsError(409, 'the config file changed since this page loaded it: reload the Settings tab, then save again')
    new = text
    for path, value in changes:
        try:
            new = yamledit.edit(new, path, value)
        except yamledit.EditError as e:
            raise SettingsError(422, str(e)) from e
    errs = check(cfg, new)
    if errs:
        raise SettingsError(400, 'not saved: ' + '; '.join(errs), errs)
    folder = config.state_dir(cfg)
    saved = backup(cfg, folder) if new != text else ''
    if new != text:
        write_text(cfg['_path'], new)
    for path, _ in changes:
        clear_override(cfg, folder, path)
    restart = sorted({'.'.join(p) for p, _ in changes} & RESTART)
    return {'backup': saved, 'etag': etag(new), 'restart': restart}


def save(collector, raw_body):
    """(status, answer) for POST /api/settings."""
    try:
        body = json.loads(raw_body or b'null')
    except ValueError:
        body = None
    cfg = collector.cfg
    if isinstance(body, dict) and body.get('action') == 'merge-configs':
        return merge_copies(collector)
    try:
        if isinstance(body, dict) and body.get('path') == ['projects'] and 'order' in body:
            done = store(cfg, order_changes(cfg, body['order']), body.get('etag'))
            collector.reload()
            return 200, {'ok': True, **done, 'applies': 'now'}
        path, value, _ = parse_change(body)
        if len(path) == 3 and path[0] in PAUSABLE and path[2] == 'paused':
            return pause(collector, path, value)
        done = store(cfg, [(path, value)], body.get('etag'))
    except SettingsError as e:
        return e.status, {'error': str(e), **({'errors': e.errors} if e.errors else {})}
    try:
        collector.reload()
    except config.ConfigError as e:
        return 500, {'error': f'saved, but the config no longer loads: {e}; the backup is {done["backup"]}'}
    return 200, {'ok': True, **done, 'applies': 'on restart' if done['restart'] else 'now'}


def order_changes(cfg, order):
    """[(path, priority)] that number the projects in `order`, most important first (0, 1, 2…)."""
    names = set(config.parse(read_file(cfg), cfg['_path']).get('projects') or {}) if writable(cfg) else set()
    if not isinstance(order, list) or len(set(order)) != len(order) or set(order) != names:
        raise SettingsError(400, f'order must list every project once, most important first: {", ".join(sorted(names))}')
    return [(['projects', n, 'priority'], i) for i, n in enumerate(order)]


def pause(collector, path, value):
    if not isinstance(value, bool):
        return 400, {'error': 'send "value": true to pause or false to resume'}
    folder = config.state_dir(collector.cfg)
    controls.set_paused(folder, PAUSABLE[path[0]], path[1], value, source='settings')
    collector.reload()
    return 200, {'ok': True, 'restart': [], 'applies': 'now'}
