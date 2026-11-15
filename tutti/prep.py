"""Device prep and restore (docs/DISPATCH.md, *Prep and restore*): `devices.<d>.prep` lists settings a run needs (screen
always on, volume low, brightness 30 %). Once the device's lock is taken, each one's current value is read and saved to a
file on the device's machine, before anything changes; then the new values are set. When the run ends, however it ends,
exactly the saved values are put back and the file is removed. A file left behind (the machine rebooted, the device was
gone) is found by the readings (`prep_left`) and restored by the next dispatch round, once nothing holds the device.

An item is `{setting: '<namespace> <key>', value: V}` (Android's `settings get|put|delete`), or
`{name, read, set, value, parse, cap}` with adb shell commands: `read` prints the current value (`parse`: a regex whose
group 1 is it), `set` has {value}; `cap: true` only lowers (a volume already at or below V stays). A read that gives
nothing (a binary the device lacks) skips the item cleanly: never set what cannot be put back. Pure shell generation."""
import re
import shlex

NULL = 'null'   # what `settings get` prints for a key that is not set: restored by `settings delete`

# The owner's defaults while a run uses a device (`prep_defaults: false` turns them off; a `prep:` item with the same name
# replaces one): screen at about 30 %, media volume at about 10 % (2 of 15 to 25 steps, only lowered; Cast plays on the
# media stream, so it is the cast volume too), and a TV's
# screensaver off and its screen on for 30 min. Every value read first is put back exactly at release.
VOLUME = {'name': 'media volume', 'read': 'cmd media_session volume --stream 3 --get', 'parse': r'volume is (\d+)',
          'set': 'cmd media_session volume --stream 3 --set {value}', 'value': 2, 'cap': True}
DEFAULTS = {
    'phone': [{'setting': 'system screen_brightness_mode', 'value': 0}, {'setting': 'system screen_brightness', 'value': 77}, VOLUME],
    'tv': [{'setting': 'secure screensaver_enabled', 'value': 0}, {'setting': 'system screen_off_timeout', 'value': 1800000}, VOLUME],
}
DEFAULTS['tablet'] = DEFAULTS['phone']
KEEP_AWAKE_SECONDS = 15   # the phone's admin policy caps the screen timeout at 5 min: a harmless key every 15 s keeps it on


def wanted(device):
    """The device's prep list: the defaults of its kind, then its own `prep:` (an item with a default's name replaces it)."""
    own = list(device.get('prep') or [])
    if device.get('prep_defaults') is False:
        return own
    names = {x.get('name') or str(x.get('setting', '')).split(None, 1)[-1] for x in own}
    base = [x for x in DEFAULTS.get(device.get('kind') or 'phone', []) if (x.get('name') or x['setting'].split()[-1]) not in names]
    return base + own


def items(device):
    out = []
    for i, x in enumerate(wanted(device)):
        if 'setting' in x:
            ns, key = str(x['setting']).split(None, 1)
            out.append({'name': x.get('name') or key, 'read': f'settings get {ns} {key}', 'set': f'settings put {ns} {key} {{value}}',
                        'delete': f'settings delete {ns} {key}', 'value': x['value'], 'parse': '', 'cap': bool(x.get('cap'))})
        else:
            out.append({'name': x.get('name') or f'item{i}', 'read': x['read'], 'set': x['set'], 'delete': '', 'value': x['value'],
                        'parse': x.get('parse') or '', 'cap': bool(x.get('cap'))})
    return out


def errors(where, prep):
    if prep is None:
        return []
    if not isinstance(prep, list):
        return [f'{where}.prep is {prep!r}: a list of {{setting: "<namespace> <key>", value: V}} or {{read, set, value}}']
    errs = []
    for i, x in enumerate(prep):
        key = f'{where}.prep[{i}]'
        if not isinstance(x, dict) or 'value' not in x:
            errs.append(f'{key} is {x!r}: give it a value and either setting: "<namespace> <key>" or read: and set: commands')
        elif 'setting' in x:
            if not re.fullmatch(r'(global|secure|system) [\w.]+', str(x['setting'])):
                errs.append(f'{key}.setting is {x["setting"]!r}: "<global|secure|system> <key>", e.g. "secure screensaver_enabled"')
        elif not (isinstance(x.get('read'), str) and isinstance(x.get('set'), str) and '{value}' in x['set']):
            errs.append(f'{key}: needs read: (prints the current value) and set: (with {{value}}) adb shell commands')
        elif x.get('parse'):
            try:
                re.compile(x['parse'])
            except re.error:
                errs.append(f'{key}.parse is {x["parse"]!r}: a regular expression whose group 1 is the value')
    return errs


