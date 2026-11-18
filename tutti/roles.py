"""Roles of machines and devices (docs/MACHINES.md, *Roles*): what each one is for, so a suite only goes where it can run.

A role is detected (the daily probe below for a machine; the device readings for an Android device), comes from the config
(the main machine, the backup main machine, the browser machine) or is set by an admin:
- `machines.<m>.roles` / `devices.<d>.roles`: the whole list, an override (as the Cast roles always were);
- `roles_add` / `roles_off`: added to, or taken from, what was detected (`tutti roles set|unset`, the dashboard's Settings).
Each role carries why it is there ('detected: docker 27.3', 'set by an admin'), shown as the chip's tooltip.

A suite asks with `roles: [windows-test-runner, ui-desktop]` (every one needed); dispatch.compatible refuses a target that
lacks one, and among the ones left the power score picks as before. Pure, except machine_roles (probe and cache)."""
import json
import os
import subprocess

from . import castroles, homeassistant, lanmedia

ROLES = {
    'cast-sender': 'sends to a Cast receiver (Chrome, Chromium, the desktop sender, an Android phone)',
    'cast-receiver': 'a Cast target (a TV, a Chromecast, a hub-mode tablet)',
    'windows-test-runner': 'runs Windows suites (Windows, or WSL with Windows next to it)',
    'linux-test-runner': 'runs Linux suites',
    'macos-test-runner': 'runs macOS suites',
    'android-test-runner': 'an adb host: runs suites on the Android devices plugged into it',
    'android-device': 'an Android device suites run on',
    'tv-device': 'an Android TV (leanback)',
    'emulator-host': 'runs Android emulators (an SDK emulator and at least one AVD)',
    'chrome-browser': 'the household browser machine (Claude in Chrome, browser.machine)',
    'docker-host': 'runs containers (Playwright images, quality images)',
    'gpu-ai': 'local models on a GPU (Ollama, VRAM)',
    'build-machine': 'heavy builds and test runs (many cores, much memory)',
    'ui-desktop': 'an interactive desktop for UI suites',
    'main': 'the machine Tutti runs on',
    'backup-main': 'the backup main machine (failover.backup)',
    'vpn-node': 'on the private network (WireGuard or Tailscale)',
    'ios-webkit-runner': 'runs iOS simulators (Xcode) or Playwright WebKit',
}
ROLES.update(lanmedia.ROLES)   # media roles of LAN devices (a soundbar, a smart TV), found over mDNS and SSDP
ROLES.update(homeassistant.ROLES)   # Home Assistant host roles (home-automation-hub, presence, power/UPS, …)

# one `key=value` per line; every check is cheap (no docker daemon call, no GPU load)
MACHINE_PROBE = r'''h() { command -v "$1" > /dev/null 2>&1; }
echo "os=$(uname -s)"
grep -qi microsoft /proc/version 2> /dev/null && echo wsl=1
echo "cores=$(getconf _NPROCESSORS_ONLN 2> /dev/null || sysctl -n hw.ncpu 2> /dev/null)"
awk '/MemTotal/ {printf "mem_gb=%d\n", $2/1048576}' /proc/meminfo 2> /dev/null
[ "$(uname -s)" = Darwin ] && echo "mem_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))"
for t in docker adb dotnet pwsh ollama wg tailscale xcrun emulator; do h "$t" && echo "$t=1"; done
for p in "$HOME/Android/Sdk/platform-tools/adb" "$HOME/Library/Android/sdk/platform-tools/adb" \
  "/mnt/c/Program Files/Android/platform-tools/adb.exe"; do [ -x "$p" ] && echo adb=1; done
for p in "$HOME/Android/Sdk/emulator/emulator" "$HOME/Library/Android/sdk/emulator/emulator"; do [ -x "$p" ] && echo emulator=1; done
echo "avds=$(ls -d "$HOME"/.android/avd/*.avd 2> /dev/null | wc -l)"
[ -e /mnt/c/Windows ] && { echo windows=1; ls /mnt/c/Program\ Files/PowerShell/*/pwsh.exe > /dev/null 2>&1 && echo pwsh=1;
  [ -e "/mnt/c/Program Files/dotnet/dotnet.exe" ] && echo dotnet=1; }
ls -d "$HOME"/.cache/ms-playwright/webkit-* > /dev/null 2>&1 && echo webkit=1
ip link show wg0 > /dev/null 2>&1 && echo wg=1
h nvidia-smi && nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2> /dev/null | head -1 | sed 's/^/vram_mb=/'
pgrep -x 'Xorg|Xwayland|gnome-shell|xfce4-session|kwin_wayland|plasmashell|cinnamon|mate-session' > /dev/null 2>&1 && echo desktop=1
[ "$(uname -s)" = Darwin ] && echo desktop=1
''' + castroles.MACHINE_PROBE + '\n'

