"""Waits that end. Never wait with `pgrep -f <pattern>` from a shell whose own command line holds the pattern: it matches itself
and never ends. Wait on a PID, a done-file or a log line, always with a timeout."""
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
    return lambda: not os.path.exists(f'/proc/{int(pid)}')


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
