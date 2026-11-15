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
import time

NULL = 'null'   # what `settings get` prints for a key that is not set: restored by `settings delete`

# The owner's defaults while a run uses a device (`prep_defaults: false` turns them off; a `prep:` item with the same name
# replaces one): screen at about 30 %, media volume at about 10 % (2 of 15 to 25 steps, only lowered; Cast plays on the
# media stream, so it is the cast volume too), and a TV's
# screensaver off and its screen on for 30 min. Every value read first is put back exactly at release.
VOLUME = {'name': 'media volume', 'read': 'cmd media_session volume --stream 3 --get', 'parse': r'volume is (\d+)',
          'set': 'cmd media_session volume --stream 3 --set {value}', 'value': 2, 'cap': True}
DEFAULTS = {
    'phone': [{'setting': 'system screen_brightness_mode', 'value': 0}, {'setting': 'system screen_brightness', 'value': 77}, VOLUME],
    'tv': [{'setting': 'secure screensaver_enabled', 'value': 0}, {'setting': 'secure screensaver_activate_on_sleep', 'value': 0},
           {'setting': 'system screen_off_timeout', 'value': 1800000}, VOLUME],
}
DEFAULTS['tablet'] = DEFAULTS['phone']
# A TV must never start a run with its screensaver able to come on (its photos covered the app, RoRadio 2026-11-15): these
# settings are required, and a prep that cannot read one fails loudly instead of running
REQUIRED = ('screensaver_enabled', 'screensaver_activate_on_sleep', 'screen_off_timeout', 'sleep_timeout',
            'stay_on_while_plugged_in')
KEEP_AWAKE_SECONDS = 15   # the phone's admin policy caps the screen timeout at 5 min: a harmless key every 15 s keeps it on


# Night mode (devices.night, on by default 22-08): while Tutti holds or works a device at night it runs at 1 % brightness
# with every volume stream at 0 (music, ring, notification, system; the alarm stays the person's own); Cast plays on the
# music stream. It replaces the brightness and volume items of the day.
NIGHT_HOURS = '22-08'
STREAMS = {1: 'system volume', 2: 'ring volume', 3: 'media volume', 5: 'notification volume'}


def _stream(n, name, value, cap=False):
    return {'name': name, 'read': f'cmd media_session volume --stream {n} --get', 'parse': r'volume is (\d+)',
            'set': f'cmd media_session volume --stream {n} --set {{value}}', 'value': value, 'cap': cap}


NIGHT = [{'setting': 'system screen_brightness_mode', 'value': 0}, {'setting': 'system screen_brightness', 'value': 1},
         *(_stream(n, name, 0) for n, name in STREAMS.items())]


def night_now(device, now=None):
    """True while the device's night mode applies now (devices.<d>._night, filled from the config's `night:`)."""
    from . import hours
    n = device.get('_night') or {'on': True, 'hours': NIGHT_HOURS}
    if not n.get('on'):
        return False
    try:
        return hours.window(n.get('hours') or NIGHT_HOURS, None, time.time() if now is None else now) is not None
    except ValueError:
        return False


def _touches(x, words):
    text = ' '.join(str(x.get(k, '')) for k in ('setting', 'set', 'name'))
    return any(w in text for w in words)


def wanted(device, night=None):
    """The device's prep list: the defaults of its kind, then its own `prep:` (an item with a default's name replaces it);
    at night (night_now) the brightness and volume items give way to NIGHT."""
    own = list(device.get('prep') or [])
    if device.get('prep_defaults') is False:
        base = []
    else:
        names = {x.get('name') or str(x.get('setting', '')).split(None, 1)[-1] for x in own}
        base = [x for x in DEFAULTS.get(device.get('kind') or 'phone', []) if (x.get('name') or x['setting'].split()[-1]) not in names]
    out = base + own
    if night_now(device) if night is None else night:
        out = [x for x in out if not _touches(x, ('screen_brightness', '--stream', 'volume'))] + NIGHT
    return out


