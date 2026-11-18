"""Experimental owner loop with supervisor-supplied gateway descriptors; never started by Tutti services.

Requires Unix SOCK_SEQPACKET and SCM_RIGHTS. The inherited control socket is a privileged capability, not an authenticated
user channel. Anyone who holds it can replace gateways and impersonate users; a real supervisor must protect it.
"""
import array
import math
import os
import secrets
import select
import socket
import time

from . import gatewayfence, owneripc


class SupervisorError(ValueError):
    """Invalid supervisor control packet or unsupported descriptor."""


def begin_attach(control, expected, owner_endpoint, token):
    """Submit one handoff; return its ID before receiving the ACK. The control channel must be single-caller."""
    if len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
        raise SupervisorError('Handoff token must be 32 lowercase hex characters.')
    payload = f'attach:{token}:{expected}'.encode('ascii')
    rights = array.array('i', [owner_endpoint.fileno()])
    if control.sendmsg([payload], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)]) != len(payload):
        raise SupervisorError('Control packet was not sent whole: reconcile before proceeding.')
    return token


def _await(control, token, timeout):
    deadline = time.monotonic() + timeout
    previous = control.gettimeout()
    try:
        for _ in range(32):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Supervisor response deadline expired.')
            control.settimeout(remaining)
            packet = control.recv(256).decode('ascii').split(':')
            if len(packet) >= 2 and packet[1] == token:
                return packet
        raise SupervisorError('Too many unrelated supervisor responses: stop and investigate.')
    finally:
        control.settimeout(previous)


def attach(control, expected, owner_endpoint):
    """Compatibility helper for tests; callers needing recovery must retain their own token."""
    token = secrets.token_hex(16)
    begin_attach(control, expected, owner_endpoint, token)
    response = _await(control, token, 5)
    return f'active:{response[2]}' if response[0] == 'active' and len(response) == 3 else response[0]


def reconcile(control, expected, handoff_token, timeout=2):
    """Classify an ambiguous handoff on the same ordered control connection; errors remain unknown."""
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 30
            or not math.isfinite(timeout)):
        return 'unknown'
    if (not isinstance(expected, int) or isinstance(expected, bool) or expected < 0
            or not isinstance(handoff_token, str) or len(handoff_token) != 32
            or any(c not in '0123456789abcdef' for c in handoff_token)):
        return 'unknown'
    query = secrets.token_hex(16)
    try:
        control.sendall(f'status:{query}'.encode('ascii'))
        packet = _await(control, query, timeout)
        if len(packet) != 4 or packet[0] != 'state' or not packet[2].isdecimal():
            return 'unknown'
        epoch, active_token = int(packet[2]), packet[3]
        if epoch == expected + 1 and active_token == handoff_token:
            return 'applied'
        if epoch == expected and active_token != handoff_token:
            return 'not_applied'
        return 'conflict'
    except (OSError, UnicodeError, SupervisorError, ValueError):
        return 'unknown'


def _peer(fd):
    peer = None
    try:
        os.set_inheritable(fd, False)
        peer = socket.socket(fileno=fd)
        if peer.family != socket.AF_UNIX or peer.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
            raise SupervisorError('Gateway descriptor must be a connected Unix stream socket.')
        peer.getpeername()
        return peer
    except (OSError, SupervisorError) as e:
        if peer is not None:
            peer.close()
        else:
            os.close(fd)
        raise SupervisorError('Gateway descriptor is not a connected Unix stream socket.') from e


def _control_packet(control):
    data, ancillary, flags, _addr = control.recvmsg(128, socket.CMSG_SPACE(array.array('i').itemsize * 8))
    received = []
    for level, kind, raw in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds = array.array('i')
            fds.frombytes(raw[:len(raw) - len(raw) % fds.itemsize])
            received.extend(fds)
    try:
        if not data and not received:
            return None
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise SupervisorError('Truncated control packet: close this channel.')
        try:
            parts = data.decode('ascii').split(':')
            kind, token = parts[:2]
            if len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
                raise ValueError('invalid token')
            if kind == 'status' and len(parts) == 2 and not received:
                return kind, token, None, None
            if kind != 'attach' or len(parts) != 3 or len(received) != 1 or not parts[2].isdecimal():
                raise ValueError('invalid attach')
            expected = int(parts[2])
        except (UnicodeError, ValueError) as e:
            raise SupervisorError('Invalid control packet: close this channel.') from e
        return kind, token, expected, _peer(received.pop())
    finally:
        for fd in received:
            os.close(fd)


def _command(control, fence, active, epoch, active_token):
    packet = _control_packet(control)
    if packet is None:
        return active, epoch, active_token, False
    kind, token, expected, candidate = packet
    if kind == 'status':
        control.send(f'state:{token}:{epoch}:{active_token or "-"}'.encode('ascii'))
        return active, epoch, active_token, True
    if expected != epoch:
        candidate.close()
        control.send(f'stale:{token}:{epoch}'.encode('ascii'))
        return active, epoch, active_token, True
    try:
        replaced = fence.replace(active, candidate)
    except (gatewayfence.FenceError, OSError):
        replaced = False
    if not replaced:
        candidate.close()
        control.send(f'refused:{token}:{epoch}'.encode('ascii'))
        return active, epoch, active_token, True
    if active is not None:
        active.close()
    epoch += 1
    control.send(f'active:{token}:{epoch}'.encode('ascii'))
    return candidate, epoch, token, True


def serve(control, jobs):
    """Run an isolated owner until the trusted supervisor control channel closes or sends an invalid packet."""
    fence = gatewayfence.GatewayFence(jobs)
    active, epoch, active_token = None, 0, None
    try:
        while True:
            readable, _, _ = select.select([control] + ([active] if active is not None else []), [], [])
            if control in readable:   # acknowledgement fences the old endpoint before any later request
                active, epoch, active_token, keep = _command(control, fence, active, epoch, active_token)
                if not keep:
                    return
            elif active in readable:
                try:
                    request = owneripc.receive(active, frame_timeout=1.0)
                    if request is None:
                        raise ConnectionError('gateway closed')
                    fence.dispatch(active, request)
                except (OSError, owneripc.ChannelError):
                    fence.retire(active)
                    active.close()
                    active, active_token = None, None
    finally:
        if active is not None:
            active.close()
