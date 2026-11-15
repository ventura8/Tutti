"""Work follows the devices that are here (docs/DISPATCH.md, *When a device goes away*). A device that is away (a planned
absence, presence lost, a person using it, its blocked hours, paused) has its pending shards moved to other devices that
can run them: the same form factor (a phone or a tablet for touch work, a TV for TV work), at least the API level and every
capability the suite asks for (`requires: {kinds, min_sdk, capabilities}`; else the named devices' form factor). Work only
that device can run waits, with the reason. When it comes back its shards go back to it (the move is decided afresh each
round, so nothing has to be undone). Pure: the dispatcher applies it and tells the projects (events, moves.jsonl)."""

TOUCH = ('phone', 'tablet')


def kind_of(d):
    return (d or {}).get('kind') or 'phone'


def family(kind):
    return 'touch' if kind in TOUCH else kind


def requirement(cfg, w):
    """The suite's own `requires`, else the form factor of the devices it names (capabilities only when the suite asks)."""
    req = dict(w.get('requires') or {})
    named = [(cfg.get('devices') or {}).get(n) or {} for n in w.get('devices') or []]
    if 'kinds' not in req:
        req['kinds'] = sorted({k for d in named for k in (TOUCH if family(kind_of(d)) == 'touch' else (kind_of(d),))})
    return req


def fits(d, req):
    """Why the device cannot take the work ('' when it can)."""
    if req.get('kinds') and kind_of(d) not in req['kinds']:
        return f'it needs a {" or ".join(req["kinds"])}'
    sdk = d.get('sdk')
    if req.get('min_sdk') and sdk is not None and sdk < req['min_sdk']:
        return f'it needs Android API {req["min_sdk"]}+'
    missing = [c for c in req.get('capabilities') or [] if c not in (d.get('capabilities') or [])]
    return f'it needs {", ".join(missing)}' if missing else ''


def plan(cfg, pend, away):
    """For each pending shard bound to devices that are all away: (the shards with `devices` set to the substitutes,
    {away device: {'moved': {to device: n}, 'waits': [why...]}}). `away`: {device name: why}."""
    devs = cfg.get('devices') or {}
    out, report = [], {}
    for w in pend:
        names = w.get('devices') or []
        if not names or any(n not in away for n in names) or w.get('pin'):
            out.append(w)
            continue
        req = requirement(cfg, w)
        subs = [n for n, d in devs.items() if n not in away and not d.get('paused') and not fits(d, req)]
        for n in names:
            report.setdefault(n, {'moved': {}, 'waits': []})
        if subs:
            for n in names:
                for s in subs[:1]:
                    report[n]['moved'][s] = report[n]['moved'].get(s, 0) + 1
            out.append({**w, 'devices': subs, 'moved_from': names})
        else:
            report[names[0]]['waits'].append(f'{w["project"]} {w["key"]}: no other device fits ({fits({}, req) or "none is here"})')
            out.append(w)
    return out, report


def summary(cfg, report, away):
    """The dashboard's lines: 'Pixel 9 Pro away (planned absence): 3 shards moved to pixeltablet, 1 waits for a phone'."""
    lines = []
    for name, r in sorted(report.items()):
        label = ((cfg.get('devices') or {}).get(name) or {}).get('model') or name
        parts = [f'{n} shard{"s" if n != 1 else ""} moved to {to}' for to, n in r['moved'].items()]
        if r['waits']:
            parts.append(f'{len(r["waits"])} wait{"s" if len(r["waits"]) == 1 else ""} for it ({r["waits"][0].split(": ", 1)[1]})')
        if parts:
            lines.append(f'{label} away ({away.get(name) or "away"}): ' + ', '.join(parts))
    return lines


def with_role(cfg, role, away=(), roles=None):
    """The devices with a role and here: detected (`roles`: {device: [...]}, tutti/castroles.py), or the admin's
    `devices.<d>.roles` override."""
    found = []
    for n, d in (cfg.get('devices') or {}).items():
        have = (roles or {}).get(n) if roles and n in roles else d.get('roles') or []
        if role in have and n not in away and not d.get('paused'):
            found.append(n)
    return found


def paired(cfg, w, away=(), roles=None):
    """A suite that needs a partner device (`pair: {role: cast-receiver}`: a Cast sender and its receiver) holds the first
    one with that role that is here, so the scheduler leases both at once (dispatch.holds_free); the shard waits, with the
    reason, while none is."""
    role = (w.get('pair') or {}).get('role') if isinstance(w.get('pair'), dict) else None
    if not role or w.get('holds'):
        return w
    found = [n for n in with_role(cfg, role, away, roles) if n not in (w.get('devices') or [])]
    if not found:
        return {**w, 'wait': w.get('wait') or f'no {role} is here: it waits for one (devices.<d>.roles)'}
    return {**w, 'holds': [found[0]]}