def items(device, night=None):
    out = []
    for i, x in enumerate(wanted(device, night)):
        if 'setting' in x:
            ns, key = str(x['setting']).split(None, 1)
            out.append({'name': x.get('name') or key, 'key': _key(x.get('name') or key), 'read': f'settings get {ns} {key}',
                        'set': f'settings put {ns} {key} {{value}}',
                        'delete': f'settings delete {ns} {key}', 'value': x['value'], 'parse': '', 'cap': bool(x.get('cap'))})
        else:
            out.append({'name': x.get('name') or f'item{i}', 'key': _key(x.get('name') or f'item{i}'), 'read': x['read'],
                        'set': x['set'], 'delete': '', 'value': x['value'],
                        'parse': x.get('parse') or '', 'cap': bool(x.get('cap'))})
    return out


def _key(name):
    """The saved file's key of an item: its name in one word (the day and the night lists differ, so never an index)."""
    return re.sub(r'\W', '_', str(name))


def all_items(device):
    """Every item a prep may have saved, day and night: what a release may have to put back."""
    seen, out = set(), []
    for it in items(device, False) + items(device, True):
        if it['key'] not in seen:
            seen.add(it['key'])
            out.append(it)
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


def _reconnect(device):
    """adb connect by the device's mDNS name (a wireless device whose connection dropped), then a short pause."""
    from . import adbpair
    if not adbpair.guid_of(str(device['serial'])):
        return ':'
    return f'timeout 20 {shlex.quote(device.get("adb") or "adb")} connect {shlex.quote(str(device["serial"]))} > /dev/null 2>&1; sleep 3'


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
    for it in its:
        # slow or dropped adb: reconnect and read again with a longer timeout before giving up
        reads.append(f'v=$({_read(device, it)}); [ -n "$v" ] || {{ {_reconnect(device)}; '
                     f'v=$({_read(device, it).replace("timeout 20 ", "timeout 45 ", 1)}); }}')
        bad = 'grep -qiE "not found|inaccessible|unknown|error|exception|usage:"'
        reads.append(f'if [ -n "$v" ] && ! printf "%s" "$v" | {bad}; then printf "%s\\t%s\\n" {it["key"]} "$v" >> {f}.tmp; '
                     + (f'else echo "tutti: prep failed: {device["name"]} does not answer {shlex.quote(it["read"])} even after a '
                        'reconnect; the run does not start (a TV could fall into its screensaver): check its Wi-Fi and Wireless '
                        f'debugging, then start again"; rm -f {f}.tmp; exit 4; fi'
                        if device.get('kind') == 'tv' and it['key'] in REQUIRED else
                        f'else echo "tutti: prep {it["name"]} skipped: the device does not answer {shlex.quote(it["read"])}"; fi'))
    reads.append(f'mv {f}.tmp {f}')
    lines = [f'if [ ! -e {f} ]; then {"; ".join(reads)}; fi']
    for it in its:
        value = shlex.quote(str(it['value']))
        set_line = f'{_adb(device)} {shlex.quote(it["set"].replace("{value}", str(it["value"])))} > /dev/null 2>&1'
        cond = f'v=$(awk -F"\\t" \'$1 == "{it["key"]}" {{print $2}}\' {f}); [ -n "$v" ]'
        if it['cap']:
            cond += f' && ! {{ [ "$v" -le {value} ] 2>/dev/null; }}'
        lines.append(f'if {cond}; then {set_line} && echo "tutti: prep {it["name"]}: $v -> {it["value"]}"; fi')
        if device.get('kind') == 'tv' and it['key'] in REQUIRED:   # read back: a setting that did not take stops the run
            lines.append(f'c=$({_read(device, it).replace("timeout 20 ", "timeout 45 ", 1)}); if [ "$c" != {value} ]; then '
                         f'echo "tutti: prep failed: {device["name"]} keeps {it["name"]} at ${{c:-?}} (not {it["value"]}); the run '
                         'does not start, so its screensaver cannot cover the app: check the device, then start again"; exit 4; fi')
    return '; '.join(lines)


