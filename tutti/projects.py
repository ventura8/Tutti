"""The projects Tutti's work belongs to, for the dashboard: each job's folder (its working directory, its worktree) leads to a
git repository, named by its remote (`.../bucurestiuldisparut.github.io.git` -> that name) or its folder. Every machine is
also looked at, read-only, for processes working inside a git repository (a peer session's tests, its agent), its yield flag
and its GPU, so a project Tutti did not start (another session's) still shows with its activity. The look-around script runs
inside remote.probe's one ssh call; probe_activity reads that stashed round. Nothing is started or stopped here.

`projects:` in the config can name a project and say what belongs to it:
    projects: {RoRadio: {match: roradio, machines: [w5090], containers: [roradio-], windows: {w5090: 'C:\\Users\\me\\RoRadio'}}}
`match` is a regular expression over the repository's path and remote; `machines` lends the project those machines' yield
flag and GPU; `containers` claims the test containers with those name prefixes; `windows` names the project's folder on the
Windows side of a WSL machine, whose processes WSL cannot see: Windows processes whose command line names that folder's
name count for the project (read over the machine's plain ssh, program names and start times only)."""
import base64
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

from . import remote

WIN_SHELLS = {'cmd.exe', 'pwsh.exe', 'powershell.exe', 'conhost.exe', 'ssh.exe', 'openconsole.exe', 'windowsterminal.exe'}
SHELLS = {'bash', 'sh', 'zsh', 'fish', 'dash', 'tmux', 'tmux: server', 'screen', 'sshd', 'login', 'sleep', 'su', 'sudo', 'git',
          'ssh', 'ssh-agent', 'gpg-agent', 'dbus-daemon', 'systemd', 'cat', 'less', 'tail', 'flock', 'timeout', 'wsl-run64.sh'}
AGENTS = {'claude', 'cursor-agent', 'agy', 'copilot', 'gemini', 'codex', 'aider'}
NUMBER = re.compile(r'^\d+(\.\d+)?$')


# ---- one folder -> its repository, from the files under .git (no git process: the dashboard asks often)


def git_dir(path):
    """(the folder holding .git, the worktree's git dir) for `path` or a parent, or (None, None)."""
    d = os.path.abspath(path or '')
    while d and d != os.path.dirname(d):
        g = os.path.join(d, '.git')
        if os.path.isdir(g):
            return d, g
        if os.path.isfile(g):
            try:
                with open(g, encoding='utf8') as f:
                    line = f.read().strip()
            except OSError:
                return None, None
            if line.startswith('gitdir:'):
                return d, os.path.normpath(os.path.join(d, line[len('gitdir:'):].strip()))
        d = os.path.dirname(d)
    return None, None


def read(path):
    try:
        with open(path, encoding='utf8', errors='replace') as f:
            return f.read()
    except OSError:
        return ''


def common_dir(gdir):
    """A linked worktree's git dir names the main repository's .git in `commondir`."""
    c = read(os.path.join(gdir, 'commondir')).strip()
    return os.path.normpath(os.path.join(gdir, c)) if c else gdir


def origin_url(config_text):
    """The `url` of [remote "origin"] (else of the first remote) in a git config file's text, or ''."""
    section, first = None, ''
    for line in config_text.splitlines():
        s = line.strip()
        if s.startswith('['):
            section = s
            continue
        if section and section.startswith('[remote') and s.replace(' ', '').startswith('url='):
            url = s.split('=', 1)[1].strip()
            if section == '[remote "origin"]':
                return url
            first = first or url
    return first


HEAD_REF = 'ref: refs/heads/'


def branch_name(head):
    """The branch a HEAD file's line names, `detached <sha>` for a bare commit, None for nothing."""
    head = head.strip()
    if head.startswith(HEAD_REF):
        return head[len(HEAD_REF):]
    return f'detached {head[:8]}' if head else None


def branch_of(gdir):
    return branch_name(read(os.path.join(gdir, 'HEAD')))


def name_from(url, top):
    """The project's name: the remote's last part without .git, else the main checkout's folder name."""
    base = (url or '').rstrip('/').rsplit('/', 1)[-1].rsplit(':', 1)[-1]
    base = base[:-4] if base.endswith('.git') else base
    return base or os.path.basename(top.rstrip('/')) or None


