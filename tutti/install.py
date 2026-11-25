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


def systemd_unit(tutti, config_path=None):
    """The systemd --user unit: update first (opt-out in the config), then the dashboard; restarted when it fails."""
    # Only simple absolute paths can be embedded safely in systemd's single-quoted shell command.
    def safe(value):
        return bool(value and os.path.isabs(value) and re.fullmatch(r'/[A-Za-z0-9_./-]+', value))
    if config_path is not None and not (safe(tutti) and safe(config_path)):
        raise ValueError('dashboard service command or config path cannot be safely pinned: use absolute paths '
                         'containing only letters, numbers, /, ., _, or - and run `tutti setup` again')
    pinned = f' --config "{config_path}"' if config_path is not None else ''
    return ('[Unit]\nDescription=Tutti dashboard (http://127.0.0.1:4777/)\nAfter=network-online.target\n\n'
            '[Service]\n'
            f"ExecStart=/bin/sh -c '\"{tutti}\"{pinned} self-update --auto; exec \"{tutti}\"{pinned} dashboard'\n"
            'Restart=always\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n')


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


def service_plan(platform, home_dir, env=None, tutti=None, config_path=None):
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
    # linger: the user's services start at boot, before anyone logs in, so a power cut or a reboot resumes work by itself
    return [(path, systemd_unit(tutti, config_path))], [['systemctl', '--user', 'daemon-reload'],
                                           ['systemctl', '--user', 'enable', '--now', f'{SERVICE}.service'],
                                           ['loginctl', 'enable-linger']]


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


# --- self-update of a source (git) checkout ---

def is_git_checkout(root):
    """True when the running Tutti's folder is a git checkout (a `.git` folder, or the file a worktree has)."""
    return os.path.exists(os.path.join(root, '.git'))


