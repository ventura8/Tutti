"""`tutti devices move --to <machine> [devices...]`: moves Android devices from the machine whose adb reaches them to
another, with no re-pairing when it can (docs/DEVICES.md, *Moving devices*).

The best path copies the old machine's adb key pair to the new one: every device already trusts that key. A different
key already on the new machine is backed up (adbkey.tutti-backup-<time>) and replaced: the user does nothing (`--keep-key`
keeps it; `--repair`, the last resort, pairs each device again). Over the old machine's live connection each device's
Wireless debugging is set on first and re-asserted from the new machine after (tutti/keepon.py).
Preflight: adb on the new machine (installed in user space with `--install-adb`), a recent enough adb on both, every
device seen over mDNS from the new machine (so it is on the devices' network), and no run or
lock holding a device. Each device is then connected on the new machine and checked (`getprop ro.product.model`); one
that fails stays on the old machine (rollback); the ones that work get `device.machine` (and `adb`) changed in the config
and are disconnected from the old machine's adb server. Pure choices here; adb goes through tutti/adbhost.py."""
import time

import yaml

from . import adbpair, config, yamledit

KEY_ACTIONS = ('same', 'copy', 'replace', 'repair', 'ask')


class MoveError(Exception):
    """The move cannot start; the message says why and what to do."""


def named(cfg, to, names):
    """The devices the command is about: the ones named, or every device. MoveError for an unknown name or machine."""
    devs = cfg.get('devices') or {}
    if to not in cfg['machines']:
        raise MoveError(f'no machine "{to}" in {cfg["_path"]}: the machines are {", ".join(cfg["machines"])}')
    for n in names:
        if n not in devs:
            raise MoveError(f'no device "{n}": the devices are {", ".join(devs) or "(none)"}')
    return [devs[n] for n in names] if names else list(devs.values())


def pick(cfg, to, names, none_ok=False):
    """The devices to move: the ones named, or every device not on `to` yet. MoveError for an unknown name or machine;
    with none_ok, [] when all are on `to` already (the move then only consolidates: see scan)."""
    found = [d for d in named(cfg, to, names) if d['machine'] != to]
    if not found:
        if none_ok:
            return []
        raise MoveError(f'nothing to move: every device named is already on {to}')
    sources = {d['machine'] for d in found}
    if len(sources) > 1:
        raise MoveError(f'the devices are on {", ".join(sorted(sources))}: move the ones of one machine at a time '
                        '(name them: tutti devices move --to <machine> <device> ...)')
    return found


def key_action(old_fp, new_fp, choice=None):
    """What happens to the adb key: same (the new machine has it already), copy (it has none), replace (a different
    one: backed up first, then the old one copied; the default, so the user does nothing), repair (pair again: asked
    for, or the old key cannot be read: the last resort), ask (--keep-key: the new machine's own key stays)."""
    if choice == 'repair' or not old_fp:
        return 'repair'
    if old_fp == new_fp:
        return 'same'
    if not new_fp:
        return 'copy'
    return 'ask' if choice == 'keep' else 'replace'   # the default: the old key, the new one backed up (no user step)


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
    who = who or old.lock_holder(remote.home_path(d['lock']))
    ext = devices.external_lock(d)
    if ext and not who:
        stale = int(float(d.get('external_lock_stale_minutes', devices.EXTERNAL_STALE_MINUTES)) * 60)
        who = old.lock_holder(remote.home_path(ext), stale=stale)
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
        pre['problems'].append(f'{to} keeps its own adb key (--keep-key): run without it (the key is backed up first and '
                               'the devices need nothing), or with --repair (pair each device again)')
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
                'ask': f'adb key: {to} keeps its own key (--keep-key): add --repair, or drop --keep-key'}[pre['key']])
    out.append(f'Wireless debugging: set on through {src} first (each device\'s value kept), re-asserted from {to} after')
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
    config.write_text(path, text)


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


def keep_wifi_on(old, found, say):
    """Over the old machine's live connection: reads each device's Wireless debugging setting (kept in the result) and
    sets it on, so a reboot or a Wi-Fi change during the move does not strand it (tutti/keepon.py)."""
    from . import keepon
    was = {}
    for d in found:
        serial = str(d['serial'])
        if old.state(serial) == 'device':
            was[d['name']] = keepon.get(old, serial)
            if was[d['name']] != '1' and keepon.turn_on(old, serial):
                say(f'{d["name"]}: Wireless debugging was {was[d["name"]] or "unknown"}; set on through {old.name}')
    return was