def identify(path, cache=None):
    """{name, repo (the main checkout), remote, branch, folder} for a folder inside a git work tree, else None."""
    cache = {} if cache is None else cache
    if path in cache:
        return cache[path]
    top, gdir = git_dir(path)
    out = None
    if top:
        common = common_dir(gdir)
        repo = os.path.dirname(common) if os.path.basename(common) == '.git' else top
        url = origin_url(read(os.path.join(common, 'config')))
        out = {'name': name_from(url, repo), 'repo': repo, 'remote': url, 'branch': branch_of(gdir), 'folder': top}
    cache[path] = out
    return out


# ---- the config's names


def configured(cfg):
    return cfg.get('projects') or {}


def rename(cfg, found):
    """The configured project a repository belongs to (its `match` over the path and the remote), else its own name."""
    text = f'{found.get("repo") or ""} {found.get("remote") or ""} {found.get("folder") or ""}'
    for name, p in configured(cfg).items():
        if (p or {}).get('match') and re.search(p['match'], text, re.I):
            return name
    return found['name']


def by_match(cfg, text):
    """The configured project whose `match` finds itself in a path (an agent session's folder names its project), or None."""
    for name, p in configured(cfg).items():
        if text and (p or {}).get('match') and re.search(p['match'], text, re.I):
            return name
    return None


def container_project(cfg, title):
    for name, p in configured(cfg).items():
        if any(title.startswith(x) for x in (p or {}).get('containers') or []):
            return name
    return None


def run_worktree(cfg, job):
    """A delegated run's worktree, from its report (the job's log is <runs>/<id>.log)."""
    from . import delegate  # delegate imports modules that import the dashboard's
    if job.get('kind') != 'run':
        return None
    run_id = os.path.basename(job.get('log') or '')[:-len('.log')]
    report = delegate.load_report(cfg, run_id) or {}
    meta = delegate.read_meta(cfg, run_id) if delegate.valid_id(run_id) else {}
    folders = [report.get('worktree'), meta.get('worktree'), meta.get('repo')]   # a discarded worktree still has its repo
    return next((f for f in folders if f and os.path.isdir(f)), None)


def job_folder(cfg, job):
    return job.get('cwd') or job.get('worktree') or run_worktree(cfg, job)


def tag(cfg, jobs, cache=None):
    """Each job gets `project` (a name or None) and, when known, `repo` and `branch`."""
    cache = {} if cache is None else cache
    for j in jobs:
        folder = job_folder(cfg, j)
        found = identify(folder, cache) if folder else None
        if found:
            j.update(project=rename(cfg, found), repo=found['repo'], branch=found['branch'])
        elif j.get('kind') == 'remote':
            j['project'] = container_project(cfg, j['title'])
        else:
            j['project'] = j.get('project') or by_match(cfg, j.get('path') or j.get('log') or '')
    return jobs


# ---- every machine, read-only: processes working inside a git repository, the GPU


