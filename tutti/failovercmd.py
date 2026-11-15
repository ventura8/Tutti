"""`tutti main backup`, `tutti failover ...` and `tutti state ...` (docs/FAILOVER.md): the thin edge around tutti/failover.py,
tutti/statebudget.py and tutti/watchdog.py."""
import json
import os
import subprocess
import sys
import time

from . import config, failover, move, remote, selfheal, statebudget, watchdog, yamledit

UNIT = 'tutti-failover.service'


def say(msg):
    sys.stderr.write(f'tutti: {msg}\n')


def probe_facts(cfg, names, run=subprocess.run):
    """{machine: {up, uptime_s}} over ssh, each with a short timeout."""
    out = {}
    for n in names:
        try:
            r = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4', failover.ssh_of(cfg, n), 'cut -d. -f1 /proc/uptime'],
                    capture_output=True, text=True, timeout=15)
            up = r.returncode == 0
            out[n] = {'up': up, 'uptime_s': int((r.stdout or '0').strip() or 0) if up else 0}
        except (OSError, subprocess.SubprocessError, ValueError):
            out[n] = {'up': False}
    return out


def chosen_backup(cfg, facts_fn=probe_facts):
    """The admin's backup, or the one elected earlier (kept: changing it under a live lease is unsafe), or a new election."""
    o, rec = failover.opts(cfg), failover.read(cfg)
    if o['backup'] == 'none':
        return None, 'failover is off'
    if o['backup']:
        return failover.elect(cfg, {})   # the admin's pick, checked: never the main, a phone or a member's machine
    if rec.get('elected'):
        return rec['elected'], 'elected earlier'
    b, why = failover.elect(cfg, facts_fn(cfg, failover.candidates(cfg)))
    if b:
        failover.write(cfg, {**rec, 'elected': b})
    return b, why


def set_backup(cfg, machine, yes=False, run=subprocess.run):
    """`tutti main backup <machine|auto|none>`: saved in the config; the old backup is told to stand down first."""
    o, rec = failover.opts(cfg), failover.read(cfg)
    cands = failover.candidates(cfg)
    if machine not in ('auto', 'none') and machine not in cands:
        can = ', '.join(cands) or '(none: add an ssh machine under machines:)'
        raise failover.FailoverError(f'"{machine}" cannot be the backup main machine: it can be {can}, or auto (Tutti picks), '
                                     'or none (no failover)')
    old = o['backup'] or rec.get('elected')
    if old and old != machine:
        ans = failover.remote_cmd(cfg, old, ['retire'], run)
        if ans is not None and not ans.get('ok'):
            raise failover.FailoverError(f'{old} serves Tutti now ({ans.get("why") or "it is the active main"}): wait until Tutti is '
                                         f'back on {failover.main_of(cfg)}, then change the backup')
        if ans is None and not yes:
            raise failover.FailoverError(f'the current backup {old} does not answer, and it could take over by itself if it is only cut '
                                         f'off: start {old} and try again, or add --yes if {old} is gone for good')
    value = '' if machine == 'auto' else machine
    with open(cfg['_path'], encoding='utf8') as f:
        text = f.read()
    text = yamledit.edit(text, ['failover', 'backup'], value)
    with open(cfg['_path'], 'w', encoding='utf8') as f:
        f.write(text)
    failover.write(cfg, {k: v for k, v in rec.items() if k not in ('elected',)})
    return value


def setup_backup(cfg, backup, run=subprocess.run, say_fn=say):
    """Puts a copy of this Tutti on the backup (its code, its config, its state) and starts its round as a user service."""
    d = failover.remote_dir(cfg)
    dest = failover.ssh_of(cfg, backup)
    r = run(['ssh', '-o', 'BatchMode=yes', dest, f'mkdir -p {d}/state && chmod 700 {d} {d}/state && echo $HOME'], capture_output=True,
            text=True, timeout=30)
    if r.returncode != 0:
        raise failover.FailoverError(f'{backup} cannot be reached over ssh ({dest}): {r.stderr.strip()[:200]}; check `ssh {dest}` works')
    home = r.stdout.strip().splitlines()[-1]
    run(['rsync', '-az', '--delete', '--exclude=.git/', '--exclude=__pycache__/', config.ROOT.rstrip('/') + '/', f'{dest}:{d}/tutti/'],
        check=True, capture_output=True, timeout=300)
    with open(cfg['_path'], encoding='utf8') as f:
        text = failover.backup_config_text(cfg, f.read(), backup, os.path.expanduser('~'), home)
    run(['ssh', '-o', 'BatchMode=yes', dest, f'umask 077 && cat > {d}/tutti.config.yaml'], input=text, text=True, check=True,
        capture_output=True, timeout=30)
    ok, sent = failover.push(cfg, backup, run)
    unit = unit_text(f'{d}/tutti/bin/tutti', f'{d}/tutti.config.yaml').replace('~', home)
    run(['ssh', '-o', 'BatchMode=yes', dest, f'mkdir -p ~/.config/systemd/user && cat > ~/.config/systemd/user/{UNIT} && '
         f'systemctl --user daemon-reload && systemctl --user enable --now {UNIT} && (loginctl enable-linger "$USER" || true)'],
        input=unit, text=True, check=True, capture_output=True, timeout=60)
    say_fn(f'{backup}: Tutti copied to {d}, state pushed ({sent // 1024} KB), its failover round runs as a user service')
    return ok


