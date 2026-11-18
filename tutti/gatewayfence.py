"""Isolated owner-side gateway fencing experiment; never started by the dashboard or a service."""
import math
import os
import threading

from . import owneripc


class FenceError(ValueError):
    """An unsafe replacement or channel is refused."""


class GatewayFence:
    """Serialize one owner's replies and gateway replacement; caller must protect the supervisor API and descriptors."""

    def __init__(self, jobs, send_timeout=1.0):
        if (not isinstance(send_timeout, (int, float)) or isinstance(send_timeout, bool) or send_timeout <= 0
                or send_timeout > 5 or not math.isfinite(send_timeout)):
            raise FenceError('Gateway send timeout must be finite, greater than zero and at most five seconds.')
        self._jobs, self._timeout = jobs, send_timeout
        self._lock = threading.Lock()
        self._active = None
        self._identity = None
        self._poisoned = set()

    def replace(self, expected, replacement):
        """Fence old channel and select a distinct new one, only if expected is still active.

        On return, all sends through this fence to the old channel have finished or timed out. Already-sent bytes may
        remain readable by the old peer. This object cannot prevent duplicate descriptors or calls outside this fence.
        """
        identity = os.fstat(replacement.fileno())
        identity = (identity.st_dev, identity.st_ino)
        with self._lock:
            if expected is not self._active:
                return False
            if identity in self._poisoned:
                raise FenceError('Gateway endpoint had a failed write: replace it with a fresh socket.')
            if self._identity == identity:
                raise FenceError('Gateway replacement must use a distinct socket endpoint.')
            replacement.settimeout(self._timeout)
            self._active, self._identity = replacement, identity
            return True

    def retire(self, expected):
        """Fence an endpoint whose framing or receive state is no longer trustworthy."""
        with self._lock:
            if expected is not self._active:
                return False
            if self._identity is not None:
                self._poisoned.add(self._identity)
            self._active, self._identity = None, None
            return True

    def dispatch(self, channel, request):
        """Only the active channel may receive one correlated reply; replacement waits for the reply or timeout."""
        with self._lock:
            if channel is not self._active:
                return False
            correlation = request.get('request_id')
            if not isinstance(correlation, str) or not correlation or len(correlation) > 80:
                response = {'ok': False, 'error': 'A request identifier is required.'}
            else:
                response = owneripc.follow(self._jobs, request) if request.get('action') == 'follow' else {
                    'ok': False, 'error': 'Unknown request.'}
                response['request_id'] = correlation
            try:
                try:
                    owneripc.send(channel, response)
                except owneripc.ChannelError:
                    owneripc.send(channel, {'ok': False, 'request_id': correlation, 'error': 'Replay batch is too large.'})
            except OSError:
                # A timeout or partial write leaves framing uncertain; never reuse this channel.
                self._poisoned.add(self._identity)
                self._active, self._identity = None, None
                raise
            return True
