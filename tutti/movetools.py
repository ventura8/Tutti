"""What `tutti move` carries besides Tutti itself (docs/MOVE.md, *Everything Tutti relies on*): the agent CLIs at the same
versions, their logins, the repositories and worktrees Tutti works in, and the readings the old computer's session writes.

- **CLIs**: each tool's own install folder (`~/.local/share/<tool>/versions/<v>`, an npm package under
  `~/.local/lib/node_modules`, or the single file in `~/.local/bin`) is copied as is and its link in `~/.local/bin` made
  again, so the target runs the very same version without a download. Node, when a carried tool needs it and the target
  has none, is the same version from nodejs.org, checked against its SHA256SUMS. gh comes with Tutti's install.
- **Logins** (the owner's documented exception, docs/MOVE.md, *Logins*): each tool's own credential and config files are
  copied over ssh with mode 600 (files) and 700 (folders), never read, printed or logged by Tutti; gh's token, kept in the
  keyring, goes the way gh documents (`gh auth token` piped into `gh auth login --with-token`). Each tool's own status
  command then checks the login on the target; one that refuses the copy gets its own device-code sign-in, listed once.
- **Repositories**: the autopilot repos, their git worktrees and the worktrees folder, in one rsync with hard links kept
  (the shared node_modules), then `git worktree repair` with the new paths, so WIP and refs come along.
- **Readings**: a client pushes the reading files its own session writes (accounts.<n>.reading_file) to the host."""
import os
import re
import shlex
import subprocess

TOOLS = ('claude', 'cursor-agent', 'agy', 'copilot', 'gemini', 'codex')
LOGINS = {
    'claude': {'paths': ['.claude/.credentials.json', '.claude.json', '.claude/settings.json'],
               'verify': ['claude', 'auth', 'status'], 'yes': r'"loggedIn":\s*true', 'login': 'claude /login'},
    'cursor-agent': {'paths': ['.config/cursor/auth.json', '.cursor/cli-config.json'],
                     'verify': ['cursor-agent', 'status'], 'yes': r'logged in as', 'login': 'cursor-agent login'},
    'agy': {'paths': ['.gemini/oauth_creds.json', '.gemini/google_accounts.json', '.gemini/settings.json', '.gemini/installation_id',
                      '.gemini/antigravity-cli/'],
            'verify': ['agy', '-p', '/usage', '--output-format', 'json', '--print-timeout', '60s'], 'yes': r'^\s*[\[{]',
            'login': 'agy   # sign in with Google when it asks'},
    # gemini and copilot have no status command: their login file there, non-empty, is the check
    'gemini': {'paths': ['.gemini/oauth_creds.json', '.gemini/google_accounts.json', '.gemini/settings.json'],
               'file': '.gemini/oauth_creds.json', 'login': 'gemini   # sign in with Google when it asks'},
    'copilot': {'paths': ['.copilot/config.json'], 'file': '.copilot/config.json', 'login': 'copilot   # then /login'},
    'codex': {'paths': ['.codex/auth.json', '.codex/config.toml'], 'verify': ['codex', 'login', 'status'], 'yes': r'logged in',
              'login': 'codex login --device-auth'},
    'gh': {'pipe': True, 'verify': ['gh', 'auth', 'status'], 'yes': r'Logged in', 'login': 'gh auth login --web'},
}
# a tool's history, caches and conversations stay where they were made: only what its login and settings need moves
LOGIN_EXCLUDE = ['brain/', 'conversations/', 'cache/', 'annotations/', 'cli.log', '*.db', 'logs/', 'tmp/']
TOOL_EXCLUDE = ['.running/']   # a CLI's own markers of its running processes
NODE_ARCH = {'x86_64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}
TARGET_PATH = 'PATH="$HOME/.local/bin:$PATH"'


def run(argv, **kw):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=kw.pop('timeout', 30), **kw)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(argv, 127, '', str(e))


def version_of(name, runner=run):
    r = runner([name, '--version'])
    return (r.stdout or '').strip().splitlines()[0] if r.returncode == 0 and (r.stdout or '').strip() else ''


def carry(name, home, which=None, realpath=os.path.realpath):
    """{'name', 'copy': [home-relative paths], 'link': home-relative target of ~/.local/bin/<name> or None, 'node': bool} for a
    CLI installed under this home, or None (not installed, or installed outside the home by a package manager)."""
    import shutil
    found = (which or shutil.which)(name)
    if not found:
        return None
    real = realpath(found)
    home = home.rstrip('/') + '/'
    if not real.startswith(home):
        return None
    rel = real[len(home):]
    m = re.match(r'(\.local/share/[^/]+/versions/[^/]+)', rel)
    if m:
        return {'name': name, 'copy': [m.group(1)], 'link': rel, 'node': False}
    m = re.match(r'(\.local/lib/node_modules/(?:@[^/]+/)?[^/]+)', rel)
    if m:
        return {'name': name, 'copy': [m.group(1)], 'link': rel, 'node': True}
    if rel == f'.local/bin/{name}':
        return {'name': name, 'copy': [rel], 'link': None, 'node': False}
    return None


