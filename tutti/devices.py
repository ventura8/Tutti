"""Android devices: e2e targets reached over adb from a host machine, and the safe-use policy (battery, charging, heat,
screen, Wi-Fi) shared with Termux machines. The readings come from scripts/device-readings.sh (adb) or from the Termux
probe; deciding is pure and unit-tested here."""
import os
import re
import shlex
import time

from . import config

READINGS_SCRIPT = 'device-readings.sh'

# An e2e target: run while it has charge and is not hot; wake its screen first (Chrome and app UI tests need it on).
E2E_POLICY = {'min_battery': 30, 'require_charging': False, 'max_battery_c': 42, 'wifi_only': False, 'screen': 'wake'}
# A Termux machine lends its CPU: only while charging, well charged, cool, on Wi-Fi and while nobody uses it.
TERMUX_POLICY = {'min_battery': 50, 'require_charging': True, 'max_battery_c': 40, 'wifi_only': True, 'screen': 'off'}
SCREENS = ('wake', 'off', 'any')
# A TV (a Cast receiver: devices.<d>.kind: tv) runs on mains power and has no battery or call state to read.
TV_POLICY = {'min_battery': 0, 'require_charging': False, 'max_battery_c': 0, 'wifi_only': False, 'screen': 'any'}
KINDS = ('phone', 'tablet', 'tv')

# Termux's own readings, through the Termux:API app (`pkg install termux-api`): the same keys as device-readings.sh, each
# line starting `R `. termux-* commands hang when the Termux:API app is missing, so each has a timeout; /sys is the fallback.
TERMUX_READINGS = (
    'b=$(timeout 10 termux-battery-status 2>/dev/null | tr -d " \\n"); '
    'pct=$(printf %s "$b" | sed -n "s/.*\\"percentage\\":\\([0-9]*\\).*/\\1/p"); '
    'plug=$(printf %s "$b" | sed -n "s/.*\\"plugged\\":\\"\\([A-Z_]*\\)\\".*/\\1/p"); '
    'bt=$(printf %s "$b" | sed -n "s/.*\\"temperature\\":\\([0-9.]*\\).*/\\1/p"); '
    '[ -n "$pct" ] || pct=$(cat /sys/class/power_supply/battery/capacity 2>/dev/null); '
    '[ -n "$pct" ] && echo "R battery=$pct"; '
    'if [ -n "$plug" ]; then [ "$plug" = UNPLUGGED ] && echo "R charging=0" || echo "R charging=1"; fi; '
    '[ -n "$bt" ] && echo "R battery_c=$bt"; '
    'w=$(timeout 10 termux-wifi-connectioninfo 2>/dev/null | tr -d " \\n" | '
    'sed -n "s/.*\\"supplicant_state\\":\\"\\([A-Z_]*\\)\\".*/\\1/p"); '
    'if [ -n "$w" ]; then [ "$w" = COMPLETED ] && echo "R wifi=1" || echo "R wifi=0"; fi; ')


def policy(entry, termux=False):
    """The device's policy: the defaults for its use, overridden by its own `policy:`."""
    base = TERMUX_POLICY if termux else TV_POLICY if (entry or {}).get('kind') == 'tv' else E2E_POLICY
    out = {**base, **((entry or {}).get('policy') or {})}
    if out.get('screen') is False:   # YAML reads an unquoted `off` as false
        out['screen'] = 'off'
    return out


def lock_path(name, device):
    # under the login's home on the device's machine, not a world-writable folder another user could plant a file in
    return device.get('lock') or f'~/.tutti/locks/device-{name}.lock'


def normalise(cfg):
    """Every device gets its name, its host machine (default: this computer's machine, else the first) and its lock."""
    local = next((n for n, m in cfg['machines'].items() if m['transport'] == 'local'), None)
    first = next(iter(cfg['machines']), None)
    for name, d in (cfg.get('devices') or {}).items():
        d = dict(d or {})
        d['name'] = name
        d.setdefault('machine', local or first)
        d['lock'] = lock_path(name, d)
        cfg['devices'][name] = d