def restore_script(device):
    """Puts back exactly the values saved by prep_script and removes the file; nothing when no file is there."""
    its = all_items(device)
    if not its:
        return ''
    f = saved_path(device)
    values = {}
    for it in items(device, False) + items(device, True):   # Tutti's own values, day and night
        values.setdefault(it['key'], set()).add(str(it['value']))
    legacy = {it['key']: str(i) for i, it in enumerate(items(device, False))}   # a file written by 0.9.7 or older
    cases = []
    for it in its:
        put = f'{_adb(device)} "$(printf %s {shlex.quote(it["set"])} | sed "s|{{value}}|$v|")" > /dev/null 2>&1'
        if it['delete']:
            put = f'if [ "$v" = {NULL} ]; then {_adb(device)} {shlex.quote(it["delete"])} > /dev/null 2>&1; else {put}; fi'
        # a person's own change while the device was in use (an alarm, the brightness) is never undone: an item goes back
        # only while it still has the value Tutti set
        same = ' || '.join(f'[ "$c" = {shlex.quote(v)} ]' for v in sorted(values[it['key']]))
        mine = f'c=$({{ {_read(device, it)}; }} < /dev/null); {{ {same}; }}'
        label = it['key'] + (f'|{legacy[it["key"]]}' if it['key'] in legacy else '')
        cases.append(f'{label}) if {mine}; then {put} < /dev/null && echo "tutti: restored {it["name"]} to $v"; '
                     f'else echo "tutti: kept {it["name"]} at $c: changed by a person"; fi;;')
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
    awake for as long as a claim lasts, and the address the requester's adb connects to (`ADDR=<ip:port>`). `claim` is a
    shell word (remote.home_path); every other value is quoted here. The check and the claim write hold a mkdir lock, so
    two requesters cannot both win; one keep-awake loop per device (its pid in `awake.pid` beside the users folder), and
    when the last claim runs out the loop releases the device itself (restore_script), without waiting for a release."""
    from . import adbpair, procs
    u = users_path(device)
    q = shlex.quote
    name = q(str(device['name']))
    guid = adbpair.guid_of(str(device['serial']))
    addr = (f'a=$({q(device.get("adb") or "adb")} mdns services 2>/dev/null | tr -d "\\r" | '
            f'awk -v g={q(guid)} \'$1 == g && $2 ~ /_adb-tls-connect/ {{print $3; exit}}\'); '
            'echo "ADDR=${a:-unknown}"; ') if guid else f'echo ADDR={q(str(device["serial"]))}; '
    pid, lock = f'{u}/../awake.pid', f'{u}/../lease.lock'
    live = (f'live=; for c in {u}/claim-*; do [ -e "$c" ] || continue; e=$(head -c 20 "$c" | tr -cd 0-9); '
            f'[ -n "$e" ] && [ "$e" -gt "$(date +%s)" ] && live=1; done; ')
    # the end is decided under the lease's lock: a lease written meanwhile is seen, and its device is never restored under it
    # a folder gone (a test's temp home removed) ends the loop: its mkdir could never succeed (161 spinning loops, 2026-11-15)
    end = (f'if [ -z "$live" ]; then until mkdir {lock} 2>/dev/null; do [ -d {u} ] || break 2; sleep 0.1; done; {live}'
           f'if [ -z "$live" ]; then rm -f {pid}; {release_script(device)} > /dev/null; rmdir {lock}; break; fi; rmdir {lock}; fi; ')
    # one loop per device: a loop that awake.pid does not name (a second one) ends at its next round
    mine = f'[ "$(cat {pid} 2>/dev/null)" = "$$" ] || break; '
    loop = (f'sleep 1; while :; do {mine}{live}{end}'
            f'{person_recent(device)} || {_adb(device)} input keyevent KEYCODE_UNKNOWN > /dev/null 2>&1; sleep {KEEP_AWAKE_SECONDS}; done')
    # a person's touch noticed while the lease's loop lives (it ends with the loop's pid file): Tutti then sends no key
    watch = (f'while [ -e {pid} ]; do if {_adb(device).replace("timeout 20 ", "timeout 30 ")} getevent -qc 1 2>/dev/null | grep -q .; '
             f'then mkdir -p "$(dirname {person_path(device)})"; date +%s > {person_path(device)}; sleep 1; fi; done')
    tag = q(f'{procs.MARK}=claim:{device["name"]}')   # the reaper's marker (tutti/procs.py)
    awake = (f'if ! kill -0 "$(cat {pid} 2>/dev/null)" 2>/dev/null; then nohup env {tag} bash -c {q(loop)} > /dev/null 2>&1 & '
             f'echo $! > {pid}; nohup env {tag} bash -c {q(watch)} > /dev/null 2>&1 & fi; ') if procs.daemons_allowed() else ''
    take = (f'find {lock} -maxdepth 0 -mmin +1 -exec rmdir {{}} \\; 2>/dev/null; n=0; '   # a lock left by a killed claim
            f'until mkdir {lock} 2>/dev/null; do n=$((n + 1)); if [ $n -gt 100 ]; then '
            f'echo "tutti: "{name}" is being claimed by someone else right now: try again in a minute"; exit 3; fi; sleep 0.1; done; ')
    return (f'{release_script(device)} > /dev/null; '   # left-behind runs and lapsed leases are cleared first
            f'mkdir -p {u}; {take}others=$(ls -A {u} | grep -vx "$(basename {claim})" | tr "\\n" " "); '
            f'if [ -n "$others" ]; then rmdir {lock}; echo "tutti: "{name}" is in use by $others: try again when it is free"; exit 3; fi; '
            f'echo $(( $(date +%s) + {int(minutes) * 60} )) > {claim}; rmdir {lock}; '
            f'{prep_script(device) or ":"}; {unlock(device)}; {awake}'
            f'{addr}echo SERIAL={q(str(device["serial"]))}; echo "tutti: "{name}" leased to "{q(str(who))}" for {int(minutes)} min"')


IDLE_MINUTES = 3   # a person's use pauses Tutti on the device until it has been idle this long (devices.<d>.idle_minutes)


def person_path(device):
    """The file whose time is a person's last touch or key on the device (written by person_watch), on its machine."""
    from . import remote
    return remote.home_path(f'~/.tutti/devices/{device["name"]}/person')


