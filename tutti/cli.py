"""The `tutti` command. Every subcommand reads tutti.config.yaml; nothing personal is built in."""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from . import (
    __version__,
    authcmd,
    autopilot,
    backlog,
    bridge,
    browsercmd,
    clientmode,
    conduct,
    config,
    connect,
    controls,
    dashboard,
    delegate,
    devices,
    discover,
    dispatch,
    doctor,
    files,
    github,
    githubcmd,
    handover,
    helper,
    householdcmd,
    local,
    mcpserver,
    move,
    pacing,
    readers,
    remote,
    report,
    review,
    routing,
    scoutcmd,
    scouts,
    secretenv,
    selfimprove,
    ship,
    state,
    telemetry,
    telemetrycmd,
    transfer,
    translate,
    trial,
    tz,
    update,
    vpncmd,
    waiting,
    workers,
    yamledit,
)

SCRIPTS = remote.SCRIPTS
NONE = '(none)'
SUMMARY = 'In at most 5 short lines: which check failed, the file:line and the exact error. No advice.'


def say(msg):
    sys.stderr.write(f'tutti: {msg}\n')


def machine_of(cfg, name):
    if name not in cfg['machines']:
        raise config.ConfigError(f'no machine "{name}" in {cfg["_path"]}: the machines are {", ".join(cfg["machines"]) or "(none)"}')
    return cfg['machines'][name]


def worker_of(cfg, name, kind):
    w = cfg['workers'].get(name)
    if not w and name in cfg.get('disabled_workers', []):
        paid = any(o['name'] == name and o['costs_money'] for o in cfg['off'])
        why = 'it costs money per use and is opt-in' if paid else 'enabled: false'
        raise config.ConfigError(f'"{name}" is turned off in {cfg["_path"]} ({why}): set workers.{name}.enabled: true to use it')
    if not w or w['kind'] != kind:
        names = [n for n, x in cfg['workers'].items() if x['kind'] == kind]
        have = ', '.join(names) or NONE
        raise config.ConfigError(f'"{name}" is not a {kind} worker in {cfg["_path"]}: the {kind} workers are {have}')
    return w


def lead_hours_warnings(cfg):
    """One line per account whose lead_hours is not the default 1 (the old example said 3), with the exact fix."""
    out = []
    for name, acc in cfg['accounts'].items():
        lead = (acc or {}).get('lead_hours', 1)
        if lead != 1:
            out.append(f'account "{name}" has lead_hours: {lead} (the old example\'s value; the default is 1 hour before the reset): '
                       f'set lead_hours: 1 for "{name}" in {cfg["_path"]}, or remove the key')
    return out


def cmd_config(cfg, a):
    print(f'{cfg["_path"]}: ok ({len(cfg["machines"])} machines, {len(cfg["workers"])} workers, {len(cfg["accounts"])} accounts)')
    for line in lead_hours_warnings(cfg):
        say(f'warning: {line}')
    return 0


def config_problems(a):
    """The drifted copies of the config (docs/CONFIG.md); with --merge-configs, merged into the one in use first."""
    from . import configone
    try:
        cfg = config.load(a.config)
    except config.ConfigError:
        return []   # no config yet, or a broken one: `tutti config` says which
    if getattr(a, 'merge_configs', False):
        try:
            done = configone.merge(cfg)
        except (config.ConfigError, OSError, yamledit.EditError) as e:
            return [f'the copies were not merged: {e}']
        for m in done['merged']:
            print(f'merged {m["path"]} into {done["file"]}: {len(m["added"])} key(s) added, {len(m["kept"])} kept as they were '
                  f'in {done["file"]} ({", ".join(m["kept"]) or "none"}); '
                  + (m['error'] if m.get('error') else f'the copy is now a link to it, backup {m["backup"]}'))
    return configone.warnings(cfg)


def cmd_doctor(cfg, a):
    from . import diskroom, stuck
    problems = doctor.check_git() + config_problems(a) + secretenv.doctor_problems() + (diskroom.problems(cfg) if cfg else [])
    problems += [w for w in [stuck.check(remote.ROOT)] if w]   # merged versions no release publishes (docs/INSTALL.md)
    if cfg:   # the state's size against its budget, and the backup main machine (docs/FAILOVER.md)
        from . import failover, failovercmd, statebudget
        lines, over = statebudget.report(config.state_dir(cfg), statebudget.opts(cfg))
        print('\n'.join(lines))
        print(failovercmd.describe(failover.view(cfg)))
        problems += over
    for line in problems:
        say(f'problem: {line}')
    for line in [w for w in [github.doctor_line(cfg)] if w]:   # a note, not a problem: GitHub is optional (docs/GITHUB.md)
        say(f'note: {line}')
    print('doctor: ' + (f'{len(problems)} problem(s)' if problems else 'ok'))
    return 1 if problems else 0


def cmd_update(cfg, a):
    if a.machine and a.machine not in cfg['machines']:
        say(f'"{a.machine}" is not under machines: in {cfg["_path"]}: the machines are {", ".join(cfg["machines"]) or NONE}')
        return 2
    try:
        data = update.run(cfg, check=a.check, only=a.machine)
    except update.Busy as e:
        say(str(e))
        return 1
    print(update.report(data) or 'nothing to update: no CLI workers, models or update.packages in the config')
    failed = any(r['error'] or any(x['status'] == 'failed' for x in r['items']) for r in data['machines'].values())
    return 1 if failed or any(x['status'] == 'failed' for x in data['models']) else 0


def cmd_setup(cfg, a):
    """`tutti setup`: the guided first run (docs/INSTALL.md); works before any config exists."""
    from . import setupcmd
    try:
        return setupcmd.run(a)
    except config.ConfigError as e:
        say(str(e))
        return 1


def cmd_self_update(cfg, a):
    """`tutti self-update [--check|--auto]`: the newest checksummed release (docs/INSTALL.md, *Updates*)."""
    from . import install
    home_dir = install.home()
    if not install.is_release_install(remote.ROOT, home_dir):
        if a.auto:
            return 0
        say(f'self-update: this Tutti runs from {remote.ROOT}, not a release install: update it the way it was installed '
            '(git pull, uv tool upgrade tutti-agents or pipx upgrade tutti-agents)')
        return 1
    if a.auto:
        try:
            on = (config.load(a.config).get('self_update') or {}).get('enabled', True)
        except config.ConfigError:
            on = True
        if not on:
            return 0
    skip = bool(getattr(a, 'insecure_skip_verify', False)) and not a.auto   # never unattended
    if skip:
        say(f'self-update: {install.SKIP_WARNING}')
    code, msg = install.self_update(home_dir, check_only=a.check, skip_verify=skip)
    say(msg)
    return code


def cmd_machines(cfg, a):
    """Exit 1 when a machine does not answer, so a script can tell."""
    if a.action == 'add-android':
        print(devices.TERMUX_STEPS.format(name=a.name or 'phone', host=a.host or '<the device IP>', user=a.user or '<whoami>',
                                          port=a.port))
        return 0
    down = 0
    for name, m in cfg['machines'].items():
        p = remote.probe(m)
        down += not p.reachable
        state = routing.unfit(m, p) or 'fit'
        print(f'{name:12} {m["transport"]:7} slots {p.free}/{m["slots"]}  load {p.load:g}/{p.cores}{heat_text(p)}  {state}')
    return 1 if down else 0


def heat_text(p):
    out = ''.join(f'  {w} {t:.0f} C' for t, w in ((p.temp, 'CPU'), (p.gpu_temp, 'GPU')) if t is not None)
    out += '  CPU not readable' if p.temp is None and p.temp_note else ''
    return out + (power_text(p.readings) if p.readings is not None else '')


def power_text(r):
    """`  battery 80 % charging 31 C wifi` from a device's readings."""
    parts = [f'battery {r["battery"]:g} %' if r.get('battery') is not None else 'battery ?',
             {1: 'charging', 0: 'on battery'}.get(r.get('charging'), ''),
             f'{r["battery_c"]:g} C' if r.get('battery_c') is not None else '',
             {1: 'wifi', 0: 'no wifi'}.get(r.get('wifi'), ''), {1: 'screen on', 0: 'screen off'}.get(r.get('screen'), '')]
    return '  ' + ' '.join(x for x in parts if x)


def cmd_devices(cfg, a):
    """Exit 1 when a device is offline (not connected, not authorised, or its machine does not answer)."""
    if a.action in ('pair', 'move', 'claim', 'release', 'install', 'hours', 'lease', 'night'):
        from . import devicescmd
        return devicescmd.ACTIONS[a.action](cfg, a)
    if a.action == 'add':
        print(devices.ADB_STEPS.format(name=a.name or 'phone', serial=a.serial or '<serial from adb devices>',
                                       machine=a.machine or next(iter(cfg['machines']), 'here')))
        return 0
    if not cfg['devices']:
        say('no devices: `tutti devices pair` pairs one over Wi-Fi (`tutti devices add` prints the manual setup)')
        return 0
    from . import keepon  # a device whose Wireless debugging went off is switched back on through any machine that reaches it
    offline = 0
    for row in devices.devices_state(cfg, keepon.readings_with_revive(cfg, remote.device_readings)):
        offline += row['status'] == 'offline'
        print(f'{row["name"]:12} {row["serial"]:22} on {row["machine"]:10} {row["status"]:8}{power_text(row)}  {row["reason"]}')
    return 1 if offline else 0


def cmd_control(cfg, a):
    """`tutti pause|resume <kind> <name> [--stop-now]` and `tutti optin <kind> <name> on|off`: the dashboard's switches."""
    section = controls.SECTIONS[a.kind]
    names = cfg['raw_entries'].get(section) or cfg.get(section) or {}
    if a.name not in names:
        say(f'no {a.kind} "{a.name}" in {cfg["_path"]}: the {section} are {", ".join(names) or NONE}')
        return 2
    if a.cmd == 'optin':
        controls.set_enabled(config.state_dir(cfg), a.kind, a.name, a.state == 'on', source='cli')
        print(f'{a.kind} {a.name}: {a.state}')
        return 0
    pause = a.cmd == 'pause'
    start = until = None
    if pause and (getattr(a, 'start', None) or getattr(a, 'until', None)):
        try:
            start = controls.parse_when(a.start or 'now')
            until = None if (a.until or 'unknown') == 'unknown' else controls.parse_when(a.until, after=start)
        except ValueError as e:
            say(str(e))
            return 2
        if until is not None and until <= start:
            say(f'--until is not after --from: the absence would be over before it starts; give an end after {a.start or "now"}')
            return 2
        if a.stop_now:
            say('--stop-now does not go with --from: a planned absence hands its running work over at its start by itself')
            return 2
    controls.set_paused(config.state_dir(cfg), a.kind, a.name, pause, source='cli', note=getattr(a, 'note', None) or '',
                        start=start, until=until)
    if start is not None:
        print(controls.paused_note({'paused': controls.read(config.state_dir(cfg))['paused'][controls.key(a.kind, a.name)]}))
        return 0
    print(f'{a.kind} {a.name}: {"paused" if pause else "resumed"}')
    handed = handover.stop_now(config.load(cfg['_path']), a.kind, a.name) if pause and a.stop_now else []
    for log, to in handed:
        print(f'  {os.path.basename(log)}: {to}')
    return 0


def tree_and_command(cfg, a):
    """(the tree, the command words) of a `tutti test`. `tutti test --device pixel -- ./gradlew connectedAndroidTest`
    names no track: the current folder is the tree and every word is the command."""
    src = config.expand(a.dir) if a.dir else os.path.join(config.expand(cfg['project']['worktrees']), a.track)
    if a.device and not a.dir and not os.path.isdir(src):
        src, command = os.getcwd(), [a.track, *a.command]
        a.track = os.path.basename(src) or 'here'
        return src, command
    return src, a.command


def test_choice(cfg, a):
    """(routing.Choice, device or None) for a `tutti test`; raises ConfigError when no device gets ready."""
    if a.device:
        device = devices.wait_ready(cfg, a.device, remote.device_readings, say=lambda m: say(f'test: {m}'), wait_minutes=a.wait_minutes)
        return routing.Choice(device['machine'], False, f'the machine device {device["name"]} is on'), device
    if a.on:
        return routing.Choice(machine_of(cfg, a.on)['name'], False, 'chosen with --on'), None
    try:
        from . import sharing  # a household's shared machine offers only this member's grant (docs/HOUSEHOLD.md)
        now = time.time()
        return routing.choose(cfg['routing']['order'], cfg['machines'],
                              lambda n: sharing.limited_probe(cfg, n, remote.probe(cfg['machines'][n]), now)), None
    except routing.NoMachine as e:
        raise config.ConfigError(str(e)) from e


def test_job(cfg, a, machine, device, src, command):
    """The run's job record: the dashboard shows it, and a "pause, stop now" knows how to start it again elsewhere."""
    args = ['--dir', src, *[f'--input={p}' for p in a.input or []],
            *[f'--fetch={p}' for p in a.fetch or []], a.track, *command]
    on = machine['name'] + (f' / {device["name"]}' if device else '')
    target = f'machine:{machine["name"]}' + (f' device:{device["name"]}' if device else '')
    if os.environ.get('TUTTI_HH_JOB'):   # a household member's job this owner's Tutti runs: the member's demand, not ours
        target += ' class:member'
    if os.environ.get('TUTTI_BACKGROUND'):   # started by the autopilot: background demand on a household's shared machine
        target += ' class:background'
    return transfer.Job(cfg, f'test {a.track} on {on}: {" ".join(command)}'[:200], folder='devices' if device else 'tests',
                        target=target, spec={'kind': 'test', 'track': a.track, 'device': bool(device), 'args': args})


def test_on_runner(cfg, a, runner):
    """`tutti test <Project> --device <d> -- <command>` for a project with `runner:` (a PC that builds and runs the tests): the device is
    leased and the runner's own adb connected to it, and the command runs there in the project's own tree (its `windows:`
    path), never synced from here (docs/DISPATCH.md, *A suite on a runner machine*)."""
    import argparse

    from . import devicescmd, shards
    why = shards.empty_filter(command_line(a.command)) or \
        ('--input and --fetch need a synced tree: on a project runner the command reads and writes its own tree'
         if a.input or a.fetch else '')
    if why:
        say(f'test: refused: {why}')
        return 2
    holds = list(getattr(a, 'hold_device', None) or [])
    words = a.command[1:] if a.command[:1] == ['--'] else a.command   # only a leading separator: the command keeps its own
    lease = argparse.Namespace(names=[a.device, *holds, '--', command_line(words)], runner=runner,
                               who=f'test-{a.track}', minutes=None, action='claim')
    return devicescmd.lease_on_runner(cfg, lease)


TEST_OPTIONS = {'--dir': 'dir', '--on': 'on', '--device': 'device', '--wait-minutes': 'wait_minutes'}
TEST_LISTS = {'--fetch': 'fetch', '--input': 'input', '--hold-device': 'hold_device'}


