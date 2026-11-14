"""Writes that survive a hard power-off (docs/PRESENCE.md, "Nothing is lost").

- a state file (the backlog, dispatch's leases and queue, the autopilot's state, controls, the owner's profile, presence) is
  written to a temp file of this process, flushed and fsynced, renamed over the old one, and the folder fsynced: a crash at
  any point leaves the old file or the new one, never half of one;
- a log (the usage readings) is appended one fsynced line at a time; a power-off mid-line leaves a torn last line, which
  `read_jsonl` skips and `repair` cuts off at the next start;
- `boot_id` tells a run that died with its host (a reboot or power-off: interrupted, not failed) from one that crashed."""
import glob
import json
import os

BOOT_ID = '/proc/sys/kernel/random/boot_id'


def _fsync_dir(folder):
    try:
        fd = os.open(folder or '.', os.O_RDONLY)
    except OSError:   # Windows: a folder cannot be opened; its rename is durable enough there
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def dump_json(path, data, indent=None):
    """`data` as JSON at `path`, atomically and durably."""
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(data, f, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(os.path.dirname(path))


def append_line(path, text):
    """One line appended and fsynced."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'a', encoding='utf8') as f:
        f.write(text.rstrip('\n') + '\n')
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path):
    """The JSON lines of `path`; a torn line (a power-off mid-append) is skipped, never an error."""
    out = []
    try:
        with open(path, encoding='utf8', errors='replace') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        return []
    return out


def repair_jsonl(path):
    """Cuts a torn last line off `path` (no newline at its end, or not JSON). Returns True when it cut something."""
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError:
        return False
    if not raw:
        return False
    keep = raw if raw.endswith(b'\n') else raw[:raw.rfind(b'\n') + 1]
    lines = keep.splitlines(keepends=True)
    while lines:
        try:
            json.loads(lines[-1])
            break
        except ValueError:
            lines.pop()
    fixed = b''.join(lines)
    if fixed == raw:
        return False
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'wb') as f:
        f.write(fixed)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return True


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:   # PermissionError: it lives, under another user
        return True
    return True


def sweep(folder, alive=pid_alive):
    """Removes the temp files of writers that are gone (a power-off mid-write). Returns their paths."""
    out = []
    for tmp in glob.glob(os.path.join(folder, '*.tmp')) + glob.glob(os.path.join(folder, '*', '*.tmp')):
        parts = tmp.rsplit('.', 2)
        pid = int(parts[1]) if len(parts) == 3 and parts[1].isdigit() else None
        if pid is None or not alive(pid):
            try:
                os.remove(tmp)
                out.append(tmp)
            except OSError:
                pass
    return out


def boot_id(path=BOOT_ID):
    """This boot's id (Linux), or '' where none is readable: then a dead run is judged by its pid alone."""
    try:
        with open(path, encoding='utf8') as f:
            return f.read().strip()
    except OSError:
        return ''


def died_with_host(recorded, now_boot=None):
    """True when the run was started in an earlier boot: its host went down under it (interrupted, not failed)."""
    now_boot = boot_id() if now_boot is None else now_boot
    return bool(recorded and now_boot and recorded != now_boot)
