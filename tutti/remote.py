"""Runs commands on machines (this computer, ssh, or WSL behind a Windows ssh) and copies a work tree to them."""
import base64
import os
import shlex
import subprocess
import sys

from . import routing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLOT_SCRIPT = os.path.join(ROOT, 'scripts', 'slot.sh')
BIN = os.path.join(ROOT, 'bin', 'tutti')
SSH = ['ssh', '-o', 'LogLevel=ERROR']


_BASH_PATHS = {}


def bash_path(p):
    """A path on this computer as the local bash sees it. On Windows that bash is WSL's (C:\\x -> /mnt/c/x) or Git
    Bash (-> /c/x): handed the Windows path, `cd` failed and every local run exited 1. Elsewhere the path itself."""
    if os.name != 'nt':
        return p
    if p not in _BASH_PATHS:
        # The path goes inside the line, which travels base64-encoded (argv): WSL's bash.exe passes no arguments after
        # the -c line, so a "$1" came empty and every path became the current folder.
        q = shlex.quote(p)
        r = subprocess.run(argv({'transport': 'local'}, f'wslpath -a {q} 2>/dev/null || cygpath -u {q}'),
                           capture_output=True, encoding='utf-8', errors='replace', check=False)
        _BASH_PATHS[p] = r.stdout.strip() or p
    return _BASH_PATHS[p]


def home_path(p):
    """A path for a remote bash line: a leading ~ becomes $HOME, the rest is quoted."""
    if p == '~' or p.startswith('~/'):
        return '"$HOME"' + shlex.quote(p[1:]) if p[1:] else '"$HOME"'
    return shlex.quote(p)


def root_path(machine):
    r = machine['root']
    return home_path(r if r.startswith(('/', '~')) else '~/' + r)


def argv(machine, script, ssh_opts=()):
    """The local argv that runs a bash script on the machine."""
    t = machine['transport']
    if t == 'local':
        if os.name == 'nt':
            # Windows rebuilds the command line for bash.exe (WSL or Git Bash) and mangles double quotes inside it:
            # `cut -d" "` arrived as `cut -d\"`, so the local probe printed garbage and `here` showed "unreachable".
            # The line travels base64-encoded instead, as it does for a WSL machine behind ssh: no quotes to mangle.
            # It is decoded to a temporary file, not piped into bash, so the command keeps the caller's standard input.
            packed = base64.b64encode(script.encode()).decode()
            return ['bash', '-c', f'f=$(mktemp) && echo {packed} | base64 -d > $f && {{ bash $f; r=$?; rm -f $f; exit $r; }}']
        return ['bash', '-c', script]
    if t == 'ssh':
        return [*SSH, *ssh_opts, machine['host'], script]
    packed = base64.b64encode(f'cd ~ && {script}'.encode()).decode()
    return [*SSH, *ssh_opts, machine['host'], f'wsl -e {machine["wsl_helper"]} {packed}']


def sh(machine, script, capture=False, timeout=None, stdin=None, ssh_opts=()):
    return subprocess.run(argv(machine, script, ssh_opts), capture_output=capture, text=True, timeout=timeout, input=stdin)


def rsh(cfg, machine):
    """What git (GIT_SSH_COMMAND) and rsync (-e) use to reach the machine: plain ssh, or `tutti rsh` for WSL."""
    if machine['transport'] != 'wsl':
        return 'ssh'
    return f'{shlex.quote(sys.executable)} {shlex.quote(BIN)} --config {shlex.quote(cfg["_path"])} rsh {shlex.quote(machine["name"])}'


def rsh_main(machine, args):
    """`tutti rsh <machine> [ssh options...] <host> <command...>`: the remote-shell shim git and rsync call for a WSL machine."""
    args = list(args)
    while args and args[0].startswith('-'):
        if args[0] in ('-o', '-p', '-l'):
            args.pop(0)
        args.pop(0)
    command = args[1:]
    script = command[0] if len(command) == 1 else ' '.join(shlex.quote(a) for a in command)
    return subprocess.run(argv(machine, script)).returncode


def lock_files(machine):
    """The machine's slot lock files: its explicit `locks`, else <lock_prefix><n>.lock for each slot."""
    return machine.get('locks') or [f'{machine["lock_prefix"]}{i}.lock' for i in range(1, int(machine['slots']) + 1)]


def probe_script(machine):
    y = home_path(machine['yield_file'])
    locks = ' '.join(shlex.quote(p) for p in lock_files(machine))
    return (f'n=0; for l in {locks}; do flock -n "$l" true 2>/dev/null && n=$((n+1)); done; '
            f'y=0; [ -e {y} ] && y=1; echo "$n $(cut -d" " -f1 /proc/loadavg) $(nproc) $y"')


def probe(machine, timeout=20):
    try:
        r = sh(machine, probe_script(machine), capture=True, timeout=timeout, ssh_opts=('-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes'))
    except (OSError, subprocess.SubprocessError):
        return routing.Probe()
    return routing.parse_probe(r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else '')


