"""Experimental owner loop with supervisor-supplied gateway descriptors; never started by Tutti services.

Requires Unix SOCK_SEQPACKET and SCM_RIGHTS. The inherited control socket is a privileged capability, not an authenticated
user channel. Anyone who holds it can replace gateways and impersonate users; a real supervisor must protect it.
"""
import array
import os
import select
import socket

from . import gatewayfence, owneripc


class SupervisorError(ValueError):
    """Invalid supervisor control packet or unsupported descriptor."""


def attach(control, expected, owner_endpoint):
    """Ask the isolated owner to replace the gateway, passing one connected Unix stream descriptor."""
    payload = f'attach:{expected}'.encode('ascii')
    rights = array.array('i', [owner_endpoint.fileno()])
    control.sendmsg([payload], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])
    return control.recv(64).decode('ascii')


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
    data, ancillary, flags, _addr = control.recvmsg(64, socket.CMSG_SPACE(array.array('i').itemsize * 8))
    received = []
    for level, kind, raw in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds = array.array('i')
            fds.frombytes(raw[:len(raw) - len(raw) % fds.itemsize])
            received.extend(fds)
    try:
        if not data and not received:
            return None, None
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(received) != 1:
            raise SupervisorError('Control packet must carry exactly one complete socket descriptor.')
        try:
            raw_epoch = data.decode('ascii').removeprefix('attach:')
            if not data.startswith(b'attach:') or not raw_epoch.isascii() or not raw_epoch.isdecimal():
                raise ValueError('invalid epoch')
            expected = int(raw_epoch)
        except (UnicodeError, ValueError) as e:
            raise SupervisorError('Control packet needs an integer expected epoch.') from e
        fd = received.pop()
        peer = _peer(fd)
        return expected, peer
    finally:
        for fd in received:
            os.close(fd)


def _replace(control, fence, active, epoch):
    expected, candidate = _control_packet(control)
    if candidate is None:
        return active, epoch, False
    if expected != epoch:
        candidate.close()
        control.send(b'stale')
        return active, epoch, True
    try:
        replaced = fence.replace(active, candidate)
    except (gatewayfence.FenceError, OSError):
        replaced = False
    if not replaced:
        candidate.close()
        control.send(b'refused')
        return active, epoch, True
    if active is not None:
        active.close()
    epoch += 1
    control.send(f'active:{epoch}'.encode('ascii'))
    return candidate, epoch, True


def serve(control, jobs):
    """Run an isolated owner until the trusted supervisor control channel closes or sends an invalid packet."""
    fence = gatewayfence.GatewayFence(jobs)
    active, epoch = None, 0
    try:
        while True:
            readable, _, _ = select.select([control] + ([active] if active is not None else []), [], [])
            if control in readable:   # acknowledgement fences the old endpoint before any later request
                active, epoch, keep = _replace(control, fence, active, epoch)
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
                    active = None
    finally:
        if active is not None:
            active.close()