def saved_path(device):
    from . import remote
    return remote.home_path(f'~/.tutti/prep/{device["name"]}.saved')


def _adb(device):
    return f'timeout 20 {shlex.quote(device.get("adb") or "adb")} -s {shlex.quote(str(device["serial"]))} shell'


def _read(device, it):
    line = f'{_adb(device)} {shlex.quote(it["read"])} 2>/dev/null | tr -d "\\r" | tail -n 1'
    if it['parse']:
        # sed's ERE has no \d or \s: the usual Python shorthands are translated
        pattern = it['parse'].replace('\\d', '[0-9]').replace('\\s', '[[:space:]]').replace('/', '\\/')
        line += ' | sed -nE ' + shlex.quote('s/.*' + pattern + '.*/\\1/p')
    return line


def prep_script(device):
    """Reads every item, saves what was read (before any change), then sets each one that was read."""
    its = items(device)
    if not its:
        return ''
    f = saved_path(device)
    # a saved file already there holds the device's own values (another run still uses it, or a crash): never read again,
    # or the prepared values would become the "originals"
    reads = [f'mkdir -p "$(dirname {f})"', f': > {f}.tmp']
    for i, it in enumerate(its):
        reads.append(f'v=$({_read(device, it)})')
        bad = 'grep -qiE "not found|inaccessible|unknown|error|exception|usage:"'
        reads.append(f'if [ -n "$v" ] && ! printf "%s" "$v" | {bad}; then printf "%s\\t%s\\n" {i} "$v" >> {f}.tmp; '
                     f'else echo "tutti: prep {it["name"]} skipped: the device does not answer {shlex.quote(it["read"])}"; fi')
    reads.append(f'mv {f}.tmp {f}')
    lines = [f'if [ ! -e {f} ]; then {"; ".join(reads)}; fi']
    for i, it in enumerate(its):
        value = shlex.quote(str(it['value']))
        set_line = f'{_adb(device)} {shlex.quote(it["set"].replace("{value}", str(it["value"])))} > /dev/null 2>&1'
        cond = f'v=$(awk -F"\\t" \'$1 == {i} {{print $2}}\' {f}); [ -n "$v" ]'
        if it['cap']:
            cond += f' && ! {{ [ "$v" -le {value} ] 2>/dev/null; }}'
        lines.append(f'if {cond}; then {set_line} && echo "tutti: prep {it["name"]}: $v -> {it["value"]}"; fi')
    return '; '.join(lines)


def restore_script(device):
    """Puts back exactly the values saved by prep_script and removes the file; nothing when no file is there."""
    its = items(device)
    if not its:
        return ''
    f = saved_path(device)
    cases = []
    for i, it in enumerate(its):
        put = f'{_adb(device)} "$(printf %s {shlex.quote(it["set"])} | sed "s|{{value}}|$v|")" > /dev/null 2>&1'
        if it['delete']:
            put = f'if [ "$v" = {NULL} ]; then {_adb(device)} {shlex.quote(it["delete"])} > /dev/null 2>&1; else {put}; fi'
        cases.append(f'{i}) {put} && echo "tutti: restored {it["name"]} to $v";;')
    return (f'if [ -e {f} ]; then while IFS="$(printf "\\t")" read -r i v; do case "$i" in {" ".join(cases)} esac; done < {f}; '
            f'rm -f {f}; fi')


def users_path(device):
    """The folder of the device's current users on its machine: `run-<pid>` per Tutti run, `claim-<who>` per session claim
    (`tutti devices claim`). The device is released only when it is empty and no other tool's lock file is fresh."""
    from . import remote
    return remote.home_path(f'~/.tutti/devices/{device["name"]}/users')


