"""One config per user (docs/CONFIG.md): finds other copies of tutti.config.yaml that drifted apart from the one in use, and
merges them into it in one go. A copy left over in a project folder, read by commands started there, made Settings edits
land in one file only; `tutti doctor` and the dashboard's Settings warn about it and offer the merge.

The merge keeps the file in use as it is (its comments and order too) and adds what only a copy has; where both have a key
with different values the file in use wins and the key is listed. Each copy is then kept as <copy>.bak-merge-<time> and
replaced by a symlink to the file in use, so it can never drift again."""
import os
import time

import yaml

from . import config, yamledit


def _parse(path):
    try:
        with open(path, encoding='utf8') as f:
            return config.parse(f.read(), path) or {}
    except (OSError, ValueError, yaml.YAMLError, config.ConfigError):
        return None   # a copy that cannot be read is not merged


def candidates(cfg, start=None, env=None):
    """The files `config.find` could pick for this user: the one in use first, the XDG ones, the nearest from `start`
    (the working directory) upwards. Each file once (compared by its real path); a path as found, so a symlink stays the
    link that is moved, never its target. A repository's own config elsewhere is not this user's copy and is left alone."""
    env = os.environ if env is None else env
    base = config.xdg_tutti_dir(env)
    out, seen = [], set()
    for p in (cfg['_path'], *(os.path.join(base, n) for n in config.XDG_NAMES), config.nearest(start)):
        if p and os.path.isfile(p) and os.path.realpath(p) not in seen:
            seen.add(os.path.realpath(p))
            out.append(os.path.abspath(p))
    return out


def diverging(cfg, start=None, env=None):
    """[{path, missing, conflicts}]: the other copies whose content differs from the file in use."""
    real = os.path.realpath(cfg['_path'])
    mine = _parse(real) or {}
    out = []
    for p in candidates(cfg, start, env)[1:]:
        theirs = _parse(p)
        if theirs is None or theirs == mine:
            continue
        missing, conflicts = compare(mine, theirs)
        out.append({'path': p, 'missing': ['.'.join(k) for k, _ in missing], 'conflicts': ['.'.join(k) for k in conflicts]})
    return out


def compare(mine, theirs, prefix=()):
    """([(key path, value)] that only `theirs` has, [key paths] both have with different values)."""
    missing, conflicts = [], []
    for k, v in theirs.items():
        path = (*prefix, str(k))
        if k not in mine:
            missing.append((path, v))
        elif isinstance(v, dict) and isinstance(mine[k], dict):
            m, c = compare(mine[k], v, path)
            missing += m
            conflicts += c
        elif mine[k] != v:
            conflicts.append(path)
    return missing, conflicts


def merge(cfg, start=None, env=None, clock=time.time):
    """Merges every drifted copy into the file in use, then links each copy to it. {file, merged: [{path, backup, added,
    kept}]}; config.ConfigError when the merged text does not check out (nothing is written then)."""
    real = os.path.realpath(cfg['_path'])
    with open(real, encoding='utf8') as f:
        text = f.read()
    found = diverging(cfg, start, env)
    done = []
    for d in found:
        missing, _ = compare(config.parse(text, real) or {}, _parse(d['path']) or {})
        for path, value in missing:
            text = yamledit.edit(text, list(path), value)
        done.append({'path': d['path'], 'added': ['.'.join(k) for k, _ in missing], 'kept': d['conflicts']})
    errs = config.validate(config.normalise(config.parse(text, real) or {}))
    if errs:
        raise config.ConfigError('the merged config would not check out, so nothing was changed:\n  - ' + '\n  - '.join(errs))
    if found:
        config.write_text(real, text)
    stamp = time.strftime('%Y%m%dT%H%M%S', time.localtime(clock()))
    for d in done:
        link(d, real, stamp)
    return {'file': real, 'merged': done}


def link(d, real, stamp):
    """The copy kept as <copy>.bak-merge-<stamp> and replaced by a symlink to the file in use; when the link cannot be
    made (no symlink rights, say) the copy is put back and d['error'] says why (its keys are merged already)."""
    d['backup'] = f'{d["path"]}.bak-merge-{stamp}'
    try:
        os.replace(d['path'], d['backup'])
    except OSError as e:
        d.update(backup=None, error=f'not linked: {e}; delete or rename {d["path"]} yourself')
        return
    try:
        os.symlink(real, d['path'])
    except OSError as e:
        os.replace(d['backup'], d['path'])
        d.update(backup=None, error=f'not linked: {e}; delete or rename {d["path"]} yourself')


def warnings(cfg, start=None, env=None):
    """Plain lines for `tutti doctor`: which copy differs, what it holds, and the fix."""
    return [f'{d["path"]} differs from the config in use, {cfg["_path"]} ({len(d["missing"])} key(s) only there, '
            f'{len(d["conflicts"])} with other values): commands started in its folder may have read it. Run '
            '`tutti doctor --merge-configs` (or Settings, Merge into one) to merge it in and link it to the one file'
            for d in diverging(cfg, start, env)]
