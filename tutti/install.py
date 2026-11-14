"""Install, layout, checksums, self-update and the login service: the shared module of `tutti setup`, the bootstrap
scripts and `tutti household join` (docs/INSTALL.md, *The interface*).

A release install lives in $TUTTI_HOME (default ~/.local/share/tutti, %LOCALAPPDATA%\\tutti on Windows):

    versions/<version>/   one unpacked release tarball each (tutti-<version>.tar.gz of the GitHub release)
    current               a symlink (a junction on Windows) to the version in use
    venv/                 the Python environment every version runs in (uv-made, PyYAML only)

and the `tutti` command (~/.local/bin/tutti, or %LOCALAPPDATA%\\tutti\\bin\\tutti.cmd) runs current/bin/tutti. Every
download is checked against the release's SHA256SUMS before it is unpacked; nothing unverified is ever run.
Pure helpers (sums, versions, service files, install lines) are unit-tested; the I/O sits in the few functions below them."""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

from . import __version__

REPO = 'ventura8/Tutti'
RELEASES = f'https://github.com/{REPO}/releases'
SUMS = 'SHA256SUMS'
TARBALL = re.compile(r'^tutti-(\d+\.\d+\.\d+)\.tar\.gz$')
SERVICE = 'tutti-dashboard'                  # the systemd unit, the launchd label's tail and the scheduled task's name
LAUNCHD_LABEL = 'io.github.tutti.dashboard'
# Distribution channels. npm is a ready hook: once `tutti-agents` is published (the owner's 2FA), flip `published`.
CHANNELS = {
    'github': {'published': True, 'install': 'the bootstrap line (docs/INSTALL.md)', 'update': 'tutti self-update'},
    'uv': {'published': True, 'install': f'uv tool install git+https://github.com/{REPO}', 'update': 'uv tool upgrade tutti-agents'},
    'pipx': {'published': True, 'install': f'pipx install git+https://github.com/{REPO}', 'update': 'pipx upgrade tutti-agents'},
    'npm': {'published': False, 'install': 'npm install -g tutti-agents', 'update': 'npm update -g tutti-agents'},
}


def home(env=None, windows=None):
    """The install folder: $TUTTI_HOME, else the per-OS default."""
    env = os.environ if env is None else env
    windows = os.name == 'nt' if windows is None else windows
    if env.get('TUTTI_HOME'):
        return env['TUTTI_HOME']
    if windows:
        return os.path.join(env.get('LOCALAPPDATA', os.path.expanduser('~')), 'tutti')
    return os.path.join(os.path.expanduser('~'), '.local', 'share', 'tutti')


def parse_sums(text):
    """{file name: sha256} from a SHA256SUMS text (`<hex>  <name>` or `<hex> *<name>` per line)."""
    out = {}
    for line in (text or '').splitlines():
        m = re.match(r'^([0-9a-fA-F]{64}) [ *](\S+)$', line.strip())
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 16), b''):
            h.update(block)
    return h.hexdigest()


def verify(path, sums, name=None):
    """None when the file's SHA-256 is the one SHA256SUMS lists for it, else the reason (never install on a reason)."""
    name = name or os.path.basename(path)
    want = sums.get(name)
    if not want:
        return f'{name} is not listed in {SUMS}: the release is incomplete, so nothing was installed'
    got = sha256_of(path)
    if got != want:
        return f'{name}: SHA-256 {got[:12]}... is not the published {want[:12]}...: a broken or altered download, nothing was installed'
    return None


def newest(sums):
    """The newest version a SHA256SUMS lists a tarball of, or None."""
    versions = [m.group(1) for m in map(TARBALL.match, sums) if m]
    return max(versions, key=vtuple) if versions else None


def vtuple(v):
    return tuple(int(x) for x in re.findall(r'\d+', v or '0')[:3])


def is_release_install(root, home_dir):
    """True when the running Tutti is a release install under home_dir (a git checkout updates with git, pipx with pipx)."""
    try:
        base = os.path.normcase(os.path.realpath(os.path.join(home_dir, 'versions'))) + os.sep
        return os.path.normcase(os.path.realpath(root)).startswith(base)   # normcase: Windows paths differ in case
    except OSError:
        return False


# --- the login service: pure text per OS ---

def tutti_command(home_dir, windows=False):
    if windows:
        return os.path.join(home_dir, 'bin', 'tutti.cmd')
    return os.path.join(os.path.expanduser('~'), '.local', 'bin', 'tutti')


def systemd_unit(tutti):
    """The systemd --user unit: update first (opt-out in the config), then the dashboard; restarted when it fails."""
    return ('[Unit]\nDescription=Tutti dashboard (http://127.0.0.1:4777/)\nAfter=network-online.target\n\n'
            '[Service]\n'
            f"ExecStart=/bin/sh -c '\"{tutti}\" self-update --auto; exec \"{tutti}\" dashboard'\n"
            'Restart=on-failure\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n')


