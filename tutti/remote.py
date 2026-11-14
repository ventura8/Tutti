"""Runs commands on machines (this computer, ssh, or WSL behind a Windows ssh) and copies a work tree to them."""
import base64
import hashlib
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time

from . import routing, worktree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLOT_SCRIPT = os.path.join(ROOT, 'scripts', 'slot.sh')
BIN = os.path.join(ROOT, 'bin', 'tutti')
SSH = ['ssh', '-o', 'LogLevel=ERROR']
BATCH = 'BatchMode=yes'                        # never a password prompt: a key works, or the machine counts as down
QUICK = ('-o', 'ConnectTimeout=5', '-o', BATCH)
# One ssh connection per machine, shared: the dashboard's probes, `tutti test`, rsync and git reuse it instead of a new
# handshake each (nuc 0.18 s -> 0.02 s a call, 2026-11-14). The master is its own background process (-N -f, no captured
# pipes: a ControlPersist master started by a captured call keeps the pipe open and the caller hangs); calls join it with
# ControlMaster=no, and connect on their own while it is missing. Off with machines.<m>.ssh_reuse: false; never on Windows.
MUX_PERSIST = 600
MUX_RETRY = 60   # seconds before another master is tried for the same machine (a lock file per machine, across processes)


_BASH_PATHS = {}


def bash_path(p):
    """A path on this computer as the local bash sees it. On Windows that bash is WSL's (C:\\x -> /mnt/c/x) or Git
    Bash (-> /c/x): handed the Windows path, `cd` failed and every local run exited 1. Elsewhere the path itself."""
    if os.name != 'nt':
        return p
    if p not in _BASH_PATHS:
        # The path goes inside the line, which travels base64-encoded (argv): WSL's bash.exe passes no arguments after
        # the -c line, so a "$1" came empty and every path became the current folder.
        q = shlex.quote(p)
        r = subprocess.run(argv({'transport': 'local'}, f'wslpath -a {q} 2>/dev/null || cygpath -u {q}'),
                           capture_output=True, encoding='utf-8', errors='replace', check=False)
        _BASH_PATHS[p] = r.stdout.strip() or p
    return _BASH_PATHS[p]


def home_path(p):
    """A path for a remote bash line: a leading ~ becomes $HOME, the rest is quoted."""
    if p == '~' or p.startswith('~/'):
        return '"$HOME"' + shlex.quote(p[1:]) if p[1:] else '"$HOME"'
    return shlex.quote(p)


def root_path(machine):
    r = machine['root']
    return home_path(r if r.startswith(('/', '~')) else '~/' + r)


def argv(machine, script, ssh_opts=()):
    """The local argv that runs a bash script on the machine."""
    t = machine['transport']
    if t == 'local':
        if os.name == 'nt':
            # Windows rebuilds the command line for bash.exe (WSL or Git Bash) and mangles double quotes inside it:
            # `cut -d" "` arrived as `cut -d\"`, so the local probe printed garbage and `here` showed "unreachable".
            # The line travels base64-encoded instead, as it does for a WSL machine behind ssh: no quotes to mangle.
            # It is sourced from a process substitution, not piped into bash, so the command keeps the caller's standard
            # input. No quotes, `&&`, `$( )` or braces either: the 0.1.1 form (mktemp && ... > $f && { bash $f; ... })
            # reached bash as "syntax error near unexpected token `&&'" and every local run failed again (QA round 2,
            # Windows 11, 2026-11-14). `source <(...)` was the one form tested that kept stdin, the exit code and quotes.
            packed = base64.b64encode(script.encode()).decode()
            return ['bash', '-c', f'source <(echo {packed} | base64 -d)']
        return ['bash', '-c', script]
    mux = mux_opts(machine)
    if t == 'ssh':
        return [*SSH, *mux, *ssh_opts, machine['host'], script]
    if t == 'android':
        return [*SSH, *port_opts(machine), *mux, *ssh_opts, dest(machine), script]
    packed = base64.b64encode(f'cd ~ && {script}'.encode()).decode()
    return [*SSH, *mux, *ssh_opts, machine['host'], f'wsl -e {machine["wsl_helper"]} {packed}']


