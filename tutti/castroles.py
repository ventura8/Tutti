"""Cast roles, detected (docs/DISPATCH.md, *Paired devices*): whether a device or machine can be a Cast sender, a receiver
or both, so a Cast suite's sender and receiver are picked from what is here. An admin's `roles:` in the config overrides.

Android (read by scripts/device-readings.sh once a day and when a device is paired):
- receiver: a TV (the leanback feature), the Cast receiver service (`com.google.android.chromecast.chromecastservice`), or
  Google Play services' Cast mirroring receiver (a Pixel Tablet's hub mode, a phone as a receiver);
- sender: Google Play services' Cast framework on a device with a screen to send from (not a TV).
A machine (Windows, Linux, macOS) is a sender when it has Chrome or Chromium (their Cast) or the project's desktop sender.
Pure: decide, machine_sender_script, roles_of."""

RECEIVER, SENDER = 'cast-receiver', 'cast-sender'

# the readings' line: `cast=<leanback>,<castservice>,<mirroring receiver>,<gms>` (1 or 0 each)
ANDROID_PROBE = ('l=0; pm list features 2>/dev/null | grep -q android.software.leanback && l=1; '
                 'c=0; pm path com.google.android.chromecast.chromecastservice > /dev/null 2>&1 && c=1; '
                 'm=0; cmd package query-activities -a com.google.android.gms.cast.mirroring.receiver.ACTION_START_MIRRORING '
                 '2>/dev/null | grep -q MirroringActivity && m=1; '
                 'g=0; pm path com.google.android.gms > /dev/null 2>&1 && g=1; echo "cast=$l,$c,$m,$g"')

MACHINE_PROBE = ('for c in google-chrome google-chrome-stable chromium chromium-browser; do command -v "$c" > /dev/null && '
                 '{ echo sender=chrome; exit 0; }; done; '
                 'for p in "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe" '
                 '"/c/Program Files/Google/Chrome/Application/chrome.exe" "/Applications/Google Chrome.app" '
                 '"/Applications/Chromium.app"; do [ -e "$p" ] && { echo sender=chrome; exit 0; }; done; echo sender=none')


def decide(flags):
    """The roles from the readings' `cast` value ('1,0,1,1'); [] when it is unknown."""
    try:
        leanback, service, mirroring, gms = (str(flags).split(',') + ['0'] * 4)[:4]
    except (AttributeError, ValueError):
        return []
    roles = []
    if '1' in (leanback, service, mirroring):
        roles.append(RECEIVER)
    if gms == '1' and leanback != '1':
        roles.append(SENDER)
    return roles


def roles_of(entry, detected):
    """The roles that count: the admin's `roles:` when set (an override), else the detected ones."""
    own = (entry or {}).get('roles')
    return list(own) if own else list(detected or [])


def machine_sender(text, desktop_sender=False):
    """A machine's roles from MACHINE_PROBE's answer: a sender with Chrome or Chromium, or the project's desktop sender."""
    return [SENDER] if 'sender=chrome' in (text or '') or desktop_sender else []


FILE = 'cast-machines.json'
EVERY = 86400


def machine_roles(cfg, now, sh=None, read=None, write=None):
    """{machine: roles}, each machine asked at most once a day (MACHINE_PROBE over its transport); an admin's
    `machines.<m>.roles` overrides. The answers are kept in <state>/cast-machines.json."""
    import json
    import os
    import subprocess

    from . import config, remote
    path = os.path.join(config.state_dir(cfg), FILE) if not (read and write) else ''
    try:
        if read:
            seen = read()
        else:
            with open(path, encoding='utf8') as f:
                seen = json.load(f)
    except (OSError, ValueError):
        seen = {}
    out, changed = {}, False
    for name, m in (cfg.get('machines') or {}).items():
        if m.get('transport') == 'android':
            continue
        rec = seen.get(name) or {}
        if 'at' not in rec or now - rec['at'] > EVERY:
            try:
                r = (sh or remote.sh)(m, MACHINE_PROBE, capture=True, timeout=30)
                rec = {'at': now, 'roles': machine_sender(r.stdout, bool(m.get('desktop_sender')))}
            except (OSError, ValueError, subprocess.SubprocessError):   # one that does not answer keeps its last answer
                rec = {**rec, 'at': now}
            seen[name], changed = rec, True
        out[name] = roles_of(m, rec.get('roles'))
    if changed:
        try:
            if write:
                write(seen)
            else:
                with open(path, 'w', encoding='utf8') as f:
                    json.dump(seen, f)
        except OSError:
            pass
    return out
