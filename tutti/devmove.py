"""`tutti devices move --to <machine> [devices...]`: moves Android devices from the machine whose adb reaches them to
another, with no re-pairing when it can (docs/DEVICES.md, *Moving devices*).

The best path copies the old machine's adb key pair to the new one: every device already trusts that key. A different
key already on the new machine is never replaced without a choice (`--replace-key` backs it up first; `--repair` pairs
each device again instead). Preflight: adb on the new machine (installed in user space with `--install-adb`), a recent
enough adb on both, every device seen over mDNS from the new machine (so it is on the devices' network), and no run or
lock holding a device. Each device is then connected on the new machine and checked (`getprop ro.product.model`); one
that fails stays on the old machine (rollback); the ones that work get `device.machine` (and `adb`) changed in the config
and are disconnected from the old machine's adb server. Pure choices here; adb goes through tutti/adbhost.py."""
import time

import yaml

from . import adbpair, yamledit

KEY_ACTIONS = ('same', 'copy', 'replace', 'repair', 'ask')


class MoveError(Exception):
    """The move cannot start; the message says why and what to do."""


def pick(cfg, to, names):
    """The devices to move: the ones named, or every device not on `to` yet. MoveError for an unknown name or machine."""
    devs = cfg.get('devices') or {}
    if to not in cfg['machines']:
        raise MoveError(f'no machine "{to}" in {cfg["_path"]}: the machines are {", ".join(cfg["machines"])}')
    for n in names:
        if n not in devs:
            raise MoveError(f'no device "{n}": the devices are {", ".join(devs) or "(none)"}')
    found = [devs[n] for n in names] if names else [d for d in devs.values() if d['machine'] != to]
    found = [d for d in found if d['machine'] != to]
    if not found:
        raise MoveError(f'nothing to move: every device named is already on {to}')
    sources = {d['machine'] for d in found}
    if len(sources) > 1:
        raise MoveError(f'the devices are on {", ".join(sorted(sources))}: move the ones of one machine at a time '
                        '(name them: tutti devices move --to <machine> <device> ...)')
    return found


def key_action(old_fp, new_fp, choice=None):
    """What happens to the adb key: same (the new machine has it already), copy (it has none), replace (a different
    one, backed up first: asked for), repair (pair again: asked for, or the old key cannot be read), ask (a choice is
    needed: the new machine's own key may be trusted by other devices)."""
    if choice == 'repair' or not old_fp:
        return 'repair'
    if old_fp == new_fp:
        return 'same'
    if not new_fp:
        return 'copy'
    return 'replace' if choice == 'replace' else 'ask'


def leased(cfg, name):
    """The dispatch lease holding the device, or ''."""
    from . import dispatch
    for lease in dispatch.read_state(cfg)['leases'].values():
        if lease.get('target') == f'device:{name}':
            return f'dispatch lease {lease.get("id")} ({lease.get("project")} {lease.get("key")})'
    return ''


def busy(cfg, d, old, lease_fn=leased):
    """Who holds the device now ('' when nobody): a dispatch lease, Tutti's device lock, or a lock file of another tool
    (`devices.<name>.external_lock`, a path on its machine)."""
    from . import devices, remote
    who = lease_fn(cfg, d['name'])
    ext = devices.external_lock(d)
    for path in (remote.home_path(d['lock']), *([remote.home_path(ext)] if ext else [])):
        who = who or old.lock_holder(path)
    return who


