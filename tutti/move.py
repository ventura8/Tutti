"""`tutti move --to <machine>`: Tutti (the dashboard, the state, the loops) moves to another machine (docs/MOVE.md).

One guided, resumable operation, the same from the terminal and from the dashboard's "Move Tutti to…" button:

  preflight -> install -> copy -> freeze -> jobs -> sync -> configure -> start -> health -> client -> verify -> done

Each step is recorded in <state>/move/journal.json as it finishes, so an interrupted move continues where it stopped
(`tutti move --to <m>` again) and `tutti move --rollback` knows exactly what to undo. A failed health check rolls back by
itself. `--dry-run` runs the preflight and prints the whole plan, changing nothing.

The old computer becomes a client (tutti/clientmode.py): its commands and its connected agents reach the new host over
ssh, and `tutti client` keeps one ssh connection up that carries the dashboard's port and the bridge (tutti/bridge.py),
through which the host runs the jobs that need the old computer's repositories and sign-ins. Nothing is ever exposed to
the internet: the dashboard stays on 127.0.0.1 at both ends, and every address the move writes is checked by
tutti/netpolicy.py (localhost, LAN or VPN only)."""
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import yaml

from . import __version__, bridge, clientmode, config, delegate, guided, install, netpolicy, remote, yamledit

STEPS = ('preflight', 'install', 'copy', 'freeze', 'jobs', 'sync', 'configure', 'start', 'health', 'client', 'verify', 'done')
STEP_TEXT = {
    'preflight': 'check the target: ssh, OS and architecture, disk, Tutti, addresses, always-on',
    'install': 'install Tutti there when it is missing or older (the one-line install, tutti/install.py)',
    'copy': 'copy the state, the usage log and the per-login state (while Tutti still runs here)',
    'freeze': 'stop the dashboard here: no more writes to the state here',
    'jobs': 'running autopilot jobs: wait for them, or hand them over',
    'sync': 'copy what changed since (the final sync)',
    'configure': 'write the target\'s config (machine names from its point of view) and the secrets (mode 600)',
    'start': 'start Tutti there as a user service (systemd --user), and its stable name on the LAN',
    'health': 'check that the dashboard answers there (a failure rolls back by itself)',
    'client': 'this computer becomes a client: its config points at the new host; tutti-client keeps the bridge and the dashboard',
    'verify': 'the bridge is up and the dashboard opens here through it',
    'done': 'record the move',
}
FOLDER = 'move'
JOURNAL = 'journal.json'
LAST = 'last.json'
FROZEN = 'frozen'
NAME = 'tutti.local'       # the stable LAN name members and clients use (mDNS); a VPN name may replace it (host.address)
UNIT = f'{install.SERVICE}.service'   # the login service tutti setup installs (tutti/install.py)
NAME_UNIT = 'tutti-name.service'
CLIENT_UNIT = 'tutti-client.service'
STATE_EXCLUDE = ['/move/', '/bridge/', '/host-mirror/', '/llm-busy-*', '*.lock', '/locks/']
XDG_STATE_EXCLUDE = ['/locks/', '/waits/']
SECRET_FILES = ('*.env', 'household.json')
# commands that never change the state; every read (clientmode.READ_ONLY) runs too
SAFE_DURING_FREEZE = ('move', 'config', 'doctor', 'connect', 'mcp', 'bridge', 'client')
PROGRAMS = ('git', 'gh', 'docker')
FREE_MARGIN_GB = 1.0
ROLLED_BACK = 'rolled back'
BACKUP = '.bak-pre-move'
PRIVATE_NAMES = ('.local', '.ts.net')   # mDNS and Tailscale names: they resolve only on the LAN or the tailnet


class MoveError(RuntimeError):
    """A step that cannot go on: the message says what, why and the fix."""


# ---- config: the host block and its checks


def errors(cfg):
    h = cfg.get('host')
    if h in (None, {}):
        return []
    if not isinstance(h, dict):
        return [f'host is {h!r}: a mapping written by `tutti move` (machine, me, ...); remove it to run Tutti here']
    errs = [f'host.{k} is missing: `tutti move` writes it; run `tutti move --rollback` or remove host:' for k in ('machine', 'me')
            if not h.get(k)]
    if h.get('machine') and h.get('me') and h['machine'] != h['me']:
        errs += [f'host.{k} is missing on this client: run the move again (`tutti move --to {h["machine"]}`)'
                 for k in ('ssh', 'config', 'state') if not h.get(k)]
    if h.get('jobs') and h['jobs'] not in cfg['machines']:
        errs.append(f'host.jobs names "{h["jobs"]}", which is not under machines:')
    if h.get('address') and not str(h['address']).endswith(PRIVATE_NAMES):
        errs += netpolicy.errors('host.address', h['address'])
        if not errs and not netpolicy._is_ip(str(h['address'])):
            errs.append(f'host.address is {h["address"]}: tutti.local, a *.local or *.ts.net name, or a LAN or VPN address '
                        f'(never a public name: {netpolicy.WHY})')
    return errs


def move_dir(cfg):
    d = os.path.join(config.state_dir(cfg), FOLDER)
    os.makedirs(d, exist_ok=True)
    return d


def frozen_note(cfg, cmd, action=None):
    """While a move copies the state, a command that would change it here is refused (it would be lost)."""
    frozen = os.path.exists(os.path.join(config.state_dir(cfg), FOLDER, FROZEN))
    if not frozen or cmd in SAFE_DURING_FREEZE or clientmode.is_read_only(cmd, action):
        return ''
    j = read_journal(cfg)
    return (f'Tutti is moving to {j.get("to", "another machine")} (step {j.get("step", "?")}): `{cmd}` would change a state that is '
            f'being copied. Wait for the move (`tutti move --status`), or undo it with `tutti move --rollback`')


def read_journal(cfg):
    try:
        with open(os.path.join(move_dir(cfg), JOURNAL), encoding='utf8') as f:
            j = json.load(f)
    except (OSError, ValueError):
        return {}
    return j if isinstance(j, dict) else {}


def write_journal(cfg, j):
    path = os.path.join(move_dir(cfg), JOURNAL)
    with open(path + '.tmp', 'w', encoding='utf8') as f:
        json.dump(j, f, indent=1)
    os.replace(path + '.tmp', path)


def log(cfg, j, text, say=None):
    j.setdefault('log', []).append(f'{time.strftime("%H:%M:%S")} {text}')
    j['log'] = j['log'][-200:]
    write_journal(cfg, j)
    if say:
        say(text)


# ---- the preflight: what the target is


