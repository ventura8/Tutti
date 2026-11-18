"""Isolated owner-side gateway fencing experiment; never started by the dashboard or a service."""
import math
import os
import threading

from . import owneripc


class FenceError(ValueError):
    """An unsafe replacement or channel is refused."""


class GatewayFence:
    """Serialize one owner's replies and gateway replacement; caller must protect the supervisor API and descriptors."""

    def __init__(self, jobs, send_timeout=1.0, authorize=None):
        if (not isinstance(send_timeout, (int, float)) or isinstance(send_timeout, bool) or send_timeout <= 0
                or send_timeout > 5 or not math.isfinite(send_timeout)):
            raise FenceError('Gateway send timeout must be finite, greater than zero and at most five seconds.')
        self._jobs, self._timeout, self._authorize = jobs, send_timeout, authorize
        self._lock = threading.Lock()
        self._auth_slot = threading.BoundedSemaphore(1)
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

    def _authorize_bounded(self, request):
        """Run read-only authorization with a bounded wait and at most one outstanding worker."""
        denied = {'ok': False, 'error': 'Answer unavailable.'}
        if self._authorize is None or not self._auth_slot.acquire(blocking=False):
            return denied
        ready, outcome = threading.Event(), []
        def check():
            try:
                outcome.append(self._authorize(self._jobs, request))
            except Exception:
                outcome.append(denied)
            finally:
                self._auth_slot.release()
                ready.set()
        try:
            threading.Thread(target=check, daemon=True, name='tutti-owner-auth').start()
        except Exception:
            self._auth_slot.release()
            return denied
        if not ready.wait(self._timeout) or not outcome or not isinstance(outcome[0], dict):
            return denied
        return outcome[0]

    def dispatch(self, channel, request):
        """Only the active channel may receive one correlated reply; replacement waits for the reply or timeout."""
        with self._lock:
            if channel is not self._active:
                return False
            correlation = request.get('request_id')
            if not isinstance(correlation, str) or not correlation or len(correlation) > 80:
                response = {'ok': False, 'error': 'A request identifier is required.'}
            else:
                response = self._authorize_bounded(request) if request.get('action') == 'follow' else {
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