def late_options(a):
    """Options written after the track (`tutti test MyApp --on pc -- cmd`) are taken as options, up to a `--`
    or the first word that is not one of them; the rest is the command, and a lone `--` before it is dropped."""
    words, i = list(a.command or []), 0
    while i < len(words) and (words[i] in TEST_OPTIONS or words[i] in TEST_LISTS) and i + 1 < len(words):
        flag, value = words[i], words[i + 1]
        if flag in TEST_LISTS:
            setattr(a, TEST_LISTS[flag], [*(getattr(a, TEST_LISTS[flag], None) or []), value])
        else:
            setattr(a, TEST_OPTIONS[flag], int(value) if flag == '--wait-minutes' else value)
        i += 2
    words = words[i:]
    a.command = words[1:] if words[:1] == ['--'] else words
    return a


def command_line(words):
    """One shell line from the command words: a single word is the line as written (a quoted string); of several, a word
    with a space or a quote is quoted (it reaches the machine whole) and every other word stays raw, so `&&`, `|`, `>`
    and `$ANDROID_SERIAL` keep working as before."""
    import shlex
    if len(words) == 1:
        return words[0]
    return ' '.join(shlex.quote(w) if re.search(r'[\s\'"]', w) or not w else w for w in words)


WINDOWS_PATH = re.compile(r'^([A-Za-z]):[\\/](.*)$')


def wsl_path(path):
    """C:\\Users\\x -> /mnt/c/Users/x (a Windows path seen from the PC's WSL); other paths unchanged."""
    m = WINDOWS_PATH.match(str(path))
    return f'/mnt/{m.group(1).lower()}/' + m.group(2).replace('\\', '/') if m else str(path)


def windows_tree(cfg, a):
    """The project's own work tree on the chosen machine (`projects.<p>.windows.<machine>`), used as it is: nothing synced."""
    project = (cfg.get('projects') or {}).get(a.track) or {}
    path = (project.get('windows') or {}).get(a.on or '') if not a.dir else None
    return wsl_path(path) if path else ''


def test_in_windows_tree(cfg, a, tree):
    import shlex
    machine = cfg['machines'][a.on]
    if a.input:
        say('test: refused: --input needs a synced tree; the project\'s own work tree is used as it is (copy the file there)')
        return 2
    say(f'test: {a.on} (the project\'s own work tree {tree}, nothing synced)')
    rc = remote.sh(machine, f'cd {shlex.quote(tree)} && {command_line(a.command)}').returncode
    for p in a.fetch or []:   # a relative path is under the project's tree
        fetch_windows(machine, p if WINDOWS_PATH.match(str(p)) or str(p).startswith('/') else f'{tree}/{p}')
    return rc


def fetch_windows(machine, path, dest='.'):
    """Copies one file from a Windows path (or any path on that machine) into `dest`, by base64 over the machine's shell."""
    import base64
    import shlex
    r = remote.sh(machine, f'base64 -w0 {shlex.quote(wsl_path(path))}', capture=True)
    if r.returncode != 0:
        say(f'test: could not copy {path} back from {machine["name"]}: {(r.stderr or "").strip()[:200]}')
        return 1
    name = os.path.join(dest, re.split(r'[\\/]', str(path))[-1])
    with open(name, 'wb') as f:
        f.write(base64.b64decode(r.stdout))
    say(f'test: copied {path} to {name}')
    return 0


def cmd_test(cfg, a):
    late_options(a)
    if a.on and a.on in cfg['machines'] and windows_tree(cfg, a) and a.command and not a.device:
        return test_in_windows_tree(cfg, a, windows_tree(cfg, a))
    if not a.command and not (a.device and a.track):
        say('name the command: tutti test <track> <command ...> (or one quoted string); '
            'with a device: tutti test --device <name> -- <command>')
        return 2
    project = (cfg.get('projects') or {}).get(a.track) or {}
    if a.device and project.get('runner') and not a.on:   # the project's runner runs it with its own tree: nothing synced
        return test_on_runner(cfg, a, project['runner'])
    src, command = tree_and_command(cfg, a)
    if a.device:
        from . import shards
        empty = shards.empty_filter(command_line(command))
        if empty:
            say(f'test: refused: {empty}')
            return 2
    if not os.path.isdir(src):
        say(f'no work tree at {src}: create the worktree for track "{a.track}" there, or pass --dir <path>')
        return 2
    try:
        choice, device = test_choice(cfg, a)
    except config.ConfigError as e:
        say(f'test: {e}')
        return 3
    say(f'test: {choice.machine}{", queued" if choice.queued else ""} ({choice.reason})' + (f', device {device["name"]}' if device else ''))
    machine = cfg['machines'][choice.machine]
    if machine['transport'] == 'household':   # another member's machine: its owner's Tutti runs it (tutti/hhjobs.py)
        return household_test(cfg, a, machine, src, command)
    how = 'on' if a.on else 'routed'
    if device:
        how = 'device'
    telemetry.record('feature', 'routing.choice', how=how, machine_kind=telemetry.machine_kind(machine), queued=bool(choice.queued),
                     order=len(cfg['routing']['order']))
    started = time.monotonic()
    job = test_job(cfg, a, machine, device, src, command)
    rc = 1
    try:
        with job_inputs(cfg, machine, a.input) as base:
            holds = [devices.of(cfg, n) for n in getattr(a, 'hold_device', None) or []]
            rc = remote.run(cfg, machine, a.track, src, command_line(command), inputs=base and transfer.shell_path(base), device=device,
                            out=job.log, holds=holds)
        fetch_results(cfg, machine, a.track, src, a.fetch)
        return rc
    finally:
        job.end(rc)
        telemetry.record('feature', 'device.run' if device else 'test.run', machine_kind=telemetry.machine_kind(machine), rc=rc,
                         outcome='passed' if rc == 0 else 'failed', minutes=round((time.monotonic() - started) / 60, 2),
                         inputs=len(a.input or []), fetch=len(a.fetch or []))


def household_test(cfg, a, machine, src, command):
    from . import hhjobs
    if a.input or a.fetch or a.device:
        say(f'test: {machine["name"]} is another member\'s machine: --input, --fetch and --device go to your own machines; '
            'route this run elsewhere (--on <machine>)')
        return 2
    job = test_job(cfg, a, machine, None, src, command)
    rc = 1
    try:
        rc = hhjobs.run_on(cfg, machine, src, command, out=sys.stdout, say=lambda m: (say(f'test: {m}'), job.say(m)))
        return rc
    finally:
        job.end(rc)
        telemetry.record('feature', 'test.run', machine_kind='household', rc=rc, outcome='passed' if rc == 0 else 'failed')


def fetch_results(cfg, machine, track, src, paths):
    for p in paths or []:
        if WINDOWS_PATH.match(str(p)):   # a Windows path on a WSL machine: read where it is, not under the synced tree
            fetch_windows(machine, p)
            continue
        if remote.fetch(cfg, machine, track, src, p) != 0:
            say(f'test: could not copy {p} back from {machine["name"]}: it may not exist when the run made none')


@contextmanager
def job_inputs(cfg, machine, paths):
    """A job's --input files, copied to the machine's cache first and pinned there until the job ends (no eviction, clean or
    replacing send touches them meanwhile). Yields the cache folder, or None without inputs."""
    if not paths:
        yield None
        return
    token = f'{os.getpid()}-{int(datetime.now(timezone.utc).timestamp())}'
    base, result = transfer.send(cfg, machine, [os.path.abspath(p) for p in paths], pin_token=token)
    say(f'inputs on {machine["name"]}: ' + ', '.join(f'{n} ({how})' for n, how, _ in result) + f' in {base} ($TUTTI_INPUTS)')
    try:
        yield base
    finally:
        transfer.unpin(machine, base, [n for n, _, _ in result], token)


def cmd_rsh(cfg, a):
    return remote.rsh_main(machine_of(cfg, a.machine), a.args)


def cmd_ship(cfg, a):
    if os.environ.get('TUTTI_CONDUCT') and (not a.dry_run or a.allow_protected):
        raise ship.ShipError(f'a main agent is conducting (run {os.environ["TUTTI_CONDUCT"]}): ship only with --dry-run and without '
                             '--allow-protected; a person ships after reading the plan')
    ship.ship(cfg, a.dir, a.message, full=a.full, no_add=a.no_add, track=a.track, allow_protected=a.allow_protected, dry_run=a.dry_run)
    return 0


def usage_log_path(cfg):
    return config.expand((cfg.get('usage') or {}).get('log') or os.path.join(config.state_dir(cfg), 'usage.jsonl'))


def read_now(cfg, path, now):
    """`tutti usage read`: every automatic reader once and the recent job logs; prints what was found and any problem."""
    r = readers.Readers(cfg)
    found = r.read_all(now, force=True)
    written = readers.record(path, found, r.scan_jobs(now), now)
    for name, x in found.items():
        if x['problem']:
            say(f'{name}: {x["problem"]}')
    say(f'usage read: {len(found)} account(s) with a reader, {len(written)} new reading(s) in {path}')


def page_scripts(cfg, match=None):
    """`tutti usage page-script [<url part>]`: for each usage page the reading helper knows, the JavaScript an orchestrator runs
    in the owner's open, signed-in tab (its browser tool), then `tutti usage page-reading '<the JSON it returned>'`."""
    pages = {u: t for u, t in helper.helper_pages(cfg).items() if not match or match in u}
    if not pages:
        say('usage page-script: no account has a usage_url without its own reader' + (f' matching "{match}"' if match else ''))
        return 2
    for url, targets in pages.items():
        names = ', '.join(t['account'] for t in targets)
        print(f"// {url} ({names}): open it signed in, run this, then tutti usage page-reading '<result>'")
        print(helper.page_script(targets))
    return 0


def page_reading(cfg, path, text, now):
    """`tutti usage page-reading '<json>'` (or `-` for stdin): records what a page script returned, each reading checked as the
    dashboard checks a posted one."""
    try:
        data = json.loads(sys.stdin.read() if text in (None, '-') else text)
        readings, missing = data['readings'], data.get('missing') or []
        if not (isinstance(readings, list) and all(isinstance(r, dict) for r in readings)
                and isinstance(missing, list) and all(isinstance(m, str) for m in missing)):
            raise TypeError('readings must be a list of objects and missing a list of labels')
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        say(f'usage page-reading: the text is not what `tutti usage page-script` returns ({e}): pass the JSON string it evaluated to')
        return 2
    if data.get('loading') and not readings:
        say('usage page-reading: the page was still loading (no meter shown, e.g. its spinner), so nothing was saved: wait until '
            'the meters show and run the page script again')
        return 3
    for m in missing:
        say(f'usage page-reading: "{m}" was not found on the page (its layout may have changed): read it by eye and '
            f'`tutti usage log <account> <percent>`')
    for r in readings:
        try:
            account, used, resets, _ = dashboard.parse_reading(cfg, {k: r.get(k) for k in ('account', 'used', 'resets')}, now)
        except ValueError as e:
            say(f'usage page-reading: {r.get("account")}: {e}')
            return 2
        pacing.append_log(path, account, used, now, f'read from the page by the orchestrator ({r.get("how") or "page"})',
                          resets=resets, source='page script')
        say(f'usage page-reading: {account} {used:g} %' + (f', resets {resets}' if resets else ''))
    return 1 if missing else 0


def cmd_usage(cfg, a):
    path = usage_log_path(cfg)
    now = pacing.parse_time(a.at) if a.at else datetime.now(timezone.utc)
    if a.action in ('read', 'refresh'):
        read_now(cfg, path, now)
    if a.action == 'page-script':
        return page_scripts(cfg, a.account)
    if a.action == 'page-reading':
        return page_reading(cfg, path, a.account, now)
    for problem in readers.record_files(path, cfg['accounts'], cfg)[1]:
        say(problem)
    if a.action == 'log':
        if a.account in cfg['disabled_accounts'] and a.percent is not None:
            pacing.append_log(path, a.account, float(a.percent), now, a.note, resets=a.resets)
            say(f'{a.account} is turned off (enabled: false in {cfg["_path"]}): the reading is kept in the log but not used')
            return 0
        if a.account not in cfg['accounts'] or a.percent is None:
            names = ', '.join(cfg['accounts']) or NONE
            say(f'usage: tutti usage log <account> <percent> [--resets <ISO time>]; the accounts are {names}')
            return 2
        pacing.append_log(path, a.account, float(a.percent), now, a.note, resets=a.resets)
    entries = pacing.read_log(path)
    states = state.usage_state(cfg, now)['accounts']   # the owner's reserve and the scouts, as the dashboard shows them
    print(pacing.report(cfg['accounts'], entries, now, cfg['autopilot'].get('quiet_hours'), states))
    for name, acc in cfg['accounts'].items():
        s = pacing.account_state(name, acc, entries, now, *pacing.plan_args(cfg))
        if s.get('remind'):
            say(f'reminder: {s["remind"]}')
    return 0


def cmd_workers(cfg, a):
    print(workers.listing(cfg))
    return 0


def choose_worker(cfg, now):
    """`tutti run auto`: the worker whose account has room, from the latest readings (panic and spent accounts are skipped)."""
    entries = pacing.read_log(usage_log_path(cfg))
    accounts = [pacing.account_state(n, x, entries, now, *pacing.plan_args(cfg)) for n, x in cfg['accounts'].items()]
    return state.next_worker(cfg, accounts)


def cmd_run(cfg, a):
    extra = []
    if a.worker == 'auto':
        pick = choose_worker(cfg, datetime.now(timezone.utc))
        if not pick['worker']:
            say(f'run auto: {pick["reason"]}')
            return 3
        say(f'run auto: {pick["worker"]} {" ".join(pick["args"])} ({pick["reason"]})'.replace('  ', ' '))
        a.worker, extra, a.account = pick['worker'], pick['args'], pick['account']
    if not (a.repo and cfg['workers'].get(a.worker, {}).get('kind') == 'ollama'):
        worker_of(cfg, a.worker, 'cli')   # a local model takes a delegated task through `tutti agent`'s loop
    if a.input and not a.on:
        say('run --input: name the machine the inputs go to with --on <machine> (the worker then finds them there)')
        return 2
    if a.show:
        print(workers.brief(cfg, a.task, a.files))
        return 0
    if bridge.jobs_machine(cfg) and not a.input:
        return run_on_jobs_machine(cfg, a, extra)
    if a.repo and a.worker == 'claude' and not a.id and not a.track and not a.no_backlog:
        return run_claude(cfg, a)
    if a.repo:
        return run_delegated(cfg, a, extra)
    with job_inputs(cfg, machine_of(cfg, a.on) if a.input else None, a.input) as base:
        if base:
            names = ', '.join(os.path.basename(os.path.abspath(p).rstrip(os.sep)) for p in a.input)
            a.task += f'\n\nInput files ({names}) are on the machine {a.on} in {base}.'
        return run_worker(cfg, a, extra)


