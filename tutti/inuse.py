"""A worker program's use lock. A Tutti run of a CLI worker holds `<state>/locks/use-<program>.lock` shared for as long as the
program runs; `tutti update` takes it exclusively (`flock -n`) around that program's updater, so an update never replaces a
program a Tutti run is using, and a run that starts during an update waits until the update is done."""
import contextlib
import os

try:
    import fcntl
except ImportError:   # Windows without WSL: no flock; runs and updates are not coordinated there
    fcntl = None


def lock_dir(state):
    return os.path.join(state, 'locks')


def lock_path(state, program):
    return os.path.join(lock_dir(state), f'use-{os.path.basename(program)}.lock')


@contextlib.contextmanager
def using(state, program):
    """Holds the program's use lock shared while the block runs (waits while an update holds it)."""
    if fcntl is None:
        yield
        return
    os.makedirs(lock_dir(state), exist_ok=True)
    fd = os.open(lock_path(state, program), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)   # closing releases the lock


@contextlib.contextmanager
def exclusive(path):
    """An exclusive lock taken without waiting: yields True when this process has it, False when another one does."""
    if fcntl is None:
        yield True
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)
