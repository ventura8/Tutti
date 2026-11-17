"""What `tutti setup` finds on its own: agents and their logins, GPUs and local models, candidate machines, Android
devices, git repositories, gh's login and readable plans. Pure over a Probe (which, run, files), so tests use fake PATHs,
fake ssh configs and fake adb output. Logins are judged by the tool's own status command or by whether its login file
exists; no credential file is ever opened."""
import json
import os
import re
import shutil
import subprocess

PROMPT = '{prompt}'   # where the brief goes in a worker's command
# Each agent: its program, how to tell it is signed in, the tool's own sign-in, and a worker entry for the config.
AGENTS = {
    'claude': {'bin': 'claude', 'login_files': ['~/.claude/.credentials.json'], 'login': ['claude', '/login'],
               'worker': {'kind': 'cli', 'command': ['claude', '-p', PROMPT]}},
    'cursor': {'bin': 'cursor-agent', 'status': ['cursor-agent', 'status'], 'yes': r'logged in as|✓ logged in',
               'no': r'not logged in', 'login': ['cursor-agent', 'login'],
               'plan': {'command': ['cursor-agent', 'about', '--format', 'json'], 'field': 'subscriptionTier'},
               'worker': {'kind': 'cli', 'command': ['cursor-agent', '-p', PROMPT, '--trust', '--force', '--output-format', 'text']}},
    'codex': {'bin': 'codex', 'status': ['codex', 'login', 'status'], 'yes': r'logged in', 'no': r'not logged in',
              'login': ['codex', 'login', '--device-auth'],
              'worker': {'kind': 'cli', 'command': ['codex', 'exec', '--skip-git-repo-check', '--approve-for-me', PROMPT]}},
    'agy': {'bin': 'agy', 'login': ['agy'],
            'worker': {'kind': 'cli', 'command': ['agy', '-p', PROMPT, '--print-timeout', '600s', '--mode', 'accept-edits',
                                                 '--log-file', '{log}'], 'denied': 'soft-denying'}},
    'copilot': {'bin': 'copilot', 'login_files': ['~/.copilot/config.json'], 'login': ['copilot'],
                'worker': {'kind': 'cli', 'command': ['copilot', '-p', PROMPT, '--allow-all-tools', '--no-ask-user', '-s'],
                           'costs_money': True}},
}
REPO_ROOTS = ('~/Projects', '~/projects', '~/src', '~/code', '~/dev', '~/git', '~/repos', '~/workspace', '~/work',
              '~/Documents/GitHub', '~/source/repos')
GIT_HOSTS = ('github.com', 'gitlab.com', 'bitbucket.org', 'ssh.dev.azure.com', 'codeberg.org')


class Probe:
    """The machine, through three doors; tests pass fakes."""

    def __init__(self, path=None, home=None, timeout=10):
        self.path = path
        self.home = home or os.path.expanduser('~')
        self.timeout = timeout

    def which(self, name):
        return shutil.which(name, path=self.path)

    def run(self, argv):
        """(exit code, stdout + stderr); 127 when the program is missing or hangs."""
        exe = self.which(argv[0])
        if not exe:
            return 127, ''
        try:
            env = {**os.environ, 'LC_ALL': 'C', **({'PATH': self.path} if self.path else {})}
            r = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=self.timeout, check=False, env=env,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return 127, ''
        return r.returncode, (r.stdout or '') + (r.stderr or '')

    def expand(self, p):
        return os.path.join(self.home, p[2:]) if p.startswith('~/') else p

    def exists(self, p):
        return os.path.exists(self.expand(p))

    def read(self, p):
        try:
            with open(self.expand(p), encoding='utf8', errors='replace') as f:
                return f.read()
        except OSError:
            return ''

    def listdir(self, p):
        try:
            return sorted(os.listdir(self.expand(p)))
        except OSError:
            return []

    def mtime(self, p):
        try:
            return os.path.getmtime(self.expand(p))
        except OSError:
            return 0.0


def login_state(probe, spec):
    """'yes', 'no' or 'unknown' (the tool keeps its login where Tutti does not look, e.g. a keychain)."""
    if spec.get('status'):
        code, out = probe.run(spec['status'])
        if code == 127:
            return 'unknown'
        if re.search(spec['no'], out, re.I):
            return 'no'
        if re.search(spec['yes'], out, re.I):
            return 'yes'
        return 'unknown'
    if spec.get('login_files'):
        return 'yes' if any(probe.exists(f) for f in spec['login_files']) else 'no'
    return 'unknown'


def plan_of(probe, spec):
    """The plan a tool prints about itself, where it prints one (`cursor-agent about --format json`: subscriptionTier)."""
    if not spec.get('plan'):
        return None
    code, out = probe.run(spec['plan']['command'])
    try:
        value = json.loads(out).get(spec['plan']['field']) if code == 0 else None
    except (ValueError, AttributeError):
        return None
    return str(value) if value else None


def agents(probe):
    out = []
    for name, spec in AGENTS.items():
        if probe.which(spec['bin']):
            out.append({'name': name, 'bin': spec['bin'], 'login': login_state(probe, spec), 'plan': plan_of(probe, spec),
                        'login_command': spec['login']})
    return out


