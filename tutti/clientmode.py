"""This computer as a client of a Tutti that runs elsewhere (docs/MOVE.md, *After the move*).

After `tutti move --to <machine>`, the config of the old computer has a `host:` block naming the new host. Commands that
read or change Tutti's state (the backlog, the autopilot, pause and resume, the readings, runs) then run on the host over
ssh, so the terminal, the scripts and every connected agent (`tutti mcp` runs these same commands) see the one state the
dashboard shows. Commands about this computer itself (tests, ship, connect, mcp, update, doctor) stay here.

When the host cannot be reached, a command that only reads runs here on a read-only copy of the host's state (kept fresh
by `tutti client`) and says so; a command that would change the state is refused with the reason, except `tutti run`,
which works here alone (the laptop keeps working when the NUC is down) and says that its record stays on this computer."""
import os
import shlex
import subprocess
import sys
import time

from . import config

FORWARD = ('backlog', 'autopilot', 'pause', 'resume', 'optin', 'usage', 'main', 'owner', 'dispatch', 'selfimprove', 'scout', 'runs',
           'wait', 'household', 'user', 'run', 'job', 'bridge')
READ_ONLY = {'usage': ('show', None), 'backlog': ('list', None), 'autopilot': ('status', None), 'runs': None, 'main': None,
             'owner': None, 'dispatch': ('status', None), 'household': ('status', None), 'user': ('show', None),
             'selfimprove': ('status', None)}
UNREACHABLE = 255          # ssh's own exit code: the host could not be reached
MIRROR = 'host-mirror'
MIRROR_EXCLUDE = ['/runs/', '/locks/', '/bridge/', '/move/', '*.png', '*.lock', '/llm-busy-*', '/files/']


def role(cfg):
    """'host' (Tutti runs here, or no move happened) or 'client' (the host is another machine)."""
    h = cfg.get('host') or {}
    return 'client' if h.get('machine') and h.get('me') and h['machine'] != h['me'] else 'host'


def should_forward(cfg, cmd, env=None):
    env = os.environ if env is None else env
    return role(cfg) == 'client' and cmd in FORWARD and not env.get('TUTTI_LOCAL')


def absolute_args(argv):
    """Arguments that name an existing file or folder here become absolute paths (the host's working folder differs)."""
    return [os.path.abspath(x) if not x.startswith('-') and os.path.sep in x and os.path.exists(x) else x for x in argv]


def ssh_command(cfg, argv, tty=False, h=None):
    h = h or cfg['host']
    remote = shlex.join([h.get('tutti') or 'tutti', '--config', h['config'], *absolute_args(argv)])
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', *(['-t'] if tty else []), h['ssh'], remote]


def forward(cfg, argv, run=subprocess.run, isatty=None):
    """The command's exit code on the host, or None when the host could not be reached."""
    isatty = isatty or (lambda: sys.stdin.isatty() and sys.stdout.isatty())
    # the host, then its backup main machines (host.failover: [{ssh, config, tutti}], docs/FAILOVER.md): a host that is not the
    # active main refuses changes itself, so the first one that answers is the one to use
    for h in [cfg['host'], *[{**cfg['host'], **f} for f in cfg['host'].get('failover') or [] if isinstance(f, dict) and f.get('ssh')]]:
        try:
            rc = run(ssh_command(cfg, argv, tty=isatty(), h=h)).returncode
        except OSError:
            continue
        if rc != UNREACHABLE:
            return rc
    return None


def is_read_only(cmd, action):
    allowed = READ_ONLY.get(cmd, ())
    return cmd in READ_ONLY and (allowed is None or action in allowed)


def mirror_dir(cfg):
    return os.path.join(config.state_dir(cfg), MIRROR)


def fallback_cfg(cfg):
    """The config pointed at the read-only copy of the host's state."""
    d = mirror_dir(cfg)
    out = dict(cfg)
    out['project'] = {**cfg['project'], 'state': d}
    out['usage'] = {**(cfg.get('usage') or {}), 'log': os.path.join(d, 'usage.jsonl')}
    out['autopilot'] = {**cfg['autopilot'], 'enabled': False}
    out['_readonly'] = mirror_note(cfg)
    return out


def mirror_age(cfg):
    try:
        return time.time() - os.path.getmtime(os.path.join(mirror_dir(cfg), '.synced'))
    except OSError:
        return None


def mirror_note(cfg):
    age = mirror_age(cfg)
    when = 'never copied yet' if age is None else f'copied {int(age // 60)} min ago'
    return (f'{cfg["host"]["machine"]} (where Tutti runs) cannot be reached: this is a read-only copy of its state ({when}). '
            f'Start {cfg["host"]["machine"]}, or check that `ssh {cfg["host"]["ssh"]}` works from here')


def refresh_mirror(cfg, run=subprocess.run):
    """The read-only copy of the host's state, for when the host is down (`tutti client` calls this every few minutes)."""
    h = cfg['host']
    d = mirror_dir(cfg)
    os.makedirs(d, exist_ok=True)
    excl = [f'--exclude={x}' for x in MIRROR_EXCLUDE]
    ssh = ['-e', 'ssh -o BatchMode=yes -o ConnectTimeout=5']
    r = run(['rsync', '-a', '--delete', *ssh, *excl, f'{h["ssh"]}:{h["state"].rstrip("/")}/', d + '/'], capture_output=True, text=True)
    if r.returncode == 0 and h.get('usage_log'):
        r = run(['rsync', '-a', *ssh, f'{h["ssh"]}:{h["usage_log"]}', os.path.join(d, 'usage.jsonl')], capture_output=True, text=True)
    if r.returncode == 0:
        with open(os.path.join(d, '.synced'), 'w', encoding='utf8') as f:
            f.write(f'{time.time()}\n')
    return r.returncode == 0


def refusal(cfg, cmd):
    return (f'{cmd} changes Tutti\'s state, which lives on {cfg["host"]["machine"]}, and {cfg["host"]["machine"]} cannot be reached '
            f'(ssh {cfg["host"]["ssh"]} failed). Start it or reconnect, then run the command again; `tutti move --rollback` brings '
            f'Tutti back to this computer if {cfg["host"]["machine"]} is gone for good')


_PUSHED = {}   # reading file -> the mtime last sent to the host


def changed_readings(cfg):
    """[(path here, its mtime)] of this computer's reading files that changed since they were last sent."""
    out = []
    for a in cfg['accounts'].values():
        f = config.expand((a or {}).get('reading_file') or '') if (a or {}).get('reading_file') else ''
        try:
            seen = os.path.getmtime(f) if f else None
        except OSError:
            seen = None
        if seen and _PUSHED.get(f) != seen:
            out.append((f, seen))
    return out


def push_readings(cfg, run=subprocess.run):
    """The reading files this computer's own session writes (accounts.<n>.reading_file, e.g. Claude's readings from its
    get_usage) sent to the same path under the host's home whenever they change, so the host paces with them. Returns the
    files sent."""
    h = cfg['host']
    home, there_home = os.path.expanduser('~').rstrip('/'), (h.get('home') or '').rstrip('/')
    sent = []
    for f, seen in changed_readings(cfg) if there_home else []:
        if not f.startswith(home + '/'):
            continue
        there = there_home + f[len(home):]
        with open(f, encoding='utf8') as fh:
            text = fh.read()
        q = shlex.quote(there)
        r = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', h['ssh'],
                 f'mkdir -p "$(dirname {q})" && cat > {q}.tmp && mv {q}.tmp {q}'], input=text, capture_output=True, text=True)
        if r.returncode == 0:
            _PUSHED[f] = seen
            sent.append(there)
    return sent
