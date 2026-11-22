"""Easy setup of a computer found on the network (t225, docs/MACHINES.md, *Set up a found computer*):
`tutti machines add --host <address>` does every step but the one that must be the person's.

1. A key of this computer's own (`ssh-keygen`, ed25519, only when there is none).
2. Does key login work already (`ssh -o BatchMode=yes ... true`)?
3. If not, `ssh-copy-id`: the person types the host's password once, into ssh itself. Tutti never reads, keeps or sends
   it; it only hands the terminal to ssh-copy-id.
4. Key login again; only when it works is `machines.<name>` written into the config (a backup first).

Pure: `entry`, `name_for`, `target`. `setup` takes `run` and the paths, so tests never reach a real machine."""
import os
import re
import subprocess

NAME_RX = re.compile(r'[a-z][a-z0-9-]{0,30}')
ADDRESS_RX = re.compile(r'[A-Za-z0-9][A-Za-z0-9.:-]*')
USER_RX = re.compile(r'[a-z_][a-z0-9_-]{0,31}')
KEYS = ('id_ed25519', 'id_ecdsa', 'id_rsa')


class SetupError(Exception):
    pass


def name_for(address, taken):
    """A free machine name from the address: its first label, or host-<last part> for an IP; -2, -3... when taken."""
    first = address.split('.')[0].lower() if not re.fullmatch(r'[0-9.]+|[0-9a-f:]+', address) else \
        'host-' + re.split(r'[.:]', address)[-1]
    base = re.sub(r'[^a-z0-9-]', '-', first).strip('-') or 'host'
    base = base if base[0].isalpha() else f'host-{base}'
    name, n = base[:30], 2
    while name in taken:
        name, n = f'{base[:27]}-{n}', n + 1
    return name


def check(address, name, user, port):
    """Raises SetupError naming the flag and the fix; nothing that could be an ssh option gets through."""
    if not ADDRESS_RX.fullmatch(address or ''):
        raise SetupError(f'--host {address!r} is not an address or host name: give the one the Discovered tab shows')
    if name and not NAME_RX.fullmatch(name):
        raise SetupError(f'--name {name!r}: lower-case letters, digits and -, starting with a letter')
    if user and not USER_RX.fullmatch(user):
        raise SetupError(f'--user {user!r} is not a user name: the account you log in to on that computer')
    if not 0 < int(port) < 65536:
        raise SetupError(f'--port {port}: a port between 1 and 65535 (22 is ssh\'s own)')


def target(address, user):
    return f'{user}@{address}' if user else address


def entry(address, user, port):
    """The config entry for the new machine: one slot to start with (raise it on Settings once it is trusted)."""
    out = {'host': address, 'slots': 1}
    if user:
        out['user'] = user
    if port != 22:
        out['port'] = port
    return out


def _key(home):
    return next((os.path.join(home, '.ssh', k) for k in KEYS if os.path.exists(os.path.join(home, '.ssh', k + '.pub'))), None)


def setup(cfg, address, name=None, user=None, port=22, run=subprocess.run, home=None, store=None, say=print):
    """Every step, each said as it goes; returns the machine name. Raises SetupError with the step, the cause and the fix."""
    check(address, name, user, port)
    name = name or name_for(address, set(cfg.get('machines') or {}))
    if name in (cfg.get('machines') or {}):
        raise SetupError(f'machines.{name} is already in the config: pick another --name, or change it on the Machines tab')
    home = home or os.path.expanduser('~')
    key = _key(home)
    if not key:
        key = os.path.join(home, '.ssh', 'id_ed25519')
        say(f'1/4 No ssh key here yet: making one ({key}).')
        os.makedirs(os.path.dirname(key), mode=0o700, exist_ok=True)
        if run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', key], check=False).returncode != 0:
            raise SetupError(f'step 1: ssh-keygen could not make {key}: run it yourself (ssh-keygen -t ed25519), then retry')
    else:
        say(f'1/4 Using this computer\'s ssh key ({key}.pub).')
    to = target(address, user)
    probe = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=accept-new', '-p', str(port),
             '--', to, 'true']
    if run(probe, check=False, capture_output=True).returncode == 0:
        say('2/4 Key login works already.')
    else:
        say(f'2/4 Key login does not work yet. 3/4 ssh-copy-id now asks for {to}\'s password: type it there, once. '
            'Tutti does not see it.')
        if run(['ssh-copy-id', '-i', key + '.pub', '-p', str(port), to], check=False).returncode != 0:
            raise SetupError(f'step 3: ssh-copy-id did not install the key on {to}: check the address, the user (--user) and that '
                             'ssh is on there (Linux: sudo systemctl enable --now ssh; macOS: Remote Login), then retry')
        if run(probe, check=False, capture_output=True).returncode != 0:
            raise SetupError(f'step 3: the key was sent but key login to {to} still fails: the host may refuse keys '
                             '(PubkeyAuthentication no) or the home folder permissions are too open; fix that, then retry')
    if store is None:
        from . import settings
        store = settings.store
    try:
        store(cfg, [(['machines', name], entry(address, user, port))])
    except Exception as e:   # SettingsError and friends: the step and the fix
        raise SetupError(f'step 4: key login works, but machines.{name} was not saved ({e}); add it on the Machines tab') from e
    say(f'4/4 Saved as machines.{name}. `tutti machines` probes it; its roles show on the Machines tab.')
    return name