def activity_script():
    """Prints `W\\t<cwd>\\t<program>` for each of the user's processes in a folder under $HOME (not $HOME itself), then
    `R\\t<cwd>\\t<top>\\t<branch>\\t<remote url>` for each such folder in a git work tree, then `G <util %> <MiB used> <MiB total>`
    when an NVIDIA GPU answers. It only reads."""
    # a private folder of its own (mode 700, mktemp): never a predictable /tmp name another login could read or plant
    return ('export PATH="$PATH:/usr/lib/wsl/lib"; T=$(umask 077; mktemp -d 2>/dev/null) || exit 0; '
            'trap \'rm -f "$T/cwd" "$T/activity"; rmdir "$T"\' EXIT; '
            # two processes for all of them (find for the folders, ps for the programs), joined by pid in awk: a fork
            # or three per process took 14 s on a computer with 800 processes (2026-11-14), now 0.2 s
            'if find /proc/self/cwd -maxdepth 0 -printf "" 2>/dev/null && ps -o pid=,args= -p $$ > /dev/null 2>&1; then '
            'find /proc/[0-9]*/cwd -maxdepth 0 -user "$(id -u)" -printf "%h\\t%l\\n" 2>/dev/null > $T/cwd; '
            'ps -u "$(id -u)" -o pid=,args= 2>/dev/null | awk -v H="$HOME/" -v F=$T/cwd '
            "'BEGIN { while ((getline l < F) > 0) { split(l, a, \"\\t\"); sub(\"/proc/\", \"\", a[1]); c[a[1]] = a[2] } } "
            "{ d = c[$1]; if (d == \"\" || index(d, H) != 1) next; n = $2; sub(/.*\\//, \"\", n); "
            "if (n != \"\") printf \"W\\t%s\\t%s\\n\", d, n }' > $T/activity; rm -f $T/cwd; "
            # BusyBox (Alpine, some Termux) has neither find -printf nor ps -o: the portable loop, slower but right
            'else for p in /proc/[0-9]*; do [ -O "$p" ] || continue; c=$(readlink "$p/cwd" 2>/dev/null) || continue; '
            'case "$c" in "$HOME"/*) ;; *) continue ;; esac; '
            'n=$(tr "\\0" "\\n" < "$p/cmdline" 2>/dev/null | head -n 1); n=${n%% *}; [ -n "$n" ] || continue; '
            'printf "W\\t%s\\t%s\\n" "$c" "${n##*/}"; done > $T/activity; fi; '
            'cat $T/activity; '
            'cut -f2 $T/activity | sort -u | while IFS= read -r c; do '
            't=$(git -C "$c" rev-parse --show-toplevel 2>/dev/null) || continue; '
            'b=$(git -C "$c" symbolic-ref --short -q HEAD || echo "detached $(git -C "$c" rev-parse --short HEAD 2>/dev/null)"); '
            'u=$(git -C "$c" config --get remote.origin.url 2>/dev/null); '
            'cm=$(git -C "$c" rev-parse --path-format=absolute --git-common-dir 2>/dev/null); '
            'printf "R\\t%s\\t%s\\t%s\\t%s\\t%s\\n" "$c" "$t" "$b" "$u" "$cm"; done; rm -f $T/activity; '
            'nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null '
            '| head -n 1 | tr -d "," | sed "s/^/G /"; true')


def parse_gpu(line):
    parts = line.split()[1:]
    if len(parts) != 3 or not all(NUMBER.match(x) for x in parts):
        return None
    util, used, total = (float(x) for x in parts)
    return {'util_pct': util, 'mem_used_mib': used, 'mem_total_mib': total}


def parse_activity(text):
    """{'repos': {cwd: {top, branch, remote, repo}}, 'procs': [(cwd, program)], 'gpu': {...} or None} from activity_script's lines."""
    out = {'repos': {}, 'procs': [], 'gpu': None}
    for line in (text or '').splitlines():
        f = line.split('\t')
        if f[0] == 'W' and len(f) == 3:
            out['procs'].append((f[1], f[2]))
        elif f[0] == 'R' and len(f) >= 5:
            common = f[5] if len(f) > 5 else ''
            repo = os.path.dirname(common) if os.path.basename(common) == '.git' else f[2]
            out['repos'][f[1]] = {'top': f[2], 'branch': f[3] or None, 'remote': f[4], 'repo': repo}
        elif line.startswith('G '):
            out['gpu'] = parse_gpu(line)
    return out


def probe_activity(machine, timeout=25):
    """What one machine is working on, from the last remote.probe() round, or a one-off ssh. Read-only."""
    cached = remote.round_activity(machine)
    if cached is not None:
        return cached
    try:
        r = remote.sh(machine, activity_script(), capture=True, timeout=timeout, ssh_opts=('-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes'))
    except (OSError, subprocess.SubprocessError) as e:
        return {'error': f'not looked at ({type(e).__name__}): the machine did not answer in {timeout} s'}
    if r.returncode != 0:
        return {'error': f'the look-around script exited {r.returncode}: {(r.stderr or "").strip()[:200]}'}
    return parse_activity(r.stdout)


def windows_folders(cfg, machine_name):
    """[(project, Windows folder)] the config names for this machine."""
    return [(name, (p or {})['windows'][machine_name]) for name, p in configured(cfg).items()
            if isinstance((p or {}).get('windows'), dict) and (p or {})['windows'].get(machine_name)]


def ps_quote(text):
    return "'" + str(text).replace("'", "''") + "'"