def runner_command(machine, track, command):
    """The line that runs `command` at the tree root: plain bash, or a capped container (memory, no extra swap, CPUs)."""
    r = machine['runner']
    inner = f'{r["prelude"]} && {command}' if r.get('prelude') else command
    if r['kind'] == 'shell':
        return f'bash -c {shlex.quote(inner)}'
    env = ' '.join(f'-e {shlex.quote(f"{k}={v}")}' for k, v in r.get('env', {}).items())
    extra = ' '.join(shlex.quote(str(a)) for a in r.get('args', []))
    return (f'docker run --rm --memory={r["memory_gb"]}g --memory-swap={r["memory_gb"]}g --cpus={r["cpus"]} {env} {extra} '
            f'--name tutti-{machine["name"]}-{track}-$RANDOM -u "$(id -u):$(id -g)" -v "$PWD:$PWD" -w "$PWD" '
            f'{shlex.quote(r["image"])} bash -c {shlex.quote(inner)}')


def slot_prefix(machine):
    locks = shlex.quote(' '.join(lock_files(machine)))
    return f'MIN_FREE_GB={int(machine["min_free_gb"])} SLOT_LOCKS={locks}'


def _check(result, what):
    code = result if isinstance(result, int) else result.returncode
    if code != 0:
        raise RuntimeError(f'{what} failed (exit {code})')


def sync(cfg, machine, track, src):
    """Copies the tree (incremental rsync, the config's excludes) and gives the copy a shallow git repository at the tree's
    commit: a worktree's `.git` is a file that points at this computer, so tests that ask git would fail on the copy."""
    name, root, rs = machine['name'], root_path(machine), rsh(cfg, machine)
    dest = f'{machine["host"]}:{machine["root"]}'
    is_git = subprocess.run(['git', '-C', src, 'rev-parse', '--git-dir'], capture_output=True).returncode == 0
    _check(sh(machine, f'mkdir -p {root}/{track} && {{ [ -d {root}/repo.git ] || git init -q --bare {root}/repo.git; }}'),
           f'preparing {name}')
    if is_git:
        _check(subprocess.run(['git', '-C', src, 'push', '-q', '-f', f'{dest}/repo.git', f'HEAD:refs/heads/tutti/{track}'],
                              env={**os.environ, 'GIT_SSH_COMMAND': rs}), f'pushing HEAD to {name}')
    excludes = [a for e in cfg['sync']['exclude'] for a in ('--exclude', e)]
    _check(subprocess.run(['rsync', '-a', '--delete', '-e', rs, *excludes, f'{src}/', f'{dest}/{track}/']), f'copying the tree to {name}')
    with open(SLOT_SCRIPT, encoding='utf8') as f:
        _check(sh(machine, f'cat > {root}/tutti-slot.sh', stdin=f.read()), f'installing slot.sh on {name}')
    return is_git


def run(cfg, machine, track, src, command):
    """Runs `command` over the tree on the machine, behind its slot queue. Returns the command's exit code."""
    slot = slot_prefix(machine)
    if machine['transport'] == 'local':
        script = (f'cd {shlex.quote(bash_path(src))} && {slot} bash {shlex.quote(bash_path(SLOT_SCRIPT))} '
                  f'{runner_command(machine, track, command)}')
        return sh(machine, script).returncode
    is_git = sync(cfg, machine, track, src)
    root = root_path(machine)
    git = (f'{{ [ -d .git ] || {{ rm -f .git; git init -q; }}; }} && git fetch -q --depth 1 {root}/repo.git tutti/{track} && '
           'git update-ref --no-deref HEAD FETCH_HEAD && git reset -q && ') if is_git else ''
    dirs = ' '.join(shlex.quote(d) for d in cfg['sync']['ensure_dirs'])
    mk = f'mkdir -p {dirs} && ' if dirs else ''
    script = f'cd {root}/{track} && {git}{mk}{slot} bash {root}/tutti-slot.sh {runner_command(machine, track, command)}'
    return sh(machine, script).returncode


def set_yield(machine, on, note=''):
    y = home_path(machine['yield_file'])
    script = f'mkdir -p "$(dirname {y})" && printf "%s\\n" {shlex.quote(note)} > {y}' if on else f'rm -f {y}'
    return sh(machine, script).returncode


def yield_note(machine):
    """None when the flag is down, else the flag file's text (may be empty; the word `unload` asks for models to be unloaded)."""
    y = home_path(machine['yield_file'])
    try:
        r = sh(machine, f'[ -e {y} ] && {{ echo UP; cat {y}; }} || echo DOWN', capture=True, timeout=40,
               ssh_opts=('-o', 'ConnectTimeout=15', '-o', 'BatchMode=yes'))
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out[2:].strip() if out.startswith('UP') else None