def mux_path(machine):
    """The control socket of a machine's shared connection: short (a socket path has ~100 bytes) and private to this user."""
    key = hashlib.sha256(f'{dest(machine)}:{machine.get("port") or ""}:{machine.get("transport")}'.encode()).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f'tutti-ssh-{os.getuid()}', key)


def reuses(machine):
    return os.name != 'nt' and machine.get('transport') in ('ssh', 'wsl', 'android') and machine.get('ssh_reuse', True) is not False


def mux_opts(machine):
    """The options that join the machine's shared connection, or none while there is no master (a call then connects itself)."""
    if not reuses(machine):
        return []
    p = mux_path(machine)
    return ['-o', 'ControlMaster=no', '-o', f'ControlPath={p}'] if os.path.exists(p) else []


def ensure_master(machine, spawn=subprocess.Popen, clock=time.time):
    """Starts the machine's shared connection in the background when it has none; at most once a MUX_RETRY per machine
    (a lock file), so a host that is down is not dialled twice per probe. Returns True when it started one."""
    if not reuses(machine):
        return False
    p = mux_path(machine)
    if os.path.exists(p):
        return False
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    lock = p + '.try'
    try:
        if clock() - os.path.getmtime(lock) < MUX_RETRY:
            return False
    except OSError:
        pass
    with open(lock, 'w', encoding='utf8') as f:
        f.write(f'{os.getpid()}\n')
    spawn([*SSH, *port_opts(machine), '-o', BATCH, '-o', 'ConnectTimeout=5', '-o', 'ControlMaster=yes', '-o', f'ControlPath={p}',
           '-o', f'ControlPersist={MUX_PERSIST}', '-N', '-f', dest(machine)],
          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def dest(machine):
    """The ssh destination: [user@]host (a Termux machine's user is like u0_a123)."""
    return f'{machine["user"]}@{machine["host"]}' if machine.get('user') else machine['host']


def port_opts(machine):
    """`-p <port>` for a Termux machine (its sshd listens on 8022), else nothing."""
    return ['-p', str(machine.get('port') or 8022)] if machine.get('transport') == 'android' else []


def sh(machine, script, capture=False, timeout=None, stdin=None, ssh_opts=()):
    if machine['transport'] != 'local':
        ensure_master(machine)
    return subprocess.run(argv(machine, script, ssh_opts), capture_output=capture, text=True, timeout=timeout, input=stdin)


def tee(machine, script, out):
    """Runs the script with its output (stdout and stderr) both on this terminal and in `out`, a job log. Returns the exit code."""
    p = subprocess.Popen(argv(machine, script), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
    for line in p.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        out.write(line)
        out.flush()
    return p.wait()


def rsh(cfg, machine):
    """What git (GIT_SSH_COMMAND) and rsync (-e) use to reach the machine: plain ssh, or `tutti rsh` for WSL."""
    if machine['transport'] == 'android':
        return f'ssh -p {int(machine.get("port") or 8022)}'
    if machine['transport'] != 'wsl':
        return ' '.join(['ssh', *mux_opts(machine)])
    return f'{shlex.quote(sys.executable)} {shlex.quote(BIN)} --config {shlex.quote(cfg["_path"])} rsh {shlex.quote(machine["name"])}'


def rsh_main(machine, args):
    """`tutti rsh <machine> [ssh options...] <host> <command...>`: the remote-shell shim git and rsync call for a WSL machine."""
    args = list(args)
    while args and args[0].startswith('-'):
        if args[0] in ('-o', '-p', '-l'):
            args.pop(0)
        args.pop(0)
    command = args[1:]
    script = command[0] if len(command) == 1 else ' '.join(shlex.quote(a) for a in command)
    return subprocess.run(argv(machine, script)).returncode


def lock_files(machine):
    """The machine's slot lock files: its explicit `locks`, else <lock_prefix><n>.lock for each slot."""
    return machine.get('locks') or [f'{machine["lock_prefix"]}{i}.lock' for i in range(1, int(machine['slots']) + 1)]


def probe_script(machine):
    y = home_path(machine['yield_file'])
    locks = ' '.join(shlex.quote(p) for p in lock_files(machine))
    return (f'n=0; for l in {locks}; do flock -n "$l" true 2>/dev/null && n=$((n+1)); done; '
            f'y=0; [ -e {y} ] && y=1; '
            # the hottest readable thermal zone in millidegrees (0 when none): routing caps or skips a hot machine
            't=$(cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | sort -n | tail -n 1); '
            f'{GPU_TEMP}'
            f'{containers_script(machine)}'
            f'{termux_readings(machine)}'
            # Android may refuse /proc/loadavg (SELinux): the load is then 0, not a broken line
            f'echo "$n $(cut -d" " -f1 /proc/loadavg 2>/dev/null || echo 0) $(nproc) $y ${{t:-0}} ${{g:--}}"')


def termux_readings(machine):
    """A Termux machine's battery, charging, heat and Wi-Fi lines (`R key=value`), before the summary line."""
    if machine.get('transport') != 'android':
        return ''
    from . import devices
    return devices.TERMUX_READINGS


# The hottest GPU in degrees C, `-` when none is readable: nvidia-smi (in WSL it lives in /usr/lib/wsl/lib, not on PATH),
# else an AMD or Intel GPU's hwmon sensor (amdgpu, xe, i915). Read-only.
SMI = '--query-gpu=temperature.gpu --format=csv,noheader,nounits'
GPU_TEMP = ('g=$( { nvidia-smi ' + SMI + ' 2>/dev/null || /usr/lib/wsl/lib/nvidia-smi ' + SMI + ' 2>/dev/null; '
            'for h in /sys/class/hwmon/hwmon*; do case "$(cat "$h/name" 2>/dev/null)" in amdgpu|xe|i915) '
            'awk \'{print $1/1000}\' "$h/temp1_input" 2>/dev/null;; esac; done; } | sort -n | tail -n 1); ')

# A WSL machine has no /sys thermal zones: its CPU temperature is Windows', read over the machine's plain ssh with PowerShell
# (read-only): LibreHardwareMonitor's or OpenHardwareMonitor's "CPU Package" sensor when one of them runs, else the hottest
# ACPI thermal zone. Many boards report a fixed placeholder there (about 17 C on a desktop), so a value under 20 C or over
# 110 C counts as not readable, and the dashboard says so instead of showing a wrong number.
WIN_TEMP = (
    "$t = $null; foreach ($ns in 'root/LibreHardwareMonitor','root/OpenHardwareMonitor') { try { "
    "$s = Get-CimInstance -Namespace $ns -ClassName Sensor -ErrorAction Stop | Where-Object { $_.SensorType -eq 'Temperature' -and "
    "$_.Name -match 'CPU Package|Core Max|Tctl' } | Sort-Object Value -Descending | Select-Object -First 1; "
    "if ($s) { $t = [double]$s.Value; 'S ' + $ns.Split('/')[1]; break } } catch { } }; "
    "if ($t -eq $null) { try { $z = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop | "
    "Sort-Object CurrentTemperature -Descending | Select-Object -First 1; if ($z) { $t = $z.CurrentTemperature / 10 - 273.15; 'S ACPI' } } "
    "catch { } }; if ($t -ne $null) { 'T ' + [Math]::Round($t, 1).ToString([Globalization.CultureInfo]::InvariantCulture) }")
WIN_TEMP_SECONDS = 120
_win_temps = {}


def parse_win_temp(text):
    """(degrees C or None, why not) from WIN_TEMP's lines `S <source>` and `T <degrees>`."""
    source, t = '', None
    for line in (text or '').splitlines():
        if line.startswith('S '):
            source = line[2:].strip()
        elif line.startswith('T '):
            try:
                t = float(line[2:].strip())
            except ValueError:
                t = None
    if t is None:
        return None, 'CPU not readable: Windows offers no temperature sensor (install LibreHardwareMonitor to give it one)'
    if not 20 <= t <= 110:
        return None, (f'CPU not readable: Windows\' {source or "sensor"} reports {t:g} C, a placeholder, not the CPU '
                      '(LibreHardwareMonitor gives a real one)')
    return t, ''


def windows_temp(machine, clock=time.time, timeout=30):
    """(CPU degrees C or None, why not) of a WSL machine's Windows side, asked at most every WIN_TEMP_SECONDS."""
    hit = _win_temps.get(machine['name'])
    if hit and clock() - hit[0] < WIN_TEMP_SECONDS:
        return hit[1]
    packed = base64.b64encode(WIN_TEMP.encode('utf-16le')).decode()
    try:
        r = subprocess.run([*SSH, *QUICK, machine['host'],
                            f'powershell -NoProfile -NonInteractive -EncodedCommand {packed}'],
                           capture_output=True, text=True, timeout=timeout, check=False)
        value = parse_win_temp(r.stdout) if r.returncode == 0 else (None, f'CPU not read: the Windows side answered exit {r.returncode}')
    except (OSError, subprocess.SubprocessError) as e:
        value = (None, f'CPU not read: the Windows side did not answer ({type(e).__name__})')
    _win_temps[machine['name']] = (clock(), value)
    return value


def containers_script(machine):
    """Lines `C <name> <running for>` for the machine's running test containers (names starting with one of
    `containers`, default tutti-), before the probe's summary line: what holds its slots, for the dashboard."""
    prefixes = machine.get('containers') or ['tutti-']
    rx = shlex.quote('^(' + '|'.join(re.escape(p) for p in prefixes) + ')')
    return ("docker ps --format '{{.Names}} {{.RunningFor}}' 2>/dev/null | "
            f"grep -E {rx} | sed 's/^/C /'; ")


def probe(machine, timeout=20, windows_fn=None):
    try:
        r = sh(machine, probe_script(machine), capture=True, timeout=timeout, ssh_opts=QUICK)
    except (OSError, subprocess.SubprocessError):
        return routing.Probe()
    windows_fn = windows_fn or windows_temp
    lines = r.stdout.strip().splitlines() if r.returncode == 0 else []
    p = routing.parse_probe(lines[-1] if lines else '')
    p.containers = [line[2:].strip() for line in lines[:-1] if line.startswith('C ')]
    if machine['transport'] == 'android':
        from . import devices
        p.readings = devices.parse_readings('\n'.join(lines[:-1]), prefix='R ')
    if p.reachable and p.temp is None and machine['transport'] == 'wsl':
        p.temp, p.temp_note = windows_fn(machine)
    return p


def rewrite(runner, command):
    """runner.rewrite maps a substring to its replacement, e.g. a pipeline entry point that starts docker itself to the one
    that runs inside the image (there is no docker in the container)."""
    for old, new in (runner.get('rewrite') or {}).items():
        command = command.replace(old, new)
    return command


def runner_command(machine, track, command, mount=''):
    """The line that runs `command` at the tree root: plain bash, or a capped container (memory, no extra swap, CPUs).
    `mount` is extra docker -v arguments (a linked worktree's main .git)."""
    r = machine['runner']
    command = rewrite(r, command)
    inner = f'{r["prelude"]} && {command}' if r.get('prelude') else command
    if r['kind'] == 'shell':
        return f'bash -c {shlex.quote(inner)}'
    if r['kind'] == 'native':   # Android (Termux): no Docker; niced so the phone stays responsive
        return f'nice -n 10 bash -c {shlex.quote(inner)}'
    env = ' '.join(f'-e {shlex.quote(f"{k}={v}")}' for k, v in r.get('env', {}).items())
    extra = ' '.join(shlex.quote(str(a)) for a in r.get('args', []))
    return (f'docker run --rm --memory={r["memory_gb"]}g --memory-swap={r["memory_gb"]}g --cpus={r["cpus"]} {env} {extra} '
            f'--name tutti-{machine["name"]}-{track}-$RANDOM -u "$(id -u):$(id -g)" -v "$PWD:$PWD" -w "$PWD" '
            f'{mount + " " if mount else ""}{shlex.quote(r["image"])} bash -c {shlex.quote(inner)}')


def slot_prefix(machine):
    """slot.sh's environment: the memory floor, the lock files and the heat limits, so a run that already waits in the queue
    re-reads the temperature right before it starts (routing only looked when it was queued)."""
    locks = shlex.quote(' '.join(lock_files(machine)))
    heat = machine.get('heat') or {}
    names = {'stop_c': 'HEAT_STOP_C', 'cap_c': 'HEAT_CAP_C', 'hot_slots': 'HOT_SLOTS'}
    extra = ''.join(f' {names[k]}={int(float(heat[k]))}' for k in names if heat.get(k) is not None)
    return f'MIN_FREE_GB={int(machine["min_free_gb"])} SLOT_LOCKS={locks}{extra}'


def _check(result, what):
    code = result if isinstance(result, int) else result.returncode
    if code != 0:
        raise RuntimeError(f'{what} failed (exit {code})')


def sync(cfg, machine, track, src):
    """Copies the tree (incremental rsync, the config's excludes) and gives the copy a shallow git repository at the tree's
    commit: a worktree's `.git` is a file that points at this computer, so tests that ask git would fail on the copy."""
    name, root, rs = machine['name'], root_path(machine), rsh(cfg, machine)
    to = f'{dest(machine)}:{machine["root"]}'
    is_git = subprocess.run([worktree.git_bin(), '-C', src, 'rev-parse', '--git-dir'], capture_output=True).returncode == 0
    _check(sh(machine, f'mkdir -p {root}/{track} && {{ [ -d {root}/repo.git ] || git init -q --bare {root}/repo.git; }}'),
           f'preparing {name}')
    if is_git:
        _check(subprocess.run([worktree.git_bin(), '-C', src, 'push', '-q', '-f', f'{to}/repo.git', f'HEAD:refs/heads/tutti/{track}'],
                              env={**os.environ, 'GIT_SSH_COMMAND': rs}), f'pushing HEAD to {name}')
    excludes = [a for e in cfg['sync']['exclude'] for a in ('--exclude', e)]
    _check(subprocess.run(['rsync', '-a', '--delete', '-e', rs, *excludes, f'{src}/', f'{to}/{track}/']), f'copying the tree to {name}')
    with open(SLOT_SCRIPT, encoding='utf8') as f:
        _check(sh(machine, f'cat > {root}/tutti-slot.sh', stdin=f.read()), f'installing slot.sh on {name}')
    return is_git


def inputs_env(inputs):
    """(export line, docker arguments) that hand the folder of a job's input files to its command as $TUTTI_INPUTS, read-only
    inside a container. `inputs` is the folder as a bash path on the machine (tutti files send put the files there)."""
    if not inputs:
        return '', ''
    return f'export TUTTI_INPUTS={inputs} && ', '-e TUTTI_INPUTS -v "$TUTTI_INPUTS:$TUTTI_INPUTS:ro"'


def merge_runner(base, over):
    """A runner with another's keys on top (env merged, args added after the machine's own)."""
    over = over or {}
    out = {**base, **{k: v for k, v in over.items() if k not in ('env', 'args')}}
    out['env'] = {**base.get('env', {}), **over.get('env', {})}
    out['args'] = [*base.get('args', []), *over.get('args', [])]
    return out


def device_wrap(device, command):
    """A device run: ANDROID_SERIAL set (adb, Gradle and Playwright pick the device from it) and the device's lock held, so one
    run at a time uses it; a second run waits in the same queue as a machine's slots (slot.sh, memory floor off)."""
    if not device:
        return command
    serial, lock = shlex.quote(str(device['serial'])), home_path(device['lock'])
    return f'mkdir -p "$(dirname {lock})" && env ANDROID_SERIAL={serial} SLOT_LOCKS={lock} MIN_FREE_GB=0 bash {{slot}} env {command}'


def run(cfg, machine, track, src, command, inputs=None, device=None, out=None):
    """Runs `command` over the tree on the machine, behind its slot queue, after the steps it needs first (test.prepare).
    With `device` the command also holds that Android device's lock and gets ANDROID_SERIAL. With `out` (a job log) the
    output goes there too. Returns the command's exit code."""
    command = routing.prepare(command, (cfg.get('test') or {}).get('prepare'))
    slot = slot_prefix(machine)
    export, mount = inputs_env(inputs)
    if device:
        export += f'export ANDROID_SERIAL={shlex.quote(str(device["serial"]))} && '
        runner = merge_runner(machine['runner'], device.get('runner'))   # a device's own runner (shell for Gradle, host network)
        machine = {**machine, 'runner': {**runner, 'env': {**runner.get('env', {}), 'ANDROID_SERIAL': str(device['serial'])}}}
    if machine['transport'] == 'local':
        mount = ' '.join(x for x in (worktree.docker_mount(src), mount) if x)
        slot_sh = shlex.quote(bash_path(SLOT_SCRIPT))
        inner = device_wrap(device, f'{slot} bash {slot_sh} {runner_command(machine, track, command, mount)}').replace('{slot}', slot_sh)
        script = f'cd {shlex.quote(bash_path(src))} && {export}{inner}'
        return tee(machine, script, out) if out else sh(machine, script).returncode
    is_git = sync(cfg, machine, track, src)
    root = root_path(machine)
    git = (f'{{ [ -d .git ] || {{ rm -f .git; git init -q; }}; }} && git fetch -q --depth 1 {root}/repo.git tutti/{track} && '
           'git update-ref --no-deref HEAD FETCH_HEAD && git reset -q && ') if is_git else ''
    dirs = ' '.join(shlex.quote(d) for d in cfg['sync']['ensure_dirs'])
    mk = f'mkdir -p {dirs} && ' if dirs else ''
    inner = device_wrap(device, f'{slot} bash {root}/tutti-slot.sh {runner_command(machine, track, command, mount)}')
    script = f'cd {root}/{track} && {git}{mk}{export}{inner.replace("{slot}", f"{root}/tutti-slot.sh")}'
    return tee(machine, script, out) if out else sh(machine, script).returncode


def fetch(cfg, machine, track, src, path):
    """Copies a result folder or file (screenshots, videos, reports) from the machine's copy of the tree back into `src`.
    Nothing to do for this computer, where the tree is used in place."""
    if machine['transport'] == 'local':
        return 0
    rel = path.strip('/')
    os.makedirs(os.path.dirname(os.path.join(src, rel)) or src, exist_ok=True)
    return subprocess.run(['rsync', '-a', '-e', rsh(cfg, machine), f'{dest(machine)}:{machine["root"]}/{track}/{rel}',
                           os.path.dirname(os.path.join(src, rel)) + '/']).returncode


def device_readings(machine, device, wake=False, timeout=90):
    """The device's readings (a dict, tutti/devices.py) from its machine; {'state': 'unreachable'} when the machine does not answer."""
    from . import devices
    try:
        if machine['transport'] == 'local':
            scripts = shlex.quote(bash_path(os.path.join(ROOT, 'scripts')))
        else:
            scripts = root_path(machine)
            with open(os.path.join(ROOT, 'scripts', devices.READINGS_SCRIPT), encoding='utf8') as f:
                up = sh(machine, f'mkdir -p {scripts} && cat > {scripts}/{devices.READINGS_SCRIPT}', stdin=f.read(), timeout=timeout,
                        ssh_opts=QUICK)
            if up.returncode != 0:
                return {'state': 'unreachable'}
        r = sh(machine, devices.readings_command(device, scripts, wake), capture=True, timeout=timeout, ssh_opts=QUICK)
    except (OSError, subprocess.SubprocessError):
        return {'state': 'unreachable'}
    return devices.parse_readings(r.stdout) if r.returncode == 0 else {'state': 'unreachable'}


def set_yield(machine, on, note=''):
    y = home_path(machine['yield_file'])
    script = f'mkdir -p "$(dirname {y})" && printf "%s\\n" {shlex.quote(note)} > {y}' if on else f'rm -f {y}'
    return sh(machine, script).returncode


def yield_note(machine):
    """None when the flag is down, else the flag file's text (may be empty; the word `unload` asks for models to be unloaded)."""
    y = home_path(machine['yield_file'])
    try:
        r = sh(machine, f'[ -e {y} ] && {{ echo UP; cat {y}; }} || echo DOWN', capture=True, timeout=40,
               ssh_opts=('-o', 'ConnectTimeout=15', '-o', BATCH))
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out[2:].strip() if out.startswith('UP') else None
