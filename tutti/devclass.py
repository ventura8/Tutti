"""Per device class command allow-list (docs/DISPATCH.md, *Device classes*): some adb commands are unsafe on some kinds of
device. On a TV (Android's leanback) `wm size`/`wm density`, the rotation settings and `cmd device_state` change a panel
that has no rotation or fold and may leave it unusable until a person resets it (RoRadio, 2026-11-16). Tutti never runs a
denied command itself (a prep item that matches is dropped), refuses a run whose own command line names one, and tells the
project's script through TUTTI_DEVICE_CLASS and TUTTI_DEVICE_DENY (a regex) so it can skip them too. `device_classes.<kind>.deny`
in the config replaces a kind's list; `deny: []` allows everything. Pure."""
import re

DEFAULT_DENY = {
    'tv': [r'\bwm\s+(?:size|density)\b', r'\b(?:user_rotation|accelerometer_rotation)\b', r'\bcmd\s+device_state\b',
           r'\bsettings\s+put\s+system\s+(?:user_rotation|accelerometer_rotation)\b'],
}


def kind(device):
    return str(device.get('kind') or 'phone')


def deny(cfg, device):
    """The regexes a device of this kind must never run."""
    own = ((cfg or {}).get('device_classes') or {}).get(kind(device))
    if isinstance(own, dict) and isinstance(own.get('deny'), list):
        return [str(x) for x in own['deny']]
    return list(DEFAULT_DENY.get(kind(device), []))


def pattern(rules):
    return '|'.join(f'(?:{r})' for r in rules)


def denied(rules, text):
    """The first rule `text` breaks, or ''."""
    for r in rules:
        if re.search(r, text or ''):
            return r
    return ''


def rules_of(device):
    return device.get('_deny', DEFAULT_DENY.get(kind(device), []))


def refusal(found, command):
    """Why `command` must not run on these devices ('' when it may)."""
    for d in found:
        rule = denied(rules_of(d), command)
        if rule:
            return (f'tutti: refused: the command runs "{rule}" on {d["name"]}, a {kind(d)}, whose class forbids it '
                    f'(device_classes.{kind(d)}.deny): take it out of the command or run it on another device')
    return ''


def export(found):
    """The shell prefix that tells the project's script the devices' class and what it must not run."""
    import shlex
    kinds = ','.join(sorted({kind(d) for d in found}))
    rules = pattern(sorted({r for d in found for r in rules_of(d)}))
    return f'export TUTTI_DEVICE_CLASS={shlex.quote(kinds)} TUTTI_DEVICE_DENY={shlex.quote(rules)}; '


def errors(cfg):
    errs = []
    classes = (cfg or {}).get('device_classes')
    if classes is None:
        return errs
    if not isinstance(classes, dict):
        return [f'device_classes is {classes!r}: a mapping of device kind (phone, tablet, tv) to {{deny: [regex, ...]}}']
    for k, v in classes.items():
        if not isinstance(v, dict) or not isinstance(v.get('deny', []), list):
            errs.append(f'device_classes.{k} is {v!r}: write it as {{deny: [<regex of a command never run on a {k}>]}}')
            continue
        for i, r in enumerate(v.get('deny') or []):
            try:
                re.compile(str(r))
            except re.error:
                errs.append(f'device_classes.{k}.deny[{i}] is {r!r}: a regular expression, e.g. "\\\\bwm size\\\\b"')
    return errs