def run_claude(cfg, a):
    """`tutti run claude "<task>" --repo <repo>`: a backlog task pinned to Claude, under its account's pace (tutti/claudework.py).
    --repo is a repo name under autopilot.repos, or its folder."""
    from . import claudework
    repos = cfg['autopilot']['repos']
    name = a.repo if a.repo in repos else next((n for n, r in repos.items()
                                                if os.path.realpath(config.expand(r['path'])) == os.path.realpath(a.repo)), None)
    if not name:
        say(f'run claude: {a.repo} is not under autopilot.repos, so Tutti cannot track it; add it there, or run untracked with '
            '--no-backlog')
        return 2
    try:
        tid, run, why = claudework.start(cfg, name, a.task, size=a.size, check=(a.check or [''])[0])
    except (claudework.ClaudeError, backlog.BacklogError) as e:
        say(f'run claude: {e}')
        return 2
    print(f'{tid}: {why}' + (f'; run {run} (tutti runs show {run})' if run else '; the autopilot starts it when claude has room'))
    return 0


def jobs_args(a, extra, run_id):
    """`run ...` for the jobs machine: the same run, with the worker, the account and the args chosen here."""
    tail = ['run', a.worker, a.task, *a.files, '--id', run_id]
    tail += ['--repo', a.repo] if a.repo else []
    tail += ['--dir', a.dir] if a.dir else []
    for flag, value in (('--track', a.track), ('--remote-track', a.remote_track), ('--account', getattr(a, 'account', None))):
        tail += [flag, value] if value else []
    tail += [flag for flag, on in (('--no-checks', a.no_checks), ('--discard', a.discard)) if on]
    for c in a.check or []:
        tail += ['--check', c]
    tail += ['--check-where', a.check_where] if a.check else []
    return tail + [f'--extra={x}' for x in [*extra, *(a.extra or [])]]


class Tee:
    def __init__(self, *outs):
        self.outs, self.text = outs, []

    def write(self, text):
        self.text.append(text)
        for o in self.outs:
            o.write(text)

    def flush(self):
        for o in self.outs:
            o.flush()


def run_on_jobs_machine(cfg, a, extra):
    """host.jobs (docs/MOVE.md): the run happens on the computer with the repositories and the workers' sign-ins, through
    its bridge; the readings, the record and the report stay here, on the host."""
    if a.bg:
        return run_in_background(cfg, a, extra)
    jobs = bridge.jobs_machine(cfg)
    run_id = a.id or delegate.new_id(cfg)
    p = delegate.paths(cfg, run_id)
    os.makedirs(os.path.dirname(p['log']), exist_ok=True)
    with open(p['log'], 'a', encoding='utf8') as log:
        out = Tee(sys.stdout, log)
        rc = bridge.exec_run(cfg, jobs, run_id, jobs_args(a, extra, run_id), out=out)
    lines = ''.join(out.text).splitlines()[-workers.KEEP_LINES:]
    if rc != clientmode.UNREACHABLE:
        note_limits(cfg, a.worker, lines)
        delegate.log_run(cfg, a.worker, getattr(a, 'account', None) or state.account_of_worker(cfg, a.worker), rc, run_id, lines)
    return rc


def run_worker(cfg, a, extra):
    # restarted with a handoff when it is stopped at autocompact.threshold_pct, as a --repo run is
    rc, lines = delegate.run_compacting(cfg, a.worker, a.task, a.files, a.dir, sys.stdout, extra, None)
    note_limits(cfg, a.worker, lines)
    delegate.log_run(cfg, a.worker, getattr(a, 'account', None) or state.account_of_worker(cfg, a.worker), rc, None, lines)
    return rc


