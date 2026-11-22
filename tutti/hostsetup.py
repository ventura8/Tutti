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
    if int(port) != 22:   # Tutti's ssh, rsync and git calls reach a machine by its host alone, so the port must live in ssh's own config
        raise SetupError(f'--port {port}: add `Host <alias>` with `HostName {address}` and `Port {port}` to ~/.ssh/config, then '
                         'run tutti machines add --host <alias>')


def target(address, user):
    return f'{user}@{address}' if user else address


def entry(address, user):
    """The config entry for the new machine: one slot to start with (raise it on Settings once it is trusted). The user goes
    into the host (user@address), the form every ssh, rsync and git call of Tutti's uses."""
    return {'host': target(address, user), 'slots': 1}


def _key(home):
    """The first private key this computer has (its .pub may be missing: setup derives it, never replaces the key)."""
    return next((os.path.join(home, '.ssh', k) for k in KEYS if os.path.exists(os.path.join(home, '.ssh', k))), None)


def _run(run, cmd, step, **kw):
    try:
        return run(cmd, check=False, **kw).returncode
    except OSError as e:
        raise SetupError(f'step {step}: {cmd[0]} could not start ({e}): install OpenSSH\'s client tools (ssh, ssh-keygen, '
                         'ssh-copy-id), then retry') from e


def _ensure_key(run, home, say):
    """This computer's key: an existing one (its .pub derived when missing), else a new ed25519 pair."""
    key = _key(home)
    if not key:
        key = os.path.join(home, '.ssh', 'id_ed25519')
        say(f'1/4 No ssh key here yet: making one ({key}).')
        os.makedirs(os.path.dirname(key), mode=0o700, exist_ok=True)
        if _run(run, ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', key], 1) != 0:
            raise SetupError(f'step 1: ssh-keygen could not make {key}: run it yourself (ssh-keygen -t ed25519), then retry')
    elif not os.path.exists(key + '.pub'):
        say(f'1/4 {key} has no .pub next to it: deriving it (the key itself is kept).')
        try:
            with open(key + '.pub', 'w', encoding='utf8') as f:
                if _run(run, ['ssh-keygen', '-y', '-f', key], 1, stdout=f) != 0:
                    raise SetupError(f'step 1: ssh-keygen -y could not read {key} (a passphrase?): make {key}.pub yourself '
                                     f'(ssh-keygen -y -f {key} > {key}.pub), then retry')
        except SetupError:
            os.remove(key + '.pub')
            raise
    else:
        say(f'1/4 Using this computer\'s ssh key ({key}.pub).')
    return key


def _ensure_login(run, key, to, say):
    """Key login to `to`, with ssh-copy-id (the person types the password into ssh) only when it fails."""
    probe = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=accept-new', '--', to, 'true']
    if _run(run, probe, 2, capture_output=True) == 0:
        say('2/4 Key login works already.')
    else:
        say(f'2/4 Key login does not work yet. 3/4 ssh-copy-id now asks for {to}\'s password: type it there, once. '
            'Tutti does not see it.')
        if _run(run, ['ssh-copy-id', '-i', key + '.pub', to], 3) != 0:
            raise SetupError(f'step 3: ssh-copy-id did not install the key on {to}: check the address, the user (--user) and that '
                             'ssh is on there (Linux: sudo systemctl enable --now ssh; macOS: Remote Login), then retry')
        if _run(run, probe, 3, capture_output=True) != 0:
            raise SetupError(f'step 3: the key was sent but key login to {to} still fails: the host may refuse keys '
                             '(PubkeyAuthentication no) or the home folder permissions are too open; fix that, then retry')


def setup(cfg, address, name=None, user=None, port=22, run=subprocess.run, home=None, store=None, say=print):
    """Every step, each said as it goes; returns the machine name. Raises SetupError with the step, the cause and the fix."""
    check(address, name, user, port)
    name = name or name_for(address, set(cfg.get('machines') or {}))
    if name in (cfg.get('machines') or {}):
        raise SetupError(f'machines.{name} is already in the config: pick another --name, or change it on the Machines tab')
    home = home or os.path.expanduser('~')
    if store is None:
        from . import settings
        tag = settings.etag(settings.read_file(cfg)) if cfg.get('_path') else None   # a save in between makes ours fail

        def store(c, changes):
            return settings.store(c, changes, tag)
    key = _ensure_key(run, home, say)
    _ensure_login(run, key, target(address, user), say)
    try:
        store(cfg, [(['machines', name], entry(address, user))])
    except Exception as e:   # SettingsError (the file changed meanwhile, e.g. machines.<name> added) and friends
        raise SetupError(f'step 4: key login works, but machines.{name} was not saved ({e}): if the config changed meanwhile, '
                         f'rerun with another --name or add it on the Machines tab') from e
    say(f'4/4 Saved as machines.{name}. `tutti machines` probes it; its roles show on the Machines tab.')
    return name