def windows_script(folders):
    """PowerShell that prints `X\t<project>\t<program>\t<started>` for each Windows process whose command line names a
    project's folder name, `B\t<project>\t<HEAD>` for each folder and `G <util> <used> <total>` for an NVIDIA GPU. Only reads."""
    pairs = '; '.join(f'{ps_quote(p)}={ps_quote(f)}' for p, f in folders)
    return (f'$w=@{{{pairs}}}; '
            'Get-CimInstance Win32_Process | ForEach-Object { $c=$_.CommandLine; if($c){ foreach($k in $w.Keys){ '
            '$n=Split-Path -Leaf $w[$k]; if($c -like "*$n*"){ "X`t$k`t$($_.Name)`t$($_.CreationDate.ToString(\'o\'))"; break } } } }; '
            'foreach($k in $w.Keys){ '
            '$h=Get-Content (Join-Path $w[$k] \'.git\\HEAD\') -ErrorAction SilentlyContinue | Select-Object -First 1; '
            '"B`t$k`t$h" }; '
            '$g=(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>$null '
            '| Select-Object -First 1); '
            "if($g){ 'G ' + ($g -replace ',', '') }")


def parse_windows(text):
    out = {'procs': [], 'branches': {}, 'gpu': None}
    for line in (text or '').splitlines():
        f = line.rstrip('\r').split('\t')
        if f[0] == 'X' and len(f) == 4:
            out['procs'].append((f[1], f[2], f[3]))
        elif f[0] == 'B' and len(f) == 3:
            out['branches'][f[1]] = branch_name(f[2])
        elif line.startswith('G '):
            out['gpu'] = parse_gpu(line.rstrip('\r'))
    return out


def probe_windows(machine, folders, timeout=40):
    """The Windows side of a WSL machine, over its plain ssh (the Windows host), or {'error': why}. Read-only."""
    script = base64.b64encode(windows_script(folders).encode('utf-16le')).decode()
    argv = [*remote.SSH, *remote.mux_opts(machine), '-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes', machine['host'],
            f'powershell -NoProfile -NonInteractive -EncodedCommand {script}']
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return {'error': f'the Windows side was not looked at ({type(e).__name__})'}
    if r.returncode != 0:
        why = (r.stderr or '').strip()[:200]
        return {'error': f'the Windows side answered with exit {r.returncode} (is its OpenSSH server up?): {why}'}
    return parse_windows(r.stdout)


def look(cfg, name, activity_fn, windows_fn):
    machine = cfg['machines'][name]
    act = activity_fn(machine)
    folders = windows_folders(cfg, name)
    if folders and machine['transport'] == 'wsl' and not act.get('error'):
        act = {**act, 'windows': windows_fn(machine, folders)}
        act['gpu'] = act['gpu'] or act['windows'].get('gpu')
    return act


def probe_all(cfg, activity_fn=probe_activity, windows_fn=probe_windows):
    names = list(cfg['machines'])
    if not names:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
        return dict(zip(names, pool.map(lambda n: look(cfg, n, activity_fn, windows_fn), names), strict=True))


# ---- the projects, as the dashboard shows them


def blank(name):
    return {'name': name, 'repos': {}, 'jobs': 0, 'running': 0, 'agents': 0, 'runs': 0, 'last_activity': None, 'machines': {}}


def latest(a, b):
    return max(x for x in (a, b) if x) if (a or b) else None


def add_job(p, j):
    p['jobs'] += 1
    p['running'] += j['status'] in ('running', 'stalled')
    p['agents'] += j['kind'] in ('agent', 'agent task') and j['status'] in ('running', 'stalled')
    p['runs'] += j['kind'] == 'run'
    p['last_activity'] = latest(p['last_activity'], j.get('updated') or j.get('started'))
    if j.get('repo'):
        p['repos'].setdefault(f'here:{j["repo"]}', {'machine': 'here', 'path': j['repo'], 'branch': j.get('branch')})


def home_project(cfg, cache):
    """The project of the local worktrees (the first folder under project.worktrees that is a git work tree)."""
    if 'home' not in cache:
        root = os.path.expanduser(cfg['project']['worktrees'])
        names = sorted(os.listdir(root)) if os.path.isdir(root) else []
        found = (identify(os.path.join(root, n), cache) for n in names if os.path.isdir(os.path.join(root, n)))
        cache['home'] = next((f for f in found if f), None)
    return cache['home']


def copy_of(cfg, machine, repo, cache):
    """A track's copy under the machine's root (<root>/<track>, made by `tutti test`) is the local worktree's project."""
    root = machine.get('root') or ''
    seg = '/' + root.replace('~/', '').strip('/') + '/'
    if not root or seg not in repo + '/':
        return None
    track = (repo + '/').split(seg, 1)[1].split('/', 1)[0]
    return identify(os.path.join(os.path.expanduser(cfg['project']['worktrees']), track), cache) or home_project(cfg, cache)


