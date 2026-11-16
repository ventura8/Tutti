"""Model and effort by pace: an account that would end its binding limit under its quota gets its stronger models, a higher
effort and the larger tasks; one that would end over it gets its cheaper models, a lower effort and the small tasks; on
pace it keeps its own `args`. The aim is the most useful work from the quota that is left, not burning it.

The tiers live per account in the config, never guessed: `accounts.<name>.tiers: {strong: {args: [...], sizes: [L, M]},
cheap: {args: [...], sizes: [S]}}`. A tier's flags replace the same flags of the account's `args` (`--model x`, `--effort x`);
the model names come from the worker's own list (`agy models`, `cursor-agent --list-models`, `claude --help`), which
`tutti doctor` can check. An account without tiers always runs with its own args. A per-model cap (an account with a
`match.model` and no `worker`, e.g. Gemini 4 Argon's weekly limit) that is spent takes its models out of the choice until
its reset: the next tier, or the account's own args, whose model has room runs instead. Pure."""

import re

TIERS = ('strong', 'cheap')
ON_PACE = 'on pace'
SIZES = ('S', 'M', 'L')
VALUED = ('--model', '--effort', '-m')   # flags followed by their value
BEHIND_BELOW = 90   # a limit projected to end under this % is behind: room for the strong tier
AHEAD_OVER = 100    # projected over this %: the cheap tier


def pace_of(s):
    """(pace, why) of what binds the account: 'behind', 'ahead' or 'on pace'. A window under a limit: the limit's projected
    end (the last day's rate carried on) when known, else the limit's verdict; any other account: its own verdict."""
    p = s.get('quota_plan') or {}
    if p.get('limit'):
        if p.get('projected') is not None:
            return _projected(p['projected']), f'{p["limit"]} projected to end at {p["projected"]:g} %'
        return _verdict(p.get('limit_verdict')), f'{p["limit"]} {p.get("limit_verdict") or "unread"}'
    return _verdict(s.get('verdict')), f'{s.get("name")} {s.get("verdict") or "unread"}'


def _projected(end):
    if end < BEHIND_BELOW:
        return 'behind'
    return 'ahead' if end > AHEAD_OVER else ON_PACE


def _verdict(v):
    if v in ('behind', 'late'):
        return 'behind'
    return 'ahead' if v == 'ahead' else ON_PACE


def merge(base, extra):
    """`base` with every valued flag that `extra` sets replaced by extra's, then extra's other words appended."""
    drop = {extra[i] for i in range(len(extra) - 1) if extra[i] in VALUED}
    out, i = [], 0
    while i < len(base):
        if base[i] in drop and i + 1 < len(base):
            i += 2
            continue
        out.append(base[i])
        i += 1
    return out + list(extra)


def flag(args, *names):
    for i in range(len(args) - 1):
        if args[i] in names:
            return args[i + 1]
    return None


def model_effort(args):
    """(model, effort) named by the args: --effort, else an agy-style suffix of the model (-low ... -xhigh)."""
    model = flag(args, '--model', '-m')
    effort = flag(args, '--effort')
    if not effort and model:
        tail = model.rsplit('-', 1)[-1]
        effort = tail if tail in ('low', 'medium', 'high', 'xhigh', 'max') else None
    return model, effort


def capped_models(cfg, states):
    """{account: model regex} of the spent per-model caps: accounts with a `match.model` and no `worker` of their own."""
    rows = states.values() if isinstance(states, dict) else (states or [])
    accounts = cfg.get('accounts') or {}
    out = {}
    for st in rows:
        a = accounts.get((st or {}).get('name')) or {}
        model = (a.get('match') or {}).get('model')
        if model and not a.get('worker') and st.get('verdict') == 'spent':
            out[st['name']] = model
    return out


def _capped_by(model, capped):
    return next((n for n, rx in capped.items() if model and re.search(rx, model, re.I)), None)


def choose(cfg, name, s, states=None):
    """{tier, args, model, effort, sizes, reason, label} for a run on account `name` with pacing state `s` (or None);
    `states` (all accounts' pacing states) lets a spent per-model cap move the run to a model with room."""
    a = (cfg.get('accounts') or {}).get(name) or {}
    pace, why = pace_of(s) if s else (ON_PACE, 'no pacing state')
    tier = {'behind': 'strong', 'ahead': 'cheap'}.get(pace)
    spec = ((a.get('tiers') or {}).get(tier) if tier else None) or {}
    args = merge(list(a.get('args') or []), list(spec.get('args') or []))
    model, effort = model_effort(args)
    reason = f'{name} {pace}: {why}'
    if tier and not spec:
        reason += f' (no accounts.{name}.tiers.{tier}: its own args)'
    capped = capped_models(cfg, states)
    cap = _capped_by(model, capped)
    if cap:
        tier, spec, args, model, effort, reason = _with_room(a, tier, cap, capped, reason)
    short = '/'.join(x for x in (model, effort) if x) or 'its default model'
    return {'tier': tier if spec else 'default', 'args': args, 'model': model, 'effort': effort,
            'sizes': list(spec.get('sizes') or []), 'reason': reason, 'label': f'{short}: {reason}'}


def _with_room(a, tier, cap, capped, reason):
    """The first of: the account's own args, then its other tiers, whose model no spent cap holds; else the tier as it was."""
    tiers = a.get('tiers') or {}
    options = [(None, {})] + [(t, tiers[t]) for t in TIERS if t != tier and tiers.get(t)]
    for t, spec in options:
        args = merge(list(a.get('args') or []), list(spec.get('args') or []))
        model, effort = model_effort(args)
        if not _capped_by(model, capped):
            return t, spec, args, model, effort, f'{reason}; {cap} is spent: {model or "its default model"} instead'
    spec = tiers.get(tier) or {}
    args = merge(list(a.get('args') or []), list(spec.get('args') or []))
    model, effort = model_effort(args)
    return tier, spec, args, model, effort, f'{reason}; {cap} is spent and no other model of this account has room'


def errors(cfg):
    """Config problems of accounts.<name>.tiers, each naming the key and the fix."""
    errs = []
    for name, a in (cfg.get('accounts') or {}).items():
        t = (a or {}).get('tiers')
        if t is None:
            continue
        key = f'accounts.{name}.tiers'
        if not isinstance(t, dict) or any(k not in TIERS for k in t):
            errs.append(f'{key} is {t!r}: a mapping with strong and/or cheap, each {{args: [--model, <name>], sizes: [L, M]}}')
            continue
        errs += [e for k, spec in t.items() for e in tier_errors(f'{key}.{k}', spec)]
    return errs


def tier_errors(key, spec):
    args = spec.get('args', []) if isinstance(spec, dict) else None
    if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        return [f'{key} is {spec!r}: {{args: [--model, <a model the worker lists>], sizes: [S, M, L]}}']
    if any(x not in SIZES for x in spec.get('sizes', [])):
        return [f'{key}.sizes is {spec["sizes"]!r}: a list of S, M and L']
    return []
