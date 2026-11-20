"""A backup main machine with automatic failover (docs/FAILOVER.md).

The main machine runs Tutti (dashboard, coordinator, autopilot, device leases). A backup main machine holds a warm copy of its
state, pushed by the main every `sync_seconds`. When the main is silent for `minutes` (or says it is shutting down), the backup
takes over; when the main is back and healthy for `failback_minutes`, it catches up and Tutti returns to it.

**No split brain.** Every change of who serves raises a *term*, stored in `failover.json` on each machine (never synced). A
machine acts (autopilot, coordinator writes, device leases: `may_write`) only while it is the active one of the highest term it
knows, and the main only while it holds a *lease*: the backup acknowledged its heartbeat less than `minutes` minus a margin ago.
The backup takes over only after `minutes` plus a margin of silence, so the main has stopped writing before the backup starts,
whatever the network does. A returning main sees the higher term, stays read-only, catches up from the backup, and only then
asks for the hand-back; between the backup stepping down and the main starting, nobody serves. Members and connected clients
follow whoever answers as active (the failover list), and a coordinator that is not active refuses their calls with the active
one's address.

Everything the main sends goes over its own ssh to the backup (the backup never needs ssh back). The core is pure: decide()
and elect() take facts and a clock; the edge (ssh, rsync, starting the dashboard) is injected."""
import json
import os
import subprocess
import sys
import time

from . import config, durable

FILE = 'failover.json'          # this machine's term record: never synced
BEAT = 'failover-beat.json'     # on the backup: the main's last heartbeat
COPY_WARN = 3                   # copies in a row that failed before every round says so
MARGIN = 30                     # seconds: the main stops before, the backup starts after, the lease's end
DEFAULTS = {'backup': '', 'main': '', 'minutes': 5, 'failback_minutes': 5, 'sync_seconds': 60, 'dir': '~/.tutti-failover',
            'reach': {}, 'addresses': {}}
# never copied to the backup: logs, caches, screenshots, live processes' files, other copies (the runs index runs.jsonl goes)
SYNC_EXCLUDE = ['/snapshots/', '*.corrupt',
                '/runs/', '/telemetry/', '/tests/', '/files/', '/host-mirror/', '/move/', '/bridge/', '/config-backups/',
                '/work-queue/', '/chat/', '/failover-dashboard.pid', f'/{FILE}', f'/{BEAT}', '*.log', '*.pid', '*.rc', '*.cmd', '*.png',
                '*.lock', '*.bak*', '/llm-busy-*', '/local-run-*', '/tunnel-*', '*.cut', '*.tmp*']


class FailoverError(RuntimeError):
    """The backup cannot be set; the message says why and what to do."""


def opts(cfg):
    return {**DEFAULTS, **((cfg or {}).get('failover') or {})}


def state_file(cfg, name=FILE):
    return os.path.join(config.state_dir(cfg), name)


FENCED = 75   # the exit code of a command refused because this machine is not the active main (clients try the next one)
STARTED = time.time()   # this process's start: a backup that just started waits one full silence window first


def read(cfg, name=FILE):
    """The record, {} when there is none; {'corrupt': True} when the file exists but does not parse (fail closed)."""
    try:
        with open(state_file(cfg, name), encoding='utf8') as f:
            d = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {'corrupt': True}
    return d if isinstance(d, dict) else {'corrupt': True}


def write(cfg, data, name=FILE):
    durable.dump_json(state_file(cfg, name), data)


# ---- who is who (pure)


def me(cfg):
    """This machine's name: failover.me (the backup's copy of the config says it), the host block's, or the local machine's."""
    o, h = opts(cfg), cfg.get('host') or {}
    if o.get('me'):
        return o['me']
    if h.get('me'):
        return h['me']
    return next((n for n, m in cfg['machines'].items() if m.get('transport') == 'local'), '')


def main_of(cfg):
    o, h = opts(cfg), cfg.get('host') or {}
    return o.get('main') or h.get('machine') or me(cfg)


def candidates(cfg):
    """Machines that can be the backup main: ours, reachable over ssh or WSL, not the main, not a phone or a member's. On the
    backup's own copy of the config the backup is `host: local`, and it stays a candidate there."""
    main = main_of(cfg)
    mine = me(cfg)
    return [n for n, m in cfg['machines'].items() if n != main and m.get('transport') in ('ssh', 'wsl', 'local')
            and (m.get('host') != 'local' or n == mine)]