FILE, EVERY = 'roles-machines.json', 86400
DETECTED = 'detected: '
RETRY = 600   # a machine that never answered is asked again after 10 minutes


def parse(text):
    """The probe's answer as {key: value}."""
    out = {}
    for line in (text or '').splitlines():
        k, eq, v = line.strip().partition('=')
        if eq and k:
            out[k] = v.strip()
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _os_roles(f):
    out = {}
    if f.get('windows') == '1' or f.get('os', '').startswith(('MINGW', 'MSYS', 'CYGWIN')):
        out['windows-test-runner'] = 'detected: Windows' + (' (through WSL)' if f.get('wsl') else '')
        out['ui-desktop'] = 'detected: a Windows desktop'
    if f.get('os') == 'Linux':
        out['linux-test-runner'] = 'detected: Linux' + (' (WSL)' if f.get('wsl') else '')
    if f.get('os') == 'Darwin':
        out['macos-test-runner'] = 'detected: macOS'
        out['airplay-sender'] = 'detected: macOS (AirPlay)'
    return out


def _tool_roles(f):
    out = {}
    if f.get('docker') == '1':
        out['docker-host'] = 'detected: docker'
    if f.get('adb') == '1':
        out['android-test-runner'] = 'detected: adb'
    if f.get('emulator') == '1' and _num(f.get('avds')) > 0:
        out['emulator-host'] = f'detected: the Android emulator and {int(_num(f["avds"]))} AVD(s)'
    if f.get('xcrun') == '1' or f.get('webkit') == '1':
        out['ios-webkit-runner'] = DETECTED + ('Xcode' if f.get('xcrun') == '1' else 'Playwright WebKit')
    if f.get('wg') == '1' or f.get('tailscale') == '1':
        out['vpn-node'] = DETECTED + ('WireGuard' if f.get('wg') == '1' else 'Tailscale')
    if f.get('desktop') == '1':
        out['ui-desktop'] = out.get('ui-desktop') or 'detected: a desktop session'
    if f.get('sender') == 'chrome':
        out['cast-sender'] = 'detected: Chrome or Chromium'
    return out


def _hw_roles(f):
    out = {}
    vram = _num(f.get('vram_mb')) / 1024
    if vram >= 6:
        out['gpu-ai'] = f'detected: a GPU with {vram:.0f} GB' + (' and Ollama' if f.get('ollama') == '1' else '')
    elif f.get('ollama') == '1':
        out['gpu-ai'] = 'detected: Ollama (no GPU found: CPU models)'
    cores, mem = _num(f.get('cores')), _num(f.get('mem_gb'))
    if cores >= 8 and mem >= 16:
        out['build-machine'] = f'detected: {cores:.0f} cores, {mem:.0f} GB'
    return out


def detect_machine(text):
    """{role: why} from MACHINE_PROBE's answer."""
    f = parse(text)
    return {**_os_roles(f), **_tool_roles(f), **_hw_roles(f)}


def from_config(cfg, name, m):
    """{role: why} that the config says: the main machine, the backup main, the browser machine, the desktop sender."""
    out = {}
    if m.get('transport') == 'local':
        out['main'] = 'config: the machine Tutti runs on'
    if ((cfg.get('failover') or {}).get('backup')) == name:
        out['backup-main'] = 'config: failover.backup'
    browser = (cfg.get('browser') or {}).get('machine', '')
    if browser == name or (not browser and m.get('transport') == 'local'):
        out['chrome-browser'] = 'config: browser.machine'
    if m.get('desktop_sender'):
        out['cast-sender'] = 'config: desktop_sender'
    hosted = sorted(d for d, v in (cfg.get('devices') or {}).items() if (v or {}).get('machine') == name)
    if hosted:
        out['android-test-runner'] = 'config: hosts ' + ', '.join(hosted)
    if m.get('transport') == 'wsl':
        out.setdefault('windows-test-runner', 'config: a WSL machine')
    return out


def detect_device(d, readings):
    """{role: why} of an Android device from its readings (the `cast` flags: leanback, Cast service, mirroring, GMS)."""
    out = {'android-device': 'detected: an Android device'}
    cast = (readings or {}).get('cast')
    if cast and str(cast).split(',')[0] == '1':
        out['tv-device'] = 'detected: leanback (a TV)'
    for r in castroles.decide(cast) if cast else []:
        out[r] = DETECTED + ('a Cast receiver' if r == castroles.RECEIVER else 'Google Play services\' Cast')
    if (d or {}).get('kind') == 'tv':
        out.setdefault('tv-device', 'config: kind tv')
    return out