def tools_plan(home, facts, runner=run, which=None):
    """[carry + {'version', 'there'}] of every CLI to carry: the ones the target lacks or has at another version."""
    out = []
    for name in TOOLS:
        c = carry(name, home, which)
        if not c:
            continue
        c['version'] = version_of(name, runner)
        c['there'] = facts.get(f'ver_{name}', '')
        if c['there'] != c['version']:
            out.append(c)
    return out


def node_script(version, arch):
    """Node `version` (v24.19.0) for `arch` into ~/.local/share/tutti/node on the target, checked against nodejs.org's sums."""
    a = NODE_ARCH.get(arch, arch)
    f = f'node-{version}-linux-{a}.tar.xz'
    url = f'https://nodejs.org/dist/{version}'
    d = '"$HOME/.local/share/tutti/node"'
    return (f'set -e; t=$(mktemp -d); cd "$t"; curl -fsSLO {url}/{f}; curl -fsSL {url}/SHASUMS256.txt | grep " {f}$" | sha256sum -c -; '
            f'rm -rf {d}; mkdir -p {d}; tar -xJf {f} -C {d} --strip-components=1; mkdir -p "$HOME/.local/bin"; '
            f'ln -sf {d}/bin/node "$HOME/.local/bin/node"; rm -rf "$t"')


def link_script(carries):
    lines = ['mkdir -p "$HOME/.local/bin"']
    for c in carries:
        if c['link']:
            lines.append(f'ln -sfn "$HOME/"{shlex.quote(c["link"])} "$HOME/.local/bin/"{shlex.quote(c["name"])}')
    return ' && '.join(lines)


def uncarried(home, which=None):
    """CLIs installed here outside this home (a package manager's): a move cannot carry them."""
    import shutil
    w = which or shutil.which
    return [n for n in TOOLS if w(n) and not carry(n, home, w)]


def login_paths(home, names):
    """The home-relative login files and folders of these tools that exist here (names only: nothing is read)."""
    out = []
    for n in names:
        for p in LOGINS.get(n, {}).get('paths', []):
            if os.path.exists(os.path.join(home, p.rstrip('/'))) and p not in out:
                out.append(p)
    return out


def login_tools(which=None):
    """The tools whose login moves: installed here (gh included)."""
    import shutil
    w = which or shutil.which
    return [n for n in LOGINS if w(n)]


def verify_script(names):
    """One line per tool on the target: `<name> yes|no`, from its own status command (its output is never shown)."""
    lines = []
    for n in names:
        spec = LOGINS[n]
        if spec.get('file'):
            lines.append(f'if [ -s "$HOME/"{shlex.quote(spec["file"])} ]; then echo "{n} yes"; else echo "{n} no"; fi')
            continue
        cmd = shlex.join(spec['verify'])
        lines.append(f'if out=$({TARGET_PATH} timeout 90 {cmd} 2>&1) && printf "%s" "$out" | grep -Eqi {shlex.quote(spec["yes"])}; '
                     f'then echo "{n} yes"; else echo "{n} no"; fi')
    return '\n'.join(lines)


def parse_verify(text):
    return {k: v == 'yes' for k, v in (line.split(' ', 1) for line in text.splitlines() if ' ' in line)}


# ---- repositories


def worktrees_of(repo, runner=run):
    r = runner(['git', '-C', repo, 'worktree', 'list', '--porcelain'])
    return [line[9:] for line in (r.stdout or '').splitlines() if line.startswith('worktree ')] if r.returncode == 0 else []


def repo_set(cfg, home, expand, runner=run):
    """{'paths': home-relative folders to copy (none inside another), 'repos': {repo: [its worktrees]}} for the autopilot repos,
    their worktrees and the worktrees folder; folders outside this home stay where they are."""
    home = home.rstrip('/') + '/'
    repos = {}
    for r in (cfg['autopilot'].get('repos') or {}).values():
        if r.get('path') and os.path.isdir(expand(r['path'])):
            p = expand(r['path'])
            repos[p] = [w for w in worktrees_of(p, runner) if w != p]
    found = [*repos, *[w for ws in repos.values() for w in ws], expand(cfg['project']['worktrees'])]
    inside = sorted({p.rstrip('/') for p in found if p.startswith(home) and os.path.isdir(p)})
    top = [p for p in inside if not any(p != q and p.startswith(q + '/') for q in inside)]
    return {'paths': [p[len(home):] for p in top], 'repos': repos}


