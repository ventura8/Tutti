"""Automatic setup for a remote host or Android device (`tutti connect <address>` and the dashboard Set up flow).

Pure address parsing and command building here; I/O (ssh-keygen, ssh-copy-id, ssh) goes through an injectable `run`
callable so unit tests never touch a real machine. Passwords are only passed to the one-time SSH key copy and are never
stored, logged or returned.
"""
import os
import re
import shutil
import subprocess

from . import connect

_ADDR_RE = re.compile(r'^[A-Za-z0-9._-]+@[A-Za-z0-9._:-]+$')
_IP_RE = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?$')


def is_address(target):
    """True when `target` is a host or device address rather than an agent name."""
    if not isinstance(target, str) or target in connect.AGENTS:
        return False
    if target.startswith('adb:'):
        return len(target) > 4
    return bool(_ADDR_RE.match(target) or _IP_RE.match(target))


def parse_address(address):
    """Parse an address into {'kind': 'adb', 'serial': ...} or {'kind': 'ssh', 'target': ..., 'port': ...}."""
    if address.startswith('adb:'):
        return {'kind': 'adb', 'serial': address[4:]}
    target, port = address, None
    if ':' in address and not address.startswith('['):
        head, tail = address.rsplit(':', 1)
        if tail.isdigit():
            target, port = head, tail
    return {'kind': 'ssh', 'target': target, 'port': port}


def ssh_key_path(home=None):
    """Default ed25519 private key path under the user's ~/.ssh directory."""
    base = home or os.path.expanduser('~')
    return os.path.join(base, '.ssh', 'id_ed25519')


def ensure_ssh_key(home=None, run=subprocess.run):
    """Ensure an ed25519 SSH keypair exists; returns (pub_path, created, error)."""
    key = ssh_key_path(home)
    pub = f'{key}.pub'
    if os.path.isfile(key) and os.path.isfile(pub):
        return pub, False, None
    os.makedirs(os.path.dirname(key), mode=0o700, exist_ok=True)
    res = run(['ssh-keygen', '-t', 'ed25519', '-N', '', '-f', key, '-q'], check=False)
    if res.returncode != 0:
        return None, False, 'ssh-keygen failed: install OpenSSH or check permissions on ~/.ssh'
    return pub, True, None


def _ssh_copy_cmd(parsed, pub_path, password=None, which=shutil.which):
    cmd = ['ssh-copy-id', '-i', pub_path, '-o', 'StrictHostKeyChecking=accept-new']
    if parsed.get('port'):
        cmd.extend(['-p', parsed['port']])
    cmd.append(parsed['target'])
    if password and which('sshpass'):
        return ['sshpass', '-e', *cmd], {'SSHPASS': password}
    return cmd, None


def install_ssh_key(parsed, pub_path, password=None, run=subprocess.run, which=shutil.which):
    """Copy the public key to the target host. Never stores or logs `password`."""
    cmd, extra_env = _ssh_copy_cmd(parsed, pub_path, password=password, which=which)
    env = {**os.environ, **extra_env} if extra_env else None
    try:
        res = run(cmd, env=env, check=False)
    except OSError as e:
        return False, f'ssh-copy-id could not start ({e}): install openssh-client and try again'
    if res.returncode != 0:
        return False, 'SSH key install failed: check the address, SSH service and password, then try again'
    return True, None


def _ssh_base(parsed):
    cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new']
    if parsed.get('port'):
        cmd.extend(['-p', parsed['port']])
    cmd.append(parsed['target'])
    return cmd


def install_tutti(parsed, run=subprocess.run, script_path=None):
    """Pipe install/install.sh to the target host over SSH."""
    if script_path is None:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script_path = os.path.join(repo_root, 'install', 'install.sh')
    try:
        with open(script_path, encoding='utf8') as f:
            script_content = f.read()
    except OSError as e:
        return False, f'could not read {script_path}: {e}'
    try:
        res = run([*_ssh_base(parsed), 'bash', '-s'], input=script_content, text=True, check=False)
    except OSError as e:
        return False, f'ssh could not start ({e}): install openssh-client and try again'
    if res.returncode != 0:
        return False, 'Tutti installation on the host failed: check SSH access and network on the target host'
    return True, None


def detect_services(parsed, run=subprocess.run):
    """Best-effort list of running Docker containers on the target host."""
    try:
        res = run(
            [*_ssh_base(parsed), 'docker ps --format "{{.Names}}" 2>/dev/null || true'],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return []
    if res.returncode != 0 or not res.stdout:
        return []
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def _error(address, note):
    return {'agent': address, 'status': 'error', 'changed': [], 'note': note}


def _connected_result(address, pub, created, services):
    changed = [{'file': pub}] if created else []
    svc_note = f' (detected services: {", ".join(services)})' if services else ''
    return {
        'agent': address, 'status': 'connected', 'changed': changed,
        'services': services, 'note': f'SSH key installed and Tutti configured{svc_note}',
    }


def _setup_ssh(address, parsed, password, home, run, which, script_path):
    pub, created, err = ensure_ssh_key(home=home, run=run)
    if err:
        return _error(address, err)
    ok, err = install_ssh_key(parsed, pub, password=password, run=run, which=which)
    if not ok:
        return _error(address, err)
    ok, err = install_tutti(parsed, run=run, script_path=script_path)
    if not ok:
        return _error(address, err)
    return _connected_result(address, pub, created, detect_services(parsed, run=run))


def setup_host(address, password=None, home=None, run=subprocess.run, which=shutil.which, script_path=None):
    """Guided automatic setup for an SSH host or ADB device."""
    if not is_address(address):
        return _error(address, f'"{address}" is not a host or device address: use user@host or adb:<serial>')
    parsed = parse_address(address)
    if parsed['kind'] == 'adb':
        return _error(address, 'Pair Android devices with `tutti devices pair` or the dashboard Pair a device button')
    return _setup_ssh(address, parsed, password, home, run, which, script_path)