def score(m, f):
    """Higher is better: up first, then a UPS, then always on, then uptime."""
    return (bool(f.get('up')), bool(m.get('ups')), bool(m.get('always_on')), int(f.get('uptime_s') or 0))


def elect(cfg, facts):
    """(backup, why): the admin's pick when it is up; else the healthiest always-on candidate with a UPS or most uptime."""
    pick = opts(cfg)['backup']
    if pick == 'none':
        return None, 'failover is off (failover.backup: none)'
    cands = candidates(cfg)
    if pick and pick in cands:
        return pick, 'chosen by an admin'
    if pick:
        return None, (f'failover.backup is {pick}, which cannot be the backup (the main itself, a phone or a member\'s machine): '
                      f'pick one of {", ".join(cands) or "(no other ssh machine)"}')
    up = [n for n in cands if (facts.get(n) or {}).get('up')]
    if not up:
        return None, 'no candidate answers (' + (', '.join(cands) or 'no other ssh machine under machines:') + ')'
    best = max(up, key=lambda n: score(cfg['machines'][n], facts[n]))
    f, m = facts[best], cfg['machines'][best]
    return best, ('elected: ' + ', '.join(x for x in ['has a UPS' if m.get('ups') else '', 'always on' if m.get('always_on') else '',
                                                        f'up {int(f.get("uptime_s") or 0) // 3600} h'] if x))


# ---- the term protocol (pure)


def lease_seconds(o):
    return float(o['minutes']) * 60


def may_write(cfg, now=None, rec=None):
    """True when this machine may act: failover off, or it is the active one and (the backup, or the main within its lease)."""
    if opts(cfg)['backup'] == 'none':
        return True
    rec = rec if rec is not None else read(cfg)
    if rec.get('corrupt'):
        return False   # a damaged record never lets a machine act: `tutti failover` says how to repair it
    if not rec.get('active'):
        return True   # never set up: nothing to fence yet
    now = now or time.time()
    if rec.get('active') != me(cfg):
        return False
    return me(cfg) != main_of(cfg) or now < float(rec.get('lease_until') or 0)


def on_beat(rec, beat, now):
    """The backup's answer to the main's heartbeat {term, from, lease_seconds, leaving}: (new record, answer). The lease is a
    duration stamped with the backup's own clock, so the two machines' clocks never have to agree."""
    term = int(rec.get('term') or 0)
    if term > int(beat['term']):
        return rec, {'ok': False, 'term': term, 'active': rec.get('active')}
    new = {**rec, 'term': int(beat['term']), 'active': beat['from'], 'beat_at': now,
           'lease_until': now if beat.get('leaving') else now + float(beat['lease_seconds'])}
    return new, {'ok': True, 'term': new['term'], 'active': new['active']}


def backup_decide(rec, mine, now, since=None, lease=None):
    """The backup's round: 'serve' (it is active), 'take-over' (the main's lease ran out), or 'stand-by'. `since`: when this
    backup started watching; a backup that just started waits a full lease first (its stored lease may be old)."""
    if rec.get('active') == mine:
        return 'serve'
    if rec.get('beat_at') is None:
        return 'stand-by'   # never heard from a main: nothing to take over
    leaving = rec.get('lease_until') is not None and float(rec['lease_until']) <= float(rec['beat_at'])
    if since is not None and lease and not leaving and now < since + lease + MARGIN:
        return 'stand-by'
    return 'take-over' if now >= float(rec.get('lease_until') or 0) + MARGIN else 'stand-by'


def take_over(rec, mine, now):
    return {**rec, 'term': int(rec.get('term') or 0) + 1, 'active': mine, 'since': now, 'lease_until': None}


def main_decide(rec, mine, answer, now, healthy_since, o):
    """The main's round after its heartbeat. `answer`: the backup's answer, or None when it could not be reached.
    Returns (record, action): 'serve', 'read-only' (lease ran out), 'step-down' (a higher term), 'catch-up', 'hand-back'."""
    term = int(rec.get('term') or 0)
    if rec.get('active', mine) != mine and answer and answer.get('active') == mine:
        return {**rec, 'term': int(answer['term']), 'lease_until': None}, 'adopt'   # handed back: copy the last changes first
    if rec.get('active', mine) == mine and (answer is None or answer.get('ok')):
        if answer is None:
            return rec, ('serve' if now < float(rec.get('lease_until') or 0) else 'read-only')
        return {**rec, 'term': answer['term'], 'active': mine, 'lease_until': now + lease_seconds(o) - MARGIN}, 'serve'
    if answer is None:
        return {**rec, 'lease_until': None}, 'read-only'   # the backup serves, or may: wait for it
    newer = {**rec, 'term': max(term, int(answer['term'])), 'active': answer['active'], 'lease_until': None}
    if rec.get('active', mine) == mine:
        return newer, 'step-down'
    ready = healthy_since is not None and now - healthy_since >= float(o['failback_minutes']) * 60 and rec.get('caught_up')
    return newer, ('hand-back' if ready else 'catch-up')