def preflight_script(programs, reach):
    lines = [
        'echo "os=$(uname -s)"; echo "arch=$(uname -m)"; echo "home=$HOME"; echo "user=$(id -un)"; echo "hostname=$(hostname)"',
        'echo "free_gb=$(df -Pk "$HOME" | awk \'NR==2{printf "%.1f", $4/1048576}\')"',
        't=$(command -v tutti || ls "$HOME/.local/bin/tutti" 2>/dev/null); echo "tutti=$t"',
        '[ -n "$t" ] && echo "tutti_version=$("$t" --version 2>/dev/null | awk \'{print $2}\')"',
        'echo "python=$(python3 -c \'import sys; print("%d.%d" % sys.version_info[:2])\' 2>/dev/null)"',
        'python3 -c "import venv, ensurepip" 2>/dev/null && echo venv=yes || echo venv=no',
        'echo "rsync=$(command -v rsync)"; echo "avahi=$(command -v avahi-publish)"',
        'echo "systemd_user=$(systemctl --user is-system-running 2>/dev/null)"',
        'echo "linger=$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)"',
        'echo "suspend=$(systemctl is-enabled suspend.target 2>/dev/null)"',
        'echo "idle_action=$(grep -hs "^IdleAction=" /etc/systemd/logind.conf /etc/systemd/logind.conf.d/*.conf | tail -1 | cut -d= -f2)"',
        'echo "uptime_s=$(cut -d. -f1 /proc/uptime 2>/dev/null)"',
        'echo "ifaces=$(ip -j addr 2>/dev/null | base64 -w0)"',
        'PATH="$HOME/.local/bin:$PATH" gh auth status >/dev/null 2>&1 && echo gh_auth=yes || echo gh_auth=no',
    ]
    lines += [f'echo "has_{p}=$(command -v {shlex.quote(p)})"' for p in programs]
    lines += [f'ssh -o BatchMode=yes -o ConnectTimeout=3 {shlex.quote(host)} true >/dev/null 2>&1 && echo reach_{n}=yes '
              f'|| echo reach_{n}=no' for n, host in reach]
    return '\n'.join(lines)


def parse_facts(text):
    import base64
    facts = {}
    for line in text.splitlines():
        k, sep, v = line.partition('=')
        if sep and re.match(r'^[a-z_][a-z0-9_-]*$', k):
            facts[k] = v.strip()
    try:
        rows = json.loads(base64.b64decode(facts.get('ifaces') or '') or b'[]')
    except ValueError:
        rows = []
    facts['ifaces'] = [(r.get('ifname', ''), (r.get('linkinfo') or {}).get('info_kind') or '',
                        [a.get('local') for a in r.get('addr_info') or [] if a.get('local')]) for r in rows]
    return facts


def programs_of(cfg):
    """The programs the jobs and readers use: each worker's and each reader's or plan's first word, and git, gh, docker."""
    out = list(PROGRAMS)
    for w in cfg['workers'].values():
        if w.get('command'):
            out.append(os.path.basename(w['command'][0]))
    for a in cfg['accounts'].values():
        for k in ('reader', 'plan'):
            if ((a or {}).get(k) or {}).get('command'):
                out.append(os.path.basename(a[k]['command'][0]))
    return sorted({p for p in out if re.match(r'^[A-Za-z0-9._-]+$', p)})


def ssh_machines(cfg, target):
    return [(n, m['host']) for n, m in cfg['machines'].items() if m['transport'] in ('ssh', 'wsl') and n != target and not m.get('via')]


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r'\d+', v or '')[:3]) or (0,)


def judge(facts, need_gb):
    """(blockers, warnings, owner_steps) of the target's facts."""
    blockers, warnings, owner = [], [], []
    if not facts.get('os'):
        return [f'the target did not answer over ssh: {facts.get("_error") or "no reply"}. Check `ssh <it> true` from here '
                f'(a key, not a password: BatchMode)'], [], []
    if facts['os'] != 'Linux':
        blockers.append(f'the target runs {facts["os"]}: a host needs Linux with systemd for now (docs/MOVE.md); pick a Linux machine')
    free = float(facts.get('free_gb') or 0)
    if free < need_gb:
        blockers.append(f'the target has {free:g} GB free in its home and the state needs {need_gb:g} GB: free space there first')
    if not facts.get('rsync'):
        blockers.append('rsync is missing on the target: install it there (e.g. `sudo apt install rsync`), then run the move again')
    if facts.get('venv') != 'yes' and not facts.get('tutti'):
        blockers.append('python3 venv is missing on the target, so Tutti cannot be installed: `sudo apt install python3-venv` there')
    if not facts.get('systemd_user'):
        blockers.append('systemd --user does not answer on the target: a host keeps Tutti running as a user service')
    if facts.get('linger') != 'yes':
        owner.append(guided.step('sudo', f'Keep Tutti running on {facts.get("hostname", "the target")} with nobody signed in',
                                 f'sudo loginctl enable-linger {facts.get("user", "<user>")}',
                                 'a user service stops at sign-out unless lingering is on (run it on the target)'))
    if facts.get('suspend') != 'masked' or facts.get('idle_action') not in ('', 'ignore', None):
        warnings.append('the target may suspend when idle, which stops Tutti')
        owner.append(guided.step('sudo', f'Keep {facts.get("hostname", "the target")} always on',
                                 'sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target',
                                 'an always-on host never sleeps; suspend is not masked there (run it on the target)'))
    return blockers, warnings, owner


# ---- the config, from the target's point of view


def home_map(path, src_home, dst_home):
    """A path under this computer's home, the same path under the target's home; any other path unchanged."""
    if not isinstance(path, str):
        return path
    full = config.expand(path) if path.startswith('~') else path
    if full == src_home or full.startswith(src_home.rstrip('/') + '/'):
        return dst_home.rstrip('/') + full[len(src_home.rstrip('/')):]
    return path


def source_name(cfg):
    """This computer's machine name: the one with host: local (a machine is added when none has it)."""
    h = cfg.get('host') or {}
    if h.get('me'):
        return h['me']
    return next((n for n, m in cfg['machines'].items() if m['transport'] == 'local'), socket.gethostname().split('.')[0].lower())


def machine_edits(cfg, target, facts, src):
    """The target becomes local; this computer an ssh machine reached through its own bridge; a machine the target cannot ssh
    to is probed through this computer too."""
    edits = [(['machines', target, 'host'], 'local'), (['machines', target, 'transport'], yamledit.DELETE)]
    me = {'host': src['address'], 'user': src['user'], 'via': src['name']}
    if src['name'] in cfg['machines']:
        edits += [(['machines', src['name'], k], v) for k, v in me.items()]
    else:
        edits.append((['machines', src['name']], me))
    return edits + [(['machines', n, 'via'], src['name']) for n, _h in ssh_machines(cfg, target) if facts.get(f'reach_{n}') != 'yes']


