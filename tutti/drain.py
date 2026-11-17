"""In-process admission gate for a future dashboard supervisor; it does not start, stop or route any service."""
import contextlib
import threading
import time


class Draining(RuntimeError):
    """New work cannot enter this generation while its existing work drains."""


class Gate:
    """Track admitted work through a drain, without cancelling it or admitting work after the drain starts.

    Drain tokens prevent an old waiter or rollback from completing a later drain. This is process-local: a supervisor
    must arrange exclusive admission, register all work, and hold authority through handover: wait() is only a snapshot.
    """

    def __init__(self):
        self._condition = threading.Condition()
        self._active = 0
        self._generation = None
        self._draining = False

    @contextlib.contextmanager
    def admit(self):
        with self._condition:
            if self._draining:
                raise Draining('The dashboard is preparing an update: retry after it finishes or cancels the update.')
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def begin(self):
        """Stop admission atomically and return a token; concurrent drain requests are rejected."""
        with self._condition:
            if self._draining:
                raise Draining('A dashboard update is already draining work: wait for it to finish or cancel it.')
            self._generation = object()
            self._draining = True
            return self._generation

    def wait(self, token, timeout):
        """True only if this drain reaches zero active work within timeout and was not cancelled."""
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            while self._draining and token is self._generation and self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return self._draining and token is self._generation and self._active == 0

    def cancel(self, token):
        """Reopen admission only for the current drain; in-flight work is never cancelled."""
        with self._condition:
            if not self._draining or token is not self._generation:
                return False
            self._draining = False
            self._condition.notify_all()
            return True