def effective(entry, detected):
    """[{role, why, source}] that count: `roles:` overrides everything; else detected plus `roles_add` minus `roles_off`."""
    entry = entry or {}
    if entry.get('roles') is not None:   # `roles: []` is an override too: no roles at all
        return [{'role': r, 'why': 'set by an admin (roles: overrides detection)', 'source': 'set'} for r in entry['roles']]
    off = set(entry.get('roles_off') or [])
    out = [{'role': r, 'why': w, 'source': 'detected' if w.startswith('detected') else 'config'}
           for r, w in sorted(detected.items()) if r not in off]
    have = {x['role'] for x in out}
    out += [{'role': r, 'why': 'set by an admin (roles_add)', 'source': 'set'} for r in entry.get('roles_add') or [] if r not in have]
    return out


def names(rows):
    return [x['role'] for x in rows]


def _read(path):
    try:
        with open(path, encoding='utf8') as f:
            seen = json.load(f)
        return seen if isinstance(seen, dict) else {}
    except (OSError, ValueError):
        return {}


def _probe(m, rec, now, sh, refresh):
    """A machine's record: probed again when it is a day old (or `refresh`); one that does not answer keeps its last answer."""
    from . import remote
    every = EVERY if rec.get('detected') else RETRY   # never answered yet: asked again soon, not in a day
    if m.get('transport') == 'android' or not (refresh or 'at' not in rec or now - rec['at'] > every):
        return rec, False
    try:
        r = (sh or remote.sh)(m, MACHINE_PROBE, capture=True, timeout=30)
        if getattr(r, 'returncode', 0) == 255:   # ssh could not reach it: keep the last answer
            raise OSError('ssh failed')
        return {'at': now, 'detected': detect_machine(r.stdout)}, True
    except (OSError, ValueError, subprocess.SubprocessError):
        return {**rec, 'at': now}, True


def machine_roles(cfg, now, sh=None, folder=None, refresh=False):
    """{machine: [{role, why, source}]}; each machine probed at most once a day (or `refresh`), the answers kept in
    <state>/roles-machines.json."""
    from . import config
    path = os.path.join(folder or config.state_dir(cfg), FILE)
    seen, changed, out = _read(path), False, {}
    for name, m in (cfg.get('machines') or {}).items():
        rec, new = _probe(m, seen.get(name) or {}, now, sh, refresh)
        if new:
            seen[name], changed = rec, True
        out[name] = effective(m, {**(rec.get('detected') or {}), **from_config(cfg, name, m)})
    if changed:
        try:
            with open(path, 'w', encoding='utf8') as f:
                json.dump(seen, f)
        except OSError:
            pass
    return out


def cached(cfg, folder=None):
    """machine_roles without probing (the dashboard's request path): the last answers plus the config."""
    from . import config
    seen = _read(os.path.join(folder or config.state_dir(cfg), FILE))
    return {n: effective(m, {**((seen.get(n) or {}).get('detected') or {}), **from_config(cfg, n, m)})
            for n, m in (cfg.get('machines') or {}).items()}


def missing(needed, have):
    """The roles a suite needs that the target lacks."""
    return [r for r in needed or [] if r not in (have or [])]


def unknown(values):
    return [r for r in values if r not in ROLES]


def edit(path, kind, name, role, on):
    """`tutti roles set|unset`: adds the role to roles_add (and takes it from roles_off), or the reverse, in the config."""
    from . import config, yamledit
    with open(path, encoding='utf8') as f:
        text = f.read()
    entry = (((config.parse(text, path) or {}).get(kind) or {}).get(name)) or {}
    if entry.get('roles') is not None:
        raise ValueError(f'{kind}.{name}.roles overrides detection: edit that list instead (Settings, or the config)')
    add, off = list(entry.get('roles_add') or []), list(entry.get('roles_off') or [])
    keep, drop = (add, off) if on else (off, add)
    keep = keep if role in keep else [*keep, role]
    drop = [r for r in drop if r != role]
    add, off = (keep, drop) if on else (drop, keep)
    for key, value in (('roles_add', add), ('roles_off', off)):
        text = yamledit.edit(text, [kind, name, key], value if value else yamledit.DELETE)
    config.write_text(path, text)
    return add, off