def repair_script(repos, src_home, dst_home):
    """`git worktree repair` on the target with each worktree's new path, so a worktree and its repository find each other."""
    def m(p):
        return dst_home.rstrip('/') + p[len(src_home.rstrip('/')):] if p.startswith(src_home.rstrip('/') + '/') else p
    lines = []
    for repo, wts in repos.items():
        mine = [shlex.quote(m(w)) for w in wts if w.startswith(src_home.rstrip('/') + '/')]
        lines.append(f'if [ -d {shlex.quote(m(repo))} ]; then git -C {shlex.quote(m(repo))} worktree repair {" ".join(mine)} || exit 1; fi')
    return '\n'.join(lines) or 'true'


def size_kb(paths, home, runner=run):
    """The real size (hard links counted once) of these home-relative folders, in KB; 0 when it cannot be read in time."""
    if not paths:
        return 0
    r = runner(['du', '-skc', *[os.path.join(home, p) for p in paths]], timeout=600)
    last = (r.stdout or '').strip().splitlines()[-1:] or ['0']
    try:
        return int(last[0].split()[0])
    except (ValueError, IndexError):
        return 0


# ---- ssh access to the machines the target cannot reach yet (the owner's decision, docs/MOVE.md, *Machines*)


def ssh_blocks(aliases, text):
    """{alias: {'block', 'identities', 'hostname', 'port'}} of `Host <alias>` entries in an ssh config's text (the block as
    written; its IdentityFile paths; where it points). Only exact single-alias blocks are taken."""
    out, cur, lines = {}, None, text.splitlines()
    for line in [*lines, 'Host __end__']:
        words = line.strip().split()
        if words and words[0].lower() in ('host', 'match'):
            if cur and cur['alias'] in aliases:
                out[cur['alias']] = cur
            cur = {'alias': words[1], 'block': [line], 'identities': [], 'hostname': words[1], 'port': '22'} \
                if words[0].lower() == 'host' and len(words) == 2 else None
            continue
        if cur is None:
            continue
        cur['block'].append(line)
        if len(words) >= 2 and words[0].lower() == 'identityfile':
            cur['identities'].append(words[1])
        elif len(words) >= 2 and words[0].lower() == 'hostname':
            cur['hostname'] = words[1]
        elif len(words) >= 2 and words[0].lower() == 'port':
            cur['port'] = words[1]
    return {a: {**b, 'block': '\n'.join(b['block']).rstrip() + '\n'} for a, b in out.items()}


def ssh_carry(aliases, home, read=None):
    """What gives the target the same ssh access as here to these machines: their config blocks and key files."""
    path = os.path.join(home, '.ssh', 'config')
    try:
        text = read(path) if read else open(path, encoding='utf8').read()
    except OSError:
        return {}
    found = ssh_blocks(set(aliases), text)
    for b in found.values():
        b['block'] = b['block'].replace(home.rstrip('/') + '/', '~/')   # the key's path under the target's own home
        b['keys'] = [os.path.relpath(os.path.expanduser(i), home) for i in b['identities']
                     if os.path.expanduser(i).startswith(home.rstrip('/') + '/') and os.path.exists(os.path.expanduser(i))]
    return found


def known_hosts(entry, runner=run):
    """The known_hosts lines for a machine (public host keys: `ssh-keygen -F`), so the target trusts the same host key."""
    name = entry['hostname'] if entry['port'] == '22' else f'[{entry["hostname"]}]:{entry["port"]}'
    out = []
    for n in dict.fromkeys([name, entry['alias']]):
        r = runner(['ssh-keygen', '-F', n])
        out += [line for line in (r.stdout or '').splitlines() if line and not line.startswith('#')]
    return list(dict.fromkeys(out))


def ssh_script(entries, lines_of):
    """On the target: each Host block appended to ~/.ssh/config when that alias has none, the host keys to known_hosts."""
    parts = ['set -e; umask 077; mkdir -p "$HOME/.ssh"; touch "$HOME/.ssh/config" "$HOME/.ssh/known_hosts"']
    for e in entries.values():
        block = shlex.quote('\n' + e['block'])   # literal: no escapes interpreted
        parts.append(f'grep -qiE "^Host[[:space:]]+{re.escape(e["alias"])}[[:space:]]*$" "$HOME/.ssh/config" || '
                     f'printf %s {block} >> "$HOME/.ssh/config"')
        for line in lines_of(e):
            q = shlex.quote(line)
            parts.append(f'grep -qxF {q} "$HOME/.ssh/known_hosts" || printf "%s\\n" {q} >> "$HOME/.ssh/known_hosts"')
    parts.append('chmod 600 "$HOME/.ssh/config" "$HOME/.ssh/known_hosts"')
    return '\n'.join(parts)


def reverse_ports(cfg, first=11435):
    """[(worker, port on the host, address here)] for each local model served on this computer's loopback: the client's ssh
    carries it to the host's own 127.0.0.1:<port> (never a LAN or public bind)."""
    out, port = [], first
    for name, w in cfg['workers'].items():
        host = str(w.get('host') or '')
        if w.get('kind') == 'ollama' and not w.get('machine') and not w.get('tunnel') and host.split(':')[0] in ('127.0.0.1', 'localhost'):
            out.append((name, port, host))
            port += 1
    return out