def on_hand_back(rec, to, term):
    """The backup steps down for the main: the next term is the main's. Refused when the term is stale."""
    if int(term) != int(rec.get('term') or 0):
        return rec, {'ok': False, 'term': rec.get('term'), 'why': 'stale term: catch up first'}
    new = {**rec, 'term': int(term) + 1, 'active': to, 'lease_until': None, 'beat_at': None}
    return new, {'ok': True, 'term': new['term']}


def discovery(cfg):
    """The failover list members and clients follow: the active coordinator first. Addresses are host:port."""
    o, port = opts(cfg), cfg.get('household', {}).get('port', 4778)
    names = [main_of(cfg)] + ([o['backup']] if o['backup'] not in ('', 'none') else [])
    out = []
    for n in names:
        m = cfg['machines'].get(n) or {}
        host = m.get('address') or (m.get('host') if m.get('host') not in (None, 'local') and '@' not in m['host'] else '')
        addr = o['addresses'].get(n) or (f'{host}:{port}' if host else '')
        if addr:
            out.append(addr)
    return out


# ---- the edge: ssh, rsync, the dashboard


def ssh_of(cfg, name):
    o, m = opts(cfg), cfg['machines'].get(name) or {}
    return o['reach'].get(name) or m.get('host')


def remote_dir(cfg):
    return opts(cfg)['dir'].rstrip('/')


def remote_cmd(cfg, backup, args, run=subprocess.run, inp=None, timeout=60):
    """`tutti failover <args>` on the backup over ssh, its JSON answer, or None when it could not be reached."""
    d = remote_dir(cfg)
    line = f'{d}/tutti/bin/tutti --config {d}/tutti.config.yaml failover {" ".join(args)} --json'
    try:
        r = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', ssh_of(cfg, backup), line], capture_output=True, text=True,
                input=inp, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def rsync(src, dst, run=subprocess.run):
    excl = [f'--exclude={x}' for x in SYNC_EXCLUDE]
    ssh = ['-e', 'ssh -o BatchMode=yes -o ConnectTimeout=15']
    r = run(['rsync', '-az', '--delete', '--chmod=Du=rwx,go=,Fu=rw,go=', '--stats', *ssh, *excl, src, dst], capture_output=True, text=True,
            timeout=300)
    sent = 0
    for line in (r.stdout or '').splitlines():
        if line.startswith('Total bytes sent:'):
            sent = int(line.split(':')[1].strip().replace(',', '').split()[0] or 0)
    return r.returncode == 0, sent


def push(cfg, backup, run=subprocess.run):
    """The main's state to the backup's copy (secrets and all at mode 600, to the backup only); returns (ok, bytes sent)."""
    d = remote_dir(cfg)
    return rsync(config.state_dir(cfg).rstrip('/') + '/', f'{ssh_of(cfg, backup)}:{d}/state/', run)


def pull(cfg, backup, run=subprocess.run):
    """The backup's state back to the main (the catch-up after a failover)."""
    d = remote_dir(cfg)
    return rsync(f'{ssh_of(cfg, backup)}:{d}/state/', config.state_dir(cfg).rstrip('/') + '/', run)


def backup_config_text(cfg, text, backup, home_here, home_there):
    """The main's config turned into the backup's: the backup local, the main reached over ssh, paths under the backup's copy."""
    from . import yamledit
    d = opts(cfg)['dir'].replace('~', home_there, 1)
    main = main_of(cfg)
    edits = [(['machines', backup, 'host'], 'local'), (['machines', backup, 'transport'], yamledit.DELETE),
             (['project', 'state'], f'{d}/state'), (['failover', 'me'], backup), (['failover', 'main'], main)]
    # the main reached from the backup: failover.reach.<main>, else a name that never resolves (.invalid) so it shows as down
    edits.append((['machines', main, 'host'], opts(cfg)['reach'].get(main) or f'{main}.invalid'))
    for n, w in (cfg.get('workers') or {}).items():   # the backup's own model server is local there: no tunnel to itself
        if isinstance(w, dict) and w.get('machine') == backup and w.get('tunnel'):
            edits += [(['workers', n, 'host'], '127.0.0.1:11434'), (['workers', n, 'tunnel'], yamledit.DELETE)]
    for path, value in edits:
        text = yamledit.edit(text, path, value)
    return text.replace(home_here.rstrip('/') + '/', home_there.rstrip('/') + '/')