def unit_text(tutti, cfg_path):
    return (f'[Unit]\nDescription=Tutti failover round (docs/FAILOVER.md)\nAfter=network-online.target\n\n[Service]\n'
            f'ExecStart=/usr/bin/env python3 {tutti} --config {cfg_path} failover run\n'
            f'ExecStop=/usr/bin/env python3 {tutti} --config {cfg_path} failover leave\nRestart=always\nRestartSec=10\n\n'
            f'[Install]\nWantedBy=default.target\n')


def install_local_unit(cfg, run=subprocess.run):
    path = os.path.expanduser(f'~/.config/systemd/user/{UNIT}')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf8') as f:
        f.write(unit_text(remote.BIN, cfg['_path']))
    run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
    run(['systemctl', '--user', 'enable', '--now', UNIT], capture_output=True)
    return path


def one_round(cfg, run=subprocess.run, healthy_since=None, now=None):
    """One failover round on this machine, then the watchdog and the state's trim. Returns lines."""
    now = now or time.time()
    lines = []
    folder = config.state_dir(cfg)
    lines += watchdog.round_files(folder)
    if failover.me(cfg) == failover.main_of(cfg):
        backup, why = chosen_backup(cfg)
        if backup:
            act, line, sent = failover.main_round(cfg, backup, now, run, healthy_since)
            rec = failover.read(cfg)
            failover.write(cfg, {**rec, 'last': line, 'synced_at': now if sent or act == 'serve' else rec.get('synced_at'),
                                 'delta_bytes': sent, 'elected': rec.get('elected')})
            lines.append(line)
        else:
            lines.append(f'no backup main machine: {why}')
    else:
        act, line = failover.backup_round(cfg, now)
        rec = failover.read(cfg)
        failover.write(cfg, {**rec, 'last': line})
        lines.append(line)
    if failover.may_write(cfg) and failover.me(cfg) == failover.main_of(cfg) and cfg.get('_path'):
        port = cfg['dashboard']['port']
        lines += watchdog.round_services(True, lambda: move.local_health(port) is not None,
                                         lambda: failover.start_dashboard(cfg))
    _freed, pruned = statebudget.prune(folder, statebudget.opts(cfg), now)
    return lines + pruned


def heal(cfg, lines, last, now, heal_fn=None):
    """The self-heal checks (tutti/selfheal.py) every selfheal.every_seconds; returns when they last ran."""
    selfheal.note_round(cfg, lines, now)
    if not selfheal.enabled(cfg) or now - last < selfheal.every(cfg):
        return last
    for line in (heal_fn or (lambda c: selfheal.round_(selfheal.Ctx(c), selfheal.LOOP_CHECKS)))(cfg):
        say(f'self-heal: {line}')
    return now


def loop(cfg, stop=None, sleep=time.sleep):
    healthy_since, healed = None, 0.0
    while not (stop and stop()):
        try:
            lines = one_round(cfg, healthy_since=healthy_since)
            healed = heal(cfg, lines, healed, time.time())
            ok = failover.read(cfg).get('last', '').find('cannot reach') < 0
            healthy_since = (healthy_since or time.time()) if ok else None
            for line in lines:
                say(f'failover: {line}')
        except Exception as e:  # one bad round must not end the service; systemd would restart it, but the next round is enough
            say(f'failover round failed ({type(e).__name__}: {e}); trying again next round')
        sleep(float(failover.opts(cfg)['sync_seconds']))