def launchd_plist(tutti, log):
    """The launchd agent (~/Library/LaunchAgents): at login, kept alive when it fails."""
    cmd = f'"{tutti}" self-update --auto; exec "{tutti}" dashboard'
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            f'  <key>Label</key><string>{LAUNCHD_LABEL}</string>\n'
            '  <key>ProgramArguments</key>\n  <array><string>/bin/sh</string><string>-lc</string>'
            f'<string>{xml_escape(cmd)}</string></array>\n'
            '  <key>RunAtLoad</key><true/>\n'
            '  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>\n'
            f'  <key>StandardOutPath</key><string>{xml_escape(log)}</string>\n'
            f'  <key>StandardErrorPath</key><string>{xml_escape(log)}</string>\n'
            '</dict>\n</plist>\n')


def xml_escape(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def windows_launcher(tutti):
    """(dashboard.cmd, dashboard.vbs): the scheduled task runs the .vbs, which starts the .cmd with no console window."""
    cmd = f'@echo off\r\ncall "{tutti}" self-update --auto\r\ncall "{tutti}" dashboard\r\n'
    launcher = os.path.join(os.path.dirname(tutti), 'dashboard.cmd')
    vbs = f'CreateObject("WScript.Shell").Run """" & "{launcher}" & """", 0, False\r\n'
    return cmd, vbs


def schtasks_args(vbs):
    """The scheduled task: at this user's logon, with the user's own rights (no admin)."""
    return ['schtasks', '/Create', '/F', '/SC', 'ONLOGON', '/RL', 'LIMITED', '/TN', SERVICE, '/TR', f'wscript.exe "{vbs}"']


def service_plan(platform, home_dir, env=None, tutti=None):
    """[(path, text)] to write and [argv] to run to start the dashboard at login on `platform` (linux, wsl, darwin, win32).
    `tutti`: the command the service runs (default: the release install's)."""
    env = os.environ if env is None else env
    windows = platform == 'win32'
    tutti = tutti or tutti_command(home_dir, windows)
    if windows:
        cmd, vbs = windows_launcher(tutti)
        cmd_path, vbs_path = os.path.join(home_dir, 'bin', 'dashboard.cmd'), os.path.join(home_dir, 'bin', 'dashboard.vbs')
        return [(cmd_path, cmd), (vbs_path, vbs)], [schtasks_args(vbs_path)]
    if platform == 'darwin':
        path = os.path.join(os.path.expanduser('~'), 'Library', 'LaunchAgents', f'{LAUNCHD_LABEL}.plist')
        log = os.path.join(os.path.expanduser('~'), 'Library', 'Logs', 'tutti-dashboard.log')
        return [(path, launchd_plist(tutti, log))], [['launchctl', 'unload', path], ['launchctl', 'load', '-w', path]]
    config_home = env.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    path = os.path.join(config_home, 'systemd', 'user', f'{SERVICE}.service')
    return [(path, systemd_unit(tutti))], [['systemctl', '--user', 'daemon-reload'],
                                           ['systemctl', '--user', 'enable', '--now', f'{SERVICE}.service']]


def platform_name(env=None):
    """linux, wsl, darwin or win32."""
    env = os.environ if env is None else env
    if sys.platform.startswith('linux') and (env.get('WSL_DISTRO_NAME') or 'microsoft' in os.uname().release.lower()):
        return 'wsl'
    return 'linux' if sys.platform.startswith('linux') else sys.platform


# --- the one-line install, per OS, for docs, the household's invite page and the dashboard ---

def bootstrap_sha(name, root=None):
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return sha256_of(os.path.join(root, 'install', name))


def install_line(platform, join=None, root=None):
    """The exact one-line install for `platform`: download the bootstrap, check its SHA-256, run it. `join`: a household
    invite, passed through to `tutti setup --join` (the member's machine is set up and paired in one go)."""
    if platform == 'win32':
        sha = bootstrap_sha('bootstrap.ps1', root).upper()
        extra = f' -Join "{join}"' if join else ''
        return (f"irm {RELEASES}/latest/download/bootstrap.ps1 -OutFile tutti-bootstrap.ps1; "
                f"if ((Get-FileHash tutti-bootstrap.ps1).Hash -eq '{sha}') "
                f"{{ powershell -ExecutionPolicy Bypass -File tutti-bootstrap.ps1{extra} }} else {{ 'tutti: checksum mismatch, not run' }}")
    sha = bootstrap_sha('bootstrap.sh', root)
    extra = f' --join "{join}"' if join else ''
    return (f'curl -fsSLo tutti-bootstrap.sh {RELEASES}/latest/download/bootstrap.sh && '
            f'echo "{sha}  tutti-bootstrap.sh" | (sha256sum -c - || shasum -a 256 -c -) && bash tutti-bootstrap.sh{extra}')


# --- self-update (I/O) ---

def fetch(url, dest=None, timeout=60):
    """The body of a GitHub URL (str), or saved to `dest`."""
    req = urllib.request.Request(url, headers={'User-Agent': f'tutti/{__version__}'})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # only github.com release URLs are fetched
        if dest is None:
            return r.read().decode('utf8')
        with open(dest, 'wb') as f:
            shutil.copyfileobj(r, f)
        return dest


def unpack(tarball, target):
    """Unpack a verified tarball into target (its one top folder dropped), refusing links and paths outside it."""
    tmp = tempfile.mkdtemp(dir=os.path.dirname(target))
    with tarfile.open(tarball) as t:
        if hasattr(tarfile, 'data_filter'):
            t.extractall(tmp, filter='data')
        else:   # Python before 3.10.12 / 3.11.4: the same refusals by hand
            for m in t.getmembers():
                if m.issym() or m.islnk() or os.path.isabs(m.name) or '..' in m.name.replace('\\', '/').split('/'):
                    raise OSError(f'{os.path.basename(tarball)}: {m.name} is a link or leaves the folder; nothing was installed')
            t.extractall(tmp)
    tops = os.listdir(tmp)
    src = os.path.join(tmp, tops[0]) if len(tops) == 1 else tmp
    os.replace(src, target)
    shutil.rmtree(tmp, ignore_errors=True)


def point_current(home_dir, version):
    """Moves `current` to versions/<version> in one step (a symlink renamed over the old; a junction on Windows)."""
    target = os.path.join(home_dir, 'versions', version)
    link = os.path.join(home_dir, 'current')
    if os.name == 'nt':
        import _winapi  # CPython's own junction call: no admin rights needed, unlike a symlink
        if os.path.lexists(link):
            os.rmdir(link)
        _winapi.CreateJunction(target, link)
        return
    tmp = link + '.new'
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link)