def release_script(device, ext='', stale=1800):
    """Releases the device if nobody else uses it: no Tutti run or claim left in users_path, no fresh lock file of another
    tool (`ext`). Then its own values come back (restore_script: phones sleep again, a TV gets its screensaver and timers
    back) and `tutti: device released <name>` is printed; else `tutti: <name> stays in use by ...`."""
    u = users_path(device)
    name = shlex.quote(device['name'])
    fresh = (f'[ -e {shlex.quote(ext)} ] && [ $(( $(date +%s) - $(stat -c %Y {shlex.quote(ext)}) )) -lt {int(stale)} ] && '
             f'by="$by $(head -c 120 {shlex.quote(ext)})"; ') if ext else ''
    # a run-<pid> whose process is gone (killed, OOM, a reboot) is no user: removed before deciding; claims stay until released
    gone = (f'for r in {u}/run-*; do [ -e "$r" ] || continue; kill -0 "${{r##*/run-}}" 2>/dev/null || rm -f "$r"; done; '
            # a lease (claim) past its end time is over: a requester that crashed never holds the device for ever
            f'for c in {u}/claim-*; do [ -e "$c" ] || continue; e=$(head -c 20 "$c" | tr -cd 0-9); '
            f'[ -n "$e" ] && [ "$e" -lt "$(date +%s)" ] && rm -f "$c"; done; ')
    return (f'{gone}by=$(ls -A {u} 2>/dev/null | tr "\\n" " "); {fresh}'
            f'if [ -z "${{by// /}}" ]; then {restore_script(device) or ":"}; echo "tutti: device released {device["name"]}"; '
            f'else echo "tutti: "{name}" stays in use by $by"; fi')


def lease_script(device, claim, minutes, who):
    """A lease for a requester outside Tutti's runs (`tutti devices claim`): refused while someone else uses the device;
    else the claim file (its end time inside), the prep (the device's own values saved once), the screen woken and kept
    awake for as long as the claim lasts, and the address the requester's adb connects to (`ADDR=<ip:port>`)."""
    from . import adbpair
    u = users_path(device)
    guid = adbpair.guid_of(str(device['serial']))
    addr = (f'a=$({shlex.quote(device.get("adb") or "adb")} mdns services 2>/dev/null | tr -d "\\r" | '
            f'awk -v g={shlex.quote(guid)} \'$1 == g && $2 ~ /_adb-tls-connect/ {{print $3; exit}}\'); '
            'echo "ADDR=${a:-unknown}"; ') if guid else f'echo "ADDR={device["serial"]}"; '
    loop = (f'while [ -e {claim} ] && [ $(cat {claim}) -gt $(date +%s) ]; do '
            f'{_adb(device)} input keyevent KEYCODE_UNKNOWN > /dev/null 2>&1; sleep {KEEP_AWAKE_SECONDS}; done')
    awake = f'nohup bash -c {shlex.quote(loop)} > /dev/null 2>&1 &'
    return (f'{release_script(device)} > /dev/null; '   # left-behind runs and lapsed leases are cleared first
            f'mkdir -p {u}; others=$(ls -A {u} | grep -vx "$(basename {claim})" | tr "\\n" " "); '
            f'if [ -n "$others" ]; then echo "tutti: {device["name"]} is in use by $others: try again when it is free"; exit 3; fi; '
            f'echo $(( $(date +%s) + {int(minutes) * 60} )) > {claim}; {prep_script(device) or ":"}; {unlock(device)}; {awake} '
            f'{addr}echo "SERIAL={device["serial"]}"; echo "tutti: {device["name"]} leased to {who} for {int(minutes)} min"')


def keep_awake(device):
    """The keep-awake loop of a run (backgrounded by the caller): a harmless key every 15 s while the run lasts."""
    return (f'while :; do {_adb(device)} input keyevent KEYCODE_UNKNOWN > /dev/null 2>&1; sleep {KEEP_AWAKE_SECONDS}; done')


def unlock(device):
    """Wakes the screen and dismisses a swipe lock (a PIN lock needs the user once; a test device has none)."""
    return f'{_adb(device)} input keyevent KEYCODE_WAKEUP > /dev/null 2>&1; {_adb(device)} wm dismiss-keyguard > /dev/null 2>&1'


def scripts(found):
    """(prep, restore) shell lines for these devices; ('', '') when none has a prep list."""
    have = [d for d in found if items(d)]
    return '; '.join(prep_script(d) for d in have), '; '.join(restore_script(d) for d in have)
