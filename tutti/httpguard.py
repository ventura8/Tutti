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
    """At most `max_connections` requests in flight, and at most `max_per_client` from one address (so one host that trickles
    bytes cannot hold every slot); the next one is closed at once (it may try again)."""
    max_connections = 64
    max_per_client = 16

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self._per, self._per_lock = {}, threading.Lock()
        super().__init__(*args, **kwargs)

    def _take(self, host):
        with self._per_lock:
            if self._per.get(host, 0) >= self.max_per_client:
                return False
            if not self._slots.acquire(blocking=False):
                return False
            self._per[host] = self._per.get(host, 0) + 1
            return True

    def _give(self, host):
        with self._per_lock:
            n = self._per.get(host, 1) - 1
            if n > 0:
                self._per[host] = n
            else:
                self._per.pop(host, None)
            self._slots.release()

    def process_request(self, request, client_address):
        host = client_address[0]
        if not self._take(host):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._give(host)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._give(client_address[0])


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


ESTABLISHED = '01'
UNKNOWN = -1   # /proc/net/tcp was read, but the connection is not in it


def socket_uid(client, server, proc='/proc'):
    """The uid that owns the client end of an established TCP connection (client=(ip, port) as accepted, server=(ip, port)),
    UNKNOWN when the tables were read and it is not there, None when they cannot be read (no /proc: macOS, Windows)."""
    want_client = (str(client[0]).removeprefix('::ffff:'), int(client[1]))
    want_server = int(server[1])
    found = None
    for name in ('tcp', 'tcp6'):
        try:
            with open(os.path.join(proc, 'net', name), encoding='ascii') as f:
                rows = f.read().splitlines()[1:]
        except OSError:
            continue
        found = UNKNOWN
        for row in rows:
            cols = row.split()
            if len(cols) < 8 or cols[3] != ESTABLISHED:   # a closing socket is listed with uid 0: never taken as root
                continue
            try:
                local, remote = _hex_addr(cols[1]), _hex_addr(cols[2])
            except ValueError:
                continue
            if local == want_client and remote[1] == want_server:
                return int(cols[7])
    return found


def lsof_uid(client, server, run=None):
    """macOS (and BSD): the uid of the process whose socket is the connection's client end, from `lsof -F pun`; UNKNOWN when
    lsof answered without it, None without lsof."""
    import subprocess
    run = run or subprocess.run
    port, sport = int(client[1]), int(server[1])
    try:
        out = run(['lsof', '-nP', f'-iTCP:{port}', '-sTCP:ESTABLISHED', '-F', 'pun'], capture_output=True, text=True,
                  timeout=5).stdout or ''
    except (OSError, subprocess.SubprocessError):
        return None
    uid = None
    for line in out.splitlines():
        if line[:1] == 'u':
            uid = int(line[1:]) if line[1:].isdigit() else None
        elif line[:1] == 'n' and uid is not None:
            local, _, remote = line[1:].partition('->')
            if local.rsplit(':', 1)[-1] == str(port) and remote.rsplit(':', 1)[-1] == str(sport):
                return uid
    return UNKNOWN


def windows_peer(client, server, api=None):
    """Windows: 'same' when the process that opened the connection runs as this login, 'other' when it runs as another (its
    token cannot be read, or names another account), None when Windows' tables cannot be read. IPv4 loopback only."""
    try:
        api = api or _WinApi()
        pid = api.pid_of(int(client[1]), int(server[1]))
        if pid is None:
            return 'other'   # the tables were read and nobody of ours holds that connection
        if pid == os.getpid():
            return 'same'
        sid = api.sid_of(pid)
        return 'same' if sid and sid == api.sid_of(os.getpid()) else 'other'
    except (OSError, AttributeError, ValueError):
        return None


class _WinApi:
    """GetExtendedTcpTable (the owning process of each IPv4 connection) and the process token's user SID, through ctypes."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.c, self.w = ctypes, wintypes
        self.iphlp, self.k32, self.adv = ctypes.WinDLL('iphlpapi'), ctypes.WinDLL('kernel32'), ctypes.WinDLL('advapi32')

    def pid_of(self, client_port, server_port):
        c, w = self.c, self.w
        size = w.DWORD(0)
        self.iphlp.GetExtendedTcpTable(None, c.byref(size), False, 2, 4, 0)   # AF_INET, TCP_TABLE_OWNER_PID_CONNECTIONS
        buf = c.create_string_buffer(size.value)
        if self.iphlp.GetExtendedTcpTable(buf, c.byref(size), False, 2, 4, 0) != 0:
            raise OSError('GetExtendedTcpTable failed')
        n = int.from_bytes(buf.raw[:4], 'little')
        for i in range(n):
            row = buf.raw[4 + i * 24:4 + (i + 1) * 24]   # state, local addr, local port, remote addr, remote port, pid
            lport = int.from_bytes(row[8:10], 'big')
            rport = int.from_bytes(row[16:18], 'big')
            if lport == client_port and rport == server_port:
                return int.from_bytes(row[20:24], 'little')
        return None

    def sid_of(self, pid):
        c, w = self.c, self.w
        h = self.k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        tok = w.HANDLE()
        try:
            if not self.adv.OpenProcessToken(h, 0x0008, c.byref(tok)):   # TOKEN_QUERY
                return None
            need = w.DWORD(0)
            self.adv.GetTokenInformation(tok, 1, None, 0, c.byref(need))   # TokenUser
            info = c.create_string_buffer(need.value)
            if not self.adv.GetTokenInformation(tok, 1, info, need, c.byref(need)):
                return None
            psid = c.cast(info, c.POINTER(c.c_void_p))[0]
            text = w.LPWSTR()
            if not self.adv.ConvertSidToStringSidW(c.c_void_p(psid), c.byref(text)):
                return None
            sid = text.value
            self.k32.LocalFree(text)
            return sid
        finally:
            if tok:
                self.k32.CloseHandle(tok)
            self.k32.CloseHandle(h)


def loopback_owner_ok(client, server, uid=None, proc='/proc', sleep=None, platform=None, run=None, winapi=None):
    """True only when the loopback client is a process of this login. Linux reads /proc/net/tcp, macOS asks lsof, Windows
    reads the TCP table and the process's token. A connection the tables were read for but do not show (looked for twice)
    is refused; only where nothing can be read at all (no lsof, Windows' API failing) it is True, as before."""
    import sys
    platform = platform or sys.platform
    if platform == 'win32':
        return windows_peer(client, server, winapi) != 'other'
    uid = os.getuid() if uid is None and hasattr(os, 'getuid') else uid
    if uid is None:
        return True

    def look():
        found = socket_uid(client, server, proc)
        return lsof_uid(client, server, run) if found is None and platform != 'linux' else found
    found = look()
    if found == UNKNOWN:
        (sleep or __import__('time').sleep)(0.05)
        found = look()
    return found is None or found == uid