def _git(root, *args, run=subprocess.run):
    """(code, output) of one git command in `root`; a missing git or a hang is a code 1 with its reason."""
    try:
        r = run(['git', '-C', root, *args], capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, f'git {args[0]} could not run ({e})'
    return r.returncode, (r.stdout or '').strip() or (r.stderr or '').strip()


def git_refusal(root, run=subprocess.run):
    """'' when the checkout may be updated by `git pull --ff-only`, else why not and what to do. Only a clean checkout on its
    default branch (origin's HEAD, else main or master) that tracks origin is touched: nothing of the owner's is overwritten."""
    code, branch = _git(root, 'symbolic-ref', '--short', 'HEAD', run=run)
    if code:
        return f'{root} is on a detached HEAD: check out its default branch first (git switch main)'
    code, upstream = _git(root, 'rev-parse', '--abbrev-ref', '@{u}', run=run)
    if code or not upstream.startswith('origin/'):
        return f'{root} ({branch}) does not track a branch of origin: set it with `git branch --set-upstream-to=origin/{branch}`'
    code, head = _git(root, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD', run=run)
    default = head.split('/', 1)[1] if not code and '/' in head else None
    if default is None:
        default = next((n for n in ('main', 'master') if _git(root, 'rev-parse', '--verify', '-q', f'refs/remotes/origin/{n}',
                                                               run=run)[0] == 0), None)
    if branch != default:
        return (f'{root} is on branch {branch}, not its default branch ({default or "unknown"}): Tutti updates only the default '
                f'branch, so work in progress is never touched; switch with `git switch {default or "main"}` to update')
    code, dirty = _git(root, 'status', '--porcelain', '--untracked-files=no', run=run)
    if code or dirty:
        return f'{root} has local changes: commit or stash them (git status shows which), then run `tutti self-update` again'
    return ''


def git_update(root, check_only=False, run=subprocess.run):
    """(code, message): `git pull --ff-only` in a clean checkout of the default branch that tracks origin (docs/INSTALL.md,
    *Updates*). A running dashboard restarts itself onto the new code (start_code_watch); anything else starts the new code
    next time. A refusal or a failed git command changes nothing."""
    why = git_refusal(root, run)
    if why:
        return 1, f'self-update: {why}'
    code, out = _git(root, 'fetch', '--quiet', 'origin', run=run)
    if code:
        return 1, f'self-update: git fetch failed in {root} ({out}); offline? Nothing changed; it tries again next time'
    code, behind = _git(root, 'rev-list', '--count', 'HEAD..@{u}', run=run)
    if code or not behind.isdigit():
        return 1, f'self-update: could not compare {root} with origin ({behind}); nothing changed'
    if behind == '0':
        return 0, f'self-update: {root} is up to date with origin'
    if check_only:
        return 0, f'self-update: origin is {behind} commit(s) ahead of {root}; run `tutti self-update` to pull them'
    code, out = _git(root, 'pull', '--ff-only', '--quiet', run=run)
    if code:
        return 1, (f'self-update: git pull --ff-only failed in {root} ({out}); nothing changed. The checkout has commits origin '
                   'lacks: rebase or reset it by hand')
    return 0, (f'self-update: pulled {behind} commit(s) into {root} (a running dashboard restarts itself onto the new code '
               'within a minute, once Chat is idle)')


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


def stage_env(env=None):
    """Do not let pip, uv or Python environment overrides redirect staging into an existing environment."""
    source = os.environ if env is None else env
    out = {k: v for k, v in source.items() if not k.upper().startswith(('PIP_', 'UV_', 'PYTHON')) and k.upper() != 'VIRTUAL_ENV'}
    return {**out, 'PIP_CONFIG_FILE': os.devnull, 'UV_NO_CONFIG': '1'}


def create_stage_venv(path):
    return subprocess.run([sys.executable, '-I', '-m', 'venv', path], check=False, capture_output=True, text=True,
                          env=stage_env()).returncode == 0


def stage_requirements(stage, version_dir):
    """Install pinned requirements only into the staged venv, without user/global package-manager configuration."""
    py = os.path.join(stage, 'venv', 'Scripts' if os.name == 'nt' else 'bin', 'python')
    req = os.path.join(version_dir, 'requirements.txt')
    uv = shutil.which('uv')
    argv = ([uv, 'pip', 'install', '--quiet', '--require-hashes', '--only-binary=:all:', '--python', py, '-r', req] if uv else
            [py, '-m', 'pip', 'install', '--quiet', '--require-hashes', '--only-binary=:all:', '-r', req])
    return subprocess.run(argv, check=False, capture_output=True, text=True, cwd=stage, env=stage_env()).returncode == 0


def stage_release(home_dir, tarball, version, create_venv=None, install_requirements=None):
    """Prepare a verified release and its own environment without touching current or the shared environment."""
    create_venv = create_venv or create_stage_venv
    install_requirements = install_requirements or stage_requirements
    stage = tempfile.mkdtemp(prefix=f'staged-{version}-', dir=home_dir)
    complete = False
    try:
        code = os.path.join(stage, 'release')
        unpack(tarball, code)
        if not create_venv(os.path.join(stage, 'venv')) or not install_requirements(stage, code):
            raise OSError('the isolated Python environment could not be prepared')
        complete = True
    except (OSError, tarfile.TarError, ValueError) as e:
        return None, f'staging v{version} failed ({e}): the active installation stays unchanged'
    finally:
        if not complete:
            shutil.rmtree(stage, ignore_errors=True)
    return stage, None


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
    return 0, f'self-update: {current} -> {latest} (a running dashboard restarts itself within a minute, once Chat is idle).{warn}'


def stage_update(home_dir=None, current=__version__, fetch_fn=fetch, verify_fn=None, sleep=time.sleep):
    """Verify and stage a newer release in an isolated environment; never switch the active installation."""
    home_dir = home_dir or home()
    try:
        sums_text = fetch_sums(fetch_fn, sleep=sleep)
        sums = parse_sums(sums_text)
    except OSError as e:
        return 1, f'self-update: could not read {RELEASES}/latest ({e}); the active installation stays unchanged'
    latest = newest(sums)
    if not latest:
        return 1, f'self-update: the latest release lists no tutti-<version>.tar.gz in {SUMS}; nothing changed'
    if vtuple(latest) <= vtuple(current):
        return 0, f'self-update: {current} is the newest; nothing staged'
    os.makedirs(home_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=home_dir) as tmp:
        why = signature_refusal(tmp, sums_text, latest, fetch_fn, False, verify_fn or
                                (lambda s, b, v: verify_signature(s, b, v, gh_program(home_dir))))
        if why:
            return 1, f'self-update: {why}; the active installation stays unchanged'
        name, path = f'tutti-{latest}.tar.gz', os.path.join(tmp, f'tutti-{latest}.tar.gz')
        try:
            fetch_fn(f'{RELEASES}/download/v{latest}/{name}', path)
        except OSError as e:
            return 1, f'self-update: could not download {name} ({e}); the active installation stays unchanged'
        bad = verify(path, sums)
        if bad:
            return 1, f'self-update: {bad}; the active installation stays unchanged'
        staged, problem = stage_release(home_dir, path, latest)
    return (1, f'self-update: {problem}') if problem else \
        (0, f'self-update: v{latest} staged at {staged}; the active installation is unchanged')


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
# After a version is installed, retry a deferred Chat-safe restart without waiting a full day (t404).
RESTART_RETRY = 300


CODE_EVERY = 60   # seconds between looks at the dashboard's own code on disk


def code_fingerprint(root):
    """(newest mtime, file count) of the Python code under <root>/tutti: it changes when a merge, a pull or an update
    lands. The pages (tutti/web) are read from disk on every request, so only the code can go stale in a running dashboard."""
    newest, count = 0.0, 0
    for folder, _dirs, files in os.walk(os.path.join(root, 'tutti')):
        for f in files:
            if f.endswith('.py'):
                try:
                    newest, count = max(newest, os.path.getmtime(os.path.join(folder, f))), count + 1
                except OSError:
                    pass
    return newest, count


def start_code_watch(stop, root, restart, every=CODE_EVERY, fingerprint=code_fingerprint, thread=None):
    """A dashboard whose code changed on disk restarts itself (docs/DASHBOARD.md, *New code*): otherwise new pages would
    call routes the running server does not have yet. `restart` may return False (Chat is busy): it is asked again next time."""
    import threading
    start = fingerprint(root)

    def loop():
        while not stop.wait(every):
            if fingerprint(root) != start and restart() is not False:
                return
    t = (thread or threading.Thread)(target=loop, daemon=True, name='tutti-code-watch')
    t.start()
    return t


VERSION_LINE = re.compile(r"^__version__\s*=\s*['\"]([0-9][0-9A-Za-z.+-]*)['\"]", re.M)
RESTARTED_ENV = 'TUTTI_RESTARTED_FOR'   # the installed version a restart was already made for: no restart loop if it did not help


def installed_version(home_dir):
    """The version `current` points at (read from its tutti/__init__.py), or None when it cannot be read."""
    try:
        with open(os.path.join(home_dir, 'current', 'tutti', '__init__.py'), encoding='utf8') as f:
            m = VERSION_LINE.search(f.read(4096))
    except OSError:
        return None
    return m.group(1) if m else None


def restart_pending(root, running=__version__, home_dir=None, installed=installed_version):
    """None, or {running, installed, text}: a release install whose `current` moved on while this process kept the old code.
    A git checkout or pipx install has no `current`, so it never reports one."""
    home_dir = home_dir or home()
    if not is_release_install(root, home_dir):
        return None
    now = installed(home_dir)
    if not now or now == running:
        return None
    return {'running': running, 'installed': now,
            'text': f'Tutti {now} is installed but this dashboard still runs {running}: it restarts by itself when Chat is idle '
                    f'(or restart it from Settings)'}


def start_version_watch(stop, root, restart, running=__version__, home_dir=None, every=CODE_EVERY, installed=installed_version,
                        env=None, thread=None):
    """A dashboard whose installed version differs from the one it runs (docs/DASHBOARD.md, *New code*) restarts at the next
    safe point. `tutti self-update` moves `current` to a new folder, which the code watch (it looks at the folder it started
    from) cannot see. `restart` may return False (Chat is busy): it is asked again next time. A restart that did not change
    the running version is not repeated for the same installed version (the environment remembers it)."""
    import threading
    env = os.environ if env is None else env

    def loop():
        while not stop.wait(every):
            pending = restart_pending(root, running, home_dir, installed)
            if not pending or env.get(RESTARTED_ENV) == pending['installed']:
                continue
            env[RESTARTED_ENV] = pending['installed']
            if restart() is False:
                env.pop(RESTARTED_ENV, None)   # deferred: try again next time
                continue
            return
    t = (thread or threading.Thread)(target=loop, daemon=True, name='tutti-version-watch')
    t.start()
    return t


def start_selftest(stop, check, first=60, every=DAY, thread=None):
    """The live self-test (t241): `check()` a minute after the dashboard starts, then daily. A failing check never stops it."""
    import threading

    def loop():
        delay = first
        while not stop.wait(delay):
            try:
                check()
            except Exception as e:   # a broken self-test must not take the dashboard down; it says so instead
                sys.stderr.write(f'tutti: the live self-test could not run ({type(e).__name__}: {e})\n')
            delay = every
    t = (thread or threading.Thread)(target=loop, daemon=True, name='tutti-selftest')
    t.start()
    return t


def start_daily(cfg, stop, root, restart, update=self_update, wait=DAY, retry=RESTART_RETRY):
    """The dashboard's daily self-update (release installs with self_update.enabled): a new version restarts the dashboard
    in place (same pid, so a service manager sees no exit), and the page reloads itself for the new version (update.js).

    `restart` may return False to defer (active Chat work); the loop then retries after `retry` seconds without
    re-downloading. Any other return (including None from a successful execv that never returns) clears the pending flag.
    Returns the thread, or None when it does not apply."""
    import threading
    home_dir = home()
    if not (cfg.get('self_update') or {}).get('enabled', True) or not is_release_install(root, home_dir):
        return None

    def loop():
        pending, delay = False, wait
        while not stop.wait(delay):
            if not pending:
                code, msg = update(home_dir)
                if code == 0 and '->' in msg:
                    pending = True
            if not pending:
                delay = wait
                continue
            if restart() is False:
                delay = retry
                continue
            pending, delay = False, wait
    t = threading.Thread(target=loop, name='tutti-self-update', daemon=True)
    t.start()
    return t
