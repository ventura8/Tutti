"""Phone calls and the owner picking a device up (docs/DISPATCH.md, *Phone calls*): an Android device that rings or is in a
call takes no new work, a run on it is paused (the suite's `pause`/`resume` hooks) or stopped cleanly and queued again
(never counted as a failure), its app focus is released (the Home key) so the call screen works, and the device takes
work again `dispatch.resume_after_call_seconds` (30) after the call ends. Opt-in (`dispatch.pause_on_owner_use`): a
screen a person turned on while no run drives the device holds it the same way.

The call state is `dumpsys telephony.registry`'s mCallState on every SIM slot (devices.call_state). The holds are pure;
the adb look and the Home key are the thin edge at the end."""
import shlex
import subprocess

from . import devices

CALL = 'Paused: phone call'
OWNER = 'Paused: someone is using it (its screen was turned on while no run drove it)'


def hold_for(cfg, st, d, now, leased):
    """Why the device is held for a call or its owner, or ''; keeps st['calls'][name] = {active, ended}."""
    s = cfg.get('dispatch') or {}
    delay = float(s.get('resume_after_call_seconds', 30))
    calls = st.setdefault('calls', {})
    name = d['name']
    conf = (cfg.get('devices') or {}).get(name) or {}
    checks_calls = conf.get('calls', True) is not False and conf.get('kind') != 'tv'   # a TV has no call state
    if checks_calls and devices.on_call(d):
        calls[name] = {'active': True, 'since': (calls.get(name) or {}).get('since') or now}
        return f'{CALL} ({devices.CALL_WORDS[d["call"]]})'
    c = calls.get(name) or {}
    if c.get('active'):
        c = calls[name] = {'active': False, 'ended': now}
    left = delay - (now - c['ended']) if c.get('ended') is not None else 0
    if left > 0:
        return f'{CALL}: the call ended; work starts again in {int(left)} s (dispatch.resume_after_call_seconds)'
    calls.pop(name, None)
    if s.get('pause_on_owner_use') and not leased and d.get('screen') == 1:
        return OWNER
    return ''


def holds(cfg, st, devices_rows, now):
    """{device name: why it is held} for every device on a call, just after one, or (opt-in) in its owner's hands."""
    leased = {lease['name'] for lease in st['leases'].values() if lease.get('kind') in ('android', 'tv')}
    out = {}
    for d in devices_rows or []:
        why = hold_for(cfg, st, d, now, d['name'] in leased)
        if why:
            out[d['name']] = why
    return out


# ---- the edge: one cheap adb look, and the Home key


def adb_line(device, args):
    adb = shlex.quote(device.get('adb') or 'adb')
    return f'timeout 10 {adb} -s {devices.serial_expr(device)} shell {args}'


def look(cfg, device):
    """The device's call state now (0, 1, 2), or None when it could not be read."""
    from . import remote
    try:
        r = remote.sh(cfg['machines'][device['machine']], adb_line(device, 'dumpsys telephony.registry'), capture=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return devices.call_state(r.stdout) if r.returncode == 0 else None


def release_focus(cfg, device):
    """The Home key: the app under test leaves the front, so the call screen can take it."""
    from . import remote
    try:
        remote.sh(cfg['machines'][device['machine']], adb_line(device, 'input keyevent KEYCODE_HOME'), capture=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass


def hook(cfg, device, command):
    """A suite's `pause` or `resume` command, run on the device's machine with ANDROID_SERIAL set; True when it worked."""
    from . import remote
    line = f'ANDROID_SERIAL={shlex.quote(str(device["serial"]))} {command}'
    try:
        return remote.sh(cfg['machines'][device['machine']], line, capture=True, timeout=120).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