def run(cfg, to, found, old, new, pre, *, say, pair_fn=None, stamp=None, wait=60, sleep=time.sleep):
    """Does the move after a clean preflight, asking nothing. Returns {'moved': [...], 'stayed': {name: why},
    'restored': bool, 'wifi_was': {name: '0'|'1'|''}}."""
    from . import keepon
    stamp = stamp or time.strftime('%Y%m%d-%H%M%S')
    wifi_was = keep_wifi_on(old, found, say)
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
        keepon.turn_on(new, live_serial(new, d))   # re-asserted from the new machine: it survives the old one letting go
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
    return {'moved': moved, 'stayed': stayed, 'restored': restored, 'wifi_was': wifi_was}


def live_serial(host, d):
    """The serial this machine's adb lists the device under: its mDNS name, else the ip:port it announces now."""
    serial = str(d['serial'])
    if host.state(serial) == 'device':
        return serial
    addr = adbpair.connect_address(host.services(), adbpair.guid_of(serial)) if adbpair.guid_of(serial) else None
    return addr if addr and host.state(addr) == 'device' else serial


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


# Consolidation: the devices on `to` are connected on `to` only (owner, 2026-11-16: "move the pairing of all devices to the
# NUC"). Another machine's adb server that still holds a connection to one of them is a stray: it is disconnected (never
# unpaired), and a Windows runner that runs device suites gets adb_via_device_host (tutti/adbvia.py) instead of its own adb.
# `adb devices` would start a server where none runs (and that server would auto-connect to what it hears over mDNS), so a
# machine is asked only when an adb server already listens there.
LISTED = ('[ -n "$A" ] || exit 127; case "$A" in *.exe) powershell.exe -NoProfile -NonInteractive -Command '
          '"if (Get-NetTCPConnection -LocalPort 5037 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 4 }" '
          '> /dev/null 2>&1 || exit 4;; *) command -v ss > /dev/null || exit 5; '
          '[ -n "$(ss -ltnH "sport = :5037" 2> /dev/null)" ] || exit 4;; esac; "$A" devices 2>&1 | tr -d "\\r"')
SKIP = {127: 'no adb there', 4: 'no adb server runs there: it holds no connection', 5: 'cannot tell whether an adb server runs '
        '(no ss): not touched'}


def ssh_host(cfg, name):
    return str(((cfg.get('machines') or {}).get(name) or {}).get('host') or name)


def listed(text):
    """The serials `adb devices` lists (any state but the header)."""
    out = []
    for line in (text or '').splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith(('List of', '*')):
            out.append(parts[0])
    return out


def match(serials, devs, ips):
    """[(serial, device name)] of the serials that are one of `devs`: by the mDNS guid in the serial, or by the device's IP
    (`ips`: {name: {ip}}, from the target's mDNS services)."""
    out = []
    for s in serials:
        host = s.rsplit(':', 1)[0] if ':' in s else ''
        for d in devs:
            guid = adbpair.guid_of(d['serial'])
            if (guid and guid in s) or s == str(d['serial']) or (host and host in ips.get(d['name'], set())):
                out.append((s, d['name']))
                break
    return out


def device_runners(cfg):
    """The machines named as a `runner:` anywhere outside `machines:` (the projects' device suites run there)."""
    found = set()

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == 'runner' and isinstance(v, str):
                    found.add(v)
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk({k: v for k, v in cfg.items() if k not in ('machines', '_path')})
    return sorted(n for n in found if n in (cfg.get('machines') or {}))


def device_ips(target, devs):
    """{name: {ip}}: each device's addresses, from the target's mDNS services and its own ip:port connections (a TV that
    stopped announcing still has one there)."""
    services = target.services()
    ips = {d['name']: {a.rsplit(':', 1)[0] for inst, svc, a in services
                       if svc == adbpair.CONNECT and inst == adbpair.guid_of(d['serial'])} for d in devs}
    hw = {adbpair.guid_of(d['serial'])[4:].rsplit('-', 1)[0]: d['name'] for d in devs if adbpair.guid_of(d['serial'])}
    for s in listed(target.adb('devices', timeout=30)[1]):
        name = hw.get(target.adb('-s', s, 'get-serialno', timeout=20)[1].strip()) if ':' in s else None
        if name:
            ips[name].add(s.rsplit(':', 1)[0])
    return ips


