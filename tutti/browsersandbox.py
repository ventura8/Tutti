"""The isolated work browser (browser.mode: isolated, docs/BROWSER.md): a fresh Chrome used only by Tutti, isolated from
the rest of the machine. Made for the NUC after the move; the laptop keeps the owner's existing Chrome.

- It runs as its own unprivileged system user, `tutti-browser`, in a systemd unit with the sandbox below: no home folders
  (ProtectHome), a private /tmp, the whole filesystem read-only except its state folder (/var/lib/tutti-browser: the
  profile and the downloads), no new privileges, IPv4/IPv6/Unix/netlink sockets only, and no local addresses
  (IPAddressDeny of loopback and the private ranges) except the resolver and browser.allow_local.
- It has no TCP DevTools port at all: Chrome speaks DevTools on a pipe (--remote-debugging-pipe) to a small bridge in the
  same unit, and the bridge offers it on one Unix socket (/run/tutti-browser/devtools.sock, mode 0660, group
  tutti-broker; its folder 0711, so it can be reached but not listed). Only the members of tutti-broker (the user Tutti
  runs as) can connect. The profile files stay 0770 tutti-browser, but a DevTools client on the socket can still ask the
  browser for cookies: the broker is a policy layer, the sandbox is the boundary (docs/BROWSER.md).
- Downloads go to /var/lib/tutti-browser/downloads (2770, group tutti-broker), and a worker gets a file only by naming it
  in a task whose type may take files (browserbroker.Broker.take, browser.file_tasks).

The one-time root setup is one command for the owner (`tutti browser isolate` prints it): it creates the user, the group,
the bridge and the unit, and starts it. Tutti never runs it itself."""
import os
import shlex

