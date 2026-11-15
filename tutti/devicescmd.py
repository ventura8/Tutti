"""`tutti devices pair` and `tutti devices move`: the terminal side of pairing over wireless debugging and of moving
devices to another machine (docs/DEVICES.md). The dashboard's Pair a device and Move devices buttons call the same code."""
import sys

from . import adbhost, adbpair, devmove, qr


def say(msg):
    print(f'tutti: {msg}', file=sys.stderr)


def default_machine(cfg):
    return next((n for n, m in cfg['machines'].items() if m['transport'] == 'local'), None) or next(iter(cfg['machines']))


def host_for(cfg, name, adb=None):
    return adbhost.Host(name, cfg['machines'][name], adb=adb)


def ensure_adb(host, install, say_fn=say):
    """adb's path on the machine; with `install` a user-space copy when there is none. '' when it is still missing."""
    path = host.path()
    if path or not install:
        return path
    say_fn(f'installing Android platform-tools on {host.name} (~/.local/share/tutti/platform-tools)')
    rc, out = host.install_tools()
    if rc != 0:
        say_fn(f'the install failed on {host.name}: {out.strip()[-300:]}')
        return ''
    return host.path()


def kind_hint(host, a, usb):
    """The device's kind before pairing: --tv, else read over USB, else unknown (a phone or tablet: the QR flow)."""
    if a.tv:
        return 'tv'
    if usb:
        return adbpair.describe(host, usb[0])['kind']
    return None


def show_steps(kind, flow):
    print('On the device:')
    for i, step in enumerate(adbpair.steps(kind, flow), 1):
        print(f'  {i}. {step}')


def pair_one(cfg, host, a, kind=None, read_code=input):
    """Pairs and connects one device on `host`; returns (the serial to keep, the serial adb lists now)."""
    before = adbpair.baseline(host)   # before the user opens the device's pairing screen
    rc, listed = host.adb('devices', timeout=30)
    usb = adbpair.usb_serials(listed) if rc == 0 else []
    kind = kind or kind_hint(host, a, usb)
    flow = adbpair.flow_for(kind, a.flow)
    if usb and adbpair.open_settings(host, usb[0]):
        print(f'Developer options are open on the device plugged in by USB ({usb[0]}).')
    show_steps(kind, flow)
    if flow == 'usb':
        if not usb:
            raise adbpair.PairError('no device on USB: plug it in, allow USB debugging on it, then run again')
        serial = adbpair.via_usb(host, usb[0])
        return serial, serial
    if flow == 'code':
        code = a.code or read_code('Type the 6-digit code the device shows: ').strip()
        if not (code.isdigit() and len(code) == 6):
            raise adbpair.PairError('the pairing code is the 6 digits the device shows: open its pairing screen and run again')
        return adbpair.pair(host, code=code, ignore=before, wait=a.wait, say=say)
    name, password = adbpair.session()
    payload = adbpair.qr_payload(name, password)
    print(qr.terminal(payload))
    print(f'(the QR code holds: {payload}; it works once, for {a.wait} s)')
    return adbpair.pair(host, name=name, password=password, wait=a.wait, say=say)


def cmd_pair(cfg, a, read_code=input):
    machine = a.machine or default_machine(cfg)
    if machine not in cfg['machines']:
        say(f'no machine "{machine}": the machines are {", ".join(cfg["machines"])}')
        return 2
    host = host_for(cfg, machine, a.adb)
    path = ensure_adb(host, a.install_adb)
    if not path:
        say(f'adb is not on {machine}: run again with --install-adb (a user-space copy of Android platform-tools)')
        return 1
    if host.version() < adbpair.MIN_ADB and a.flow != 'usb':
        say(f'adb on {machine} is too old for wireless pairing: it needs platform-tools {adbpair.MIN_ADB}+; '
            'run again with --install-adb')
        return 1
    try:
        serial, live = pair_one(cfg, host, a, read_code=read_code)
    except adbpair.PairError as e:
        say(str(e))
        return 1
    info = adbpair.describe(host, live)
    name = devmove.save_device(cfg['_path'], cfg.get('devices'), machine, serial, info, adb=a.adb, name=a.name)
    print(f'{name}: {info["model"]} ({info["kind"]}; {", ".join(info["capabilities"]) or "no extras"}) on {machine}, '
          f'serial {serial}; saved in {cfg["_path"]}')
    return 0