def path_edits(raw, cfg, facts):
    """The state, the worktrees and the usage log under the target's home."""
    src_home, dst_home = os.path.expanduser('~'), facts['home']
    keys = (['project', 'state'], ['project', 'worktrees'], ['usage', 'log'])
    edits = [(key, home_map(_get(raw, key), src_home, dst_home)) for key in keys if isinstance(_get(raw, key), str)]
    if _get(raw, ['project', 'state']) is None:
        edits.append((['project', 'state'], home_map(config.expand(cfg['project']['state']), src_home, dst_home)))
    return edits


def needs_here(a, missing):
    """An account whose reader or plan program is not on the target (or is built in, reading this computer's sign-in)."""
    cmd = [((a.get(k) or {}).get('command') or [''])[0] for k in ('reader', 'plan') if a.get(k)]
    return any(os.path.basename(c) in missing or not c for c in cmd)


def account_edits(raw, cfg, missing, src):
    """Readings that need this computer's sign-ins or files are read here, through the bridge."""
    edits = []
    for name, a in cfg['accounts'].items():
        if name not in (raw.get('accounts') or {}):
            continue
        if needs_here(a or {}, missing):
            edits.append((['accounts', name, 'read_on'], src['name']))
        if (a or {}).get('reading_file'):
            edits.append((['accounts', name, 'reading_from'], src['name']))
    return edits


def host_edits(raw, cfg, target, facts, src):
    """[(dotted path as a list, value)] that turn this computer's config into the target's (the host's)."""
    missing = {p for p in programs_of(cfg) if not facts.get(f'has_{p}')}
    edits = machine_edits(cfg, target, facts, src) + path_edits(raw, cfg, facts) + account_edits(raw, cfg, missing, src)
    jobs_need = {os.path.basename(w['command'][0]) for w in cfg['workers'].values() if w.get('command')}
    host = {'machine': target, 'me': target, 'address': src['address_name'], 'port': src['port']}
    if jobs_need & missing or cfg['autopilot'].get('repos'):
        host['jobs'] = src['name']   # the repositories and the workers' sign-ins stay on this computer: jobs run here, via its bridge
    edits.append((['host'], host))
    binds = (['household', 'bind'], ['dashboard', 'bind'])
    return edits + [(key, 'auto') for key in binds if _get(raw, key) not in (None, 'auto')]   # the target's own LAN and VPN addresses


def client_block(cfg, target, facts, host_cfg_path, src):
    m = cfg['machines'][target]
    state = home_map(config.expand(cfg['project']['state']), os.path.expanduser('~'), facts['home'])
    usage = home_map(config.expand((cfg.get('usage') or {}).get('log') or os.path.join(cfg['project']['state'], 'usage.jsonl')),
                     os.path.expanduser('~'), facts['home'])
    return {'machine': target, 'me': src['name'], 'ssh': remote.dest(m), 'config': host_cfg_path,
            'tutti': facts.get('tutti') or f'{facts["home"]}/.local/bin/tutti', 'state': state, 'usage_log': usage,
            'address': src['address_name'], 'port': src['port'], 'moved': time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}


def _get(d, path):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def apply_edits(text, edits):
    for path, value in edits:
        if value is yamledit.DELETE and _get(yaml.safe_load(text) or {}, path) is None:
            continue
        text = yamledit.edit(text, path, value)
    return text


def origins(cfg, port):
    """The OAuth client's authorized JavaScript origins for this config's dashboard (auth.required_origins when present)."""
    try:
        from . import auth
        return list(auth.required_origins(cfg, port))
    except (ImportError, AttributeError):
        own = [o.rstrip('/') for o in ((cfg.get('dashboard') or {}).get('origins') or []) if isinstance(o, str)]
        return [f'http://127.0.0.1:{port}', f'http://localhost:{port}', *own]


def check_origins(found, ifaces):
    """Every origin must name localhost or a LAN or VPN address (netpolicy); a public one is a blocker."""
    bad = []
    for o in found:
        host = re.sub(r'^\w+://', '', o).split('/', 1)[0].rsplit(':', 1)[0].strip('[]')
        if host in ('localhost', NAME) or host.endswith(PRIVATE_NAMES):
            continue
        try:
            netpolicy.check(host, 'an authorized origin', ifaces)
        except netpolicy.PublicAddress as e:
            bad.append(str(e))
    return bad


# ---- the plan (what --dry-run prints)


def source_facts(cfg, target_facts):
    vpn_or_lan = netpolicy.private_addresses(netpolicy.interfaces())
    t_lan = netpolicy.binds('auto', 'the target\'s dashboard', target_facts['ifaces'])
    lan = next((a for a in t_lan if '.' in a and not a.startswith('127.')), '127.0.0.1')   # its first LAN or VPN IPv4
    name = NAME if target_facts.get('avahi') else lan
    return {'name': source_name(cfg), 'user': os.environ.get('USER') or '', 'address': vpn_or_lan[0] if vpn_or_lan else '127.0.0.1',
            'address_name': name, 'target_lan': lan, 'port': int(cfg['dashboard']['port'])}


def state_size_gb(cfg):
    total = 0
    for root, _dirs, files in os.walk(config.state_dir(cfg)):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return round(total / 1e9, 2)


def bind_blockers(host_text, ifaces):
    """A bind the host's config would listen on that is not localhost, LAN or VPN (netpolicy) is a blocker."""
    out = []
    for key in (['household', 'bind'], ['dashboard', 'bind']):
        v = _get(yaml.safe_load(host_text) or {}, key)
        if v in (None, 'auto'):
            continue
        try:
            netpolicy.check(v, '.'.join(key), ifaces)
        except netpolicy.PublicAddress as e:
            out.append(str(e))
    return out


def sign_ins(cfg, target, facts, missing):
    """The sign-ins only the owner can do on the target: each worker whose jobs run there, and gh for the host's own loops."""
    ssh = remote.dest(cfg['machines'][target])
    out = [guided.step('sign-in', f'Sign {p} in on {target}', f'ssh -t {ssh} {p} login',
                       'its jobs run there now; Tutti never copies another tool\'s sign-in') for p in missing]
    if uses_github(cfg) and facts.get('gh_auth') != 'yes':
        out.append(guided.step('sign-in', f'Sign GitHub\'s gh in on {target}', f'ssh -t {ssh} ~/.local/bin/gh auth login --web',
                               'the host lists pull requests and posts scout answers and self-improvement tasks with gh'))
    return out


