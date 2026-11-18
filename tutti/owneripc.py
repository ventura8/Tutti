"""Private, inherited Unix-channel prototype for an isolated Chat job owner; not started by the dashboard.

The peer with the socket is a trusted gateway, not an end user. It must authenticate users independently and prevent
untrusted peers from obtaining or using the descriptor. This protocol is not a listener or a deployable supervisor.
"""
import json
import struct
import time

MAX_FRAME = 65536


class ChannelError(ValueError):
    """A frame is invalid, too large or truncated; close this channel before retrying."""


def _exact(sock, count, *, header=False, deadline=None):
    out = bytearray()
    while len(out) < count:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Owner channel frame deadline expired.')
            sock.settimeout(remaining)
        chunk = sock.recv(count - len(out))
        if not chunk:
            if header and not out:
                return None
            raise ChannelError('Owner channel closed mid-frame: reconnect through the supervisor.')
        out.extend(chunk)
    return bytes(out)


def receive(sock, frame_timeout=None):
    """Read one bounded JSON frame; return None on clean EOF. A deadline bounds the whole frame, not each read."""
    deadline = None if frame_timeout is None else time.monotonic() + frame_timeout
    previous = sock.gettimeout() if deadline is not None else None
    try:
        head = _exact(sock, 4, header=True, deadline=deadline)
        if head is None:
            return None
        size = struct.unpack('!I', head)[0]
        if not 0 < size <= MAX_FRAME:
            raise ChannelError('Owner channel frame size is invalid: close the channel and retry.')
        body = _exact(sock, size, deadline=deadline)
    finally:
        if deadline is not None:
            sock.settimeout(previous)
    try:
        value = json.loads(body)
    except (UnicodeError, ValueError, RecursionError) as e:
        raise ChannelError('Owner channel frame is not valid JSON: close the channel and retry.') from e
    if not isinstance(value, dict):
        raise ChannelError('Owner channel frame must be an object: close the channel and retry.')
    return value


def send(sock, value):
    """Write one bounded JSON frame; no partial frame is sent when validation fails."""
    try:
        data = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(',', ':')).encode('utf8')
    except (UnicodeError, TypeError, ValueError, RecursionError) as e:
        raise ChannelError('Owner channel response is not valid JSON: close the channel and retry.') from e
    if not 0 < len(data) <= MAX_FRAME:
        raise ChannelError('Owner channel frame exceeds its limit: retry with a smaller batch.')
    sock.sendall(struct.pack('!I', len(data)) + data)


def follow(jobs, request):
    """Respond to one already-authenticated gateway assertion; unknown and other users' jobs look the same."""
    sub, mid = request.get('subject'), request.get('message')
    if not isinstance(sub, str) or not isinstance(mid, str):
        return {'ok': False, 'error': 'Answer unavailable.'}
    job = jobs.get(mid)
    if job is None or job.sub != sub or job.replay is None:
        return {'ok': False, 'error': 'Answer unavailable.'}
    try:
        events, done = job.replay.read(request.get('generation'), request.get('after'))
    except ValueError:
        return {'ok': False, 'error': 'Replay unavailable; reload the conversation.'}
    return {'ok': True, 'events': events, 'done': done}


def session_follow(jobs, request, sessions, users):
    """Resolve a live browser session inside the owner; do not trust a gateway-supplied subject."""
    sid = request.get('session')
    if not isinstance(sid, str) or not 1 <= len(sid) <= 512:
        return {'ok': False, 'error': 'Answer unavailable.'}
    try:
        session = sessions.get(sid)
        user = users.get(session.get('sub')) if isinstance(session, dict) else None
        if not user or user.get('role') not in ('admin', 'viewer'):
            return {'ok': False, 'error': 'Answer unavailable.'}
        return follow(jobs, {**request, 'subject': user['sub']})
    except Exception:
        return {'ok': False, 'error': 'Answer unavailable.'}


def serve(sock, jobs, authorize=None):
    """Serve sequential follow requests on a private socket passed by a trusted supervisor."""
    while True:
        request = receive(sock)
        if request is None:
            return
        response = authorize(jobs, request) if request.get('action') == 'follow' and authorize else {
            'ok': False, 'error': 'Answer unavailable.'}
        try:
            send(sock, response)
        except ChannelError:
            send(sock, {'ok': False, 'error': 'Replay batch exceeds the channel limit; reload the conversation.'})
