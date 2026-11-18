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
import time
import urllib.request

from . import __version__

REPO = 'ventura8/Tutti'
RELEASES = f'https://github.com/{REPO}/releases'
SUMS = 'SHA256SUMS'
BUNDLE = 'SHA256SUMS.sigstore.json'   # the release workflow's Sigstore attestation of SHA256SUMS (keyless, GitHub OIDC)
SIGNER = f'{REPO}/.github/workflows/release.yml'
SKIP_WARNING = ('WARNING: --insecure-skip-verify: the release\'s signature was NOT checked. Anyone who could change the '
                'files on the way (or on GitHub) could have changed what was installed. Use it only offline, on a release '
                'you checked another way.')
TARBALL = re.compile(r'^tutti-(\d+\.\d+\.\d+)\.tar\.gz$')
SERVICE = 'tutti-dashboard'                  # the systemd unit, the launchd label's tail and the scheduled task's name
LAUNCHD_LABEL = 'io.github.tutti.dashboard'
# Distribution channels. npm is a ready hook: once `tutti-agents` is published (the owner's 2FA), flip `published`.
CHANNELS = {
    'github': {'published': True, 'install': 'the bootstrap line (docs/INSTALL.md)', 'update': 'tutti self-update'},
    # a release tag, never the moving main branch (an update names the newer tag the same way)
    'uv': {'published': True, 'install': f'uv tool install git+https://github.com/{REPO}@v<the latest release> ({RELEASES}/latest)',
           'update': f'uv tool install --force git+https://github.com/{REPO}@v<the newer release>'},
    'pipx': {'published': True, 'install': f'pipx install git+https://github.com/{REPO}@v<the latest release> ({RELEASES}/latest)',
             'update': f'pipx install --force git+https://github.com/{REPO}@v<the newer release>'},
    'npm': {'published': False, 'install': 'npm install -g tutti-agents', 'update': 'npm update -g tutti-agents'},
}