def make_plan(cfg, target, facts):
    """Everything --dry-run shows and the move does: blockers, warnings, owner steps, the configs, the origins."""
    need = round(state_size_gb(cfg) * 2 + FREE_MARGIN_GB, 1)
    blockers, warnings, owner = judge(facts, need)
    if blockers:
        return {'to': target, 'blockers': blockers, 'warnings': warnings, 'owner': owner, 'facts': facts}
    src = source_facts(cfg, facts)
    if src['target_lan'] == '127.0.0.1':
        lan = ', '.join(a for _n, _k, addrs in facts['ifaces'] for a in addrs) or 'none'
        blockers.append(f'the target has no LAN or VPN address (it has {lan}): a household host is reached over the LAN or a VPN, '
                        f'never the internet ({netpolicy.WHY}); connect it to the LAN or the VPN first')
        return {'to': target, 'blockers': blockers, 'warnings': warnings, 'owner': owner, 'facts': facts}
    with open(cfg['_path'], encoding='utf8') as f:
        text = f.read()
    raw = yaml.safe_load(text) or {}
    host_path = f'{facts["home"]}/.config/tutti/config.yaml'
    host_text = apply_edits(text, host_edits(raw, cfg, target, facts, src))
    hcfg = config.normalise(yaml.safe_load(host_text))
    hcfg['_path'] = host_path
    before, after = origins(cfg, src['port']), origins(hcfg, src['port'])
    blockers += check_origins(after, facts['ifaces']) + bind_blockers(host_text, facts['ifaces'])
    missing = [p for p in programs_of(cfg) if not facts.get(f'has_{p}')]
    jobs = hcfg['host'].get('jobs')
    logins = sign_ins(cfg, target, facts, [] if jobs else missing)
    choices = [guided.choice('host.address', src['address_name'], 'the stable LAN name clients and members use'),
               guided.choice('host.port', src['port'], 'the dashboard stays on 127.0.0.1 at both ends; the client\'s ssh carries it')]
    if jobs:
        choices.append(guided.choice('host.jobs', jobs, f'the repositories and the workers\' sign-ins are on {jobs}; '
                                                         f'{target} runs its jobs there through the bridge'))
    return {'to': target, 'blockers': blockers, 'warnings': warnings, 'owner': owner, 'facts': facts, 'src': src,
            'host_text': host_text, 'host_path': host_path, 'client': client_block(cfg, target, facts, host_path, src),
            'origins': {'before': before, 'after': after, 'add': [o for o in after if o not in before],
                        'remove': [o for o in before if o not in after]},
            'jobs': jobs, 'missing': missing, 'logins': logins, 'choices': choices, 'need_gb': need,
            'install': not facts.get('tutti') or version_tuple(facts.get('tutti_version')) < version_tuple(__version__)}


def uses_github(cfg):
    """Whether the host's own loops call gh: a project's repo (its pull requests), a scout's replies, self-improvement."""
    return any((p or {}).get('repo') for p in (cfg.get('projects') or {}).values()) or bool(cfg.get('scouts')) or \
        bool((cfg.get('self_improve') or {}).get('enabled'))


def describe(plan):
    """The plan in plain lines (the dry run's output, and the dashboard's)."""
    f = plan['facts']
    out = [f'Move Tutti to {plan["to"]}']
    if f.get('os'):
        out.append(f'  target: {f.get("hostname")} ({f.get("os")} {f.get("arch")}), {f.get("free_gb")} GB free, '
                   f'Tutti {f.get("tutti_version") or "not installed"}, up {int(f.get("uptime_s") or 0) // 3600} h')
    for b in plan['blockers']:
        out.append(f'  BLOCKED: {b}')
    if plan['blockers']:
        return '\n'.join(out)
    src = plan['src']
    out.append(f'  addresses: dashboard on 127.0.0.1:{src["port"]} at both ends (the client\'s ssh carries it); members and '
               f'clients find it as {src["address_name"]}; LAN address {src["target_lan"]}')
    for i, s in enumerate(STEPS, 1):
        out.append(f'  {i:2}. {s}: {STEP_TEXT[s]}' + (' (needed)' if s == 'install' and plan['install'] else ''))
    out += [f'  default: {c["key"]} = {c["value"]} ({c["why"]})' for c in plan['choices']]
    if plan['missing']:
        out.append(f'  not on {plan["to"]}: {", ".join(plan["missing"])}' + (f' (used on {plan["jobs"]} instead)' if plan['jobs'] else ''))
    o = plan['origins']
    if o['add'] or o['remove']:
        out.append('  OAuth client (console.cloud.google.com/apis/credentials): authorized JavaScript origins')
        out += [f'    add    {x}' for x in o['add']] + [f'    remove {x}' for x in o['remove']]
    else:
        out.append(f'  OAuth origins: no change ({", ".join(o["after"])})')
    out += [f'  warning: {w}' for w in plan['warnings']]
    return '\n'.join(out) + '\n' + guided.render([*plan['owner'], *plan['logins']])


# ---- running things here and on the target


class Ops:
    """The I/O of a move; tests replace it."""

    def __init__(self, cfg, target):
        self.cfg, self.m = cfg, cfg['machines'][target]

    def target(self, script, stdin=None, timeout=600):
        return remote.sh(self.m, script, capture=True, timeout=timeout, stdin=stdin, ssh_opts=remote.QUICK)

    def local(self, argv, **kw):
        return subprocess.run(argv, capture_output=True, text=True, **kw)

    def dest(self, path):
        return path if self.m['transport'] == 'local' else f'{remote.dest(self.m)}:{path}'

    def rsync(self, src, dst, exclude=(), delete=False, back=False):
        a = ['rsync', '-a', *(['--delete'] if delete else []), *[f'--exclude={x}' for x in exclude]]
        if self.m['transport'] != 'local':
            a += ['-e', remote.rsh(self.cfg, self.m)]
        pair = [self.dest(src), dst] if back else [src, self.dest(dst)]
        return self.local([*a, *pair])

    def put_secret(self, path, text):
        """A file written on the target with mode 600, its content on stdin (never in argv or a log)."""
        script = (f'umask 077; mkdir -p {remote.home_path(os.path.dirname(path))}; cat > {remote.home_path(path)}.tmp && '
                  f'chmod 600 {remote.home_path(path)}.tmp && mv {remote.home_path(path)}.tmp {remote.home_path(path)}')
        return self.target(script, stdin=text)

    def health(self, port, tries=15, sleep=2):
        script = (f'python3 -c "import urllib.request,sys; sys.stdout.write(urllib.request.urlopen('
                  f'\'http://127.0.0.1:{int(port)}/api/health\', timeout=5).read().decode())"')
        for _ in range(tries):
            r = self.target(script, timeout=30)
            if r.returncode == 0 and '"ok": true' in r.stdout:
                return json.loads(r.stdout)
            time.sleep(sleep)
        return None


def must(r, what):
    if r.returncode != 0:
        raise MoveError(f'{what} failed (exit {r.returncode}): {(r.stderr or r.stdout or "").strip()[-400:]}')
    return r


