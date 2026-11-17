"""One adb host per device: a runner machine reaches the devices through their own machine's adb server.

The devices are paired and connected only on the machine they live on (the NUC); a runner with
`adb_via_device_host: true` (the PC's PowerShell runner through WSL) gets an ssh tunnel to that machine's adb server and
ADB_SERVER_SOCKET, so its adb client lists and drives the same connections and holds no connection of its own (owner,
2026-11-16: "move the pairing of all devices to the NUC"; `tutti devices move` sets the flag, docs/DEVICES.md). Both ends
must run the same adb version, or the client restarts the server: here that would be the NUC's."""
import re
import shlex

PORT = 5038          # the runner's end of the tunnel; the device machine's adb server listens on 127.0.0.1:5037
SERVER_PORT = 5037
SOCKET = f'tcp:127.0.0.1:{PORT}'


def host_of(cfg, runner, devices):
    """The machine whose adb server the runner uses for these devices, or '' (the runner's own adb, as before): only when
    the runner asks for it and every device lives on that one other machine."""
    machines = cfg.get('machines') or {}
    if not (machines.get(runner) or {}).get('adb_via_device_host'):
        return ''
    hosts = {d.get('machine') for d in devices}
    if len(hosts) != 1:
        return ''
    host = next(iter(hosts))
    return host if host and host != runner and host in machines else ''


def ssh_target(cfg, host):
    """The name the runner's own ssh.exe reaches the device machine by (its `host:`, an ssh alias on every machine)."""
    m = (cfg.get('machines') or {}).get(host) or {}
    return str(m.get('host') or host)


def windows_tunnel(target, port=PORT):
    """A PowerShell line for a Windows runner: starts a hidden ssh tunnel (it exits by itself when the forward fails or the
    link dies) unless something already listens on the port, then waits up to 10 s for it."""
    args = ','.join(f"'{x}'" for x in ('-N', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=30',
                                        '-o', 'ServerAliveCountMax=3', '-L', f'{port}:127.0.0.1:{SERVER_PORT}', target))
    return (f'if (-not (Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue)) '
            f'{{ Start-Process ssh.exe -ArgumentList {args} -WindowStyle Hidden; '
            f'for ($i = 0; $i -lt 20 -and -not (Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue); $i++) '
            '{ Start-Sleep -Milliseconds 500 } }')


def prefix(cfg, host, port=PORT):
    """The shell prefix of a runner command: the tunnel up, ADB_SERVER_SOCKET exported and passed on to Windows programs
    (WSLENV), so `adb` there talks to the device machine's server."""
    up = 'powershell.exe -NoProfile -NonInteractive -Command ' + shlex.quote(windows_tunnel(ssh_target(cfg, host), port))
    return (f'{up} > /dev/null 2>&1; export ADB_SERVER_SOCKET=tcp:127.0.0.1:{port} '
            'WSLENV="${WSLENV:+$WSLENV:}ADB_SERVER_SOCKET"; ')


INLINE = re.compile(r'(?<![\w$])WSLENV=([A-Za-z0-9_:/]*)(?=\s)')


def with_socket(command):
    """The command with ADB_SERVER_SOCKET added to every inline `WSLENV=...` list it sets (an inline list replaces the
    exported one for that program: RoRadio's suites set WSLENV=ANDROID_SERIAL:...)."""
    def add(m):
        names = [n for n in m.group(1).split(':') if n]
        if 'ADB_SERVER_SOCKET' in [n.split('/')[0] for n in names]:
            return m.group(0)
        return 'WSLENV=' + ':'.join(names + ['ADB_SERVER_SOCKET'])
    return INLINE.sub(add, command)