def runners_to_switch(cfg, here, plan):
    for r in device_runners(cfg):
        m = cfg['machines'][r]
        if ssh_host(cfg, r) == here or m.get('adb_via_device_host'):
            continue
        if m.get('transport') != 'wsl':
            plan['runners_skipped'][r] = 'not a Windows runner through WSL: it keeps its own adb'
        else:
            plan['runners'].append(r)


def scan(cfg, to, devs, host_fn, target):
    """What consolidating `devs` (all on `to`) would do: {to, devices, strays: {machine: [(serial, name)]}, skipped:
    {machine: why}, runners: [machine], runners_skipped: {machine: why}}. `host_fn(name)` gives a machine's adbhost.Host;
    `target` is `to`'s. Machines on `to`'s own computer are not asked; unreachable ones are skipped with the reason."""
    plan = {'to': to, 'devices': [d['name'] for d in devs], 'strays': {}, 'skipped': {}, 'runners': [], 'runners_skipped': {}}
    if not devs:
        return plan
    ips = device_ips(target, devs)
    here = ssh_host(cfg, to)
    for name in cfg['machines']:
        if name == to or ssh_host(cfg, name) == here:
            continue
        rc, out = host_fn(name).bash(LISTED, timeout=45)
        hit = match(listed(out), devs, ips) if rc == 0 else []
        if hit:
            plan['strays'][name] = hit
        elif rc:
            last = (out or '').strip().splitlines()
            plan['skipped'][name] = SKIP.get(rc) or f'did not answer ({last[-1] if last else f"exit {rc}"})'
    runners_to_switch(cfg, here, plan)
    return plan


def consolidate_lines(plan):
    to = plan['to']
    if not plan['devices']:
        return []
    out = [f'consolidate {", ".join(plan["devices"])} on {to}: every other machine lets go of them']
    for m, hits in plan['strays'].items():
        out += [f'{m}: adb disconnect {s} ({n}; the pairing stays)' for s, n in hits]
    for m, why in plan['skipped'].items():
        out.append(f'{m}: skipped: {why}')
    out += [f'{r}: set machines.{r}.adb_via_device_host: true (its suites use {to}\'s adb over an ssh tunnel)' for r in plan['runners']]
    for r, why in plan['runners_skipped'].items():
        out.append(f'{r}: runner left as it is: {why}')
    if not plan['strays'] and not plan['runners']:
        out.append('nothing to consolidate: no other machine holds a connection to them')
    return out


def pending(plan):
    return bool(plan['strays'] or plan['runners'])


def consolidate(cfg, plan, host_fn, say, sleep=None, settle=10):
    """Disconnects each stray, checks after `settle` s that it stayed away, sets adb_via_device_host on the runners.
    Returns {'disconnected': {machine: [serial]}, 'back': {machine: [serial]}, 'runners': [machine]}."""
    done, back = {}, {}
    for m, hits in plan['strays'].items():
        h = host_fn(m)
        for s, n in hits:
            h.adb('disconnect', s, timeout=30)
            done.setdefault(m, []).append(s)
            say(f'{m}: disconnected {s} ({n})')
    if done:
        (sleep or time.sleep)(settle)
        for m, serials in done.items():
            rc, out = host_fn(m).bash(LISTED, timeout=45)
            again = [s for s in listed(out) if s in serials] if rc == 0 else []
            if again:
                back[m] = again
    if plan['runners']:
        with open(cfg['_path'], encoding='utf8') as f:
            text = f.read()
        for r in plan['runners']:
            text = yamledit.edit(text, ['machines', r, 'adb_via_device_host'], True)
        config.write_text(cfg['_path'], text)
        for r in plan['runners']:
            say(f'{r}: adb_via_device_host set: its runs use {plan["to"]}\'s adb')
    return {'disconnected': done, 'back': back, 'runners': list(plan['runners'])}


def consolidate_summary(result, to):
    n = sum(len(v) for v in result['disconnected'].values())
    lines = [f'consolidated on {to}: {n} stray connection(s) disconnected; runners switched to {to}\'s adb: '
             f'{", ".join(result["runners"]) or "none"}']
    for m, serials in result['back'].items():
        lines.append(f'{m}: {", ".join(serials)} connected again by itself (its adb server auto-connects to paired devices it '
                     f'hears over mDNS): set ADB_MDNS_AUTO_CONNECT=0 for that adb server, then run the move again')
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
    config.write_text(path, text)
    return name