def name_unit_text(avahi, address):
    return ('[Unit]\nDescription=Tutti\'s stable LAN name tutti.local (mDNS)\nAfter=network-online.target\n\n'
            f'[Service]\nExecStart={avahi} -a -R {NAME} {address}\nRestart=always\nRestartSec=30\n\n[Install]\nWantedBy=default.target\n')


def client_unit_text(cfg_path):
    tutti = os.path.join(os.path.expanduser('~'), '.local', 'bin', 'tutti')
    tutti = tutti if os.path.exists(tutti) else f'{sys.executable} {remote.BIN}'
    return ('[Unit]\nDescription=Tutti client: the bridge to the host and its dashboard on 127.0.0.1 (docs/MOVE.md)\n'
            f'After=network-online.target\n\n[Service]\nExecStart={tutti} --config {cfg_path} client\nRestart=always\nRestartSec=10\n'
            'Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin\n\n[Install]\nWantedBy=default.target\n')


def install_units(ops, units, enable):
    """User units written on the target, reloaded, then the named ones enabled and started."""
    for name, text in units.items():
        must(ops.target(f'mkdir -p ~/.config/systemd/user && cat > ~/.config/systemd/user/{name}', stdin=text), f'writing {name}')
    names = ' '.join(enable)
    return must(ops.target(f'systemctl --user daemon-reload && systemctl --user enable --now {names}'), f'starting {names}')


# ---- the local dashboard (the writer that the freeze stops)


def dashboards(cfg, proc='/proc'):
    """[(pid, argv)] of `tutti dashboard` processes of this login for this config's port (never --once)."""
    port = str(cfg['dashboard']['port'])
    out = []
    for d in os.listdir(proc):
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            if os.stat(os.path.join(proc, d)).st_uid != os.getuid():
                continue
            with open(os.path.join(proc, d, 'cmdline'), 'rb') as f:
                argv = [x.decode(errors='replace') for x in f.read().split(b'\0') if x]
        except OSError:
            continue
        joined = ' '.join(argv)
        if 'tutti' in joined and 'dashboard' in argv and '--once' not in argv and ('--port' not in argv or port in argv):
            out.append((int(d), argv))
    return out


def stop_local(cfg, j, run=subprocess.run, kill=os.kill, find=dashboards):
    """Stops this computer's dashboard (its user service when it has one, else the process by its pid) and records how to start
    it again on a rollback."""
    unit = run(['systemctl', '--user', 'is-active', UNIT], capture_output=True, text=True)
    j['local_unit'] = unit.returncode == 0
    if j['local_unit']:
        run(['systemctl', '--user', 'stop', UNIT], capture_output=True, text=True)
    found = find(cfg)
    j.setdefault('local_argv', found[0][1] if found else None)
    for pid, _argv in found:
        try:
            kill(pid, signal.SIGTERM)
        except OSError:
            pass
    return [p for p, _ in found]


def start_local(cfg, j, run=subprocess.run, popen=subprocess.Popen):
    if j.get('local_unit'):
        run(['systemctl', '--user', 'enable', '--now', UNIT], capture_output=True, text=True)
        return
    argv = j.get('local_argv') or [sys.executable, remote.BIN, '--config', cfg['_path'], 'dashboard']
    with open(os.path.join(config.state_dir(cfg), 'dashboard.log'), 'a', encoding='utf8') as f:
        popen(argv, stdout=f, stderr=f, stdin=subprocess.DEVNULL, start_new_session=True)


# ---- running jobs


def running_runs(cfg):
    """[(task id, pid)] of the autopilot's runs that are running on this computer."""
    from . import backlog
    out = []
    for t in backlog.load(cfg).get('tasks', []):
        if t.get('status') == 'running' and delegate.valid_id(t.get('run')):
            pid = autopilot_pid(cfg, t['run'])
            if pid:
                out.append((t['id'], pid))
    return out


def autopilot_pid(cfg, run_id):
    from . import autopilot, state
    path = autopilot.listed(delegate.runs_dir(cfg), f'{run_id}.log.pid')   # found in the runs folder's own listing
    if not path:
        return None
    try:
        with open(path, encoding='utf8') as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    return pid if state.alive(pid) else None


def hand_over(cfg, ids, end=None):
    """`--jobs handover`: each running run is stopped (its process tree) and its task queued again; the next attempt continues
    in the same worktree on the new host's schedule (the "stop now" hand-over of the dashboard, tutti/handover.py)."""
    from . import backlog, handover
    end = end or handover.end_tree
    jobs = bridge.jobs_machine(cfg)
    with backlog.locked(cfg) as data:
        for t in data.get('tasks', []):
            if t['id'] in ids and t.get('status') == 'running':
                if jobs:   # the run itself is on the jobs machine: stop it there, not only its wrapper here
                    try:
                        bridge.call(cfg, jobs, 'stop', {'id': t['run']})
                    except bridge.BridgeDown:
                        pass
                pid = autopilot_pid(cfg, t['run'])
                if pid:
                    end(pid)
                t['status'] = 'queued'
                t['handed_over'] = f'stopped by a move at {time.strftime("%H:%M")}; it continues in its worktree'
                t.pop('run', None)


# ---- the steps


