"""Private, inherited Unix-channel prototype for an isolated Chat job owner; not started by the dashboard.

The peer with the socket is a trusted gateway, not an end user. It must authenticate users independently and prevent
untrusted peers from obtaining or using the descriptor. This protocol is not a listener or a deployable supervisor.
"""
import json
import struct

MAX_FRAME = 65536


class ChannelError(ValueError):
    """A frame is invalid, too large or truncated; close this channel before retrying."""


def _exact(sock, count, *, header=False):
    out = bytearray()
    while len(out) < count:
        chunk = sock.recv(count - len(out))
        if not chunk:
            if header and not out:
                return None
            raise ChannelError('Owner channel closed mid-frame: reconnect through the supervisor.')
        out.extend(chunk)
    return bytes(out)


def receive(sock):
    """Read one bounded JSON frame; return None on clean EOF."""
    head = _exact(sock, 4, header=True)
    if head is None:
        return None
    size = struct.unpack('!I', head)[0]
    if not 0 < size <= MAX_FRAME:
        raise ChannelError('Owner channel frame size is invalid: close the channel and retry.')
    body = _exact(sock, size)
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


def serve(sock, jobs):
    """Serve sequential follow requests on a private socket passed by a trusted supervisor."""
    while True:
        request = receive(sock)
        if request is None:
            return
        response = follow(jobs, request) if request.get('action') == 'follow' else {'ok': False, 'error': 'Unknown request.'}
        try:
            send(sock, response)
        except ChannelError:
            send(sock, {'ok': False, 'error': 'Replay batch exceeds the channel limit; reload the conversation.'})