def errors(cfg):
    errs = []
    for name, d in (cfg.get('devices') or {}).items():
        where = f'devices.{name}'
        if not d.get('serial'):
            errs.append(f'{where}.serial is missing: the serial `adb devices` lists (or ip:port for wireless debugging)')
        if d.get('machine') not in cfg['machines']:
            errs.append(f'{where}.machine is {d.get("machine")!r}: name the machine its adb sees it on, one of machines:')
        errs += policy_errors(where, d.get('policy'))
        from . import prep
        errs += prep.errors(where, d.get('prep'))
        if d.get('kind', 'phone') not in KINDS:
            errs.append(f'{where}.kind is {d["kind"]!r}: use phone, tablet or tv (a Cast receiver: only suites with targets: [tv] use it)')
        if 'calls' in d and not isinstance(d['calls'], bool):
            errs.append(f'{where}.calls is {d["calls"]!r}: use true or false (false: no phone-call check, e.g. a tablet with no SIM)')
        if 'external_lock' in d and not (isinstance(d['external_lock'], str) and d['external_lock'].strip()):
            errs.append(f'{where}.external_lock is {d["external_lock"]!r}: the path of the lock file other tools keep for it '
                        '({serial} is its serial)')
        v = d.get('external_lock_stale_minutes')
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0):
            errs.append(f'{where}.external_lock_stale_minutes is {v!r}: minutes after which another tool\'s lock counts as left behind')
        errs += wireless_errors(where, d.get('wireless'))
    return errs


def wireless_errors(where, w):
    if w is None:
        return []
    if not isinstance(w, list) or not all(isinstance(x, str) and re.fullmatch(r'[A-Za-z0-9_.:\[\]-]+:\d{2,5}', x) for x in w):
        return [f'{where}.wireless is {w!r}: a list of host:port adb addresses, LAN first, e.g. [192.168.1.20:5555, '
                'phone.your-tailnet.ts.net:5555] (docs/NETWORK.md, *Android*)']
    return []


def transports(device):
    """The adb addresses in the order they are tried: the USB serial, then each wireless address (LAN, then a VPN one)."""
    return [str(device['serial']), *[a for a in device.get('wireless') or [] if a != str(device['serial'])]]


def serial_expr(device):
    """The shell word for the device's adb serial. With no `wireless:` it is the serial itself; with it, a bash snippet that picks
    the first transport adb sees as `device`, reconnects a dropped wireless one (`adb connect`) on the way, and falls back to the
    serial (the readings then say how to reconnect)."""
    if not device.get('wireless'):
        return shlex.quote(str(device['serial']))
    adb = shlex.quote(device.get('adb') or 'adb')
    usb, *wifi = [shlex.quote(t) for t in transports(device)]
    ok = '[ "$(timeout 5 ' + adb + ' -s "$t" get-state 2>/dev/null | tr -d \'\\r\')" = device ]'
    return ('"$(for t in ' + ' '.join([usb, *wifi]) + '; do ' + ok + ' && { echo "$t"; exit 0; }; case "$t" in *:*) timeout 10 ' + adb +
            ' connect "$t" >/dev/null 2>&1; ' + ok + ' && { echo "$t"; exit 0; };; esac; done; echo ' + usb + ')"')


def policy_errors(where, p):
    if p is None:
        return []
    if not isinstance(p, dict):
        return [f'{where}.policy is {p!r}: write it as {{min_battery: 30, require_charging: false, max_battery_c: 42}}']
    errs = []
    for k, top in (('min_battery', 100), ('max_battery_c', 80)):
        v = p.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= top):
            errs.append(f'{where}.policy.{k} is {v!r}: use a number from 0 to {top}')
    for k in ('require_charging', 'wifi_only'):
        if k in p and not isinstance(p[k], bool):
            errs.append(f'{where}.policy.{k} is {p[k]!r}: use true or false')
    if 'screen' in p and p['screen'] not in (*SCREENS, False):
        errs.append(f'{where}.policy.screen is {p["screen"]!r}: use wake (e2e: wake it first), off (only while nobody uses it) or any')
    return errs


def parse_readings(text, prefix=''):
    """key=value lines (from device-readings.sh, or `R key=value` from the Termux probe) as a dict of numbers and words."""
    out = {}
    for line in (text or '').splitlines():
        line = line.strip()
        if prefix:
            if not line.startswith(prefix):
                continue
            line = line[len(prefix):]
        k, sep, v = line.partition('=')
        if not sep or not k.isidentifier():
            continue
        try:
            out[k] = float(v) if '.' in v else int(v)
        except ValueError:
            out[k] = v
    return out