class Move:
    def __init__(self, cfg, target, ops=None, say=None, jobs='wait', wait_minutes=60, clock=time.time, sleep=time.sleep):
        self.cfg, self.target, self.say = cfg, target, say or (lambda _t: None)
        self.ops = ops or Ops(cfg, target)
        self.jobs, self.wait_minutes, self.clock, self.sleep = jobs, wait_minutes, clock, sleep
        self.j = read_journal(cfg)
        self.plan = None

    def note(self, text):
        log(self.cfg, self.j, text, self.say)

    def facts(self):
        reach = ssh_machines(self.cfg, self.target)
        r = self.ops.target(preflight_script(programs_of(self.cfg), reach), timeout=120)
        facts = parse_facts(r.stdout if r.returncode == 0 else '')
        if r.returncode != 0:
            facts['_error'] = (r.stderr or '').strip()[-300:] or f'ssh exited {r.returncode}'
        return facts

    def preflight(self):
        self.plan = make_plan(self.cfg, self.target, self.facts())
        if self.plan['blockers']:
            raise MoveError('the target is not ready:\n  - ' + '\n  - '.join(self.plan['blockers']))
        self.j['plan'] = {**self.plan, 'facts': {k: v for k, v in self.plan['facts'].items() if k != 'ifaces'}}

    def ensure_plan(self):
        if self.plan is None:
            self.plan = make_plan(self.cfg, self.target, self.facts())

    def install(self):
        self.ensure_plan()
        if not self.plan['install']:
            return self.note(f'Tutti {self.plan["facts"].get("tutti_version")} is already on {self.target}')
        line = install.install_line('linux') + ' --no-setup'   # the target's config comes from this move, not from setup
        must(self.ops.target(f'cd "$(mktemp -d)" && {line}', timeout=900), f'installing Tutti on {self.target}')
        r = self.ops.target('"$HOME/.local/bin/tutti" bridge --help >/dev/null && "$HOME/.local/bin/tutti" --version')
        if r.returncode != 0:
            raise MoveError(f'the Tutti installed on {self.target} cannot take a move yet ({r.stdout.strip() or r.stderr.strip()[-200:]}): '
                            'it installs the newest release; release this version first, then run the move again')
        self.plan['facts']['tutti'] = f'{self.plan["facts"]["home"]}/.local/bin/tutti'
        self.note(f'installed {r.stdout.strip()} on {self.target}')

    def paths(self):
        p = self.j['plan']
        return config.expand(self.cfg['project']['state']), p['client']['state'], p['client']['usage_log']

    def copy(self, delete=False):
        here, there, usage_there = self.paths()
        must(self.ops.target(f'mkdir -p {remote.home_path(there)} {remote.home_path(os.path.dirname(usage_there))} '
                             '~/.local/state/tutti ~/.config/tutti'), 'creating the folders')
        must(self.ops.rsync(here.rstrip('/') + '/', there.rstrip('/') + '/', STATE_EXCLUDE, delete=delete), 'copying the state')
        usage = config.expand((self.cfg.get('usage') or {}).get('log') or '')
        if usage and os.path.exists(usage) and not usage.startswith(here.rstrip('/') + '/'):
            must(self.ops.rsync(usage, usage_there), 'copying the usage log')
        xdg = os.path.join(os.path.expanduser(os.environ.get('XDG_STATE_HOME') or '~/.local/state'), 'tutti')
        if os.path.isdir(xdg) and os.path.realpath(xdg) != os.path.realpath(here):
            there_xdg = f'{self.j["plan"]["facts"]["home"]}/.local/state/tutti/'
            must(self.ops.rsync(xdg + '/', there_xdg, XDG_STATE_EXCLUDE), 'copying ~/.local/state/tutti')
        self.note(f'state copied to {self.target}:{there}' + (' (final sync)' if delete else ''))

    def freeze(self):
        with open(os.path.join(move_dir(self.cfg), FROZEN), 'w', encoding='utf8') as f:
            f.write(f'{self.target}\n')
        pids = stop_local(self.cfg, self.j)
        self.note(f'frozen: the dashboard here stopped ({", ".join(map(str, pids)) or "none was running"}); changes are refused '
                  'until the move ends')

    def wait_jobs(self):
        deadline = self.clock() + self.wait_minutes * 60
        running = running_runs(self.cfg)
        if running and self.jobs == 'wait':
            self.note(f'waiting for {len(running)} running job(s) to finish: {", ".join(t for t, _ in running)}')
        while running and self.jobs == 'wait' and self.clock() < deadline:
            self.sleep(15)
            running = running_runs(self.cfg)
        if running:
            hand_over(self.cfg, {t for t, _ in running})
            self.note(f'handed over {len(running)} job(s): stopped and queued again; they continue from their worktrees')

    def configure(self):
        p = self.j['plan']
        path = remote.home_path(p['host_path'])
        must(self.ops.target(f'[ -f {path} ] && cp {path} {path}{BACKUP}; mkdir -p ~/.config/tutti; cat > {path}',
                             stdin=p['host_text']), 'writing the target\'s config')
        sent = []
        base = config.xdg_tutti_dir()
        for pattern in SECRET_FILES:
            import glob
            for path in sorted(glob.glob(os.path.join(base, pattern))):
                with open(path, encoding='utf8') as f:
                    must(self.ops.put_secret(f'~/.config/tutti/{os.path.basename(path)}', f.read()), f'copying {os.path.basename(path)}')
                sent.append(os.path.basename(path))
        self.note(f'config written to {self.target}:{p["host_path"]}; secrets copied with mode 600: {", ".join(sent) or "none"}')

    def start(self):
        p = self.j['plan']
        home = p['facts']['home']
        files, _argv = install.service_plan('linux', f'{home}/.local/share/tutti', env={'XDG_CONFIG_HOME': f'{home}/.config'},
                                            tutti=p['client']['tutti'])
        units = {os.path.basename(path): text for path, text in files}
        enable = [UNIT]
        if p['facts'].get('avahi') and p['src']['address_name'] == NAME:
            units[NAME_UNIT] = name_unit_text(p['facts']['avahi'], p['src']['target_lan'])
            enable.append(NAME_UNIT)
        install_units(self.ops, units, enable)
        self.j['started_there'] = True
        self.note(f'Tutti runs on {self.target} as the user service {UNIT}' + (f', named {NAME} on the LAN' if NAME_UNIT in enable else ''))

    def health(self):
        h = self.ops.health(self.j['plan']['src']['port'])
        if not h:
            raise MoveError(f'the dashboard on {self.target} did not answer on 127.0.0.1:{self.j["plan"]["src"]["port"]}: see '
                            f'`journalctl --user -u {UNIT}` there')
        self.j['healthy'] = True
        self.note(f'the dashboard answers on {self.target} ({h.get("host", "")} {h.get("version", "")})')

    def client(self):
        p = self.j['plan']
        self.j['backups'] = {}
        for path in client_configs(self.cfg):
            with open(path, encoding='utf8') as f:
                text = f.read()
            backup = path + BACKUP
            if not os.path.exists(backup):
                shutil.copy2(path, backup)
            self.j['backups'][path] = backup
            with open(path, 'w', encoding='utf8') as f:
                f.write(yamledit.edit(text, ['host'], p['client']))
        self.ops.local(['systemctl', '--user', 'disable', UNIT])
        unit_dir = os.path.expanduser('~/.config/systemd/user')
        os.makedirs(unit_dir, exist_ok=True)
        with open(os.path.join(unit_dir, CLIENT_UNIT), 'w', encoding='utf8') as f:
            f.write(client_unit_text(self.cfg['_path']))
        must(self.ops.local(['systemctl', '--user', 'daemon-reload']), 'reloading the user units here')
        must(self.ops.local(['systemctl', '--user', 'enable', '--now', CLIENT_UNIT]), f'starting {CLIENT_UNIT} here')
        repoint_household(p['client']['address'], self.j['backups'])
        frozen = os.path.join(move_dir(self.cfg), FROZEN)
        if os.path.exists(frozen):
            os.unlink(frozen)
        self.note(f'this computer is a client of {self.target}: {", ".join(self.j["backups"])} point there; {CLIENT_UNIT} runs')

    def verify(self):
        port = self.j['plan']['src']['port']
        h = None
        for _ in range(20):
            h = local_health(port)
            if h and h.get('host') and h.get('host') != socket.gethostname():
                break
            self.sleep(2)
        if not h:
            raise MoveError(f'the dashboard does not open here on 127.0.0.1:{port} through {CLIENT_UNIT}: see '
                            f'`journalctl --user -u {CLIENT_UNIT}`')
        r = self.ops.target(f'{shlex.quote(self.j["plan"]["client"]["tutti"])} --config {shlex.quote(self.j["plan"]["host_path"])} '
                            'bridge status')
        self.note(f'http://127.0.0.1:{port}/ here is the dashboard of {h.get("host")}; bridge: {r.stdout.strip() or r.stderr.strip()}')

    def done(self):
        last = {'from': source_name(self.cfg), 'to': self.target, 'at': time.time(), 'result': 'moved',
                'origins': self.j['plan']['origins'], 'owner': self.j['plan']['owner'], 'logins': self.j['plan']['logins']}
        self.j['status'] = 'done'
        with open(os.path.join(move_dir(self.cfg), LAST), 'w', encoding='utf8') as f:
            json.dump(last, f)
        there = self.j['plan']['client']['state']
        self.ops.target(f'mkdir -p {remote.home_path(there + "/" + FOLDER)} && cat > {remote.home_path(there + "/" + FOLDER + "/" + LAST)}',
                        stdin=json.dumps(last))
        self.note(f'moved: Tutti runs on {self.target}')

    def run_step(self, name):
        fn = {'preflight': self.preflight, 'install': self.install, 'copy': self.copy, 'freeze': self.freeze, 'jobs': self.wait_jobs,
              'sync': lambda: self.copy(delete=True), 'configure': self.configure, 'start': self.start, 'health': self.health,
              'client': self.client, 'verify': self.verify, 'done': self.done}[name]
        self.j['step'] = name
        write_journal(self.cfg, self.j)
        fn()
        self.j.setdefault('done', []).append(name)
        write_journal(self.cfg, self.j)

    def run(self, restart=False):
        if restart or self.j.get('to') != self.target or self.j.get('status') in ('done', ROLLED_BACK, None):
            self.j = {'to': self.target, 'from': source_name(self.cfg), 'status': 'running', 'started': self.clock(), 'done': [],
                      'jobs': self.jobs, 'log': []}
            write_journal(self.cfg, self.j)
        else:
            self.note(f'resuming the move to {self.target} after {", ".join(self.j["done"]) or "nothing"}')
        for name in STEPS:
            if name in self.j.get('done', []):
                continue
            self.note(f'{STEPS.index(name) + 1}/{len(STEPS)} {name}: {STEP_TEXT[name]}')
            try:
                self.run_step(name)
            except (MoveError, bridge.BridgeDown, OSError, subprocess.SubprocessError, yamledit.EditError) as e:
                self.j['error'] = f'{name}: {e}'
                self.note(f'failed at {name}: {e}')
                if name in ('health', 'verify') or (name == 'start' and self.j.get('started_there')):
                    self.note('rolling back by itself: Tutti comes back here')
                    rollback(self.cfg, self.ops, self.say)
                else:
                    self.j['status'] = 'stopped'
                    write_journal(self.cfg, self.j)
                raise MoveError(f'the move stopped at {name}: {e}. Run `tutti move --to {self.target}` to continue, or '
                                '`tutti move --rollback` to undo it') from e
        return self.j