def route_aliases(cfg, backup):
    """ssh Host names the backup needs in ~/.ssh/config to reach the other machines (their `host` fields)."""
    out = []
    for name, m in cfg['machines'].items():
        if name == backup:
            continue
        host = m.get('host')
        if not host or host == 'local' or m.get('transport') == 'local':
            continue
        out.append(host)
    return list(dict.fromkeys(out))


def synth_route(alias, hostname, user='', port=22):
    """A Host block (host, user, port) for the backup's ~/.ssh/config when this computer has none for `alias`."""
    lines = [f'Host {alias}', f'  HostName {hostname}']
    if user:
        lines.append(f'  User {user}')
    port = int(port or 22)
    if port != 22:
        lines.append(f'  Port {port}')
    return {'alias': alias, 'hostname': hostname, 'port': str(port), 'block': '\n'.join(lines) + '\n', 'keys': []}


def route_entries(cfg, backup, home, carry_fn=None, resolve_fn=None):
    """{alias: entry} to write on the backup: from this computer's ~/.ssh/config, else synthesised from ssh -G / machine."""
    from . import movetools, remote
    aliases = route_aliases(cfg, backup)
    carry_fn = carry_fn or (lambda names, h: movetools.ssh_carry(names, h))
    resolve_fn = resolve_fn or remote.ssh_route
    found = carry_fn(aliases, home)
    by_host = {m.get('host'): m for m in cfg['machines'].values() if m.get('host')}
    for alias in aliases:
        if alias in found:
            continue
        m = by_host.get(alias) or {}
        hostname, port, user = resolve_fn(alias)
        found[alias] = synth_route(alias, hostname, m.get('user') or user, m.get('port') or port)
    return found


def dashboard_pid_file(cfg):
    return state_file(cfg, 'failover-dashboard.pid')


def start_dashboard(cfg, popen=subprocess.Popen):
    """The backup's dashboard (with its coordinator and autopilot); its pid recorded so only it is ever stopped."""
    argv = [sys.executable, os.path.join(config.ROOT, 'bin', 'tutti'), '--config', cfg['_path'], 'dashboard']
    with open(state_file(cfg, 'failover-dashboard.log'), 'a', encoding='utf8') as f:
        p = popen(argv, stdout=f, stderr=f, stdin=subprocess.DEVNULL, start_new_session=True)
    with open(dashboard_pid_file(cfg), 'w', encoding='utf8') as f:
        f.write(f'{p.pid}\n')
    return p.pid


def recorded_pid(cfg):
    try:
        with open(dashboard_pid_file(cfg), encoding='utf8') as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    return pid if durable.pid_alive(pid) else None


def stop_dashboard(cfg, kill=os.kill):
    pid = recorded_pid(cfg)
    if pid:
        try:
            kill(pid, 15)
        except OSError:
            pass
    try:
        os.remove(dashboard_pid_file(cfg))
    except OSError:
        pass
    return pid


# ---- one round on each side


def backup_round(cfg, now=None, start=start_dashboard, alive=recorded_pid, stop=stop_dashboard):
    now = now or time.time()
    rec = read(cfg)
    act = backup_decide(rec, me(cfg), now, STARTED, lease_seconds(opts(cfg)))
    if act == 'take-over':
        rec = take_over(rec, me(cfg), now)
        write(cfg, rec)
        act = 'serve'
        line = f'the main ({main_of(cfg)}) was silent past its lease: {me(cfg)} took over (term {rec["term"]})'
    else:
        line = {'serve': f'{me(cfg)} serves (term {rec.get("term")})', 'stand-by': f'{me(cfg)} stands by'}[act]
    if act == 'serve' and not alive(cfg):
        start(cfg)
        line += '; dashboard started'
    elif act == 'stand-by' and alive(cfg):
        stop(cfg)   # a backup that is not active never serves
        line += '; its dashboard stopped'
    return act, line


