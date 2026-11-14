"""What every Tutti HTTP server shares against abuse (docs/SECURITY-AUDIT-2026-11.md): a cap on requests served at once,
and, for a dashboard that runs without sign-in, the check that a loopback client is this login's own process.

- `CappedServer`: a ThreadingHTTPServer that serves at most `max_connections` requests at once; one more is closed before a
  thread starts for it, so a host that opens connections without end cannot grow the process's threads and memory.
- `loopback_owner_ok`: on a computer several people log in to (a household's shared machine), 127.0.0.1 is reachable by
  every login. Without sign-in the dashboard is its owner's alone, so a loopback connection is served only when the process
  that opened it runs as this same user. Linux answers from /proc/net/tcp{,6} (the socket's owner uid); where that cannot
  be read (macOS, Windows) the check passes, as before.

Pure apart from reading /proc."""
import os
import threading
from http.server import ThreadingHTTPServer


class CappedServer(ThreadingHTTPServer):
    """At most `max_connections` requests in flight; the next one is closed at once (it may try again)."""
    max_connections = 64

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(self.max_connections)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def _hex_addr(text):
    """('127.0.0.1' or '::1' as /proc prints it, port) of an 'ADDR:PORT' field of /proc/net/tcp{,6}."""
    addr, port = text.split(':')
    raw = bytes.fromhex(addr)
    # /proc prints each 32-bit word in host order (little-endian on every Linux Tutti runs on)
    words = b''.join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    import ipaddress
    ip = ipaddress.ip_address(words)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip), int(port, 16)


def socket_uid(client, server, proc='/proc'):
    """The uid that owns the client end of a TCP connection (client=(ip, port) as accepted, server=(ip, port)), or None when
    it cannot be told (no /proc, or the connection is not listed)."""
    want_client = (str(client[0]).removeprefix('::ffff:'), int(client[1]))
    want_server = int(server[1])
    for name in ('tcp', 'tcp6'):
        try:
            with open(os.path.join(proc, 'net', name), encoding='ascii') as f:
                rows = f.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            cols = row.split()
            if len(cols) < 8:
                continue
            try:
                local, remote = _hex_addr(cols[1]), _hex_addr(cols[2])
            except ValueError:
                continue
            if local == want_client and remote[1] == want_server:
                return int(cols[7])
    return None


def loopback_owner_ok(client, server, uid=None, proc='/proc'):
    """True unless the loopback client is a process of another user of this computer."""
    uid = os.getuid() if uid is None and hasattr(os, 'getuid') else uid
    if uid is None:
        return True   # Windows: no uids to compare
    found = socket_uid(client, server, proc)
    return found is None or found in (uid, 0)   # root may do anything here anyway