def client_configs(cfg):
    """This config, plus the per-login one when it is a copy for the same state (both must point at the new host)."""
    out = [cfg['_path']]
    xdg = config.xdg_config_path()
    if os.path.isfile(xdg) and os.path.realpath(xdg) != os.path.realpath(cfg['_path']):
        try:
            other = config.load(xdg)
        except config.ConfigError:
            other = None
        if other and config.expand(other['project']['state']) == config.expand(cfg['project']['state']):
            out.append(xdg)
    return out


def household_path():
    from . import users
    return os.path.join(config.xdg_tutti_dir(), users.HOUSEHOLD_FILE)


def repoint_household(address, backups=None):
    """This login's household membership (users.HOUSEHOLD_FILE) points at the stable name when its coordinator moved; the old
    file is kept beside it (mode 600) for a rollback."""
    path = household_path()
    try:
        with open(path, encoding='utf8') as f:
            m = json.load(f)
    except (OSError, ValueError):
        return False
    if backups is not None:
        shutil.copy2(path, path + BACKUP)
        os.chmod(path + BACKUP, 0o600)
        backups[path] = path + BACKUP
    port = str(m.get('coordinator', '')).rsplit(':', 1)[-1]
    if not port.isdigit():
        return False
    m['coordinator'] = f'{address}:{port}'
    with open(path + '.tmp', 'w', encoding='utf8') as f:
        json.dump(m, f)
    os.chmod(path + '.tmp', 0o600)
    os.replace(path + '.tmp', path)
    return True


def local_health(port, timeout=3):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{int(port)}/api/health', timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (OSError, ValueError):
        return None


def stop_there(cfg, j, ops, say):
    """The target's service stopped; the state it wrote since it started copied back here."""
    if not j.get('started_there'):
        return
    ops.target(f'systemctl --user disable --now {UNIT} {NAME_UNIT} 2>/dev/null; true')
    there = ((j.get('plan') or {}).get('client') or {}).get('state')
    if j.get('healthy') and there:
        r = ops.rsync(there.rstrip('/') + '/', config.expand(cfg['project']['state']).rstrip('/') + '/', STATE_EXCLUDE, back=True)
        say('the newer state on the target copied back here' if r.returncode == 0 else f'could not copy the state back: {r.stderr.strip()}')


def restore_here(j, ops):
    """This computer's configs and household membership as they were; the client service stopped."""
    for path, backup in (j.get('backups') or {}).items():
        if os.path.exists(backup):
            shutil.copy2(backup, path)
    if j.get('backups'):
        ops.local(['systemctl', '--user', 'disable', '--now', CLIENT_UNIT])


def rollback(cfg, ops=None, say=None):
    """Tutti back on this computer: the target's service stopped, its newer state copied back, the configs restored, the
    client service stopped and the dashboard here started again. Safe to run twice."""
    say = say or (lambda _t: None)
    j = read_journal(cfg)
    if not j.get('to'):
        raise MoveError('there is no move to roll back here (no <state>/move/journal.json)')
    ops = ops or Ops(cfg, j['to'])
    stop_there(cfg, j, ops, say)
    restore_here(j, ops)
    if 'freeze' in j.get('done', []) or j.get('step') in STEPS[STEPS.index('freeze'):STEPS.index('done')]:
        start_local(cfg, j)
    frozen = os.path.join(move_dir(cfg), FROZEN)
    if os.path.exists(frozen):
        os.unlink(frozen)
    j['status'] = ROLLED_BACK
    log(cfg, j, f'rolled back: Tutti runs on this computer again ({j.get("error") or "asked for"})', say)
    with open(os.path.join(move_dir(cfg), LAST), 'w', encoding='utf8') as f:
        json.dump({'from': j.get('from'), 'to': j['to'], 'at': time.time(), 'result': ROLLED_BACK, 'error': j.get('error')}, f)
    return j


