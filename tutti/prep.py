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


def items(device):
    out = []
    for i, x in enumerate(device.get('prep') or []):
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
    lines = [f'mkdir -p "$(dirname {f})"', f': > {f}.tmp']
    for i, it in enumerate(its):
        lines.append(f'v=$({_read(device, it)})')
        bad = 'grep -qiE "not found|inaccessible|unknown|error|exception|usage:"'
        lines.append(f'if [ -n "$v" ] && ! printf "%s" "$v" | {bad}; then printf "%s\\t%s\\n" {i} "$v" >> {f}.tmp; '
                     f'else echo "tutti: prep {it["name"]} skipped: the device does not answer {shlex.quote(it["read"])}"; fi')
    lines.append(f'mv {f}.tmp {f}')
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


def scripts(found):
    """(prep, restore) shell lines for these devices; ('', '') when none has a prep list."""
    have = [d for d in found if items(d)]
    return '; '.join(prep_script(d) for d in have), '; '.join(restore_script(d) for d in have)