def _connection(r, serial):
    if r.get('adb') == 'missing':
        return 'adb is not installed on its machine: install the Android platform tools there (`adb version` must answer)'
    if r.get('adb') == 'broken':
        return ('adb is on its machine but does not run: a Windows adb.exe called from WSL needs WSL interop (the WSLInterop '
                'binfmt entry; `ls /proc/sys/fs/binfmt_misc/WSLInterop` must list it), else install adb inside WSL')
    state = r.get('state')
    if state == 'unreachable':
        return 'its machine did not answer over ssh: check it is on and reachable (`tutti machines`)'
    if state == 'unauthorized':
        return 'adb is not allowed yet: unlock the phone and tap Allow on its "Allow USB debugging?" prompt'
    if state == 'offline':
        return f'adb sees it offline: reconnect the cable, or for wireless debugging run `adb connect {serial}` again'
    if state not in (None, 'device') and r.get('last_step'):
        return r['last_step']   # tutti/keepon.py: no machine reaches it to turn Wireless debugging back on
    if state not in (None, 'device'):
        return (f'not connected: plug it in by USB, or turn Wireless debugging on and run `adb connect {serial}` '
                'on its machine (Developer options, once per network)')
    return ''


def _battery(pol, r, hint):
    b = r.get('battery')
    if not pol.get('min_battery'):
        return ''
    if b is None:
        return f'battery not readable: {hint}'
    return f'battery {b:g} % is under {pol["min_battery"]:g} %: charge it; runs start again above that' if b < pol['min_battery'] else ''


def _heat(pol, r, hint):
    t = r.get('battery_c')
    if not pol.get('max_battery_c'):
        return ''
    if t is None:
        return f'battery temperature not readable: {hint}'
    return f'battery {t:g} C is at or over {pol["max_battery_c"]:g} C: it waits until it cools down' if t >= pol['max_battery_c'] else ''


def _use(pol, r):
    if pol.get('require_charging') and r.get('charging') != 1:
        return 'not charging: plug it in; it takes runs only while charging (policy.require_charging)'
    if pol.get('wifi_only') and r.get('wifi') != 1:
        return 'not on Wi-Fi (or not readable): connect it to Wi-Fi; it takes runs only on Wi-Fi (policy.wifi_only)'
    if pol.get('screen') == 'off' and r.get('screen') == 1:
        return 'its screen is on: someone may be using it; it waits until the screen is off (policy.screen: off)'
    return ''


def unfit(pol, r, serial=''):
    """Why the device must not take a run now, or ''. Unreadable power readings refuse it: running blind on a phone is
    how a battery gets drained or cooked."""
    hint = 'install the Termux:API app and `pkg install termux-api`' if 'state' not in r else 'check `adb shell dumpsys battery` answers'
    call = f'Paused: phone call ({CALL_WORDS[r["call"]]}): it waits until the call ends' if on_call(r) else ''
    return _connection(r, serial) or call or _battery(pol, r, hint) or _use_charging(pol, r) or _heat(pol, r, hint) or _use(pol, r)


def _use_charging(pol, r):
    """Charging before heat, as the refusals have always been ordered."""
    return _use({'require_charging': pol.get('require_charging')}, r)


def note(pol, r):
    """A warning that does not refuse a run: an e2e target whose screen stayed dark after the wake key."""
    if pol.get('screen') == 'wake' and r.get('screen') == 0:
        return ('the screen did not wake: turn on Developer options > Stay awake (while charging) or remove the lock screen, '
                'or UI tests see a dark screen')
    return ''


CALL_IDLE, CALL_RINGING, CALL_OFFHOOK = 0, 1, 2
CALL_WORDS = {CALL_RINGING: 'ringing', CALL_OFFHOOK: 'in a call'}


def call_state(text):
    """The phone's call state from `dumpsys telephony.registry`: every SIM slot has its own mCallState line (0 idle,
    1 ringing, 2 offhook); a call on any slot counts, offhook over ringing. None when no line is readable (no telephony)."""
    found = [int(m) for m in re.findall(r'mCallState=(\d)', text or '')]
    return max(found) if found else None


def on_call(r):
    return r.get('call') in (CALL_RINGING, CALL_OFFHOOK)


def status(pol, r, serial=''):
    """(status, reason) for the dashboard and `tutti devices`: offline, call, busy, waiting or ready."""
    why = unfit(pol, r, serial)
    if _connection(r, serial):
        return 'offline', why
    if on_call(r):
        return 'call', f'Paused: phone call ({CALL_WORDS[r["call"]]}); runs start again after it ends'
    if why:
        return 'waiting', why
    if r.get('locked') == 1:
        return 'busy', 'a run holds it; the next run waits for it'
    if r.get('external') == 1:
        return 'busy', f'held by {r.get("external_by") or "another tool"} (its lock file, devices.<d>.external_lock); free once that ends'
    return 'ready', note(pol, r) or 'ready: takes the next run'


EXTERNAL_STALE_MINUTES = 30