SERVICES = {
    'tutti-dashboard': {'description': 'Tutti dashboard (http://127.0.0.1:4777/)',
                        'exec': 'dashboard', 'restart': 'on-failure'},
    'tutti-failover': {'description': 'Tutti failover',
                       'exec': 'failover run', 'restart': 'on-failure'},
    'tutti-scout': {'description': 'Tutti scout',
                    'exec': 'scout tick', 'restart': 'on-failure'},
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


def systemd_unit(name, tutti, info):
    """The systemd --user unit: update first (opt-out in the config), then the service; restarted when it fails."""
    exec_cmd = info['exec']
    return (f'[Unit]\nDescription={info["description"]}\nAfter=network-online.target\n\n'
            '[Service]\n'
            f"ExecStart=/bin/sh -c '\"{tutti}\" self-update --auto; exec \"{tutti}\" {exec_cmd}'\n"
            f'Restart={info["restart"]}\nRestartSec=10\nTimeoutStopSec=30\n\n[Install]\nWantedBy=default.target\n')


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
    plans, run = [], [['systemctl', '--user', 'daemon-reload']]
    for name, info in SERVICES.items():
        path = os.path.join(config_home, 'systemd', 'user', f'{name}.service')
        plans.append((path, systemd_unit(name, tutti, info)))
        run.append(['systemctl', '--user', 'enable', '--now', f'{name}.service'])
    run.append(['loginctl', 'enable-linger'])
    return plans, run


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


# --- the release signature (docs/INSTALL.md, *How a release is checked*) ---

def signed_refs(text):
    """The git refs the attestations in `gh attestation verify --format json` were made on: the certificate's
    sourceRepositoryRef or githubWorkflowRef, or the tail of its workflow identity (…/release.yml@refs/tags/v1.2.3)."""
    import json
    try:
        items = json.loads(text or '[]')
    except ValueError:
        return set()
    refs = set()
    for it in items if isinstance(items, list) else [items]:
        cert = (((it or {}).get('verificationResult') or {}).get('signature') or {}).get('certificate') or {}
        refs |= {cert.get(k) for k in ('sourceRepositoryRef', 'githubWorkflowRef') if isinstance(cert.get(k), str)}
        san = cert.get('subjectAlternativeName')
        if isinstance(san, str) and '@' in san:
            refs.add(san.rsplit('@', 1)[1])
    return {r for r in refs if r}


def gh_program(home_dir=None, run=subprocess.run):
    """GitHub's CLI that can verify signatures (`gh attestation`, gh 2.49+): the one on PATH, else the pinned copy the
    bootstrap put in <home>/bin when the PATH one is older; the first found when none can (its error then says why)."""
    found = [g for g in (shutil.which('gh'), shutil.which('gh', path=os.path.join(home_dir or home(), 'bin'))) if g]
    for g in dict.fromkeys(found):
        try:
            if run([g, 'attestation', 'verify', '--help'], capture_output=True, text=True, timeout=30).returncode == 0:
                return g
        except (OSError, subprocess.SubprocessError):
            continue
    return found[0] if found else None


def verify_signature(sums_path, bundle_path, version, gh=None, run=subprocess.run):
    """'' when SHA256SUMS carries a valid Sigstore signature by ventura8/Tutti's release workflow, made on the tag
    v<version>; else why not, in plain words, with what to do."""
    if not gh:
        return ('the release\'s signature cannot be checked: GitHub\'s CLI (gh) is not installed; run the one-line install '
                'again (it installs gh), or install gh from https://cli.github.com')
    try:
        r = run([gh, 'attestation', 'verify', sums_path, '--bundle', bundle_path, '--repo', REPO, '--signer-workflow', SIGNER,
                 '--format', 'json'], capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        return f'the release\'s signature could not be checked ({type(e).__name__}): check the network and try again'
    if r.returncode != 0:
        why = ((r.stderr or r.stdout or '').strip().splitlines() or ['no reason given'])[-1][:200]
        if 'unknown command' in why or 'unknown flag' in why:
            return f'gh is too old to check signatures ({why}): update it (gh 2.49 or newer, https://cli.github.com)'
        return (f'v{version}\'s SHA256SUMS is not signed by {SIGNER} ({why}): it was not made by Tutti\'s release '
                'workflow, or it was changed since; nothing was installed')
    want = f'refs/tags/v{version}'
    if want not in signed_refs(r.stdout):
        return (f'v{version}\'s SHA256SUMS is signed, but not on the tag {want} (signed on '
                f'{", ".join(sorted(signed_refs(r.stdout))) or "an unknown ref"}): nothing was installed')
    return ''


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
    # --require-hashes: every wheel is checked against the hash requirements.txt carries (docs/INSTALL.md)
    argv = [uv, 'pip', 'install', '--quiet', '--require-hashes', '--python', py, '-r', req] if uv else \
        [py, '-m', 'pip', 'install', '--quiet', '--require-hashes', '-r', req]
    return subprocess.run(argv, check=False, capture_output=True, text=True).returncode == 0


SUMS_WAITS = (15, 30, 60, 120)   # seconds: the release workflow uploads the assets a few minutes after the release appears


def fetch_sums(fetch_fn, waits=SUMS_WAITS, sleep=time.sleep):
    """The latest release's SHA256SUMS, asked again with backoff while it is missing: right after a release,
    releases/latest already points at it but its assets are still being uploaded (a 404 for a few minutes)."""
    for wait in (*waits, None):
        try:
            return fetch_fn(f'{RELEASES}/latest/download/{SUMS}')
        except OSError as e:
            if wait is None or getattr(e, 'code', None) != 404:   # offline or another error: say so at once
                raise
            sleep(wait)


def self_update(home_dir=None, current=__version__, fetch_fn=fetch, check_only=False, skip_verify=False, verify_fn=None,
                sleep=time.sleep):
    """(code, message). Installs the newest release when it is newer than `current`: sums, their signature (Sigstore, by
    the release workflow on the release's tag), download, checksum, unpack, requirements, then `current` moves. Any failure
    leaves the running version in place. `skip_verify` (--insecure-skip-verify) skips the signature, loudly."""
    home_dir = home_dir or home()
    try:
        sums_text = fetch_sums(fetch_fn, sleep=sleep)
        sums = parse_sums(sums_text)
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
        why = signature_refusal(tmp, sums_text, latest, fetch_fn, skip_verify, verify_fn or
                                (lambda s, b, v: verify_signature(s, b, v, gh_program(home_dir))))
        if why:
            return 1, f'self-update: {why}; {current} stays'
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
    warn = f' {SKIP_WARNING}' if skip_verify else ''
    return 0, f'self-update: {current} -> {latest} (restart the dashboard, or it restarts itself within a day).{warn}'


def signature_refusal(tmp, sums_text, version, fetch_fn, skip_verify, verify_fn):
    """'' when SHA256SUMS is signed (or the check is skipped on purpose), else why."""
    if skip_verify:
        return ''
    sums_path, bundle_path = os.path.join(tmp, SUMS), os.path.join(tmp, BUNDLE)
    with open(sums_path, 'w', encoding='utf8', newline='') as f:
        f.write(sums_text)
    try:
        fetch_fn(f'{RELEASES}/download/v{version}/{BUNDLE}', bundle_path)
    except OSError as e:
        return (f'v{version} has no signature file {BUNDLE} ({e}): it was not published by Tutti\'s release workflow; '
                'nothing was installed (offline and sure of the files? `tutti self-update --insecure-skip-verify`)')
    return verify_fn(sums_path, bundle_path, version)


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