def cmd_move(cfg, a, read_code=input):
    try:
        found = devmove.pick(cfg, a.to, a.names or [])
    except devmove.MoveError as e:
        say(str(e))
        return 2
    src = found[0]['machine']
    old = host_for(cfg, src, found[0].get('adb'))
    new = host_for(cfg, a.to)
    if not a.dry_run and not ensure_adb(new, a.install_adb):
        say(f'adb is not on {a.to}: run again with --install-adb (a user-space copy of Android platform-tools)')
        return 1
    choice = 'keep' if a.keep_key else 'repair' if a.repair else None
    pre = devmove.preflight(cfg, a.to, found, old, new, choice)
    for line in devmove.plan_lines(pre, found):
        print(line)
    problems = pre['problems']
    for p in problems:
        say(f'cannot move yet: {p}')
    if a.dry_run:
        print('dry run: nothing changed')
        return 1 if problems else 0
    if problems:
        return 1

    def repair(d):
        print(f'Pair {d["name"]} again on {a.to}:')
        pair_one(cfg, new, a, kind=d.get('kind'), read_code=read_code)
    try:
        result = devmove.run(cfg, a.to, found, old, new, pre, say=say, pair_fn=repair)
    except (devmove.MoveError, adbpair.PairError) as e:
        say(str(e))
        return 1
    for line in devmove.summary(result, a.to, src):
        print(line)
    return 0 if not result['stayed'] else 1


def cmd_claim(cfg, a):
    """`tutti devices claim <device> --who <session>`: a session that uses the device outside Tutti's runs says so; the
    device is not released (its own settings put back) while a claim is there. `release` takes the claim away, and the
    device is released when nobody else uses it."""
    from . import devices, dispatch, prep, remote
    if not a.names or not a.who:
        say(f'name the device and who you are: tutti devices {a.action} <device> --who <session>')
        return 2
    d = devices.of(cfg, a.names[0])
    claim = f'{prep.users_path(d)}/claim-{"".join(c if c.isalnum() or c in "-_." else "_" for c in a.who)}'
    if a.action == 'claim':
        script = prep.lease_script(d, claim, a.minutes, a.who)
    else:
        ext = devices.external_lock(d)
        stale = int(float(d.get('external_lock_stale_minutes', devices.EXTERNAL_STALE_MINUTES)) * 60)
        script = f'rm -f {claim}; {prep.release_script(d, ext, stale)}'
    try:
        r = remote.sh(cfg['machines'][d['machine']], script, capture=True, timeout=180)
    except (OSError, ValueError) as e:
        say(f'{d["machine"]} did not answer ({e}): try again when it is on')
        return 1
    print((r.stdout or '').strip())
    if a.action == 'claim' and r.returncode == 3:
        return 3
    if a.action == 'release':
        dispatch.announce_free(cfg, dispatch.released(r.stdout))
    return 0 if r.returncode == 0 else 1


def cmd_install(cfg, a):
    """`tutti devices install <device> <apk>`: refused unless the APK is debuggable; one install per device at a time."""
    import os
    import subprocess

    from . import apk, devices, remote
    if len(a.names or []) != 2:
        say('name the device and the APK: tutti devices install <device> <file.apk>')
        return 2
    d, path = devices.of(cfg, a.names[0]), a.names[1]
    try:
        apk.check(path)
    except apk.ApkError as e:
        say(f'install refused: {e}')
        return 1
    import time
    machine = cfg['machines'][d['machine']]
    there, copied = os.path.abspath(path), False
    if machine['transport'] != 'local':   # a path of its own per install: two installs never overwrite each other's file
        there, copied = f'.tutti/apk/{d["name"]}-{os.getpid()}-{int(time.time() * 1000)}-{os.path.basename(path)}', True
        remote.sh(machine, 'mkdir -p ~/.tutti/apk', capture=True, timeout=60)
        if subprocess.run(['rsync', '-a', '-e', remote.rsh(cfg, machine), path, f'{remote.dest(machine)}:{there}'],
                          check=False).returncode != 0:
            say(f'the APK could not be copied to {d["machine"]}: check it answers (`tutti machines`)')
            return 1
    rm = f'; rc=$?; rm -f {there}; exit $rc' if copied else ''
    return remote.sh(machine, apk.install_script(d, there) + rm).returncode


