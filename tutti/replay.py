"""Bounded in-process event replay for a future stable Chat job owner; not wired into the dashboard."""
import json
import math
import secrets
import threading
import time
from collections import deque


class ReplayGap(ValueError):
    """The requested cursor is older than the events retained by this owner."""


class ReplayLog:
    """A single job's ordered events. Cursors are sequence numbers scoped to this one job and owner lifetime."""

    def __init__(self, limit=1024, max_event_bytes=65536, max_bytes=1048576):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError('Replay limit must be a positive integer: configure at least one retained event.')
        if any(not isinstance(n, int) or isinstance(n, bool) or n < 1 for n in (max_event_bytes, max_bytes)):
            raise ValueError('Replay byte limits must be positive integers.')
        if max_event_bytes > max_bytes:
            raise ValueError('The per-event byte limit cannot exceed the total replay byte limit.')
        self.generation = secrets.token_urlsafe(24)
        self._limit, self._max_event_bytes, self._max_bytes = limit, max_event_bytes, max_bytes
        self._bytes = 0
        self._events = deque()
        self._condition = threading.Condition()
        self._next = 1
        self._closed = False

    def append(self, event):
        """Save a JSON event within count and encoded-byte limits; events after close are refused."""
        try:
            payload = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf8')
        except (TypeError, ValueError, UnicodeError, RecursionError) as e:
            raise ValueError('Replay event must be valid JSON: send a serializable event.') from e
        if len(payload) > self._max_event_bytes:
            raise ValueError('Replay event exceeds its byte limit: send a smaller event.')
        with self._condition:
            if self._closed:
                raise ValueError('This event stream has finished: start a new job to send more events.')
            seq = self._next
            self._next += 1
            while self._events and (len(self._events) >= self._limit or self._bytes + len(payload) > self._max_bytes):
                _, discarded = self._events.popleft()
                self._bytes -= len(discarded)
            self._events.append((seq, payload))
            self._bytes += len(payload)
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
            return [(seq, json.loads(payload)) for seq, payload in self._events if seq > after], self._closed