USER = 'tutti-browser'
GROUP = 'tutti-broker'
STATE = '/var/lib/tutti-browser'
LIB = '/usr/local/lib/tutti-browser'
UNIT = '/etc/systemd/system/tutti-browser.service'
SOCKET = '/run/tutti-browser/devtools.sock'
RESOLVER = '127.0.0.53/32'   # systemd-resolved's stub: DNS keeps working with every other local address denied
DENY = ('localhost', 'link-local', 'multicast', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', 'fc00::/7')

# The sandbox, one directive per line; tests/test_browser.py checks each one is in the unit.
SANDBOX = (
    f'User={USER}', f'Group={USER}', f'SupplementaryGroups={GROUP}',
    'NoNewPrivileges=yes', 'ProtectSystem=strict', 'ProtectHome=yes', 'PrivateTmp=yes', 'PrivateDevices=yes',
    'ProtectKernelTunables=yes', 'ProtectKernelModules=yes', 'ProtectKernelLogs=yes', 'ProtectControlGroups=yes',
    'ProtectClock=yes', 'ProtectHostname=yes', 'RestrictSUIDSGID=yes', 'LockPersonality=yes', 'RestrictRealtime=yes',
    'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK', 'StateDirectory=tutti-browser', 'StateDirectoryMode=0711',
    'RuntimeDirectory=tutti-browser', 'RuntimeDirectoryMode=0711', f'ReadWritePaths={STATE}', 'UMask=0007',
    'CapabilityBoundingSet=', 'AmbientCapabilities=',
)

# Runs inside the unit as tutti-browser: Chrome on a DevTools pipe, offered on the Unix socket to one client at a time.
BRIDGE = r'''import grp, os, signal, socket, subprocess, sys, threading
sock_path, group = sys.argv[1], sys.argv[2]
r_in, w_in = os.pipe()     # bridge -> Chrome (Chrome reads fd 3)
r_out, w_out = os.pipe()   # Chrome -> bridge (Chrome writes fd 4)
def child():
    os.dup2(r_in, 3)
    os.dup2(w_out, 4)
chrome = subprocess.Popen(sys.argv[3:], pass_fds=(3, 4), preexec_fn=child, stdin=subprocess.DEVNULL)
def stop(_signum, _frame):
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
os.close(r_in)
os.close(w_out)
if os.path.exists(sock_path):
    os.remove(sock_path)
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock_path)
os.chown(sock_path, -1, grp.getgrnam(group).gr_gid)   # only tutti-broker's members (Tutti) may connect
os.chmod(sock_path, 0o660)
srv.listen(1)
client = {'c': None}
def from_chrome():
    while True:
        data = os.read(r_out, 65536)
        if not data:
            os._exit(1)
        c = client['c']
        if c is not None:
            try:
                c.sendall(data)
            except OSError:
                pass
def relay():
    threading.Thread(target=from_chrome, daemon=True).start()
    while chrome.poll() is None:
        c, _ = srv.accept()
        client['c'] = c
        with c:
            while data := c.recv(65536):
                os.write(w_in, data)
        client['c'] = None
try:
    relay()
finally:
    chrome.terminate()
    try:
        chrome.wait(timeout=2)
    except subprocess.TimeoutExpired:
        chrome.kill()
        chrome.wait()
'''


def chrome_argv(chrome):
    return [chrome, '--headless=new', '--remote-debugging-pipe', f'--user-data-dir={STATE}/profile', '--no-first-run',
            '--no-default-browser-check', '--disable-background-networking', 'about:blank']


def unit(chrome, allow_local=()):
    """The systemd unit text."""
    allow = ' '.join([RESOLVER, *allow_local])
    start = ' '.join(shlex.quote(x) for x in ['/usr/bin/python3', f'{LIB}/bridge.py', SOCKET, GROUP, *chrome_argv(chrome)])
    return '\n'.join([
        '[Unit]', 'Description=Tutti work browser (isolated Chrome, docs/BROWSER.md)', 'After=network-online.target', '',
        '[Service]', f'ExecStart={start}', 'Restart=on-failure', 'RestartSec=5', *SANDBOX,
        f'IPAddressDeny={" ".join(DENY)}', f'IPAddressAllow={allow}', '',
        '[Install]', 'WantedBy=multi-user.target', '']) + '\n'


def setup_script(chrome, broker_user, allow_local=()):
    """The one-time root setup: the user, the group, the bridge, the unit, the downloads folder; then started."""
    q = shlex.quote
    return '\n'.join([
        '#!/usr/bin/env bash', '# Tutti work browser, isolated mode: one-time setup, run with sudo (docs/BROWSER.md).', 'set -euo pipefail',
        f'id -u {USER} >/dev/null 2>&1 || useradd --system --home-dir {STATE} --no-create-home --shell /usr/sbin/nologin {USER}',
        f'getent group {GROUP} >/dev/null || groupadd --system {GROUP}',
        f'usermod -aG {GROUP} {q(broker_user)}',
        f'install -d -m 0755 {LIB}',
        f"cat > {LIB}/bridge.py <<'TUTTI_BRIDGE'", BRIDGE.rstrip('\n'), 'TUTTI_BRIDGE',
        f"cat > {UNIT} <<'TUTTI_UNIT'", unit(chrome, allow_local).rstrip('\n'), 'TUTTI_UNIT',
        f'install -d -o {USER} -g {USER} -m 0711 {STATE}',
        f'install -d -o {USER} -g {GROUP} -m 2770 {STATE}/downloads',
        'systemctl daemon-reload', 'systemctl enable --now tutti-browser.service',
        f'echo "work browser: running as {USER}; {q(broker_user)} reaches it on {SOCKET} (sign in again to pick up the group)"', ''])


def write_setup(state_dir, chrome, broker_user, allow_local=()):
    """Writes the setup script into Tutti's state folder (0700) and returns the one command the owner runs."""
    path = os.path.join(state_dir, 'tutti-browser-setup.sh')
    os.makedirs(state_dir, exist_ok=True)
    with open(path, 'w', encoding='utf8') as f:
        f.write(setup_script(chrome, broker_user, allow_local))
    os.chmod(path, 0o700)
    return f'sudo bash {shlex.quote(path)}'


def sign_in_command(chrome, url):
    """The one command for a sign-in on the isolated browser: the service stops, the same profile opens in a visible window as
    tutti-browser on this display (allowed for that user only, then taken back), and the service starts again."""
    q = shlex.quote
    return (f'xhost +SI:localuser:{USER} && sudo systemctl stop tutti-browser && sudo -u {USER} {q(chrome)} '
            f'--user-data-dir={STATE}/profile --no-first-run {q(url)}; xhost -SI:localuser:{USER}; sudo systemctl start tutti-browser')
