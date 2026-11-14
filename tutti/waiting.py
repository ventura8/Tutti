"""Waits that end. Never wait with `pgrep -f <pattern>` from a shell whose own command line holds the pattern: it matches itself
and never ends. Wait on a PID, a done-file or a log line, always with a timeout."""
import json
import os
import re
import time


def until(check, timeout, poll=2.0, clock=time.monotonic, sleep=time.sleep):
    """True as soon as check() is true; False when `timeout` seconds pass first."""
    end = clock() + timeout
    while True:
        if check():
            return True
        if clock() >= end:
            return False
        sleep(poll)


def pid_gone(pid):
    """True once process `pid` no longer exists. Uses OpenProcess on Windows: os.kill(pid, 0) is not a liveness check there
    (any non-CTRL signal terminates the process)."""
    pid = int(pid)

    def check():
        if os.name == 'nt':
            return _win_pid_gone(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    return check


def _win_pid_gone(pid):
    """OpenProcess succeeds for an exited process while another program holds its handle, and fails with
    ERROR_ACCESS_DENIED for a live protected one: read the exit code, and treat a denied open as alive."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() != 5  # ERROR_ACCESS_DENIED: the process exists
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value != 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def file_exists(path):
    return lambda: os.path.exists(path)


def file_matches(path, pattern):
    rx = re.compile(pattern)

    def check():
        try:
            with open(path, encoding='utf8', errors='replace') as f:
                return any(rx.search(line) for line in f)
        except OSError:
            return False
    return check


def stalled(path, seconds, clock=time.time):
    """True once `path` has not changed for `seconds` (a background job that hangs). Agents waiting on a job end their wait
    here instead of polling and re-announcing "still waiting" forever."""
    def check():
        try:
            return clock() - os.path.getmtime(path) >= seconds
        except OSError:
            return False
    return check


def register(folder, info, pid=None):
    """Records a running wait as <folder>/<pid>.json so the dashboard can show it and whether what it watches has stalled.
    Returns the record's path; remove it with `unregister` when the wait ends."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f'{pid or os.getpid()}.json')
    with open(path, 'w', encoding='utf8') as f:
        json.dump({**info, 'started': time.time()}, f)
    return path


def unregister(path):
    try:
        os.remove(path)
    except OSError:
        pass