def preflight(cfg, to, found, old, new, choice=None, lease_fn=leased):
    """What the move would do and what stops it: {to, from, adb, install, versions, key, seen, busy, problems}."""
    pre = {'to': to, 'from': found[0]['machine'], 'adb': new.path(), 'problems': [], 'seen': {}, 'busy': {}}
    pre['install'] = not pre['adb']
    vers = (old.version(), new.version() if pre['adb'] else 0)
    pre['versions'] = vers
    if vers[0] and vers[0] < adbpair.MIN_ADB:
        pre['problems'].append(f'adb on {pre["from"]} is platform-tools {vers[0]}: update it to {adbpair.MIN_ADB} or newer')
    if pre['adb'] and vers[1] < adbpair.MIN_ADB:
        pre['problems'].append(f'adb on {to} is platform-tools {vers[1] or "?"}: it needs {adbpair.MIN_ADB} or newer for mDNS; '
                               'run with --install-adb for a user-space copy')
    # a dry run touches no device: listen with avahi where it is (an adb server would auto-connect to what it hears)
    heard = new.listen() if hasattr(new, 'listen') else None
    services = heard if heard is not None else (new.services() if pre['adb'] else [])
    for d in found:
        guid = adbpair.guid_of(d['serial'])
        pre['seen'][d['name']] = adbpair.connect_address(services, guid) if guid else None
        if pre['adb'] and guid and not pre['seen'][d['name']]:
            pre['problems'].append(f'{d["name"]} is not seen over mDNS from {to}: it must be on the same network as {to}, with '
                                   'Wireless debugging on (and the network not blocking multicast)')
        who = busy(cfg, d, old, lease_fn)
        if who:
            pre['busy'][d['name']] = who
            pre['problems'].append(f'{d["name"]} is in use ({who}): wait until that run ends, then move it')
    pre['notes'] = [] if vers[0] else [f'adb on {pre["from"]} does not answer (for a Windows adb.exe from WSL: WSL interop is off), '
                                       'so its key cannot be read and each device is paired again; turn it on to move without '
                                       're-pairing']
    pre['key'] = key_action(old.fingerprint() if vers[0] else '', new.fingerprint() if pre['adb'] else '', choice)
    if pre['key'] == 'ask':
        pre['problems'].append(f'{to} has its own adb key, which other devices may trust: choose --replace-key (it is backed '
                               'up first) or --repair (pair each device again; the key stays)')
    return pre


def plan_lines(pre, found):
    """The plan as the user reads it, step by step."""
    to, src = pre['to'], pre['from']
    out = [f'move {", ".join(d["name"] for d in found)} from {src} to {to}']
    if pre['install']:
        out.append(f'install Android platform-tools in user space on {to} (~/.local/share/tutti/platform-tools; needs --install-adb)')
    else:
        out.append(f'adb on {to}: {pre["adb"]} (platform-tools {pre["versions"][1] or "?"}; {src}: {pre["versions"][0] or "?"})')
    out.append({'same': f'adb key: {to} already has {src}\'s key; nothing to copy',
                'copy': f'adb key: copy {src}\'s key pair to {to} over ssh at mode 600 (never printed); no re-pairing',
                'replace': f'adb key: back up {to}\'s own key (adbkey.tutti-backup-<time>), then copy {src}\'s at mode 600',
                'repair': f'adb key: not copied; pair each device again on {to} (a checklist: QR code, or the code a TV shows)',
                'ask': f'adb key: {to} has a different key: choose --replace-key or --repair'}[pre['key']])
    if pre['key'] in ('copy', 'replace'):
        out.append(f'restart the adb server on {to} so it loads the key (adb kill-server; adb start-server)')
    for d in found:
        seen = pre['seen'].get(d['name'])
        where = f'seen over mDNS at {seen}' if seen else 'not seen over mDNS yet'
        state = f'; in use: {pre["busy"][d["name"]]}' if d['name'] in pre['busy'] else ''
        out.append(f'{d["name"]} ({d["serial"]}): {where}{state}; connect on {to}, check its model, set devices.{d["name"]}.machine: '
                   f'{to}, then adb disconnect on {src}')
    out += [f'note: {n}' for n in pre.get('notes', [])]
    out.append('a device that fails to connect stays on ' + src + ' (rollback); with none moved, ' + to + '\'s key is put back')
    return out


def set_machine(path, moves):
    """Changes devices.<name>.machine (and .adb: the new machine's adb, or none when it is on PATH) in the config file,
    keeping its comments. `moves`: [(name, machine, adb or None)]."""
    with open(path, encoding='utf8') as f:
        text = f.read()
    for name, machine, adb in moves:
        text = yamledit.edit(text, ['devices', name, 'machine'], machine)
        if adb:
            text = yamledit.edit(text, ['devices', name, 'adb'], adb)
        elif 'adb' in (((yaml.safe_load(text) or {}).get('devices') or {}).get(name) or {}):
            text = yamledit.edit(text, ['devices', name, 'adb'], yamledit.DELETE)
    with open(path, 'w', encoding='utf8') as f:
        f.write(text)