def cmd_hours(cfg, a):
    """`tutti devices hours <device> <HH-HH>[,HH-HH] [--days mon-fri] | off`: the device's recurring blocked hours, in the
    config (so `tutti move` carries them)."""
    from . import devices, hours, settings, yamledit
    if len(a.names or []) != 2:
        say('name the device and the hours: tutti devices hours <device> 22-07[,12-13] [--days mon-fri], or off')
        return 2
    d, spec = devices.of(cfg, a.names[0]), a.names[1]
    try:
        value = yamledit.DELETE if spec == 'off' else {'window': ','.join(f'{x:02d}-{y:02d}' for x, y in hours.parse(spec)),
                                                       **({'days': hours.parse_days(a.days)} if a.days else {})}
        settings.store(cfg, [(['devices', d['name'], 'hours'], value)])
    except (ValueError, settings.SettingsError) as e:
        say(f'devices hours: {e}')
        return 2
    print(f'{d["name"]}: ' + ('no blocked hours' if spec == 'off' else
                              f'not used {value["window"]}' + (f' on {",".join(value["days"])}' if a.days else ' every day')
                              + '; no new work from 30 min before; its own settings come back'))
    return 0


def cmd_lease(cfg, a):
    """`tutti devices lease <device> --who <session> -- <command>`: the device leased (claim), this computer's adb connected
    to it over Wi-Fi (the same adb key the household's machines share), the command run here with ANDROID_SERIAL set to
    that address, then adb disconnected and the lease released, however the command ends."""
    import io
    import os
    import subprocess
    from contextlib import redirect_stdout

    from . import adbhost
    command = [w for w in (a.names or [])[1:] if w != '--']
    a.names = (a.names or [])[:1]
    if not a.names or not command:
        say('name the device and the command: tutti devices lease <device> --who <session> -- <command ...>')
        return 2
    a.who = a.who or 'lease'
    out = io.StringIO()
    with redirect_stdout(out):
        a.action = 'claim'
        rc = cmd_claim(cfg, a)
    text = out.getvalue()
    print(text.strip())
    if rc != 0:
        return rc
    addr = next((x[5:] for x in text.split() if x.startswith('ADDR=')), 'unknown')
    here = adbhost.Host('here', {'transport': 'local'})
    try:
        if addr != 'unknown':
            here.adb('connect', addr, timeout=30)
        serial = addr if addr != 'unknown' else str(cfg['devices'][a.names[0]]['serial'])
        env = {**os.environ, 'ANDROID_SERIAL': serial}
        return subprocess.run(command, env=env, check=False).returncode
    finally:
        if addr != 'unknown':
            here.adb('disconnect', addr, timeout=30)
        a.action = 'release'
        cmd_claim(cfg, a)


ACTIONS = {'lease': cmd_lease, 'hours': cmd_hours, 'pair': cmd_pair, 'move': cmd_move, 'claim': cmd_claim, 'release': cmd_claim,
           'install': cmd_install}


def add_arguments(s):
    """The `tutti devices` options for pair and move."""
    s.add_argument('names', nargs='*', help='move: the devices to move (default: every device not on --to); claim, release: '
                   'the device; install: the device and the APK')
    s.add_argument('--to', help='move: the machine the devices go to')
    s.add_argument('--dry-run', action='store_true', help='move: print the plan and the preflight, change nothing')
    s.add_argument('--replace-key', action='store_true', help='move: the default (kept for old scripts): back up the new '
                   'machine\'s own adb key and use the old one')
    s.add_argument('--keep-key', action='store_true', help='move: keep the new machine\'s own adb key (then --repair is needed)')
    s.add_argument('--repair', action='store_true', help='move: last resort: pair each device again instead of copying the adb key')
    s.add_argument('--install-adb', action='store_true', help='pair, move: install platform-tools in user space if adb is missing')
    s.add_argument('--flow', choices=adbpair.FLOWS, help='pair: qr (default), code (a TV: default) or usb')
    s.add_argument('--tv', action='store_true', help='pair: a TV (no camera): the pairing-code flow')
    s.add_argument('--code', help='pair: the 6-digit code the device shows (asked when missing)')
    s.add_argument('--adb', help='pair: the adb to use on the machine (default: found by itself)')
    s.add_argument('--wait', type=int, default=180, help='pair: seconds to wait for the device (180)')
    s.add_argument('--who', help='claim, release: the session or tool that uses the device')
    s.add_argument('--minutes', type=int, default=120, help='claim, lease: how long the lease lasts at most (120)')
    s.add_argument('--days', help='hours: the days, e.g. mon-fri or sat,sun (default every day)')