def idle_seconds(device):
    return int(float(device.get('idle_minutes', IDLE_MINUTES)) * 60)


def person_watch(device):
    """A background loop that notices a person: `getevent` sees only physical touches and keys (the test's own taps and
    Tutti's keep-awake key are injected and never reach it), and each one writes its time to person_path."""
    p = person_path(device)
    one = f'{_adb(device).replace("timeout 20 ", "timeout 30 ")} getevent -qc 1 2>/dev/null | grep -q .'
    loop = f'while :; do if {one}; then date +%s > {p}; sleep 1; fi; done'
    return f'mkdir -p "$(dirname {p})"; {{ {loop} & }}; '


def person_recent(device):
    """A shell test: true while a person touched the device within its idle time."""
    p = person_path(device)
    return f'{{ [ -e {p} ] && [ $(( $(date +%s) - $(cat {p} 2>/dev/null || echo 0) )) -lt {idle_seconds(device)} ]; }}'


def keep_awake(device):
    """The keep-awake loop of a run (backgrounded by the caller): a harmless key every 15 s while the run lasts."""
    # no key while a person uses it: Tutti stops sending input and leaves the device alone
    return (f'while :; do {person_recent(device)} || {_adb(device)} input keyevent KEYCODE_UNKNOWN > /dev/null 2>&1; '
            f'sleep {KEEP_AWAKE_SECONDS}; done')


def unlock(device):
    """Wakes the screen and dismisses a swipe lock (a PIN lock needs the user once; a test device has none)."""
    return f'{_adb(device)} input keyevent KEYCODE_WAKEUP > /dev/null 2>&1; {_adb(device)} wm dismiss-keyguard > /dev/null 2>&1'


def scripts(found):
    """(prep, restore) shell lines for these devices; ('', '') when none has a prep list."""
    have = [d for d in found if items(d)]
    return '; '.join(prep_script(d) for d in have), '; '.join(restore_script(d) for d in have)
