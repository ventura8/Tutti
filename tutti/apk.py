"""Installing an app on a device (`tutti devices install <device> <apk>`, docs/DISPATCH.md, *Prep and restore*): one install
per device at a time (a lock on the device's machine), and an e2e APK must be debuggable, checked before anything is sent.
The check reads the APK's binary AndroidManifest.xml here: the attribute whose resource id is android:debuggable
(0x0101000f) must be true. Standard library only."""
import shlex
import struct
import zipfile

DEBUGGABLE = 0x0101000f
RES_MAP, START_ELEMENT = 0x0180, 0x0102


class ApkError(Exception):
    """The APK cannot be installed; the message says why in plain words and what to do."""


def debuggable_in(axml):
    """True when the binary manifest sets android:debuggable to true, False when it does not (or sets it false)."""
    ids, pos = [], 8
    while pos + 8 <= len(axml):
        kind, head, size = struct.unpack_from('<HHI', axml, pos)
        if size < 8:
            break
        if kind == RES_MAP:
            ids = list(struct.unpack_from(f'<{(size - 8) // 4}I', axml, pos + 8))
        elif kind == START_ELEMENT:
            start, attr_size, count = struct.unpack_from('<HHH', axml, pos + 24)
            for i in range(count):
                at = pos + 16 + start + i * attr_size
                _ns, name, _raw, _sz, _r, _typ, data = struct.unpack_from('<IIIHBBI', axml, at)
                if name < len(ids) and ids[name] == DEBUGGABLE:
                    return data != 0
        pos += size
    return False


def check(path):
    """Refuses (ApkError) an APK that is not debuggable or cannot be read."""
    try:
        with zipfile.ZipFile(path) as z:
            manifest = z.read('AndroidManifest.xml')
    except (OSError, KeyError, zipfile.BadZipFile) as e:
        raise ApkError(f'{path} is not an app package Tutti can read ({e}): build the e2e APK again') from e
    if not debuggable_in(manifest):
        raise ApkError(f'{path} is not a debuggable build, and end-to-end tests need one: build the debug variant '
                       '(for example ./gradlew assembleDebug) and install that')


def install_script(device, remote_path):
    """The bash line that installs on the device behind its install lock: a second install waits for the first."""
    from . import remote
    lock = remote.home_path(f'~/.tutti/locks/install-{device["name"]}.lock')
    adb = f'{shlex.quote(device.get("adb") or "adb")} -s {shlex.quote(str(device["serial"]))}'
    return (f'mkdir -p "$(dirname {lock})" && flock {lock} sh -c '
            + shlex.quote(f'echo "tutti: installing on {device["name"]}"; {adb} install -r -t {shlex.quote(remote_path)}'))