def answer(data, as_json):
    print(json.dumps(data) if as_json else '\n'.join(f'{k}: {v}' for k, v in data.items()))
    return 0


def backup_side(cfg, a):
    """beat, hand-back, retire, status: what the main asks the backup over ssh."""
    rec, now = failover.read(cfg), time.time()
    if a.action == 'beat':
        new, ans = failover.on_beat(rec, json.loads(sys.stdin.read() or '{}'), now)
    elif a.action == 'hand-back':
        new, ans = failover.on_hand_back(rec, a.to, a.term)
        if ans['ok']:
            failover.stop_dashboard(cfg)
    elif a.action == 'retire':
        new, ans = {k: v for k, v in rec.items() if k not in ('beat_at', 'lease_until')}, {'ok': True}
        if rec.get('active') == failover.me(cfg):
            ans = {'ok': False, 'why': 'this machine serves now: hand Tutti back first'}
            new = rec
    else:
        return answer({'term': rec.get('term', 0), 'active': rec.get('active'), 'me': failover.me(cfg)}, a.json)
    failover.write(cfg, new)
    return answer(ans, a.json)


def cmd_failover(cfg, a):
    """`tutti failover [status|tick|run|setup|leave|beat|hand-back|retire]`."""
    if a.action in ('beat', 'hand-back', 'retire') or (a.action == 'status' and a.json):
        return backup_side(cfg, a)
    if a.action == 'tick':
        for line in one_round(cfg):
            say(f'failover: {line}')
        return 0
    if a.action == 'run':
        loop(cfg)
        return 0
    if a.action == 'leave':
        return leave(cfg)
    if a.action == 'setup':
        try:
            backup, why = chosen_backup(cfg)
            if not backup:
                raise failover.FailoverError(f'no backup main machine ({why}): pick one with `tutti main backup <machine>`')
            setup_backup(cfg, backup)
            say(f'this computer\'s round runs as a user service too: {install_local_unit(cfg)}')
        except (failover.FailoverError, subprocess.SubprocessError) as e:
            say(str(e))
            return 2
    print(describe(failover.view(cfg)))
    return 0


def leave(cfg, run=subprocess.run):
    """The main shuts down: it stops acting and tells the backup, which takes over at once."""
    rec, mine = failover.read(cfg), failover.me(cfg)
    backup = failover.opts(cfg)['backup'] or rec.get('elected')
    if mine != failover.main_of(cfg) or rec.get('active', mine) != mine or not backup:
        return 0
    failover.write(cfg, {**rec, 'lease_until': 0})
    beat = {'term': int(rec.get('term') or 0), 'from': mine, 'lease_until': 0, 'leaving': True}
    failover.remote_cmd(cfg, backup, ['beat'], run, inp=json.dumps(beat))
    return 0


def describe(v):
    who = v['backup'] or 'none yet'
    how = ' (chosen by an admin)' if v['chosen'] else ' (elected)' if v['backup'] else ''
    lines = [f'main machine: {v["main"]}; backup main machine: {who}{how}',
             f'active now: {v["active"]} (term {v["term"]}); this machine ' + ('may act' if v['may_write'] else 'is read-only')]
    if v['last']:
        lines.append(f'last round: {v["last"]}')
    if v['synced_at']:
        lines.append(f'last sync {int((time.time() - v["synced_at"]) // 60)} min ago, {int(v["delta_bytes"] or 0) // 1024} KB sent')
    lines.append(f'can be the backup: {", ".join(v["candidates"]) or "(no other ssh machine)"}')
    return '\n'.join(lines)


def cmd_main_backup(cfg, a):
    """`tutti main backup [<machine>|auto|none] [--yes]`."""
    if a.machine:
        try:
            set_backup(cfg, a.machine, yes=a.yes)
        except failover.FailoverError as e:
            say(str(e))
            return 2
        cfg = config.load(cfg['_path'])
    print(describe(failover.view(cfg)))
    return 0


def cmd_state(cfg, a):
    """`tutti state [size|prune]`."""
    folder, o = config.state_dir(cfg), statebudget.opts(cfg)
    if a.action == 'prune':
        freed, lines = statebudget.prune(folder, o)
        for line in lines:
            say(line)
        say(f'freed {statebudget.mb(freed)}')
    lines, problems = statebudget.report(folder, o)
    print('\n'.join(lines + [f'problem: {p}' for p in problems]))
    return 1 if problems else 0