def repo_project(cfg, machine, r, cache):
    found = copy_of(cfg, machine, r['repo'], cache) if not r['remote'] else None
    found = found or {'name': name_from(r['remote'], r['repo']), 'repo': r['repo'], 'remote': r['remote'], 'folder': r['top']}
    return rename(cfg, found)


def count(p, machine, prog, when, path, branch, shells):
    m = p['machines'].setdefault(machine, {'processes': 0, 'agents': 0, 'programs': []})
    if prog.lower() in shells:
        return
    m['processes'] += 1
    m['agents'] += prog.lower().removesuffix('.exe') in AGENTS
    if prog not in m['programs']:
        m['programs'].append(prog)
    p['last_activity'] = latest(p['last_activity'], when)
    p['repos'].setdefault(f'{machine}:{path}', {'machine': machine, 'path': path, 'branch': branch})


def add_machine(cfg, projects, machine, act, now_iso, cache=None):
    """The processes a machine runs inside each repository (and on its Windows side), counted per project."""
    cache = {} if cache is None else cache
    conf = cfg['machines'].get(machine) or {}
    for cwd, prog in act.get('procs') or []:
        r = act['repos'].get(cwd)
        if r:
            name = repo_project(cfg, conf, r, cache)
            count(projects.setdefault(name, blank(name)), machine, prog, now_iso, r['repo'], r['branch'], SHELLS)
    win = act.get('windows') or {}
    folders = dict(windows_folders(cfg, machine))
    for name, prog, started in win.get('procs') or []:
        p = projects.setdefault(name, blank(name))
        count(p, machine, prog, started, folders.get(name, '?'), win['branches'].get(name), WIN_SHELLS)
    for name, branch in (win.get('branches') or {}).items():
        p = projects.setdefault(name, blank(name))
        p['repos'].setdefault(f'{machine}:{folders.get(name)}', {'machine': machine, 'path': folders.get(name), 'branch': branch})


def lend_machines(cfg, projects, machines, activity):
    """A configured project's machines: their yield flag and GPU (RoRadio's PC: its flag says the PC is RoRadio's)."""
    by_name = {m['name']: m for m in machines}
    for name, conf in configured(cfg).items():
        for mname in (conf or {}).get('machines') or []:
            p = projects.setdefault(name, blank(name))
            m = p['machines'].setdefault(mname, {'processes': 0, 'agents': 0, 'programs': []})
            probe = by_name.get(mname) or {}
            m.update(yield_flag=probe.get('paused'), status=probe.get('status'), gpu=(activity.get(mname) or {}).get('gpu'))


def projects_state(cfg, jobs, machines, activity, conduct_run=None, now_iso=None):
    """[{name, repos, jobs, running, agents, runs, last_activity, machines}] busiest first, and the jobs' project names."""
    projects = {}
    for j in jobs:
        if j.get('project'):
            add_job(projects.setdefault(j['project'], blank(j['project'])), j)
    cache = {}
    for mname, act in (activity or {}).items():
        if not act.get('error'):
            add_machine(cfg, projects, mname, act, now_iso, cache)
    lend_machines(cfg, projects, machines, activity or {})
    add_conductor(cfg, projects, conduct_run)
    items = [{**p, 'repos': list(p['repos'].values()), 'machines': [{'name': n, **m} for n, m in p['machines'].items()]}
             for p in projects.values()]
    items.sort(key=lambda p: (-p['running'] - sum(m['processes'] for m in p['machines']), p['name'].lower()))
    return {'items': items, 'errors': look_errors(activity or {})}


def add_conductor(cfg, projects, conduct_run):
    """The project a running `tutti conduct` works in shows its main agent."""
    found = identify(conduct_run.get('cwd')) if conduct_run and conduct_run.get('status') == 'running' else None
    if found:
        name = rename(cfg, found)
        p = projects.setdefault(name, blank(name))
        p['conductor'] = conduct_run['agents'][-1]['agent'] if conduct_run.get('agents') else None


def look_errors(activity):
    errors = {n: a['error'] for n, a in activity.items() if a.get('error')}
    errors.update({f'{n} (Windows)': a['windows']['error'] for n, a in activity.items()
                   if isinstance(a.get('windows'), dict) and a['windows'].get('error')})
    return errors
