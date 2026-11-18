"""Bounded in-process event replay for a future stable Chat job owner; not wired into the dashboard."""
import copy
import math
import secrets
import threading
import time
from collections import deque


class ReplayGap(ValueError):
    """The requested cursor is older than the events retained by this owner."""


class ReplayLog:
    """A single job's ordered events. Cursors are sequence numbers scoped to this one job and owner lifetime."""

    def __init__(self, limit=1024):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError('Replay limit must be a positive integer: configure at least one retained event.')
        self.generation = secrets.token_urlsafe(24)
        self._events = deque(maxlen=limit)
        self._condition = threading.Condition()
        self._next = 1
        self._closed = False

    def append(self, event):
        """Save a copy and wake followers; events after close are refused."""
        event = copy.deepcopy(event)
        with self._condition:
            if self._closed:
                raise ValueError('This event stream has finished: start a new job to send more events.')
            seq = self._next
            self._next += 1
            self._events.append((seq, event))
            self._condition.notify_all()
            return seq

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def read(self, generation, after, timeout=0):
        """Return (events after cursor, closed). A lost prefix raises ReplayGap; an unknown future cursor is rejected."""
        if generation != self.generation:
            raise ValueError('Replay owner has changed: reload the saved conversation before reconnecting.')
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 0
                or timeout > threading.TIMEOUT_MAX or not math.isfinite(timeout)):
            raise ValueError('Replay timeout must be a finite non-negative number within the platform wait limit.')
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise ValueError('Replay cursor must be a non-negative integer: reconnect with the last received sequence.')
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            if after >= self._next:
                raise ValueError('Replay cursor is ahead of this job: reload its saved state before reconnecting.')
            while after == self._next - 1 and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            if self._events and after < self._events[0][0] - 1:
                raise ReplayGap('Some answer events are no longer retained: reload the saved conversation before reconnecting.')
            return [(seq, copy.deepcopy(event)) for seq, event in self._events if seq > after], self._closed