def exchange(cfg, backup, rec, mine, o, run):
    """The main's talk with the backup: (record, the backup's answer or None, bytes copied)."""
    sent = 0
    if rec.get('active', mine) == mine:
        # the heartbeat first and on its own: a busy main (a slow or failed copy) still keeps its lease; one retry
        beat = {'term': int(rec.get('term') or 0), 'from': mine, 'lease_seconds': lease_seconds(o)}
        answer = remote_cmd(cfg, backup, ['beat'], run, inp=json.dumps(beat))
        if answer is None:   # one short retry: a round stays well inside the lease even with a dead backup
            answer = remote_cmd(cfg, backup, ['beat'], run, inp=json.dumps(beat), timeout=20)
        if answer is not None and answer.get('ok'):
            copied, sent = push(cfg, backup, run)
            rec = {**rec, 'copy_failures': 0 if copied else int(rec.get('copy_failures') or 0) + 1}
    else:
        answer = remote_cmd(cfg, backup, ['status'], run)
        if answer is not None:
            answer = {'ok': answer.get('active') == mine, 'term': answer.get('term') or 0, 'active': answer.get('active')}
    return rec, answer, sent


def main_round(cfg, backup, now=None, run=subprocess.run, healthy_since=None):
    """The main's round: push, heartbeat, decide; catch-up and hand-back when the backup serves. Returns (action, line, bytes)."""
    now = now or time.time()
    o, rec, mine = opts(cfg), read(cfg), me(cfg)
    rec, answer, sent = exchange(cfg, backup, rec, mine, o, run)
    new, act = main_decide(rec, mine, answer, now, healthy_since, o)
    if act == 'serve' and int(rec.get('copy_failures') or 0) >= COPY_WARN:
        act = 'copy-failing'   # the lease is kept (the main is fine); every round says what is wrong and where to look
    elif act == 'adopt':
        if pull(cfg, backup, run)[0]:
            new = {**new, 'active': mine, 'caught_up': False, 'lease_until': now + lease_seconds(o) - MARGIN}
            act = 'resumed'
        else:
            act = 'catch-up'
    elif act == 'catch-up':
        new['caught_up'] = pull(cfg, backup, run)[0]
    elif act == 'hand-back':
        back = remote_cmd(cfg, backup, ['hand-back', '--to', mine, '--term', str(new['term'])], run)
        if back and back.get('ok') and pull(cfg, backup, run)[0]:
            # the backup has just stepped down and stands by (it never takes over before a new heartbeat): the main may act
            # at once, not only after its next round (the bug of 2026-11-16: writes refused for a round after the failback)
            new = {'term': back['term'], 'active': mine, 'lease_until': now + lease_seconds(o) - MARGIN, 'caught_up': False}
            act = 'resumed'
    write(cfg, new)
    return act, describe_action(act, cfg, backup, new), sent


def describe_action(act, cfg, backup, rec):
    main = main_of(cfg)
    return {
        'serve': f'{main} is the main; {backup} has a warm copy (term {rec.get("term")})',
        'read-only': f'{main} cannot reach the backup {backup}, so it stopped acting to be safe (read-only): start {backup}, or '
                     f'`tutti main backup none` if {backup} is gone for good',
        'step-down': f'{backup} took over while {main} was away (term {rec.get("term")}): {main} is read-only until it catches up',
        'catch-up': f'{main} is back and copying what changed on {backup}; Tutti returns to {main} once it has been healthy for a '
                    f'few minutes',
        'hand-back': f'{main} asked {backup} to hand Tutti back',
        'copy-failing': f'{main} is the main, but copying its state to {backup} failed {rec.get("copy_failures")} times in a row '
                        f'(a full disk or permissions there?): {backup} would take over with old data; check `ssh {backup} df -h`',
        'adopt': f'{backup} handed Tutti back; {main} copies the last changes first',
        'resumed': f'Tutti is back on {main} (term {rec.get("term")})',
    }.get(act, act)


def view(cfg):
    """The Machines card's line and `tutti main backup`'s answer."""
    o, rec = opts(cfg), read(cfg)
    return {'main': main_of(cfg), 'backup': o['backup'] or rec.get('elected') or '', 'chosen': o['backup'], 'term': rec.get('term', 0),
            'active': rec.get('active') or main_of(cfg), 'may_write': may_write(cfg), 'last': rec.get('last') or '',
            'candidates': candidates(cfg), 'synced_at': rec.get('synced_at'), 'delta_bytes': rec.get('delta_bytes')}