# ---- what the dashboard shows: "Where Tutti runs"


def read_last(cfg):
    try:
        with open(os.path.join(config.state_dir(cfg), FOLDER, LAST), encoding='utf8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def view(cfg, started=None):
    h = cfg.get('host') or {}
    j = read_journal(cfg)
    current = None
    if j.get('status') in ('running', 'stopped'):
        current = {'to': j.get('to'), 'status': j['status'], 'step': j.get('step'), 'done': j.get('done', []), 'steps': list(STEPS),
                   'log': j.get('log', [])[-12:], 'error': j.get('error')}
    targets = [n for n, m in cfg['machines'].items() if m['transport'] in ('ssh', 'wsl') and not m.get('via') and n != h.get('machine')]
    return {'machine': h.get('machine') or source_name(cfg), 'hostname': socket.gethostname(), 'role': clientmode.role(cfg),
            'address': h.get('address') or '127.0.0.1', 'port': h.get('port') or cfg['dashboard']['port'],
            'started': started, 'jobs': h.get('jobs'), 'clients': bridge.clients(cfg), 'move': current, 'last': read_last(cfg),
            'targets': targets, 'readonly': cfg.get('_readonly')}


# ---- the commands


def cmd(cfg, a, out=None):
    out = out or sys.stdout

    def say(text):
        sys.stderr.write(f'tutti: move: {text}\n')
    if a.status:
        v = view(cfg)
        print(json.dumps(v, indent=1, default=str) if a.json else describe_view(v), file=out)
        return 0
    try:
        if a.rollback:
            rollback(cfg, say=say)
            return 0
        if not a.to or a.to not in cfg['machines']:
            raise MoveError(f'name the machine to move to: --to one of {", ".join(cfg["machines"]) or "(none)"}')
        if cfg['machines'][a.to]['transport'] == 'local':
            raise MoveError(f'{a.to} is this computer: Tutti already runs here')
        if a.dry_run:
            m = Move(cfg, a.to, say=say)
            plan = make_plan(cfg, a.to, m.facts())
            shown = {k: v for k, v in plan.items() if k != 'host_text'}
            print(json.dumps(shown, indent=1, default=str) if a.json else describe(plan), file=out)
            return 1 if plan['blockers'] else 0
        j = Move(cfg, a.to, say=say, jobs=a.jobs, wait_minutes=a.wait_minutes).run(restart=a.restart)
        print(describe_done(j), file=out)
        return 0
    except MoveError as e:
        say(str(e))
        return 1


def describe_done(j):
    p = j.get('plan') or {}
    out = [f'Tutti runs on {j["to"]} now. Open http://127.0.0.1:{p.get("src", {}).get("port")}/ here (the client keeps it open).']
    o = p.get('origins') or {}
    if o.get('add') or o.get('remove'):
        out.append('OAuth client, authorized JavaScript origins: ' + '; '.join([*[f'add {x}' for x in o['add']],
                                                                                    *[f'remove {x}' for x in o['remove']]]))
    return '\n'.join(out) + '\n' + guided.render([*(p.get('owner') or []), *(p.get('logins') or [])])


def describe_view(v):
    lines = [f'Tutti runs on {v["machine"]} ({"this computer" if v["role"] == "host" else "a client here"}), dashboard on '
             f'127.0.0.1:{v["port"]}, address {v["address"]}']
    if v['jobs']:
        lines.append(f'jobs run on {v["jobs"]} through its bridge')
    lines += [f'client connected: {c["machine"]} (Tutti {c.get("version")})' for c in v['clients']]
    if v['move']:
        lines.append(f'move to {v["move"]["to"]}: {v["move"]["status"]} at {v["move"]["step"]}' +
                     (f' ({v["move"]["error"]})' if v['move'].get('error') else ''))
    if v['last']:
        lines.append(f'last move: {v["last"].get("from")} -> {v["last"].get("to")}: {v["last"].get("result")} at '
                     f'{time.strftime("%Y-%m-%d %H:%M", time.localtime(v["last"].get("at") or 0))}')
    return '\n'.join(lines)


def cmd_bridge(cfg, a):
    if a.action == 'serve':
        if not a.client:
            sys.stderr.write('tutti: bridge serve --machine <the client machine>\n')
            return 2
        return bridge.Server(cfg, a.client, version=__version__).serve()
    if a.action == 'exec':
        rest = a.rest[1:] if a.rest[:1] == ['--'] else a.rest
        if not a.machine or not rest:
            sys.stderr.write('tutti: bridge exec <machine> [--id <run>] -- run <worker> <task> ...\n')
            return 2
        return bridge.exec_run(cfg, a.machine, a.id, rest)
    found = bridge.clients(cfg)
    print('; '.join(f'{c["machine"]} connected (Tutti {c.get("version")})' for c in found) or 'no client connected')
    return 0


def cmd_client(cfg, a):
    if clientmode.role(cfg) != 'client':
        sys.stderr.write('tutti: client: this computer is where Tutti runs (no host: block names another machine); nothing to do\n')
        return 2
    import threading

    def say(text):
        sys.stderr.write(f'tutti: {text}\n')

    def mirror():
        while True:
            clientmode.refresh_mirror(cfg)
            time.sleep(300)
    threading.Thread(target=mirror, daemon=True).start()
    return bridge.client_loop(cfg, say, once=a.once)


def client_dashboard(cfg, a, say, serve_here):
    """`tutti dashboard` on a client: the host's dashboard through the client's tunnel; with the host down, a read-only one
    from the copy of its state, which says so."""
    port = int(cfg['host'].get('port') or cfg['dashboard']['port'])
    h = local_health(port)
    if h and h.get('host') != socket.gethostname():
        say(f'dashboard: http://127.0.0.1:{port}/ is {cfg["host"]["machine"]}\'s (Tutti runs there; {CLIENT_UNIT} keeps it open)')
        return 0
    if subprocess.run(['systemctl', '--user', 'start', CLIENT_UNIT], capture_output=True).returncode == 0:
        for _ in range(10):
            time.sleep(1)
            if local_health(port):
                say(f'dashboard: http://127.0.0.1:{port}/ is {cfg["host"]["machine"]}\'s ({CLIENT_UNIT} started)')
                return 0
    say(clientmode.mirror_note(cfg))
    fb = clientmode.fallback_cfg(cfg)
    fb['host'] = {}
    a.no_probe = True
    return serve_here(fb)