def check(new, d, wait=60, sleep=time.sleep):
    """Connects the device on the new machine and reads its model; '' when that fails."""
    guid, live = adbpair.guid_of(d['serial']), str(d['serial'])
    try:
        if guid:
            live = adbpair.connect(new, guid, wait=wait, sleep=sleep)
        else:
            new.adb('connect', live, timeout=30)
    except adbpair.PairError:
        return ''
    rc, out = new.adb('-s', live, 'shell', 'getprop', 'ro.product.model', timeout=30)
    return out.strip() if rc == 0 and new.state(live) == 'device' else ''


def copy_key(old, new, stamp, say):
    """Copies the key pair; (ok, backup suffix). The key is held in memory only, never printed or logged."""
    priv, pub = old.read_key()
    if not priv:
        return False, ''
    ok, backup = new.write_key(priv, pub, stamp)
    del priv
    if ok:
        say(f'adb key copied to {new.name} (mode 600)' + (f'; its own key kept as adbkey.{backup}' if backup else ''))
        new.restart()
    return ok, backup


def run(cfg, to, found, old, new, pre, *, say, pair_fn=None, stamp=None, wait=60, sleep=time.sleep):
    """Does the move after a clean preflight. Returns {'moved': [...], 'stayed': {name: why}, 'restored': bool}."""
    stamp = stamp or time.strftime('%Y%m%d-%H%M%S')
    backup, copied = '', False
    if pre['key'] in ('copy', 'replace'):
        copied, backup = copy_key(old, new, stamp, say)
        if not copied:
            raise MoveError(f'the adb key could not be copied from {old.name} to {new.name}: run again with --repair to pair '
                            'each device instead')
    moved, stayed = [], {}
    for d in found:
        if pre['key'] == 'repair' and pair_fn:
            pair_fn(d)
        model = check(new, d, wait=wait, sleep=sleep)
        if not model or (d.get('model') and model != d['model']):
            stayed[d['name']] = f'did not connect on {to}' if not model else f'{to} sees a {model}, not a {d["model"]}'
            continue
        moved.append(d['name'])
        say(f'{d["name"]}: connected on {to} ({model})')
    restored = False
    if not moved and copied:
        restored = new.restore_key(backup) if backup else new.remove_key()
        new.restart()
    if moved:
        set_machine(cfg['_path'], [(n, to, adb_for(new)) for n in moved])
        for d in found:
            if d['name'] in moved:
                old.adb('disconnect', str(d['serial']), timeout=30)
    return {'moved': moved, 'stayed': stayed, 'restored': restored}


def adb_for(new):
    """The adb path to write into the config: none when the new machine finds adb on its PATH."""
    path = new.path()
    rc, out = new.bash('command -v adb')
    return None if rc == 0 and out.strip() == path else path


def summary(result, to, src):
    lines = [f'moved to {to}: {", ".join(result["moved"]) or "none"}']
    for name, why in result['stayed'].items():
        lines.append(f'{name} stays on {src}: {why}; fix that and run `tutti devices move --to {to} {name}` again')
    if result['restored']:
        lines.append(f'nothing moved, so {to}\'s adb key was put back as it was')
    return lines



def save_device(path, devs, machine, serial, info, adb=None, name=None):
    """Adds a paired device to the config (or, paired again, updates the entry with that serial). Returns its name."""
    same = next((n for n, d in (devs or {}).items() if str(d.get('serial')) == serial), None)
    name = same or name or adbpair.name_for(info['model'], set(devs or {}))
    with open(path, encoding='utf8') as f:
        text = f.read()
    if same:
        for key, value in (('machine', machine), ('kind', info['kind']), ('model', info['model']),
                           ('capabilities', info['capabilities'])):
            text = yamledit.edit(text, ['devices', name, key], value)
    else:
        text = yamledit.edit(text, ['devices', name], adbpair.entry(serial, machine, info, adb))
    with open(path, 'w', encoding='utf8') as f:
        f.write(text)
    return name