def external_lock(device):
    """The device's lock file shared with other tools (`external_lock`, {serial} filled in), or ''."""
    path = device.get('external_lock') or ''
    return path.replace('{serial}', str(device['serial'])) if path else ''


def external_wrap(found, command, who='tutti'):
    """`command` as a device session: it waits while another tool's lock file (devices.<d>.external_lock) exists and is
    fresher than external_lock_stale_minutes (30), writes each with "<who> <time>", runs each device's prep (tutti/prep.py:
    the current values saved first), prints the ready marker (the clock of a suite's `started` check), and when the command
    ends, however it ends, restores the saved values and removes the lock files."""
    from . import prep
    locks = [(external_lock(d), int(float(d.get('external_lock_stale_minutes', EXTERNAL_STALE_MINUTES)) * 60)) for d in found]
    locks = [(p, stale) for p, stale in locks if p]
    setup, restore = prep.scripts(found)
    if not locks and not setup:
        return command
    waits = ''.join(
        f'while [ -e {shlex.quote(p)} ] && [ $(( $(date +%s) - $(stat -c %Y {shlex.quote(p)}) )) -lt {stale} ]; do '
        f'echo "tutti: waiting for {p}: $(head -c 200 {shlex.quote(p)})"; sleep 30; done && ' for p, stale in locks)
    files = ' '.join(shlex.quote(p) for p, _ in locks)
    take = f'for f in {files}; do printf "%s %s\\n" {shlex.quote(who)} "$(date -Iseconds)" > "$f"; done && ' if locks else ''
    release = '; '.join(x for x in (restore, f'rm -f {files}' if locks else '') if x)
    return (f'{waits}{take}trap {shlex.quote(release)} EXIT && {setup + "; " if setup else ""}echo \'tutti: device ready\' && '
            f'{{ {command}; }}; exit $?')


def readings_command(device, scripts_dir, wake=False):
    """The bash line that prints the device's readings on its machine."""
    from . import remote  # remote imports routing, which imports this module
    env = f'DEVICE_SERIAL={serial_expr(device)} DEVICE_LOCK={remote.home_path(device["lock"])}'
    if device.get('adb'):
        env += f' ADB={shlex.quote(device["adb"])}'
    if device.get('prep'):
        from . import prep
        env += f' PREP_SAVED={prep.saved_path(device)}'
    if external_lock(device):
        stale = int(float(device.get('external_lock_stale_minutes', EXTERNAL_STALE_MINUTES)) * 60)
        env += f' EXT_LOCK={shlex.quote(external_lock(device))} EXT_STALE={stale}'
    return f'{env} WAKE={1 if wake else 0} bash {scripts_dir}/{READINGS_SCRIPT}'


def device_state(d, readings):
    pol = policy(d)
    st, reason = status(pol, readings, d['serial'])
    keep = ('battery', 'charging', 'battery_c', 'screen', 'wifi', 'call', 'external_by', 'prep_left', 'revived')
    return {'name': d['name'], 'serial': str(d['serial']), 'machine': d['machine'], 'kind': d.get('kind', 'phone'), 'status': st,
            'reason': reason,
            'policy': pol, **{k: readings.get(k) for k in keep}}


def devices_state(cfg, readings_fn):
    """Every device as the dashboard shows it; a paused one is not asked (its machine may be the one lent out)."""
    out = []
    for d in (cfg.get('devices') or {}).values():
        held = held_why(cfg, d)
        if held:
            out.append({**device_state(d, {}), 'status': 'held', 'reason': held, 'owner_paused': d.get('paused')})
            continue
        out.append(device_state(d, readings_fn(cfg['machines'][d['machine']], d)))
    return out


def candidates(cfg, name):
    """The devices a run may use: the one named, or with `any` every device that is not paused."""
    if name != 'any':
        return [of(cfg, name)]
    found = [d for d in (cfg.get('devices') or {}).values() if not d.get('paused')]
    if not found:
        raise config.ConfigError('no device to use: every device is paused, or none is configured (`tutti devices add`)')
    return found


def held_why(cfg, d):
    """Why the owner holds the device back: it, or the machine its adb is on, is paused; else ''."""
    from . import controls
    host = cfg['machines'].get(d['machine']) or {}
    if d.get('paused'):
        return controls.paused_note(d)
    return f'its machine {d["machine"]} is {controls.paused_note(host)}' if host.get('paused') else ''


