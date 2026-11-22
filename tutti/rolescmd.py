"""`tutti machines --roles` and `tutti roles set|unset|list` (docs/MACHINES.md, *Roles*)."""
import time

from . import devices, lanmedia, roles


def _line(name, rows):
    return f'{name:16} ' + (', '.join(f'{r["role"]}{"*" if r["source"] == "set" else ""}' for r in rows) or '(none)')


def show(cfg, a, now=None, out=print, readings_fn=None):
    """Every machine, device and media device on the network with its roles; * marks one an admin set."""
    now = now or time.time()
    out('Machines')
    for name, rows in roles.machine_roles(cfg, now, refresh=getattr(a, 'refresh', False)).items():
        out('  ' + _line(name, rows))
    out('Devices')
    if readings_fn is None:
        from . import remote
        readings_fn = remote.device_readings
    for row in devices.devices_state(cfg, readings_fn):
        out('  ' + _line(row['name'], row.get('role_info') or []))
    lan = lanmedia.devices(cfg, now, refresh=getattr(a, 'refresh', False))
    if lan:
        out('On the network')
        for d in lan:
            out('  ' + _line(f'{d["name"]} ({d["address"]})', d['roles']))
    out('(* set by an admin; `tutti roles list` says why each one is there)')
    return 0


def _list(cfg, a, out):
    for name, rows in roles.cached(cfg).items():
        for r in rows:
            out(f'{name:16} {r["role"]:22} {r["why"]}')
    if a.all:
        for k, v in sorted(roles.ROLES.items()):
            out(f'  role {k}: {v}')
    return 0


def _kind(cfg, name):
    """machines, devices, lan_devices (a network device by its IPv4 address), or '' for a name Tutti does not know."""
    import ipaddress
    for kind in ('machines', 'devices'):
        if name in (cfg.get(kind) or {}):
            return kind
    try:
        ipaddress.IPv4Address(name)
        return 'lan_devices'
    except ValueError:
        return ''


def _matrix(cfg, out):
    """`tutti roles matrix`: every machine, every role it has, could have or cannot, why and the fix (t234)."""
    from . import rolematrix
    _code, answer = rolematrix.get(cfg)
    mark = {'has': '+', 'off': '~', 'cannot': '-'}
    for m in answer['machines']:
        out(f'{m["name"]}: {m["has"]} roles, {m["off"]} turned off, {m["cannot"]} it cannot have')
        for r in m['rows']:
            out(f'  {mark[r["state"]]} {r["role"]}: {r["why"]}' + (f'  (fix: {r["fix"]})' if r['fix'] else ''))
    return 0


def cmd_roles(cfg, a, out=print):
    if a.action == 'list':
        return _list(cfg, a, out)
    if a.action == 'matrix':
        return _matrix(cfg, out)
    if not a.name or not a.role:
        out('say which: tutti roles set|unset <machine or device> <role>')
        return 2
    if roles.unknown([a.role]):
        out(f'{a.role} is not a role Tutti knows: {", ".join(sorted(roles.ROLES))}')
        return 2
    kind = _kind(cfg, a.name)
    if not kind:
        out(f'{a.name} is not a machine, a device or a network device\'s address: `tutti machines --roles` lists them')
        return 2
    try:
        add, off = roles.edit(cfg['_path'], kind, a.name, a.role, a.action == 'set')
    except ValueError as e:
        out(str(e))
        return 2
    out(f'{kind}.{a.name}: roles_add {add or "[]"}, roles_off {off or "[]"}')
    return 0


def add_parser(add):
    s = add('roles', cmd_roles, 'roles of machines and devices: list them, the matrix (has, turned off, cannot, why and the fix), '
            'set or unset one (docs/MACHINES.md, Roles)')
    s.add_argument('action', choices=['list', 'matrix', 'set', 'unset'])
    s.add_argument('name', nargs='?', help='a machine, a device, or a network device\'s address')
    s.add_argument('role', nargs='?', help='e.g. windows-test-runner, ui-desktop, gpu-ai, airplay-receiver')
    s.add_argument('--all', action='store_true', help='list: also every role Tutti knows and what it means')