def requirements(home_dir, version_dir):
    """Installs the version's requirements into the shared venv (uv when present, else pip)."""
    py = os.path.join(home_dir, 'venv', 'Scripts' if os.name == 'nt' else 'bin', 'python')
    req = os.path.join(version_dir, 'requirements.txt')
    uv = shutil.which('uv') or shutil.which('uv', path=os.path.join(home_dir, 'bin'))
    argv = [uv, 'pip', 'install', '--quiet', '--python', py, '-r', req] if uv else [py, '-m', 'pip', 'install', '--quiet', '-r', req]
    return subprocess.run(argv, check=False, capture_output=True, text=True).returncode == 0


def self_update(home_dir=None, current=__version__, fetch_fn=fetch, check_only=False):
    """(code, message). Installs the newest release when it is newer than `current`: sums, download, verify, unpack,
    requirements, then `current` moves. Any failure leaves the running version in place."""
    home_dir = home_dir or home()
    try:
        sums = parse_sums(fetch_fn(f'{RELEASES}/latest/download/{SUMS}'))
    except OSError as e:
        return 1, f'self-update: could not read {RELEASES}/latest ({e}); offline? It tries again at the next login.'
    latest = newest(sums)
    if not latest:
        return 1, f'self-update: the latest release lists no tutti-<version>.tar.gz in {SUMS}; nothing changed'
    if vtuple(latest) <= vtuple(current):
        return 0, f'self-update: {current} is the newest'
    if check_only:
        return 0, f'self-update: {latest} is out (this is {current}); run `tutti self-update` to install it'
    name = f'tutti-{latest}.tar.gz'
    os.makedirs(os.path.join(home_dir, 'versions'), exist_ok=True)
    with tempfile.TemporaryDirectory(dir=home_dir) as tmp:
        path = os.path.join(tmp, name)
        try:
            fetch_fn(f'{RELEASES}/download/v{latest}/{name}', path)
        except OSError as e:
            return 1, f'self-update: could not download {name} ({e}); {current} stays'
        bad = verify(path, sums)
        if bad:
            return 1, f'self-update: {bad}; {current} stays'
        target = os.path.join(home_dir, 'versions', latest)
        if not os.path.isdir(target):
            unpack(path, target)
    if not requirements(home_dir, target):
        return 1, f'self-update: installing the Python requirements of {latest} failed; {current} stays'
    point_current(home_dir, latest)
    return 0, f'self-update: {current} -> {latest} (restart the dashboard, or it restarts itself within a day)'


DAY = 24 * 3600


def start_daily(cfg, stop, root, restart, update=self_update, wait=DAY):
    """The dashboard's daily self-update (release installs with self_update.enabled): a new version restarts the dashboard
    in place (same pid, so a service manager sees no exit), and the page reloads itself for the new version (update.js).
    Returns the thread, or None when it does not apply."""
    import threading
    home_dir = home()
    if not (cfg.get('self_update') or {}).get('enabled', True) or not is_release_install(root, home_dir):
        return None

    def loop():
        while not stop.wait(wait):
            code, msg = update(home_dir)
            if code == 0 and '->' in msg:
                restart()
    t = threading.Thread(target=loop, name='tutti-self-update', daemon=True)
    t.start()
    return t