def gh_auth(probe):
    if not probe.which('gh'):
        return 'missing'
    return 'yes' if probe.run(['gh', 'auth', 'status'])[0] == 0 else 'no'


def ollama_models(probe):
    """[name] of the models `ollama list` shows; None when there is no ollama."""
    if not probe.which('ollama'):
        return None
    code, out = probe.run(['ollama', 'list'])
    if code != 0:
        return []
    return [line.split()[0] for line in out.splitlines()[1:] if line.strip()]


def gpus(probe):
    """[{'name', 'memory_gb'}] from nvidia-smi, rocm-smi, or an Apple chip."""
    found = []
    code, out = probe.run(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader,nounits'])
    if code == 0:
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(',')]
            if len(parts) == 2 and parts[1].isdigit():
                found.append({'name': parts[0], 'memory_gb': round(int(parts[1]) / 1024)})
    code, out = probe.run(['rocm-smi', '--showproductname'])
    if code == 0:
        found += [{'name': m.strip(), 'memory_gb': None} for m in re.findall(r'Card series:\s*(.+)', out)]
    code, out = probe.run(['sysctl', '-n', 'machdep.cpu.brand_string'])
    if code == 0 and out.startswith('Apple'):
        found.append({'name': out.strip() + ' (Metal)', 'memory_gb': None})
    return found


def ssh_hosts(text):
    """Concrete `Host` names of an ssh config (no patterns, no git hosts)."""
    out = []
    for line in (text or '').splitlines():
        words = line.split()
        if not words or words[0].lower() != 'host':
            continue
        for h in words[1:]:
            if not re.search(r'[*?!]', h) and h.lower() not in GIT_HOSTS and h not in out:
                out.append(h)
    return out


def tailscale_peers(text):
    """[{'name', 'online', 'os'}] from `tailscale status --json`."""
    try:
        peers = (json.loads(text or '{}').get('Peer') or {}).values()
    except ValueError:
        return []
    return [{'name': (p.get('HostName') or '').lower(), 'online': bool(p.get('Online')), 'os': p.get('OS') or ''}
            for p in peers if p.get('HostName')]


def mdns_ssh(text):
    """[name] of the ssh services `avahi-browse -tpr _ssh._tcp` resolved on the LAN."""
    out = []
    for line in (text or '').splitlines():
        f = line.split(';')
        if len(f) > 6 and f[0] == '=' and f[6].endswith('.local') and f[6] not in out:
            out.append(f[6])
    return out


def machines(probe, me=''):
    """Candidate machines: ssh config hosts first (ready to use), then Tailscale peers and LAN ssh services."""
    seen, out = set(), []

    def add(name, source, **extra):
        key = name.split('.')[0].lower()
        if key and key not in seen and key != me.split('.')[0].lower():
            seen.add(key)
            out.append({'name': key, 'host': name, 'source': source, **extra})
    for h in ssh_hosts(probe.read('~/.ssh/config')):
        add(h, 'ssh')
    code, text = probe.run(['tailscale', 'status', '--json'])
    for p in tailscale_peers(text) if code == 0 else []:
        add(p['name'], 'tailscale', online=p['online'], os=p['os'])
    code, text = probe.run(['avahi-browse', '-tpr', '_ssh._tcp'])
    for h in mdns_ssh(text) if code == 0 else []:
        add(h, 'lan')
    return out


def adb_devices(text):
    """[{'serial', 'state', 'model'}] from `adb devices -l`."""
    out = []
    for line in (text or '').splitlines():
        f = line.split()
        if len(f) >= 2 and not line.startswith(('List of', '*')):
            model = next((x.split(':', 1)[1] for x in f[2:] if x.startswith('model:')), '')
            out.append({'serial': f[0], 'state': f[1], 'model': model})
    return out


def repos(probe):
    """Git repositories one level under the usual folders, most recently worked on first (their priority)."""
    found = []
    for root in REPO_ROOTS:
        for name in probe.listdir(root):
            git = f'{root}/{name}/.git'
            if os.path.isdir(probe.expand(git)):   # a .git file is a worktree of a repo already listed
                when = max(probe.mtime(f'{git}/{f}') for f in ('index', 'HEAD', 'FETCH_HEAD'))
                found.append({'name': name, 'path': f'{root}/{name}', 'when': when})
    found.sort(key=lambda r: -r['when'])
    out, names = [], set()
    for r in found:
        if r['name'] not in names:
            names.add(r['name'])
            out.append({**r, 'priority': len(out)})   # 0 is the most important (projects.<n>.priority)
    return out


def detect(probe, me=''):
    """Everything setup can find without asking."""
    code, adb = probe.run(['adb', 'devices', '-l'])
    return {
        'agents': agents(probe),
        'ollama': ollama_models(probe),
        'gpus': gpus(probe),
        'machines': machines(probe, me),
        'devices': adb_devices(adb) if code == 0 else [],
        'repos': repos(probe),
        'gh': gh_auth(probe),
        'git': bool(probe.which('git')),
    }