def run_in_background(cfg, a, extra):
    """`tutti run ... --bg`: the same run, detached, its output in the run's log; prints the id and the wait line at once."""
    run_id = delegate.new_id(cfg)
    p = delegate.paths(cfg, run_id)
    argv = [sys.executable, remote.BIN, '--config', cfg['_path'], 'run', a.worker, a.task, *a.files, '--id', run_id]
    argv += ['--repo', a.repo] if a.repo else []
    argv += [f'--extra={x}' for x in extra]
    argv += [flag for flag, on in (('--no-checks', a.no_checks), ('--discard', a.discard)) if on]
    argv += ['--track', a.track] if a.track else []
    argv += ['--remote-track', a.remote_track] if a.remote_track else []
    argv += ['--account', a.account] if getattr(a, 'account', None) else []
    for c in a.check or []:
        argv += ['--check', c]
    argv += ['--check-where', a.check_where] if a.check else []
    os.makedirs(os.path.dirname(p['log']), exist_ok=True)
    with open(p['log'], 'a', encoding='utf8') as log:
        child = subprocess.Popen(argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    with open(p['pid'], 'w', encoding='utf8') as f:
        f.write(f'{child.pid}\n')
    print(f'run {run_id} started in the background (pid {child.pid}); wait for it with: tutti wait run {run_id}')
    return 0


def run_delegated(cfg, a, extra):
    """`tutti run <worker> "<task>" --repo <repo>`: worktree, headless worker, diff, targeted checks, report."""
    extra = [*extra, *(a.extra or [])]
    if a.bg:
        return run_in_background(cfg, a, extra)
    check_list = [{'run': c, 'where': a.check_where} for c in a.check] if a.check else None
    r = delegate.delegate(cfg, a.worker, a.task, a.files, a.repo, run_id=a.id, track=a.track, extra=extra,
                          checks=not a.no_checks, discard=a.discard, check_list=check_list, remote_track=a.remote_track)
    with open(delegate.paths(cfg, r['id'])['rc'], 'w', encoding='utf8') as f:
        f.write(f'{r["rc"]}\n')
    with open(delegate.paths(cfg, r['id'])['log'], encoding='utf8', errors='replace') as f:
        lines = f.read().splitlines()[-workers.KEEP_LINES:]
    note_limits(cfg, a.worker, lines)
    account = getattr(a, 'account', None) or state.account_of_worker(cfg, a.worker)
    delegate.log_run(cfg, a.worker, account, r['rc'], r['id'], lines)
    telemetry.record('feature', 'run.delegated', worker_kind=telemetry.tool_of(a.worker), account=telemetry.tool_of(account),
                     outcome='passed' if r['rc'] == 0 else 'failed', autopilot=bool(a.id and a.account is not None),
                     checks=len(r.get('checks') or []), minutes=round((time.time() - float(str(r['id']).split('-')[0])) / 60, 2))
    with open(r['report'], encoding='utf8') as f:
        print(f.read().rstrip())
    return r['rc']


def note_limits(cfg, worker, lines):
    """A usage-limit message in the worker's output logs its account as spent (a CLI worker's; a local model has none)."""
    if 'command' not in cfg['workers'][worker]:
        return
    now = datetime.now(timezone.utc)
    cmd_text = ' '.join(cfg['workers'][worker]['command'])
    hits = readers.scan_text(cfg['accounts'], cmd_text, lines, now, f'run-{worker}-{int(now.timestamp())}')
    if hits:
        readers.record(usage_log_path(cfg), {}, hits, now)
        for name, reset, _ref, _text in hits[:1]:
            say(f'{worker} hit a usage limit: {name} is logged as spent'
                + (f' until {reset.isoformat()}' if reset else ' (no reset time in its message)') + '; send new work to other workers')


def cmd_pr(cfg, a):
    """`tutti pr review <n>`: reviewers, the wait, the fallback review."""
    repo = review.repo_of(a.repo)
    trial.in_trial(cfg, repo, a.number, say)   # a scout in its review trial is asked too; advisory, never blocking
    return review.review(cfg, a.number, repo, a.timeout, post=not a.no_post, say=say)


def cmd_job(cfg, a):
    """`tutti job register|done`: a script outside tutti shows on the dashboard with its log, progress and result;
    `tutti job waiting|move`: work waiting on an away or asleep target (cmd_jobmove)."""
    if a.action in ('waiting', 'move'):
        return cmd_jobmove(cfg, a)
    folder = os.path.join(config.state_dir(cfg), 'jobs')
    pid = a.pid or os.getppid()
    if a.action == 'register':
        path = discover.register(folder, pid, a.title or f'job {pid}', a.log)
        say(f'job {pid} registered ({path})')
        return 0
    if a.rc is None:
        say('job done needs the result: tutti job done --pid <pid> --rc <exit code>')
        return 2
    if not discover.finish(folder, pid, a.rc):
        say(f'no job {pid} in {folder}: register it first (tutti job register --pid {pid} --title ...)')
        return 1
    return 0


def cmd_runs(cfg, a):
    """`tutti runs`: the delegated runs; `show <id>` prints a report, `discard <id>` removes its worktree."""
    if a.action == 'list':
        print(delegate.listing(cfg))
        return 0
    if not a.id or not delegate.valid_id(a.id):
        say(f'name the run by its id: tutti runs {a.action} <id> (tutti runs lists them)')
        return 2
    r = delegate.load_report(cfg, a.id)
    if not r:
        say(f'no report for run {a.id} in {delegate.runs_dir(cfg)}: it may still be running (tutti wait run {a.id})')
        return 1
    if a.action == 'show':
        with open(delegate.paths(cfg, a.id)['report'], encoding='utf8') as f:
            print(f.read().rstrip())
        return 0
    wt = delegate.own_worktree(cfg, r.get('worktree'))
    if not wt:
        say(f'run {a.id}: its report names a worktree outside {cfg["project"]["worktrees"]}: nothing removed')
        return 1
    if os.path.isdir(wt):
        delegate.remove_tree(wt, say)
    else:
        say(f'run {a.id}: its worktree {wt} is already gone')
    return 0


def cmd_ask(cfg, a):
    stdin_text = '' if sys.stdin.isatty() else sys.stdin.read()
    print(local.ask(cfg, worker_of(cfg, a.worker, 'ollama'), a.instruction, a.files, stdin_text))
    return 0


def cmd_code(cfg, a):
    print(local.code(cfg, worker_of(cfg, a.worker, 'ollama'), a.file, a.instruction, a.context))
    return 0


def cmd_agent(cfg, a):
    ok = local.agent(cfg, worker_of(cfg, a.worker, 'ollama'), a.task, a.check, a.files, a.rounds, a.dir)
    return 0 if ok else 1


def cmd_yield(cfg, a):
    """A flag carries its owner, reason, pid and expiry (docs/SELFHEAL.md): `renew` moves the expiry; an expired one is lowered."""
    names = [a.machine] if a.machine else list(cfg['machines'])
    if a.action != 'status':
        code = change_yield(cfg, a)
        if code:
            return code
    for name in names:
        note = remote.yield_note(machine_of(cfg, name))
        print(f'{name:12} {"free" if note is None else "YIELD " + yield_line(note)}'.rstrip())
    return 0


def change_yield(cfg, a):
    """Raises, renews or lowers one machine's flag; 0, or the exit code after saying why not."""
    from . import hygiene
    if not a.machine:
        say(f'name the machine: tutti yield {a.action} --machine <name> (a flag is raised on one machine, never on all by default)')
        return 2
    m = machine_of(cfg, a.machine)
    if a.action == 'renew':
        old = remote.yield_note(m)
        if old is None:
            say(f'{a.machine} has no yield flag up: nothing to renew (raise one with tutti yield on --machine {a.machine})')
            return 1
        text = hygiene.renewed(old, time.time(), a.hours)
    else:
        text = hygiene.flag_text(a.owner, ('unload ' if a.unload else '') + a.note, a.hours, a.pid or None)
    if remote.set_yield(m, a.action != 'off', text) != 0:
        say(f'could not change the yield flag on {a.machine}: check that the machine is reachable (tutti machines)')
        return 1
    return 0


def yield_line(note, now=None):
    from . import hygiene
    now = now or time.time()
    f = hygiene.parse_flag(note, now, now)
    if f['legacy']:
        return f['reason'] or '(no reason given)'
    left = f['expires'] - now
    return (f'{f["owner"] or "owner unknown"}: {f["reason"] or "no reason given"} · up {hygiene.age_text(f["age_s"])} · '
            + (f'expires in {hygiene.age_text(left)}' if left > 0 else 'expired: lowered at the next self-heal round'))


def progress_target(cfg, job):
    if not job:
        return os.environ.get('TUTTI_PROGRESS_FILE')
    name = job.split(':', 1)[-1]   # the dashboard's id (quiet:<log>) or the log's name or path
    log = name if os.sep in name else os.path.join(config.state_dir(cfg), 'quiet', name)
    return log + '.progress'


def cmd_progress(cfg, a):
    """`tutti progress <n>/<m>`: how far the job is, for the dashboard's progress bar."""
    try:
        done, total = (int(x) for x in a.value.split('/'))
    except ValueError:
        done, total = -1, 0
    if total <= 0 or not 0 <= done <= total:
        say(f'progress {a.value!r}: write <done>/<total> with whole numbers, done at most total (for example 150/177)')
        return 2
    path = progress_target(cfg, a.job)
    if not path:
        say('progress: no job to report for: run it inside `tutti quiet` (it sets TUTTI_PROGRESS_FILE) or pass --job <log>')
        return 2
    with open(path, 'w', encoding='utf8') as f:
        f.write(f'{done} {total}\n')
    return 0


def cmd_quiet_mode(cfg, a):
    """`tutti quiet status|wait|guard`: quiet mode (tutti/quietmode.py). status exits 0 while quiet, 1 when free."""
    from . import quietmode
    verb, rest = a.command[0], a.command[1:]
    if verb == 'status':
        st = quietmode.status(cfg)
        say('quiet: ' + '; '.join(st['reasons']) if st['quiet'] else 'free: nobody at this computer and memory is fine')
        return 0 if st['quiet'] else 1
    if verb == 'wait':
        return 0 if quietmode.wait_free(cfg, float(rest[0]) if rest else None) else 1
    acts = quietmode.guard_round(cfg)
    say('; '.join(acts) or 'nothing to do')
    return 0


def cmd_quiet(cfg, a):
    if a.command[0] in ('status', 'wait', 'guard') and len(a.command) <= 2:
        return cmd_quiet_mode(cfg, a)
    from . import quietmode
    env = dict(os.environ, QUIET_DIR=os.path.join(config.state_dir(cfg), 'quiet'))
    if a.summary:
        worker_of(cfg, a.summary, 'ollama')
        me = f'{shlex.quote(sys.executable)} {shlex.quote(remote.BIN)} --config {shlex.quote(cfg["_path"])}'
        env['QUIET_SUMMARY'] = f'{me} ask {shlex.quote(a.summary)} {shlex.quote(SUMMARY)}'
    return subprocess.run([os.path.join(SCRIPTS, 'quiet.sh'), *quietmode.wrap(a.command, cfg)], env=env).returncode


def cmd_guard(cfg, a):
    g = cfg.get('guard') or {}
    image = g.get('image') or cfg['runner'].get('image')
    if not image:
        say('guard needs an image to watch: set guard.image (or runner.image) in the config')
        return 2
    env = dict(os.environ, GUARD_IMAGE=image, LOW_GB=str(g.get('low_gb', 3)), HIGH_GB=str(g.get('high_gb', 6)))
    return subprocess.run([os.path.join(SCRIPTS, 'memguard.sh')], env=env).returncode


def cmd_selfheal(cfg, a):
    """`tutti selfheal [--once] [--json]`: today's repairs, or one round of every check here."""
    from . import selfheal
    if a.once:
        lines = selfheal.round_(selfheal.Ctx(cfg))
        if a.json:
            print(json.dumps({'repairs': lines}))
            return 0
        say('\n'.join(lines) or 'self-heal: nothing repaired this round (a step only a person can take is in `tutti needs`)')
        return 0
    v = selfheal.view(cfg)
    if a.json:
        print(json.dumps(v))
        return 0
    say(f'self-healed: {v["today"]} repair(s) today, {v["tidied"]} routine clean-up(s)')
    for r in [*v['items'], *v['tidy_items']]:
        say(f'  {time.strftime("%H:%M", time.localtime(r["at"]))} {r["host"]} {r["check"]}: {r["what"]}')
    return 0


def cmd_reap(cfg, a):
    """`tutti reap [--dry-run]`: stops Tutti's stray helper processes of this user by exact pid (tutti/procs.py)."""
    from . import procs
    lines = procs.reap(say, dry=a.dry_run)
    if not lines:
        say('reap: no stray Tutti process here')
    return 0


def cmd_tunnel(cfg, a):
    w = worker_of(cfg, a.worker, 'ollama')
    t = w.get('tunnel')
    if not t:
        say(f'workers.{a.worker} has no tunnel: add tunnel: {{ssh: <host>, remote: 127.0.0.1:11434}} to reach its model server')
        return 2
    env = dict(os.environ, TUTTI_STATE=config.state_dir(cfg))
    ssh = t.get('ssh')
    if t.get('machine'):   # a model server on a machine of the config, e.g. ollama in Termux: its user, host and port
        m = machine_of(cfg, t['machine'])
        ssh = remote.dest(m)
        if m['transport'] == 'android':
            env['TUNNEL_SSH_PORT'] = str(m.get('port') or 8022)
    if not ssh:
        say(f'workers.{a.worker}.tunnel needs ssh: <host> or machine: <a machine name>')
        return 2
    return subprocess.run([os.path.join(SCRIPTS, 'tunnel.sh'), a.action, a.worker, w['host'], ssh, t['remote']], env=env).returncode


def wait_run_args(cfg, a):
    """`tutti wait run <id>`: a delegated run is done when its .rc file appears; its log is what stalls. A run whose process
    is gone without an .rc file ended badly and is reported at once instead of waited on until the timeout."""
    if not delegate.valid_id(a.target):
        raise config.ConfigError(f'"{a.target}" is not a run id (<time>-<pid>): `tutti runs` lists the runs')
    p = delegate.paths(cfg, a.target)
    if not os.path.exists(p['log']):
        raise config.ConfigError(f'no run {a.target} in {delegate.runs_dir(cfg)}: `tutti runs` lists the runs')
    a.stall_file = a.stall_file or p['log']
    a.stall_watch = [a.stall_file, p['dir']]   # the run's log and its folder, where each check writes its own log
    a.stall = float(cfg['dashboard']['stall_minutes']) * 60 if a.stall is None else a.stall   # --stall 0: off
    pid = state.read_rc(p['pid'])
    gone = waiting.pid_gone(pid) if pid else (lambda: False)
    return lambda: os.path.exists(p['rc']) or gone(), p['rc']


def finished_line(cfg, a, started):
    """The one notification line when a wait ends well: what, after how long, and for a run its verdict and report."""
    took = f'{time.monotonic() - started:.0f} s'
    if a.kind != 'run':
        return f'done: {a.kind} {a.target}{" " + a.pattern if a.pattern else ""} after {took}', 0
    p = delegate.paths(cfg, a.target)
    rc = state.read_rc(p['rc'])
    r = delegate.load_report(cfg, a.target)
    if rc is None or not r:
        return f'run {a.target} ended after {took} without a report: its process is gone; read {p["log"]}', 1
    checks = r['checks'] or []
    failed = sum(1 for c in checks if c['rc'] != 0)
    nocov = ' (coverage not checked)' if any(c['coverage'] for c in checks) else ''
    tested = 'not tested' if r['checks'] is None or not checks else f'checks {len(checks) - failed}/{len(checks)} passed{nocov}'
    return (f'run {a.target} done after {took}: {r["worker"]} rc {r["worker_rc"]}, {len(r["changed"])} file(s), {tested}'
            f'{", DENIED tool calls" if r["denied"] else ""}; report {r["report"]}'), rc


def wait_check(cfg, a):
    """(the done check, the target as recorded) for one kind of wait."""
    if a.kind == 'run':
        return wait_run_args(cfg, a)
    checks = {'pid': lambda: waiting.pid_gone(a.target), 'file': lambda: waiting.file_exists(a.target),
              'grep': lambda: waiting.file_matches(a.target, a.pattern or '')}
    # Absolute paths: the dashboard reads the record from its own working directory.
    return checks[a.kind](), a.target if a.kind == 'pid' else os.path.abspath(a.target)


def wait_ended(cfg, a, ended, seen, started):
    """The exit code and the one line a wait ends with."""
    if ended and seen:
        line, rc = finished_line(cfg, a, started)
        print(line)
        return rc
    if ended:
        telemetry.record('feature', 'wait.stall', cause=a.kind, seconds=round(time.monotonic() - started))
        say(f'wait {a.kind} {a.target}: {a.stall_file or a.target} has not changed for {a.stall:g} s; the job looks stalled: '
            'read its log, then stop or restart it (do not wait again on the same job)')
        return 125
    telemetry.record('feature', 'wait.timeout', cause=a.kind, seconds=round(time.monotonic() - started))
    say(f'wait {a.kind} {a.target}: not seen within {a.timeout:g} s; the thing waited for may have failed: look at its log')
    return 124


def cmd_wait(cfg, a):
    if a.kind == 'grep' and not a.pattern:
        say('wait grep needs a pattern: tutti wait grep <file> <regex>')
        return 2
    started = time.monotonic()
    done, target = wait_check(cfg, a)
    watched = getattr(a, 'stall_watch', None) or a.stall_file or a.target
    stall = waiting.stalled(watched, a.stall) if a.stall else (lambda: False)
    seen = []
    watch = os.path.abspath(a.stall_file) if a.stall_file else target
    record = waiting.register(os.path.join(config.state_dir(cfg), 'waits'), {
        'kind': a.kind, 'target': target, 'pattern': a.pattern or '', 'watch': watch, 'timeout': a.timeout, 'stall': a.stall})

    def check():
        if done():
            seen.append(True)
            return True
        return stall()
    try:
        ended = waiting.until(check, a.timeout, a.poll)
    finally:
        waiting.unregister(record)
    return wait_ended(cfg, a, ended, seen, started)


def cmd_translate(cfg, a):
    if a.check:
        problem = translate.check(a.source)
        print(problem or f'{a.source}: QA report matches')
        return 1 if problem else 0
    t = cfg.get('translate') or {}
    for role in ('translator', 'judge'):
        worker_of(cfg, t.get(role, ''), 'ollama')
    if not a.locale:
        say('name the locale: tutti translate <source.json> <locale>')
        return 2
    passed, held = translate.run(cfg, a.source, a.locale, a.out)
    print(f'{a.locale}: {passed} passed QA, {held} held for a human ({os.path.join(a.out, "held-" + a.locale + ".jsonl")})')
    return 0


def targets(cfg, name):
    """The machines a files command acts on: one by name, or `all` (every machine that is not this computer)."""
    if name == 'all':
        out = [m for m in cfg['machines'].values() if m['transport'] != 'local']
        if not out:
            raise config.ConfigError(f'--to all: {cfg["_path"]} has no machine other than this computer')
        return out
    return [machine_of(cfg, name)]


def cmd_files(cfg, a):
    try:
        return a.files_fn(cfg, a)
    except OSError as e:
        say(f'files {a.action}: {e}: check the path and the disk, then run it again')
        return 1


def files_send(cfg, a):
    failed = []
    for m in targets(cfg, a.to):
        try:
            base, result = transfer.send(cfg, m, [os.path.abspath(p) for p in a.paths], dest=a.dest, bwlimit=a.bwlimit)
        except RuntimeError as e:
            say(f'{m["name"]}: {e}')
            failed.append(m['name'])
            continue
        for name, how, size in result:
            print(f'{m["name"]}: {name} {how} ({files.human(size)}) in {base}')
    if failed:
        say(f'files send: failed on {", ".join(failed)}; what reached the other machines is stored. Fix the cause above and send '
            'again: finished files are skipped and partial ones resume')
    return 1 if failed else 0


def files_list(cfg, a):
    for m in targets(cfg, a.on or 'all'):
        st = transfer.stored(cfg, m)
        if st['problem']:
            print(f'{m["name"]}: {st["problem"]}')
            continue
        print(f'{m["name"]}: {files.human(st["bytes"])} of {files.human(st["budget_bytes"])} in {st["cache"]}')
        for it in st['items']:
            used = tz.show(datetime.fromtimestamp(it['used_at'], timezone.utc))
            print(f'  {it["name"]:40} {files.human(it["bytes"]):>10}  used {used}')
    return 0


def files_clean(cfg, a):
    for m in targets(cfg, a.on):
        gone = transfer.clean(cfg, m, a.names, everything=a.all)
        print(f'{m["name"]}: removed {", ".join(gone) if gone else "nothing (the cache is within its budget)"}')
    return 0


def cmd_dashboard(cfg, a):
    if clientmode.role(cfg) == 'client' and not a.once:
        return move.client_dashboard(cfg, a, say, lambda c: cmd_dashboard(c, a))
    collector = dashboard.Collector(cfg)
    if a.once:
        if not a.no_probe:
            collector.refresh()
        print(json.dumps(collector.snapshot(), indent=2))
        return 0
    port = cfg['dashboard']['port'] if a.port is None else a.port
    try:
        httpd = dashboard.server(cfg, port, collector)
    except dashboard.AuthRequired as e:
        say(str(e))
        return 2
    except OSError as e:
        say(f'cannot listen on {dashboard.bind_of(cfg)}:{port} ({e.strerror}): another dashboard may be running there; open it, '
            'or pass --port <n> (or set dashboard.port)')
        return 1
    refused = vpncmd.funnel_refusal(cfg)
    if refused:
        httpd.server_close()
        say(refused)
        return 1
    if not a.no_probe:
        collector.start()
    from . import install  # a release install updates itself daily and restarts in place (docs/INSTALL.md, *Updates*)
    install.start_daily(cfg, collector.stop, remote.ROOT, lambda: os.execv(sys.executable, [sys.executable, *sys.argv]))
    where = 'this computer only' if not dashboard.exposed(cfg) else 'reachable by others: sign-in with Google required'
    signin = 'sign-in with Google on' if httpd.auth else 'sign-in off: `tutti auth setup` turns it on'
    say(f'dashboard: http://127.0.0.1:{httpd.server_address[1]}/ ({where}; {signin}; Ctrl+C stops it)')
    from . import coordinator, netpolicy  # the household coordinator's computer serves the household from its dashboard too
    try:
        hh = coordinator.start_in_background(cfg)
    except netpolicy.PublicAddress as e:
        hh = None
        say(f'household coordinator refused to start: {e}')
    if hh:
        say(f'household coordinator: on {", ".join(h.server_address[0] for h in hh)}, port {hh[0].server_address[1]} '
            '(LAN and VPN only)')
    try:
        httpd.serve_forever()
    finally:
        collector.stop.set()
        httpd.server_close()
    return 0


def cmd_conduct(cfg, a):
    """`tutti conduct --main <agent> "<goal>"`: the chosen agent conducts with Tutti's commands; others take over on panic."""
    fallback = [x for x in a.fallback.split(',') if x] if a.fallback is not None else None
    commands = set(SUBCOMMANDS)
    if a.show:
        now = datetime.now(timezone.utc)
        agent = a.main or conduct.current(cfg)
        print(conduct.instructions(cfg, agent, a.goal, os.path.abspath(a.dir), '<run>/progress.md', commands, now))
        return 0
    try:
        rc, _run = conduct.conduct(cfg, a.goal, main=a.main, fallback=fallback, cwd=a.dir, commands=commands, say=say)
    except conduct.ConductError as e:
        say(str(e))
        return 2
    return rc


def cmd_main(cfg, a):
    """`tutti main`: the main agent and who has room; `tutti main <agent>` switches it (a running conductor hands over)."""
    if a.agent == 'backup':   # `tutti main backup [<machine>|auto|none]`: the backup main machine (docs/FAILOVER.md)
        from . import failovercmd
        a.machine = a.machine_name
        return failovercmd.cmd_main_backup(cfg, a)
    if a.agent:
        try:
            conduct.choose(cfg, a.agent)
        except conduct.ConductError as e:
            say(str(e))
            return 2
    print(conduct.describe(cfg))
    return 0


def cmd_handoff(cfg, a):
    try:
        path = conduct.note_progress(os.environ.get('TUTTI_CONDUCT'), a.section, a.text)
    except conduct.ConductError as e:
        say(str(e))
        return 2
    say(f'handoff: noted under {a.section} in {path}')
    return 0


def repo_of(cfg, name):
    repos = cfg['autopilot']['repos']
    if name not in repos:
        have = ', '.join(repos) or NONE
        raise backlog.BacklogError(f'no repo "{name}" under autopilot.repos in {cfg["_path"]}: the repos are {have}')
    return repos[name]


def cmd_backlog(cfg, a):
    """`tutti backlog add|list|rm|seed|retry|done`: the queue `tutti autopilot` takes its tasks from."""
    try:
        if a.action == 'list':
            print(backlog.listing(backlog.load(cfg), a.all))
            return 0
        with backlog.locked(cfg) as data:
            return backlog_change(cfg, a, data)
    except backlog.BacklogError as e:
        say(str(e))
        return 2


def backlog_change(cfg, a, data):
    if a.action == 'add':
        repo = repo_of(cfg, a.repo)
        task, new = backlog.add(data, backlog.new_task(a.repo, a.title or '', a.scope or [], a.check or repo.get('check') or '', a.size,
                                                       a.source, a.note or []))
        print(f'{task["id"]}: {"added" if new else "already in the backlog"}: {task["title"][:90]}')
    elif a.action == 'seed':
        added = backlog.seed(data, a.repo, repo_of(cfg, a.repo))
        print('\n'.join(f'{t["id"]}: {t["title"][:100]}' for t in added) or f'seed: nothing new found in {a.repo}')
    else:
        task_change(backlog.find(data, a.repo), a)   # rm, retry and done take the task id in the first argument
    return 0


def task_change(task, a):
    """rm, retry or done of one task; never while its run is going."""
    if task['status'] == 'running':
        raise backlog.BacklogError(f'{task["id"]} is running (run {task.get("run")}): wait for it '
                                   f'(tutti wait run {task.get("run")}), then {a.action} it')
    if a.action in ('claim', 'release', 'note'):
        return session_change(task, a)
    if a.action == 'accept':
        if task['status'] != 'candidate':
            raise backlog.BacklogError(f'{task["id"]} is {task["status"]}, not a candidate: only a suggested task is accepted')
        task['status'] = 'queued'
    elif a.action == 'retry':
        task['notes'] = [*task.get('notes', []), *(a.note or [])]
        task.update(status='queued', parked=None)
    else:
        task.update(status='dropped' if a.action == 'rm' else 'done', closed_at=time.time())
    print(f'{task["id"]}: {task["status"]}')


def session_change(task, a):
    """claim, release or note of a task a session works on by hand (docs/INTAKE.md)."""
    who = a.who or os.environ.get('TUTTI_SESSION') or ''
    if a.action == 'note':
        if not a.note:
            raise backlog.BacklogError(f'note: give the progress with --note "<text>": tutti backlog note {task["id"]} --note "..."')
        task['notes'] = [*task.get('notes', []), *a.note]
    elif a.action == 'claim':
        if not who:
            raise backlog.BacklogError(f'claim: say who works on it: tutti backlog claim {task["id"]} --who <session>')
        if task['status'] == 'claimed' and task.get('claimed_by') != who:
            raise backlog.BacklogError(f'{task["id"]} is claimed by {task["claimed_by"]}: pick another task, or ask them to release it')
        fixes = {'needs-person': f'answer its question first (tutti needs answer {task.get("question") or "<id>"})',
                 'candidate': f'accept it first (tutti backlog accept {task["id"]})'}
        if task['status'] not in ('queued', 'claimed', 'review'):
            raise backlog.BacklogError(f'{task["id"]} is {task["status"]}: only a queued or reviewed task is claimed; '
                                       + fixes.get(task['status'], '`tutti backlog retry` reopens one'))
        task.update(status='claimed', claimed_by=who, claimed_at=time.time(), closed_at=None)
    else:
        if task['status'] != 'claimed':
            raise backlog.BacklogError(f'{task["id"]} is {task["status"]}, not claimed: nothing to release')
        if task.get('claimed_by') != who:
            raise backlog.BacklogError(f'{task["id"]} is claimed by {task.get("claimed_by")}, not {who or "(no --who)"}: only that '
                                       'session releases it')
        task.update(status='queued', claimed_by=None, closed_at=time.time())
    print(f'{task["id"]}: {task["status"]}' + (f' by {who}' if task['status'] == 'claimed' else ''))
    return 0


def cmd_lease(cfg, a):
    """`tutti lease claim|release|list` (docs/INTAKE.md): a session's lease on a machine or a device, for a task."""
    from . import intake
    now = time.time()
    who = a.who or os.environ.get('TUTTI_SESSION') or ''
    try:
        if a.action == 'list':
            print(intake.lease_lines(intake.read_leases(cfg), now))
            return 0
        if not (a.kind and a.name):
            raise intake.IntakeError(f'name what: tutti lease {a.action} device|machine <name> --who <session>')
        if not intake.known(cfg, a.kind, a.name):
            raise intake.IntakeError(f'no {a.kind} "{a.name}" in {cfg["_path"]}: `tutti {a.kind}s` lists them')
        with intake.leases_locked(cfg) as data:
            if a.action == 'claim':
                lease = intake.claim(data, a.kind, a.name, who, a.task, a.minutes, now)
            else:
                lease = intake.release(data, a.kind, a.name, who)
            if a.kind == 'device':   # the device's own lease too; refused there, the session lease is not kept either
                from . import devicescmd
                rc = devicescmd.cmd_claim(cfg, argparse.Namespace(action=a.action, names=[a.name], who=who, minutes=a.minutes))
                if rc not in (0, None):
                    key = intake.lease_key(a.kind, a.name)
                    if a.action == 'claim':
                        data.pop(key)
                    else:
                        data[key] = lease
                    return rc
    except intake.IntakeError as e:
        say(str(e))
        return 2
    print(f'{a.kind} {a.name}: {"leased to " + who if a.action == "claim" else "released"}'
          + (f' for {lease["task"]}' if lease.get('task') else ' (no task: it shows as work outside Tutti)'))
    return 0


def cmd_intake(cfg, a):
    """`tutti intake`: work outside Tutti (docs/INTAKE.md): commits and leases no Tutti task explains."""
    from . import intake
    r = intake.report(cfg)
    print(json.dumps(r, indent=1) if a.json else intake.lines(r))
    return 1 if (r['commits'] or r['leases']) and a.strict else 0


def cmd_owner(cfg, a):
    """`tutti owner [status|mode <mode>]`: the owner's mode and the profile the planner keeps a reserve by (docs/USAGE-PACING.md)."""
    from . import owner
    folder = config.state_dir(cfg)
    if a.action in ('mode', 'window'):
        if a.action == 'window' and not a.window:
            say('owner window: name the day and its hours: tutti owner window 2026-11-16 20-24 (or off, or config to take it back)')
            return 2
        body = {'mode': None if a.mode in (None, 'config') else a.mode} if a.action == 'mode' else \
            {'date': a.mode, 'window': None if a.window == 'config' else a.window}
        code, answer = owner.change(cfg, folder, body)
        if code != 200:
            say(answer['error'])
            return 2
    now = datetime.now(timezone.utc)
    v = owner.view(cfg, folder, pacing.read_log(state.usage_log_path(cfg)), now)
    s = v['schedule']
    learned = f'learned from {v["hours_seen"]} h of readings' if v['learned'] else 'seeded from pacing.schedule'
    until = ', until ' + tz.show(pacing.parse_time(v['until'])) if v['until'] else ''
    print(f'owner: mode {v["mode"]} (in effect: {v["effective"]}{until}); accounts '
          f'{", ".join(v["accounts"]) or "(none)"}; {learned}')
    where = f'{v["zone"]}, now {tz.label(v["utc_offset"])}'
    print(f'schedule: {", ".join(s["days"])}, {s["hours"]} h in {s["window"]} ({where}); reserve per day: ' +
          ', '.join(f'{d} {sum(r):g}' for d, r in zip(v['days'], v['reserve'], strict=True)))
    if v.get('dates'):
        print('dated windows: ' + ', '.join(f'{d} {w}' for d, w in v['dates'].items()))
    return 0


def writes_state(a):
    """A command that changes Tutti's shared state (the ones a client forwards, but reads and `run`, which works alone)."""
    return a.cmd in clientmode.FORWARD and a.cmd != 'run' and not clientmode.is_read_only(a.cmd, getattr(a, 'action', None)) \
        and not (a.cmd == 'main' and not getattr(a, 'agent', None))


def failover_fenced_code():
    from . import failover
    return failover.FENCED


def fenced(cfg, what):
    """True (and says why) when this machine is not the active main machine now (docs/FAILOVER.md)."""
    from . import failover
    if failover.may_write(cfg):
        return False
    say(f'{what}: this machine is not the active main machine now ({failover.read(cfg).get("last") or "see `tutti failover`"}): '
        'nothing started')
    return True


def cmd_autopilot(cfg, a):
    """`tutti autopilot [status|on|off|tick|fill on|off|land <id>]`."""
    from . import fill
    if a.action in ('on', 'off'):
        autopilot.set_enabled(cfg, a.action == 'on')
    elif a.action == 'fill':
        if a.arg not in ('on', 'off'):
            say('autopilot fill: say on or off (autopilot.fill_room: free room on the trigger accounts is filled by itself)')
            return 2
        fill.set_fill(cfg, a.arg == 'on')
    elif a.action == 'land':
        try:
            print(fill.land(cfg, a.arg or '', **bridge.tools(cfg)))
        except backlog.BacklogError as e:
            say(str(e))
            return 2
        return 0
    elif a.action == 'allow':
        from . import resolver
        try:
            r = resolver.allow(cfg, a.arg or '')
        except ValueError as e:
            say(str(e))
            return 2
        say(f'autopilot: {r["allowed"]} takes first attempts until {tz.show(datetime.fromtimestamp(r["until"], timezone.utc))}')
    elif a.action == 'tick':
        now = datetime.now(timezone.utc)
        states = {s['name']: s for s in state.usage_state(cfg, now)['accounts']}
        from . import sharing
        machines = sharing.limit_items(cfg, state.machines_state(cfg, remote.probe), time.time())
        _plan, events = autopilot.tick(cfg, states, machines, beat=sharing.beat(cfg, now, machines))
        for e in events:
            say(f'autopilot: {e}')
    print(autopilot.describe(autopilot.view(cfg)))
    return 0


def cmd_workq(cfg, a):
    """`tutti workq [status|tick]`."""
    from . import workq
    if a.action == 'tick':
        for e in workq.tick(cfg, state.machines_state(cfg, remote.probe)):
            say(f'work queue: {e}')
    print(workq.describe(workq.view(cfg)))
    return 0


def cmd_fleet(cfg, a):
    """`tutti fleet`: every local model worker, discovered or configured, with its machine, models, speed and hardware."""
    from . import fleet
    print(fleet.describe(fleet.view(cfg)))
    return 0


def cmd_dispatch(cfg, a):
    """`tutti dispatch [status|on|off|now|run <lease>]`: keep devices and machines busy while test shards wait."""
    if a.action in ('on', 'off'):
        dispatch.set_enabled(cfg, a.action == 'on')
        print(f'dispatch: {a.action}')
        return 0
    if a.action == 'run':
        if not a.lease:
            say('name the lease: tutti dispatch run <lease> (the dispatcher starts these itself)')
            return 2
        return dispatch.run(cfg, a.lease)
    if a.action == 'build':
        if len(a.rest) != 2 or not a.lease:
            say('tutti dispatch build <project> <suite> <rc file> (the dispatcher starts these itself)')
            return 2
        from . import shards
        return shards.run_prepare(cfg, a.lease, a.rest[0], a.rest[1])
    if a.action == 'now':
        p = dispatch.live_tick(cfg)
        for t, w in p['assign']:
            print(f'sent {w["project"]} {w["key"]} to {t["id"]}')
        for tid, why in p['blocked'].items():
            print(f'{tid}: blocked by {why}')
    print(dispatch.describe_view(dispatch.view(cfg)))
    return 0


def cmd_jobmove(cfg, a):
    """`tutti job waiting|move <id> --to <target|auto> [--bump]`: work waiting on an away or asleep target, moved now."""
    from . import handover, jobmove
    if a.action == 'waiting':
        rows = jobmove.waiting(cfg)
        for j in rows:
            print(f'{j["id"]}  {j["title"][:60]}  waits on {j["target"]}: {j["label"]}{" (urgent)" if j["urgent"] else ""}')
        print('' if rows else 'nothing waits on an away or asleep target')
        return 0
    if not a.id:
        say('name the job: tutti job move <id> --to <device|machine|auto> (ids from `tutti job waiting`)')
        return 2
    try:
        print(jobmove.move(cfg, a.id, a.to, a.bump, kill=handover.end_tree, stop=dispatch.stop_pid))
    except jobmove.MoveError as e:
        say(str(e))
        return 2
    return 0


def cmd_machine(cfg, a):
    """`tutti machine evacuate <machine>`: every job waiting on it moved to the best available target; `tutti machine limit
    <machine> <HH-HH> [pct] | off`: its limited-usage window, saved in the config."""
    from . import handover, jobmove
    if a.action == 'limit':
        from . import settings, share
        try:
            changes = share.limit_change(cfg, a.name, a.window or '', a.pct)
            settings.store(cfg, changes)
        except (ValueError, settings.SettingsError) as e:
            say(f'machine limit: {e}')
            return 2
        saved = changes[0][1]
        say(f'{a.name}: ' + ('no limited hours' if a.window == 'off' else f'limited {saved["window"]} at {saved["pct"]} %'))
        return 0
    try:
        for line in jobmove.evacuate(cfg, a.name, kill=handover.end_tree, stop=dispatch.stop_pid):
            print(line)
    except jobmove.MoveError as e:
        say(str(e))
        return 2
    return 0


def cmd_presence(cfg, a):
    """`tutti presence`: each target's state (present, away, asleep, unknown), since when, and today's profile strip."""
    import time as _time

    from . import presence
    v = presence.view(cfg, _time.time())
    for tid, it in sorted(v['items'].items()):
        bar = ''.join(' ' if x is None else '.:-=#'[min(4, int(x * 5))] for x in it['strip'])
        print(f'{tid:28} {it["label"]:50} [{bar}] always-on {int(it["always_on"] * 100)} %')
    if v['host']['warning']:
        print(v['host']['warning'])
    return 0


def cmd_needs(cfg, a):
    """`tutti needs [list|answer <id> [choice]]` (docs/NEEDS.md): the questions waiting for you, and your answer."""
    from . import mcpserver, needs
    role = mcpserver.role_of(cfg)
    if a.action == 'backfill':
        for line in needs.backfill(cfg) or ['every parked task already has its question']:
            print(line)
        return 0
    if a.action == 'ask':
        return needs_ask(cfg, a)
    if a.action == 'answer':
        ok, why = needs.answer(cfg, a.id or '', a.choice, role, needs.OWNER)
        print(why) if ok else say(why)
        return 0 if ok else 2
    print(needs.lines_for_agent(cfg, role, needs.OWNER))
    return 0


def needs_ask(cfg, a):
    """`tutti needs ask <task> "<question>" [--option o]...`: a session's question for the owner, on its task (docs/INTAKE.md)."""
    from . import needs
    if not (a.id and a.choice):
        say('needs ask: name the task and the question: tutti needs ask t12 "Which colour?" --option red --option blue')
        return 2
    try:
        with backlog.locked(cfg) as data:
            t = backlog.find(data, a.id)
            if t['status'] in ('running', 'done', 'dropped'):
                raise backlog.BacklogError(f'{t["id"]} is {t["status"]}: a question goes on an open task (claimed or queued)')
            t['resume_claimed'] = t['status'] == 'claimed'
            q = needs.ask(cfg, t, a.choice, a.why or 'asked by the session working on it', a.option or [], (a.option or [''])[0])
    except backlog.BacklogError as e:
        say(str(e))
        return 2
    print(f'{q["id"]}: asked on {t["id"]}; the owner answers on the dashboard, in chat or with `tutti needs answer {q["id"]}`')
    return 0


def cmd_welcome(cfg, a):
    """`tutti welcome [show|miss|on|off]` (docs/WELCOME.md): the owner's welcome-back summary, What did I miss?, the switch."""
    from . import mcpserver, welcome
    role = mcpserver.role_of(cfg)   # a viewer's agent (TUTTI_ROLE, mcp.role) sees only household items
    key = welcome.OWNER if role == 'admin' else f'{role}-agent'
    data = welcome.read(cfg)
    u = welcome.user_of(data, key)
    if a.action in ('on', 'off'):
        u['enabled'] = a.action == 'on'
        welcome.write(cfg, data)
        print(f'welcome-back summaries: {a.action}')
        return 0
    if a.action == 'miss' or not u.get('summaries'):   # show: the latest, made only when there is none
        import time as _time
        s = welcome.miss(cfg, role, key, welcome.accounts_now(cfg, _time.time()) if role == 'admin' else [])
        welcome.remember(data, key, s)
    else:
        s = u['summaries'][-1]
    if not a.keep_unread:   # an agent showing it leaves the dashboard's panel for the person
        u['unread'] = False
    welcome.write(cfg, data)
    print(s['text'])
    return 0


def cmd_aiassist(cfg, a):
    """`tutti ai-assist [status|tick|undo <id>]` (docs/TELEMETRY.md, *AI assist*): turned on by an admin in the config or on
    the dashboard."""
    from . import aiassist
    if a.action == 'tick':
        for e in aiassist.tick(cfg):
            say(e)
    elif a.action == 'undo':
        ok, why = aiassist.undo(cfg, a.id or '')
        if not ok:
            raise SystemExit(why)
    v = aiassist.view(cfg)
    on = [k for k, x in v['switches'].items() if x]
    print(f'AI assist: {"on (" + ", ".join(on) + ")" if v["switches"]["enabled"] else "off (ai_assist.enabled: true opts in)"}; '
          f'{v["proposals"]} proposal(s), {v["done"]} done')
    for p in v['recent']:
        print(f'  proposal by {p["by"]}: {p["title"][:90]}')
    for t in v['tuned']:
        print(f'  {t["id"]} {t["path"]}: {t["old"]} -> {t["new"]}{" (undone)" if t.get("undone") else ""}: {t["why"]}')
    for t in v['tune_proposals']:
        print(f'  proposed {t["path"]}: {t["old"]} -> {t["value"]}: {t["why"]}')
    for g in v['gains']:
        print(f'  gain: {g}')
    return 0


def cmd_selfimprove(cfg, a):
    """`tutti selfimprove [status|on|off|tick]` (docs/TELEMETRY.md, *Self-improvement*)."""
    if a.action in ('on', 'off'):
        selfimprove.set_enabled(cfg, a.action == 'on')
    elif a.action == 'tick':
        for e in selfimprove.tick(cfg):
            say(e)
    v = selfimprove.lane(cfg)
    print(f'self-improvement: {"on" if v["enabled"] else "off"}; {len(v["items"])} task(s) from Sentry and the insights')
    for t in v['items']:
        stage = t['status']
        if t['merged']:
            stage = 'released' if t['released'] else 'merged'
        gate = f' ({t["gate"]})' if t['gate'] else ''
        print(f'  {t["id"]:5} {t["source"]:8} {stage:12} {t["project"] or ""}: {t["title"][:80]}{gate}')
    return 0


def cmd_security(cfg, a):
    """`tutti security [status|scan]`: dependency bots' pull requests and vulnerability reports (docs/SECURITY-INTAKE.md)."""
    from . import depbots
    if a.action == 'scan':
        _, events = depbots.scan_now(cfg)
        for e in events:
            print(e)
    v = depbots.view(cfg)
    if not v['read_at']:
        print('nothing read yet: `tutti security scan` reads every autopilot repo now')
        return 0
    print(f'{v["prs"]} dependency pull request(s), {v["alerts"]} vulnerability report(s), worst: {v["worst"] or "none"}')
    for r in v['repos']:
        print(f'  {r["name"]} ({r["slug"] or "no GitHub repo"}): {len(r["prs"])} pull request(s), {r["alerts"]} report(s)'
              f'{", worst " + r["worst"] if r["worst"] else ""}')
        for t in r['top']:
            print(f'    {t["severity"]:8} {t["title"]}  {t["url"]}')
    for p in v['problems']:
        print(f'needs the owner: {p}')
    return 0


def cmd_scout(cfg, a):
    """`tutti scout list|setup|ask|answer|watch|tick`: helpers that answer on a GitHub issue (docs/SCOUTS.md)."""
    try:
        rc = scoutcmd.run(cfg, a)
        telemetry.record('feature', f'scout.{a.action}', skill=a.skill, rc=rc, outcome='ok' if rc == 0 else 'failed')
        return rc
    except scouts.ScoutError as e:
        telemetry.record('feature', f'scout.{a.action}', skill=a.skill, outcome='refused')
        say(str(e))
        return 2


SUBCOMMANDS = []


def parser():
    p = argparse.ArgumentParser(prog='tutti', description='One orchestrator, many workers, shared machines.')
    p.add_argument('--version', action='version', version=f'tutti {__version__}')
    p.add_argument('--config', help='path of tutti.config.yaml (default: $TUTTI_CONFIG, then ~/.config/tutti/config.yaml, '
                                    'then the nearest one upwards)')
    sub = p.add_subparsers(dest='cmd', required=True)

    def add(name, fn, text):
        if name not in SUBCOMMANDS:
            SUBCOMMANDS.append(name)
        s = sub.add_parser(name, help=text, description=text)
        s.set_defaults(fn=fn)
        return s

    add('config', cmd_config, 'check the config and print its path')
    s = add('doctor', cmd_doctor, 'check this machine: a git that cannot do HTTPS comes first in PATH; another copy of the config '
                                  'that differs from the one in use')
    s.add_argument('--merge-configs', action='store_true', help='merge every differing copy into the config in use and link '
                                                                'the copies to it (backups kept)')
    s = add('update', cmd_update, 'update every worker CLI on every machine without sudo (their own updaters, npm, ollama pulls); '
                                  'name the exact command for what needs the owner (apt packages such as the Cursor editor)')
    s.add_argument('--check', action='store_true', help='only read the versions; change nothing')
    s.add_argument('--machine', help='only this machine')
    s = add('setup', cmd_setup, 'the first run, no questions: finds agents, logins, GPUs, machines, phones and repos, writes the '
                                'config with defaults, starts the dashboard at login, turns on updates, then lists the few steps '
                                'only you can do (sign-ins open in each tool\'s own window); again: only fills what is missing')
    s.add_argument('--yes', action='store_true', help='(the default) take every recommended default and ask nothing')
    s.add_argument('--ask', action='store_true', help='ask about each default instead (one question at a time)')
    s.add_argument('--no-service', dest='service', action='store_false', help='do not start the dashboard at login')
    s.add_argument('--join', help='then join a household with this invite (what a new member\'s install line passes)')
    s.add_argument('--no-connect', dest='connect', action='store_false', help='do not connect the agents found to Tutti\'s MCP '
                                                                               'server (`tutti connect --all`)')
    s.add_argument('--from-dashboard', action='store_true', help='run by the dashboard\'s Set up card: the login service is '
                                                                 'enabled for the next login, not started next to that dashboard')
    s = add('self-update', cmd_self_update, 'install the newest signed and checksummed release of Tutti (release installs)')
    s.add_argument('--insecure-skip-verify', action='store_true',
                   help='offline only: install without checking the release\'s Sigstore signature (a loud warning; never with --auto)')
    s.add_argument('--check', action='store_true', help='only say whether a newer release is out')
    s.add_argument('--auto', action='store_true', help='quiet no-op when turned off (self_update.enabled) or not a release install')
    s = add('machines', cmd_machines, 'probe every machine: free slots, load, yield flag (add-android: set up a Termux machine)')
    s.add_argument('action', nargs='?', choices=['add-android'], help='print the one-time Termux setup and the config lines')
    s.add_argument('--name', help='add-android: the machine name')
    s.add_argument('--host', help='add-android: the device IP address')
    s.add_argument('--user', help='add-android: the Termux user (whoami)')
    s.add_argument('--port', type=int, default=8022, help='add-android: Termux sshd port (8022)')
    s = add('devices', cmd_devices, 'Android devices for e2e: connection, battery, charging, heat, screen, Wi-Fi and lock; pair one '
            'over Wi-Fi (QR code, or the code a TV shows); move devices to another machine')
    s.add_argument('action', nargs='?', choices=['add', 'pair', 'move', 'claim', 'release', 'install', 'hours', 'lease', 'night'],
                   help='pair: wireless debugging (QR code); move: to another machine (--to); add: print the manual setup')
    s.add_argument('--name', help='add, pair: the device name (pair: default from its model)')
    s.add_argument('--serial', help='add: its serial from `adb devices` (or ip:port)')
    s.add_argument('--machine', help='add, pair: the machine its adb sees it on (pair: default this computer)')
    from . import devicescmd
    devicescmd.add_arguments(s)
    for verb, text in (('pause', 'pause a machine, device, worker or account: it takes nothing new (--stop-now hands running jobs over)'),
                       ('resume', 'resume a paused machine, device, worker or account')):
        s = add(verb, cmd_control, text)
        s.add_argument('kind', choices=list(controls.SECTIONS))
        s.add_argument('name')
        if verb == 'pause':
            s.add_argument('--stop-now', action='store_true',
                           help='hand running jobs over to the next machine, device or worker, then stop them')
            s.add_argument('--note', help='why, shown on the dashboard')
            s.add_argument('--from', dest='start', help='a planned absence: when it starts (local time: "2026-11-16 07:00", '
                           '"tomorrow 07:00", "07:00"); no new work 30 min before, running work handed over at it')
            s.add_argument('--until', help='when it ends (the same forms), or "unknown": it ends when presence sees it again')
    s = add('optin', cmd_control, 'turn an opt-in item on or off (a costs_money or opt_in worker or account, Android lint and unit runs)')
    s.add_argument('kind', choices=list(controls.SECTIONS))
    s.add_argument('name')
    s.add_argument('state', choices=['on', 'off'])
    s = add('test', cmd_test, 'run a command over a track\'s work tree on the first fit machine, behind its slot queue')
    s.add_argument('--dir', help='the work tree (default: <project.worktrees>/<track>)')
    s.add_argument('--on', help='skip routing and use this machine')
    s.add_argument('--device', help='an Android device (or `any`): ANDROID_SERIAL set, its lock held, on the machine it is plugged into')
    s.add_argument('--wait-minutes', type=int, default=30, help='with --device: how long to wait for it to get ready (charge, heat)')
    s.add_argument('--hold-device', action='append', metavar='NAME',
                   help='with --device: another device the run uses too (its external lock is held as well; repeat it)')
    s.add_argument('--fetch', action='append', metavar='PATH', help='a result folder or file to copy back from the machine (repeat it)')
    s.add_argument('--input', action='append', metavar='PATH',
                   help='a file or folder the job needs: copied to the machine\'s cache first, its folder in $TUTTI_INPUTS (repeat it)')
    s.add_argument('track')
    # REMAINDER: `tutti test t echo -n x` keeps -n for the command instead of reading it as an option of tutti (FEEDBACK #4)
    s.add_argument('command', nargs=argparse.REMAINDER)
    s = add('rsh', cmd_rsh, 'remote-shell shim for git and rsync on a WSL machine (internal)')
    s.add_argument('machine')
    s.add_argument('args', nargs=argparse.REMAINDER)
    s = add('ship', cmd_ship, 'format, commit, check, push and deploy the work tree you are in')
    s.add_argument('message')
    s.add_argument('--full', action='store_true', help='run ship.full_checks instead of the targeted ship.checks')
    s.add_argument('--no-add', action='store_true', help='commit only what is already staged')
    s.add_argument('--track', help='refuse unless this directory is that track\'s worktree')
    s.add_argument('--allow-protected', action='store_true')
    s.add_argument('--dry-run', action='store_true', help='print the plan; change nothing')
    s.add_argument('--dir', default='.')
    s = add('browser', browsercmd.cmd_browser, 'the household\'s work browser: status, sign-in <url>, sites, read, page <url>, '
                                               'remote <machine> <url> [-- cmd], isolate [--sign-in <url>], close (docs/BROWSER.md)')
    s.add_argument('action', choices=['status', 'sign-in', 'sites', 'read', 'page', 'remote', 'isolate', 'close'])
    s.add_argument('target', nargs='*', help='the page; for remote: <machine> <url> [-- <command>]')
    s.add_argument('--type', default='page', help='the task type whose sites the broker allows (browser.scopes)')
    s.add_argument('--sign-in', dest='sign_in_url', help='isolate: the one command for a sign-in on the isolated browser')
    s.add_argument('--script', action='store_true',
                   help='isolate: print the root setup script (`tutti browser isolate --script | sudo bash`)')
    s = add('usage', cmd_usage, 'show every account against its pace; `usage log <account> <percent>` records a reading, '
                                '`usage refresh` (or read) runs the automatic readers now')
    s.add_argument('action', nargs='?', choices=['log', 'read', 'refresh', 'page-script', 'page-reading'])
    s.add_argument('account', nargs='?')
    s.add_argument('percent', nargs='?')
    s.add_argument('--at', help='the time of the reading or of the report (ISO 8601, UTC by default)')
    s.add_argument('--note', default='')
    s.add_argument('--resets', help='usage log: the reset time the usage page shows (ISO 8601); it anchors the window from then on')
    add('workers', cmd_workers, 'list the workers and whether each is ready')
    s = add('run', cmd_run, 'brief a CLI worker (cursor, agy, claude; `auto`: the one whose account has room) in a worktree; '
                            'it must not commit')
    s.add_argument('worker')
    s.add_argument('task')
    s.add_argument('files', nargs='*')
    s.add_argument('--dir', help='the worktree the worker edits (default: here)')
    s.add_argument('--show', action='store_true', help='print the brief and stop')
    s.add_argument('--input', action='append', metavar='PATH', help='a file or folder to copy to the --on machine first (repeat it)')
    s.add_argument('--on', help='the machine --input files go to')
    s.add_argument('--repo', help='hand the task off end to end: a worktree of this repo, the worker headless, the diff, the checks, '
                                  'a report (tutti runs show <id>)')
    s.add_argument('--track', help='with --repo: reuse or create the worktree <delegate.prefix><track> instead of a new one')
    s.add_argument('--bg', action='store_true', help='with --repo: run detached; prints the id for `tutti wait run <id>`')
    s.add_argument('--no-checks', action='store_true', help='with --repo: skip the checks (the report says nothing was tested)')
    s.add_argument('--discard', action='store_true', help='with --repo: remove a new worktree after the report (the diff file stays)')
    s.add_argument('--check', action='append', metavar='CMD',
                   help='with --repo: the target repo\'s own check instead of delegate.checks (repeat it)')
    s.add_argument('--check-where', default='local', choices=['local', 'route'],
                   help='with --check: run it here (local, the default) or on a routed machine (route)')
    s.add_argument('--no-backlog', action='store_true', help='claude with --repo: run it untracked (no backlog task, no pacing)')
    s.add_argument('--size', default='S', choices=list(backlog.SIZES), help='claude with --repo: the task\'s size (default S)')
    s.add_argument('--remote-track', help=argparse.SUPPRESS)
    s.add_argument('--account', help=argparse.SUPPRESS)   # the autopilot's account of a shared worker (agy-gemini, agy-claude)
    s.add_argument('--id', help=argparse.SUPPRESS)
    s.add_argument('--extra', action='append', help=argparse.SUPPRESS)
    s = add('pr', cmd_pr, 'pull requests: `pr review <n>` asks the reviewers, says when none will come, and falls back to a local '
                          'review by a cheap worker posted to the pull request')
    s.add_argument('action', choices=['review'])
    s.add_argument('number', type=int)
    s.add_argument('--repo', help='owner/name (default: the repository of the current directory)')
    s.add_argument('--timeout', type=float, help='minutes to wait for the reviewers (default: review.timeout_minutes)')
    s.add_argument('--no-post', action='store_true', help='print the fallback review instead of posting it')
    s = add('job', cmd_job, 'register a script\'s run for the dashboard (`job register --title t`), and its result (`job done --rc n`); '
                            'work waiting on an away or asleep target: `job waiting`, `job move <id> --to <device|machine|auto> '
                            '[--bump]` (now, from its last synced checkpoint; docs/PRESENCE.md)')
    s.add_argument('action', choices=['register', 'done', 'waiting', 'move'])
    s.add_argument('id', nargs='?', help='move: the job id from `tutti job waiting`')
    s.add_argument('--to', default='auto', help='move: a device or machine name, or auto (the best available)')
    s.add_argument('--bump', action='store_true', help='move: first in its queue')
    s.add_argument('--pid', type=int, help='the process (default: the caller, the shell that runs tutti)')
    s.add_argument('--title', help='what the job does')
    s.add_argument('--log', help='its log (default: where the process\'s standard output goes, when that is a file)')
    s.add_argument('--rc', type=int, help='job done: the exit code')
    s = add('runs', cmd_runs, 'the delegated runs: list, show <id> (the report), discard <id> (removes its worktree)')
    s.add_argument('action', nargs='?', default='list', choices=['list', 'show', 'discard'])
    s.add_argument('id', nargs='?')
    s = add('ask', cmd_ask, 'a short answer from a local model about files or stdin')
    s.add_argument('worker')
    s.add_argument('instruction')
    s.add_argument('files', nargs='*')
    s = add('code', cmd_code, 'a local model rewrites one file in place; review with git diff')
    s.add_argument('worker')
    s.add_argument('file')
    s.add_argument('instruction')
    s.add_argument('context', nargs='*')
    s = add('agent', cmd_agent, 'a local model edits, runs the check, reads the failure and retries')
    s.add_argument('worker')
    s.add_argument('task')
    s.add_argument('check')
    s.add_argument('files', nargs='+')
    s.add_argument('--rounds', type=int, default=4)
    s.add_argument('--dir', default='.')
    s = add('yield', cmd_yield, 'raise or lower a machine\'s yield flag: while it is up, tutti sends that machine nothing new')
    s.add_argument('action', choices=['on', 'off', 'renew', 'status'])
    s.add_argument('--machine')
    s.add_argument('--note', default='', help='the reason, shown on the machine card')
    s.add_argument('--owner', default='', help='who holds the machine (default: this user)')
    s.add_argument('--hours', type=float, default=4, help='expires after this many hours unless renewed (default 4)')
    s.add_argument('--pid', type=int, default=0, help='the owner\'s process: once it is gone and the machine idle, the flag goes')
    s.add_argument('--unload', action='store_true', help='also ask local-model workers to unload their models')
    s = add('quiet', cmd_quiet, 'run a command, keep its log, print only the exit code (and a short failure summary); niced and '
              'CPU-capped while a person is here. `tutti quiet status` exits 0 while quiet; `wait [s]` waits; `guard` one round')
    s.add_argument('--summary', help='the local-model worker that summarises a failure')
    s.add_argument('command', nargs='+')
    s = add('progress', cmd_progress, 'report how far a job is (<done>/<total>) for the dashboard\'s progress bar')
    s.add_argument('value')
    s.add_argument('--job', help='the job: its log, or the dashboard id quiet:<log> (default: $TUTTI_PROGRESS_FILE, set by tutti quiet)')
    add('guard', cmd_guard, 'pause test containers when memory runs low, resume them when it is back')
    s = add('selfheal', cmd_selfheal, 'self-healing (docs/SELFHEAL.md): what was repaired today, or run every check now')
    s.add_argument('--once', action='store_true', help='run every check and repair now, on this machine')
    s.add_argument('--json', action='store_true')
    s = add('reap', cmd_reap, 'stop Tutti\'s stray helper processes (keep-awake, tunnel, inhibitor) by exact pid')
    s.add_argument('--dry-run', action='store_true', help='only list them')
    s = add('tunnel', cmd_tunnel, 'the self-healing ssh tunnel to a worker\'s model server')
    s.add_argument('action', choices=['start', 'stop', 'status'])
    s.add_argument('worker')
    s = add('wait', cmd_wait, 'wait for a pid to end, a file to appear or a line in a log; always with a timeout')
    s.add_argument('kind', choices=['pid', 'file', 'grep', 'run'], help='run: a `tutti run --repo --bg` id; ends with its verdict')
    s.add_argument('target')
    s.add_argument('pattern', nargs='?')
    s.add_argument('--timeout', type=float, default=3600)
    s.add_argument('--poll', type=float, default=2)
    s.add_argument('--stall', type=float, default=None, help='give up (exit 125) when the log has not changed for this many seconds')
    s.add_argument('--stall-file', help='the log watched by --stall (default: the target)')
    s = add('translate', cmd_translate, 'translate a flat JSON catalogue with mandatory QA; --check verifies an output\'s report')
    s.add_argument('source')
    s.add_argument('locale', nargs='?')
    s.add_argument('--out', default='i18n-out')
    s.add_argument('--check', action='store_true')
    s = add('files', cmd_files, 'copy files to machines (`send`), see what each stores (`list`), trim or empty its cache (`clean`)')
    fsub = s.add_subparsers(dest='action', required=True)
    f = fsub.add_parser('send', help='copy files and folders to a machine, or all')
    f.set_defaults(files_fn=files_send)
    f.add_argument('paths', nargs='+', help='files and folders on this computer')
    f.add_argument('--to', required=True, help='a machine, or all')
    f.add_argument('--dest', help='a folder on the machine instead of its cache (not counted against the budget)')
    f.add_argument('--bwlimit', type=int, help='at most this many KB/s (default files.bwlimit_kbps; 0 = no limit)')
    f = fsub.add_parser('list', help='what each machine stores')
    f.set_defaults(files_fn=files_list)
    f.add_argument('--on', help='a machine, or all (the default)')
    f = fsub.add_parser('clean', help='trim a cache to its budget, remove named items, or empty it')
    f.set_defaults(files_fn=files_clean)
    f.add_argument('names', nargs='*', help='items to remove (default: trim to the budget)')
    f.add_argument('--on', required=True, help='a machine, or all')
    f.add_argument('--all', action='store_true', help='remove every item and partial file in the cache')
    s = add('dashboard', cmd_dashboard, 'a local web dashboard (installable app): usage and pacing, machines, workers and jobs')
    s.add_argument('--port', type=int, help='the port on 127.0.0.1 (default: dashboard.port, 4777)')
    s.add_argument('--once', action='store_true', help='print the state as JSON (what /api/state answers) and stop')
    s.add_argument('--no-probe', action='store_true', help='do not probe machines and workers (ssh, model servers)')
    s = add('conduct', cmd_conduct, 'another agent (cursor, agy, copilot, a local model) as the main agent: it gets the goal, an '
                                    'instruction file with Tutti\'s commands and the safety limits; on panic the next one takes over')
    s.add_argument('goal')
    s.add_argument('--main', help='the main agent, a worker (default: the dashboard\'s choice, else main.agent)')
    s.add_argument('--fallback', help='comma-separated workers that take over in turn (default: main.fallback)')
    s.add_argument('--dir', default='.', help='the project folder the main agent works in (default: here)')
    s.add_argument('--show', action='store_true', help='print the instruction file and stop')
    s = add('handoff', cmd_handoff, 'inside `tutti conduct`: record a finished step (done) or the next one (next) for the handoff note')
    s.add_argument('section', choices=['done', 'next'])
    s.add_argument('text')
    s = add('backlog', cmd_backlog, 'the autopilot\'s queue of bounded tasks: add <repo> "<title>", list, rm <id>, seed <repo> '
                                    '(FEEDBACK open items, TODO(owner), gaps), retry <id> --note "...", done <id>, '
                                    'accept <id> (a suggested candidate)')
    s.add_argument('action', choices=['add', 'list', 'rm', 'seed', 'retry', 'done', 'accept', 'claim', 'release', 'note'])
    s.add_argument('repo', nargs='?', help='add, seed: a repo under autopilot.repos; rm, retry, done, accept, claim, release, note: '
                                           'the task id')
    s.add_argument('--who', help='claim: the session that works on it by hand (default $TUTTI_SESSION)')
    s.add_argument('title', nargs='?')
    s.add_argument('--scope', action='append', metavar='PATH', help='a path the task may change (repeat it)')
    s.add_argument('--check', help='the acceptance check (default: the repo\'s check)')
    s.add_argument('--size', default='S', choices=list(backlog.SIZES))
    s.add_argument('--source', default='orchestrator')
    s.add_argument('--note', action='append', help='a note for the worker (retry: the reviewer\'s notes)')
    s.add_argument('--all', action='store_true', help='list: also done and dropped tasks')
    s = add('lease', cmd_lease, 'a session\'s lease on a machine or a device for a task (docs/INTAKE.md): claim, release, list')
    s.add_argument('action', nargs='?', default='list', choices=['claim', 'release', 'list'])
    s.add_argument('kind', nargs='?', choices=['machine', 'device'])
    s.add_argument('name', nargs='?')
    s.add_argument('--who', help='the session (default $TUTTI_SESSION)')
    s.add_argument('--task', help='claim: the backlog task it is for (t123); without one it shows as work outside Tutti')
    s.add_argument('--minutes', type=int, default=120, help='claim: how long (default 120)')
    s = add('intake', cmd_intake, 'work outside Tutti: commits in the autopilot repos and leases that no Tutti task explains '
                                  '(the last 24 h; docs/INTAKE.md)')
    s.add_argument('--json', action='store_true')
    s.add_argument('--strict', action='store_true', help='exit 1 when anything is outside Tutti')
    s = add('owner', cmd_owner, 'the owner\'s own prompting: status, mode auto|active|away|vacation-until-YYYY-MM-DD|config, or '
                                'window <YYYY-MM-DD> <HH-HH|off|config> (one day\'s own owner hours)')
    s.add_argument('action', nargs='?', default='status', choices=['status', 'mode', 'window'])
    s.add_argument('mode', nargs='?', help='mode: auto, active, away (the rest of today), vacation-until-YYYY-MM-DD, or config; '
                                           'window: the date (YYYY-MM-DD)')
    s.add_argument('window', nargs='?', help='window: that day\'s owner hours HH-HH (e.g. 20-24), off, or config (its weekday\'s)')
    s = add('autopilot', cmd_autopilot, 'send backlog tasks to accounts that are behind their pace, by itself: status, on, off, '
                                        'tick (one round now; the dashboard ticks while it runs), fill on|off (fill free room from '
                                        'the repos\' sources), land <id> (review a result and land it)')
    s.add_argument('action', nargs='?', default='status', choices=['status', 'on', 'off', 'tick', 'fill', 'land', 'allow'])
    s.add_argument('arg', nargs='?', help='fill: on or off; land: the task id; allow: the account that may take first attempts '
                                          'for 7 days (the block resolver\'s one action)')
    s = add('workq', cmd_workq, 'the compute work queue: the projects\' gates, tests and reports on machines with a free slot '
                                '(status, or tick: one round now; docs/FLEET.md)')
    s.add_argument('action', nargs='?', default='status', choices=['status', 'tick'])
    s = add('fleet', cmd_fleet, 'the household\'s local models as workers: every machine\'s Ollama, its models, their kinds, '
                                'speed and hardware (docs/FLEET.md)')
    s = add('security', cmd_security, 'dependency bots\' pull requests (Dependabot, Cursor) and vulnerability reports '
                                      '(Dependabot and code-scanning alerts, Cursor and Greptile findings) as autopilot tasks: '
                                      'status, or scan (read every repo now and queue)')
    s.add_argument('action', nargs='?', default='status', choices=['status', 'scan'])
    s = add('scout', cmd_scout, 'scouts, helpers that are not workers (Grok Bot): ask <scout> "<question>" (answered on a GitHub '
                                'issue), answer <id>, watch [<scout> <watch>], tick (send what is due within the allowance), '
                                'review <pr> (advisory), trial (the review trial), list, setup')
    s.add_argument('action', choices=['list', 'setup', 'ask', 'answer', 'watch', 'tick', 'review', 'trial'])
    s.add_argument('scout', nargs='?', help='ask, setup, watch: the scout (setup grok: the guided setup in a terminal); '
                                            'answer: the request id; '
                                            'review: the pull request number; '
                                            'trial: status, collect, show, mark or done')
    s.add_argument('task', nargs='?', help='ask: the question or task; watch: the watch to run now; trial: owner/name#number')
    s.add_argument('rest', nargs='*', help='trial mark: the finding id and real or noise')
    s.add_argument('--via', help='review, trial: the scout (default: the only one)')
    s.add_argument('--repo', help='ask: the repository whose issue gets the answer (default: scouts.<name>.reply.github_repo)')
    s.add_argument('--skill', help='ask: research, web-browse, triage, monitoring, drafting or browser-repro (a hint for the scout)')
    s.add_argument('--no-wait', action='store_true', help='ask: return once it is sent; `tutti scout answer <id>` later')
    s.add_argument('--queue', action='store_true', help='ask: do not send now; the next round sends it when the allowance has room')
    s.add_argument('--timeout', type=float, help='ask, answer: minutes to wait for the answer (default: timeout_minutes; 0: look once)')
    s.add_argument('--key-stdin', action='store_true', help='ask: read the webhook key from the first line of stdin instead of key_env')
    s = add('telemetry', telemetrycmd.cmd, 'crash reports and usage statistics: status, on, off (sending is opt-in; recording '
                                           'on this computer is always on), setup (paste the Sentry DSN), report, send')
    s.add_argument('action', nargs='?', default='status', choices=['status', 'on', 'off', 'setup', 'report', 'send'])
    s.add_argument('--days', type=int, default=7, help='report: the last this many days (7)')
    s.add_argument('--json', action='store_true', help='report: print the summary as JSON')
    s.add_argument('--test', action='store_true', help='send: one test event straight to the project, to check the DSN')
    s = add('github', githubcmd.cmd, 'connect Tutti to GitHub (pull requests, CI, issues) with a code at github.com/login/device: '
            'status, connect, disconnect, or manifest (the App\'s manifest, for the maintainer)')
    s.add_argument('action', nargs='?', default='status', choices=('status', 'connect', 'disconnect', 'manifest'))
    s = add('report', report.cmd, 'report a problem to Tutti\'s maintainers: what happened and what you '
                                                          'expected, with a scrubbed diagnostic bundle you see before it is sent')
    s.add_argument('--what', help='what happened (asked when not given)')
    s.add_argument('--expected', help='what you expected instead (asked when not given)')
    s.add_argument('--preview', action='store_true', help='print the report and send nothing')
    s.add_argument('--yes', action='store_true', help='send without asking again after the preview')
    s = add('dispatch', cmd_dispatch, 'keep devices and machines busy: idle ones pull pending test shards (projects.<p>.tests) by '
                                      'themselves: status, on, off, now (one round with fresh probes), run <lease> and build '
                                      '<project> <suite> <rc> (internal)')
    s.add_argument('action', nargs='?', default='status', choices=['status', 'on', 'off', 'now', 'run', 'build'])
    s.add_argument('lease', nargs='?', help='run: the lease id; build: the project')
    s.add_argument('rest', nargs='*', help='build: the suite and the rc file')
    s = add('machine', cmd_machine, 'evacuate <machine>: move every job waiting on it to the best available target; '
                                    'limit <machine> <HH-HH> [pct] | off: its daily limited-usage window')
    s.add_argument('action', choices=['evacuate', 'limit'])
    s.add_argument('name')
    s.add_argument('window', nargs='?', help='limit: local hours HH-HH (18-01 wraps past midnight), or off')
    s.add_argument('pct', nargs='?', type=int, help='limit: the most Tutti uses then, in percent (default 60)')
    add('presence', cmd_presence, 'each device, machine, member and model server: here, away, asleep or unknown, and its profile')
    s = add('selfimprove', cmd_selfimprove, 'crashes and insights become autopilot tasks, pull requests, and where a project '
                                            'allows it merges and patch releases: status, on, off, tick (one round now)')
    s.add_argument('action', nargs='?', default='status', choices=['status', 'on', 'off', 'tick'])
    s = add('needs', cmd_needs, 'the questions only you can answer (a parked task\'s next step, a worker\'s question): list, '
                                 'answer <id> [choice] (empty: the default); the task resumes; backfill (tasks parked before the queue get '
                                 'their question; also at the dashboard\'s start) (docs/NEEDS.md)')
    s.add_argument('action', nargs='?', default='list', choices=['list', 'answer', 'backfill', 'ask'])
    s.add_argument('id', nargs='?', help='answer: the question id (q1, q2, ...); ask: the task id')
    s.add_argument('choice', nargs='?', help='answer: one of its options, or your own words; ask: the question')
    s.add_argument('--option', action='append', help='ask: an answer the owner can pick (repeat it; the first is the default)')
    s.add_argument('--why', help='ask: why it needs the owner')
    s = add('welcome', cmd_welcome, 'welcome back: what happened while you were away (shipped, running, failed, usage, what '
                                     'needs you): show (the latest), miss (What did I miss? since the last one), on, off')
    s.add_argument('action', nargs='?', default='show', choices=['show', 'miss', 'on', 'off'])
    s.add_argument('--keep-unread', action='store_true', help='leave the dashboard\'s Welcome back panel unread (the MCP tool)')
    s = add('ai-assist', cmd_aiassist, 'Tutti uses AI, local and online, to make itself better (an admin opts in): status, tick '
                                       '(one round now), undo <id> (a tuned parameter back)')
    s.add_argument('action', nargs='?', default='status', choices=['status', 'tick', 'undo'])
    s.add_argument('id', nargs='?', help='undo: the tuned change\'s id (u1, u2, ...)')
    s = add('user', householdcmd.cmd_user, 'who this Tutti works for: show, name <name> (config: back to the config\'s), '
                                           'main-agent <kind> (claude, codex, agy, cursor, copilot, gemini, local), '
                                           'main-machine <m> (your Tutti and main agent move there, like `tutti move`), '
                                           'share <account> on|off (room it would lose at its reset may do the household\'s '
                                           'shared tasks; docs/HOUSEHOLD.md), notify always|work-hours [--batch N] [--quiet '
                                           'HH:MM-HH:MM] (Get questions outside work hours; docs/NEEDS.md)')
    s.add_argument('action', nargs='?', default='show', choices=['show', 'name', 'main-agent', 'main-machine', 'share', 'notify'])
    s.add_argument('--batch', type=int, help='notify: questions to collect off work before you are told (default 3)')
    s.add_argument('--quiet', help='notify: your quiet hours, HH:MM-HH:MM (default 23:00-07:00; "" for none)')
    s.add_argument('--for', dest='for_user', help='notify: a signed-in user\'s e-mail (an admin changes it for them; audited)')
    s.add_argument('--dry-run', action='store_true', help='main-machine: check the machine and print the plan; change nothing')
    s.add_argument('arg', nargs='?', help='name: the name; main-agent: the kind; main-machine: the machine; share: the account')
    s.add_argument('value', nargs='?', help='share: on or off')
    s = add('household', householdcmd.cmd_household, 'several people, one set of machines (docs/HOUSEHOLD.md): status, init '
                                                     '(this computer coordinates; prints the join code), join <code>, leave, '
                                                     'code (a new join code), serve (the coordinator alone), project share|unshare <repo>, '
                                                     'machine share|unshare|use|pause|resume <machine>')
    s.add_argument('action', nargs='?', default='status', choices=list(householdcmd.ACTIONS))
    s.add_argument('arg', nargs='*', help='join: the code; project: share or unshare and the repo; machine: the action and the machine')
    s.add_argument('--user', help='machine use: your login on that machine (default: this login\'s name)')
    s.add_argument('--name', help='init: the household\'s name; join: your name in it (default: `tutti user`\'s)')
    s.add_argument('--host', help='init: the address members reach this computer at (default: its LAN address); join with an '
                                  'invite: the coordinator\'s host[:port] (default: the invite\'s host, household.port); machine share: '
                                  'the address members reach the machine at (default: its host, or this computer\'s LAN address)')
    s.add_argument('--port', type=int, help='init, serve: the port (default: household.port, 4778)')
    s.add_argument('--json', action='store_true', help='status: as JSON')
    s = add('mcp', mcpserver.cmd, 'Tutti as an MCP server for your main agent (stdio; --http for a local HTTP endpoint); '
                                  '`tutti connect` wires it in (docs/CONNECT.md)')
    s.add_argument('--http', type=int, nargs='?', const=0, default=None, metavar='PORT',
                   help='serve on http://127.0.0.1:PORT/mcp with a bearer token instead of stdio (default port: mcp.http_port, 4779)')
    s.add_argument('--bind', help='with --http: a loopback, LAN or VPN address to listen on (default 127.0.0.1, or mcp.bind); '
                                  'a public address is refused: Tutti is never exposed to the internet')
    s = add('connect', connect.cmd, 'wire Tutti into your main agents (Claude Code, Claude Desktop, Codex, Antigravity, Cursor, '
                                    'Copilot, VS Code, Gemini): the MCP server plus a short instruction; --undo takes it out')
    s.add_argument('agent', nargs='?', help=f'one agent: {", ".join(connect.AGENTS)} (default: every installed one)')
    s.add_argument('--all', action='store_true', help='every installed agent (the default)')
    s.add_argument('--undo', action='store_true', help='remove Tutti from the agent (or all), restoring its config otherwise untouched')
    s.add_argument('--status', action='store_true', help='show each agent\'s state; change nothing')
    s.add_argument('--test', action='store_true', help='with the status: start each connected agent\'s server and list its tools')
    s.add_argument('--repo', help='a household repo\'s folder: add the "all work goes through Tutti" section to its AGENTS.md '
                                  '(or CLAUDE.md) instead (docs/INTAKE.md)')
    s.add_argument('--json', action='store_true')
    s = add('move', move.cmd, 'move Tutti (the dashboard, the state, the loops) to another machine: preflight, copy, cut-over, '
                              'clients and members repointed; --dry-run shows the plan, --rollback brings it back (docs/MOVE.md)')
    s.add_argument('--to', help='the machine to move to (a name under machines:)')
    s.add_argument('--dry-run', action='store_true', help='check the target and print the whole plan; change nothing')
    s.add_argument('--rollback', action='store_true', help='Tutti back to this computer (the last move undone)')
    s.add_argument('--jobs', choices=['wait', 'handover'], default='wait',
                   help='running autopilot jobs: wait for them to finish (default), or hand them over (stop now, requeued)')
    s.add_argument('--wait-minutes', type=float, default=60, help='with --jobs wait: how long to wait for them (60)')
    s.add_argument('--restart', action='store_true', help='start the move again from the preflight instead of resuming it')
    s.add_argument('--status', action='store_true', help='where Tutti runs, and the last or current move')
    s.add_argument('--devices', action='store_true', help='every Android device follows (tutti devices move: '
                   'the adb key is copied, nothing is asked; docs/DEVICES.md)')
    s.add_argument('--json', action='store_true')
    s = add('bridge', move.cmd_bridge, 'the connection a client keeps to the host (docs/MOVE.md, *The bridge*): status, '
                                       'serve (the host end, started by the client\'s ssh), exec (a run on a client)')
    s.add_argument('action', choices=['status', 'serve', 'exec'])
    s.add_argument('machine', nargs='?', help='exec: the client machine')
    s.add_argument('--machine', dest='client', help='serve: the client machine at the other end')
    s.add_argument('--id', help='exec: the run id')
    s.add_argument('rest', nargs=argparse.REMAINDER, help='exec: -- run <worker> <task> ...')
    s = add('client', move.cmd_client, 'this computer as a client of the host Tutti moved to: keeps the bridge and the dashboard\'s '
                                       'port up (the tutti-client user service runs it); --once: one connection')
    s.add_argument('--once', action='store_true')
    s = add('vpn', vpncmd.cmd_vpn, 'an optional private network between the household\'s machines and phones (docs/NETWORK.md): '
                                   'status, setup (Tailscale, or --wireguard); Tutti works without one')
    s.add_argument('action', nargs='?', default='status', choices=['status', 'setup', 'funnel'])
    g = s.add_mutually_exclusive_group()
    g.add_argument('--tailscale', dest='provider', action='store_const', const='tailscale', help='set up Tailscale')
    g.add_argument('--wireguard', dest='provider', action='store_const', const='wireguard', help='set up plain WireGuard (no account)')
    s.add_argument('--dry-run', action='store_true', help='setup: print what it would do; change nothing')
    s.add_argument('--authkey', metavar='MACHINE', help='setup: join a headless machine with a Tailscale auth key typed hidden')
    s.add_argument('--cert', action='store_true', help='setup: also make the private https certificate for this computer\'s tailnet name')
    s = add('auth', authcmd.cmd_auth, 'sign-in with Google for the dashboard (docs/AUTH.md): status, setup [--client-id <id>] '
                                      '(prints the console steps), users, promote <email>, reset-admin --yes')
    s.add_argument('action', nargs='?', default='status', choices=list(authcmd.ACTIONS))
    s.add_argument('email', nargs='?', help='promote: the e-mail of a user who signed in once')
    s.add_argument('--client-id', help='setup: the OAuth client ID (public, not a secret)')
    s.add_argument('--yes', action='store_true', help='reset-admin: really forget every user and session')
    s.add_argument('--device-client-id', help='setup: the "TVs and Limited Input devices" client ID (sign-in from the LAN and any VPN)')
    s.add_argument('--device-secret-stdin', action='store_true', help='setup: read that client\'s secret from stdin (never argv)')
    s.add_argument('--tailscale', action='store_true', help='setup: serve the dashboard on this computer\'s Tailscale https '
                                                            'name (tailnet only) and add that origin')
    s = add('main', cmd_main, 'the main agent: show it and who has room, or `tutti main <agent>` to switch it; '
                              '`tutti main backup [<machine>|auto|none]`: the backup main machine (docs/FAILOVER.md)')
    s.add_argument('agent', nargs='?')
    s.add_argument('machine_name', nargs='?', help='backup: the machine, auto (Tutti elects one) or none (no failover)')
    s.add_argument('--yes', action='store_true', help='backup: change it although the current backup does not answer')
    from . import failovercmd
    s = add('failover', failovercmd.cmd_failover, 'the backup main machine: status, setup (copy Tutti there and start the rounds), '
                                                 'tick (one round), run (the rounds, as a service), leave (this main shuts down)')
    s.add_argument('action', nargs='?', default='status', choices=['status', 'setup', 'tick', 'run', 'leave', 'beat', 'hand-back',
                                                                  'retire'])
    s.add_argument('--to', help=argparse.SUPPRESS)
    s.add_argument('--term', type=int, default=0, help=argparse.SUPPRESS)
    s.add_argument('--json', action='store_true', help='answer as JSON (what the main reads over ssh)')
    s = add('state', failovercmd.cmd_state, 'Tutti\'s state: its size against the budget and its biggest items (size), or trim old '
                                           'runs, screenshots and logs (prune)')
    s.add_argument('action', nargs='?', default='size', choices=['size', 'prune'])
    return p


def on_client(cfg, a, argv):
    """A client of a Tutti that runs elsewhere (docs/MOVE.md): the command runs on the host; with the host unreachable, a
    read runs here on the read-only copy, `run` runs here alone, a change is refused. None: run it here as usual."""
    if not clientmode.should_forward(cfg, a.cmd) or (a.cmd == 'wait' and a.kind != 'run'):
        return None
    if a.cmd == 'run' and not a.repo and not a.dir:
        argv = [*argv, '--dir', os.getcwd()]   # the worker edits this folder, wherever the command runs
    rc = clientmode.forward(cfg, argv)
    if rc is not None:
        return rc
    if a.cmd == 'run':
        say(f'{cfg["host"]["machine"]} cannot be reached: this run works here alone; its record stays on this computer')
        os.environ['TUTTI_LOCAL'] = '1'
        return a.fn(cfg, a)
    if clientmode.is_read_only(a.cmd, getattr(a, 'action', None)) and not getattr(a, 'agent', None):
        say(clientmode.mirror_note(cfg))
        return a.fn(clientmode.fallback_cfg(cfg), a)
    say(clientmode.refusal(cfg, a.cmd))
    return 1


def parse(argv):
    """The parsed command line. `tutti devices lease <d> [<d2>] --runner <m> --who <s> -- <command>` (the dispatcher's own
    form) puts the command after the options: argparse fills `names` before them and then refused the rest as
    'unrecognized arguments', so every dispatched RoRadio shard failed before it ran (2026-11-16). Those words join
    `names`, where the lease splits devices from the command at `--`. `tutti test <track> -- <command>` drops the `--`."""
    a, extra = parser().parse_known_args(argv)
    if extra:
        if getattr(a, 'cmd', None) == 'devices' and getattr(a, 'action', None) == 'lease':
            a.names = [*(a.names or []), *extra]
        else:
            parser().error('unrecognized arguments: ' + ' '.join(extra))
    if getattr(a, 'cmd', None) == 'test' and a.command and a.command[0] == '--':
        a.command = a.command[1:]
    return a


def main(argv=None):
    real = argv is None
    argv = sys.argv[1:] if argv is None else argv
    a = parse(argv)
    if a.fn in (cmd_doctor, cmd_setup, cmd_self_update):  # they must work before any config exists
        return a.fn(None, a)
    # secrets are read from their files at the point of use, never inherited (docs/ARCHITECTURE.md, t116); the dashboard runs
    # again without them so its own /proc environ holds none
    if real and a.fn is cmd_dashboard and not a.once:
        secretenv.reexec_clean()
    secretenv.scrub_self()
    try:
        cfg = config.load(a.config)
        os.environ['TUTTI_CONFIG'] = cfg['_path']
        github.use(cfg)   # every GitHub call: the connected token, else gh's (docs/GITHUB.md)
        rc = on_client(cfg, a, [x for i, x in enumerate(argv) if x != '--config' and (i == 0 or argv[i - 1] != '--config')
                                and not x.startswith('--config=')])
        if rc is not None:
            return rc
        frozen = move.frozen_note(cfg, a.cmd, getattr(a, 'action', None))
        if frozen:
            say(frozen)
            return 1
        if writes_state(a) and fenced(cfg, a.cmd):
            return failover_fenced_code()
        return a.fn(cfg, a)
    except (config.ConfigError, ship.ShipError, backlog.BacklogError, RuntimeError, ValueError) as e:
        telemetry.record('error', type(e).__name__, command=a.cmd)
        say(str(e))
        return 1
    except KeyboardInterrupt:
        return 130


def entry(argv=None):
    """bin/tutti: main() with telemetry (every command recorded on this computer; a crash reported when sending is on)."""
    return telemetrycmd.run(main, argv)
