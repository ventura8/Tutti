"""AI credits of GitHub Copilot CLI sessions, wherever they ran. Every `copilot` session writes
`~/.copilot/session-state/<id>/events.jsonl`; its last record, `session.shutdown`, carries `accountingSnapshot.totalNanoAiu`
(the session's AI credits times 1e9: 6835050000 is the "AI Credits 6.84" the CLI prints) and `sessionStartTime`. So the
credits of runs started outside Tutti, and of runs on another machine, are counted too.

`accounts.<name>.credits_machines: [<machine>, ...]` adds those machines: over ssh their `~/.copilot`, and for a WSL machine
also its Windows side (`%USERPROFILE%\\.copilot`, over the machine's plain ssh, PowerShell), where a Windows `copilot` keeps its
sessions. Read-only: only the shutdown lines of sessions changed in the window are sent back. A machine that does not answer
is named in `credits_problem`; its last sum is kept for `CACHE_SECONDS` so the dashboard does not ask it every few seconds."""
import base64
import glob
import json
import os
import subprocess
import threading
import time

from . import pacing, remote

SESSIONS = os.path.join('~', '.copilot', 'session-state')
MARK = '"session.shutdown"'
CACHE_SECONDS = 600
_cache = {}
_lock = threading.Lock()


def shutdown_credits(lines, since):
    """The AI credits of the sessions whose shutdown record is in `lines` and that started at or after `since` (an aware
    datetime). Lines that are not such a record, or not JSON, are skipped; a session seen twice counts once."""
    total, seen = 0.0, set()
    for line in lines:
        if MARK not in line:
            continue
        try:
            snap = (json.loads(line).get('data') or {}).get('accountingSnapshot') or {}
            started = pacing.parse_time(snap['sessionStartTime'])
            nano = float(snap.get('totalNanoAiu') or 0)
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
        if started < since or (started, nano) in seen:
            continue
        seen.add((started, nano))
        total += nano / 1e9
    return round(total, 2)


def local_lines(since, root=None):
    """Shutdown lines of this computer's sessions changed since `since`; None when it has no Copilot session folder."""
    folder = os.path.expanduser(root or SESSIONS)
    if not os.path.isdir(folder):
        return None
    out = []
    for path in glob.glob(os.path.join(folder, '*', 'events.jsonl')):
        try:
            if os.path.getmtime(path) < since.timestamp():
                continue
            with open(path, encoding='utf8', errors='replace') as f:
                out += [line for line in f if MARK in line]
        except OSError:
            continue
    return out


def bash_script(since):
    """Prints the shutdown lines of `~/.copilot/session-state/*/events.jsonl` changed since `since` (bash, read-only)."""
    stamp = since.strftime('%Y-%m-%d %H:%M:%S %z')
    return (f'd=~/.copilot/session-state; [ -d "$d" ] || exit 0; '
            f'find "$d" -name events.jsonl -newermt "{stamp}" -print0 2>/dev/null | xargs -0 -r grep -h -F {MARK!r} 2>/dev/null; true')


def windows_script(since):
    """The same on the Windows side of a WSL machine (PowerShell), output as UTF-8 lines."""
    stamp = since.strftime('%Y-%m-%dT%H:%M:%SZ')
    return ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; $d = Join-Path $env:USERPROFILE '.copilot\\session-state'; "
            "if (-not (Test-Path $d)) { exit 0 }; "
            f"$since = [DateTime]::Parse('{stamp}').ToUniversalTime(); "
            "Get-ChildItem -Path $d -Recurse -File -Filter events.jsonl | Where-Object { $_.LastWriteTimeUtc -ge $since } | "
            "ForEach-Object { Select-String -LiteralPath $_.FullName -SimpleMatch -Pattern 'session.shutdown' | "
            "ForEach-Object { $_.Line } }")


def _run(argv, timeout):
    r = subprocess.run(argv, capture_output=True, text=True, encoding='utf8', errors='replace', timeout=timeout, check=False)
    if r.returncode != 0:
        raise OSError(f'exit {r.returncode}: {(r.stderr or "").strip()[:160]}')
    return r.stdout.splitlines()


def machine_lines(machine, since, timeout=60):
    """The shutdown lines on a machine: its Linux side, and for a WSL machine its Windows side too. OSError when it fails."""
    lines = _run(remote.argv(machine, bash_script(since), ('-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes')), timeout)
    if machine['transport'] == 'wsl':
        packed = base64.b64encode(windows_script(since).encode('utf-16le')).decode()
        argv = [*remote.SSH, '-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes', machine['host'],
                f'powershell -NoProfile -NonInteractive -EncodedCommand {packed}']
        lines += _run(argv, timeout)
    return lines


def machine_credits(machine, since, lines_fn=machine_lines, clock=time.time):
    """(credits, problem) of one machine since `since`, cached for CACHE_SECONDS per machine and window start."""
    key = (machine['name'], since.isoformat())
    now = clock()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1], hit[2]
    try:
        value, problem = shutdown_credits(lines_fn(machine, since), since), None
    except (OSError, subprocess.SubprocessError) as e:
        value = hit[1] if hit else None
        problem = f'{machine["name"]} was not read ({e}); its Copilot sessions are left out until it answers'
    with _lock:
        _cache[key] = (now, value, problem)
    return value, problem