def first_ready(cfg, found, readings_fn):
    """(the device to use or None, why the others are not). With several, one no run holds comes first; a busy one that is
    otherwise ready is still taken (the run waits in its lock's queue) when no other is free."""
    why, busy = [], None
    for d in found:
        held = held_why(cfg, d)
        if held:
            why.append(f'{d["name"]}: {held}')
            continue
        pol = policy(d)
        r = readings_fn(cfg['machines'][d['machine']], d, wake=pol.get('screen') == 'wake')
        reason = unfit(pol, r, d['serial'])
        if reason:
            why.append(f'{d["name"]}: {reason}')
        elif r.get('locked') == 1 and len(found) > 1:
            busy = busy or d
        else:
            return d, ''
    return busy, '; '.join(why)


def wait_ready(cfg, name, readings_fn, say, sleep=time.sleep, wait_minutes=30, poll=60):
    """The device a run uses, waiting while its policy refuses it (charge, heat); ConfigError after `wait_minutes`."""
    found, said, waited = candidates(cfg, name), '', 0
    while True:
        device, why = first_ready(cfg, found, readings_fn)
        if device:
            return device
        if waited >= wait_minutes * 60:
            raise config.ConfigError(f'no device got ready in {wait_minutes} min ({why}); fix that, then run again')
        if why != said:
            say(f'waiting for a device ({why})')
            said = why
        sleep(poll)
        waited += poll


def of(cfg, name):
    devices = cfg.get('devices') or {}
    if name not in devices:
        raise config.ConfigError(f'no device "{name}" in {cfg["_path"]}: the devices are {", ".join(devices) or "(none)"}; '
                                 '`tutti devices add` shows how to add one')
    return devices[name]


def job_folder(cfg):
    return os.path.join(config.state_dir(cfg), 'devices')


ADB_STEPS = """\
One-time setup of an Android device as an e2e target ({name}, seen by the machine "{machine}"):

On the device:
  1. Settings > About phone: tap "Build number" 7 times (Developer options turn on).
  2. Settings > System > Developer options: turn on "USB debugging" (and "Stay awake" so UI tests see a lit screen).
  3. Optional, no cable: "Wireless debugging" on, then "Pair device with pairing code" (it shows an ip:port and a code).
  4. Settings > Security > Screen lock: "None" or "Swipe" on a test device, so tests are not stopped by a lock screen.

On {machine} (the computer the device is plugged into or on the same Wi-Fi as):
  5. Install the Android platform tools (adb), e.g. `sudo apt install adb`, and check `adb version` answers.
  6. USB: plug it in and tap "Allow" on the phone's "Allow USB debugging?" prompt (tick "Always allow").
     Wireless: `adb pair <ip:pairing-port>` and type the code shown on the phone, then `adb connect <ip:port>`.
  7. `adb devices` lists its serial (USB) or ip:port (wireless): that is the serial below.

Add to tutti.config.yaml:

devices:
  {name}:
    serial: {serial}
    machine: {machine}
    policy: {{min_battery: 30, require_charging: false, max_battery_c: 42, screen: wake}}

Then `tutti devices` shows its battery, charging, heat, screen and Wi-Fi, and
`tutti test --device {name} -- <command>` runs a command against it (ANDROID_SERIAL set, one run at a time).
"""

TERMUX_STEPS = """\
One-time setup of an Android device as a Termux machine ({name}): light CPU jobs (unit tests, lint) and small local models.

On the device:
  1. Install F-Droid (f-droid.org), then from F-Droid: Termux and Termux:API (the Play Store builds are outdated).
  2. Settings > Apps > Termux > Battery: "Unrestricted", so Android does not stop its ssh server.
  3. In Termux, type:
       termux-setup-storage                      (tap Allow)
       pkg update && pkg install openssh python git rsync util-linux termux-api
       passwd                                    (choose a password yourself; Tutti never asks for it)
       sshd                                      (the ssh server, on port 8022)
       whoami                                    (the user name, like u0_a123)
     The device's IP address: Settings > About phone > Status (or Wi-Fi > the network's details).
  4. Optional local model: `pkg install ollama`, then `ollama serve &` and `ollama pull <a small model>`.

On this computer:
  5. `ssh-copy-id -p {port} {user}@{host}` and type the device's password once; then `ssh -p {port} {user}@{host} true`
     must answer without a password.

Add to tutti.config.yaml:

machines:
  {name}:
    transport: android
    host: {host}
    user: {user}
    port: {port}
    policy: {{min_battery: 50, require_charging: true, max_battery_c: 40, wifi_only: true, screen: 'off'}}

Lint and unit runs on it are opt-in: turn them on from the dashboard (Controls) or with `tutti optin machine {name} on`.
It then takes one light run at a time, natively (no Docker on Android), and always routes last: keep heavy Docker gates
off it. `tutti machines` shows its battery, charging and heat and whether it is fit.
"""
