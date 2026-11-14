"""A user's main machine (docs/HOUSEHOLD.md, *Your main machine*): the computer where their main agent and their own Tutti
live. `tutti user main-machine <m>` or the dashboard's Me -> My main machine changes it in one step.

It is `tutti move` for that user's own Tutti (tutti/move.py, no new machinery): their state (schedule, mode and learned
profile, readings, backlog, user.json), their household membership and token (mode 600), their agent logins and connected
agents' MCP entries and the readings forwarder (whatever `tutti move` moves), and the old computer becomes a client of
the new one. WIP refs live on the repository's origin and need nothing. It is never the household coordinator's own
move for others: a member's move repoints nobody; an admin whose login is also the coordinator moves it with their Tutti,
which `tutti move` already handles (members repointed).

Who may: only the user themself, on their own Tutti. A signed-in dashboard user (docs/AUTH.md) whose Google identity is
not this login's user is refused by the server, whatever their role (`allowed`)."""
import os
import subprocess
import sys
from types import SimpleNamespace

from . import config, move, users

API = '/api/user/main-machine'


class MainMachineError(RuntimeError):
    """The main machine cannot change; the message says why and what to do."""


def targets(cfg):
    """The machines this user's Tutti may move to: the ones `tutti move` can reach (ssh or WSL, not a household member's)."""
    return [n for n in move.view(cfg)['targets'] if cfg['machines'][n]['transport'] != 'household']


def current(cfg):
    v = move.view(cfg)
    return {'machine': v['machine'], 'hostname': v['hostname'], 'role': v['role'], 'clients': v['clients'],
            'targets': targets(cfg), 'move': v['move'], 'last': v['last']}


def allowed(cfg, who):
    """(ok, why): `who` is the dashboard's signed-in user ({sub, role}) or None (no sign-in: this computer's own owner, who
    reached the dashboard on 127.0.0.1 with its token). Only the user this Tutti works for may change its main machine."""
    if who is None:
        return True, ''
    me = users.identity(cfg)['id']
    if who.get('sub') == me:
        return True, ''
    return False, ('you can change only your own main machine, from your own Tutti (Me -> My main machine on your dashboard, '
                   'or `tutti user main-machine <m>` on your computer)')


def check_target(cfg, machine):
    if machine not in cfg['machines']:
        can = ', '.join(targets(cfg)) or '(no other ssh machine yet: add one, or `tutti machines`)'
        raise MainMachineError(f'no machine "{machine}" in {cfg.get("_path")}: your main machine can be {can}')
    if cfg['machines'][machine]['transport'] == 'household':
        raise MainMachineError(f'{machine} is another member\'s machine, compute only: your main machine is one of yours '
                               f'({", ".join(targets(cfg)) or "add one under machines:"})')
    if machine not in targets(cfg):
        raise MainMachineError(f'{machine} is already your main machine, or not one Tutti can move to over ssh')


def change(cfg, machine, dry_run=False, out=None):
    """The move of this user's Tutti to `machine` (resumable, rolled back by itself when its health check fails)."""
    check_target(cfg, machine)
    a = SimpleNamespace(to=machine, dry_run=dry_run, rollback=False, status=False, json=False, jobs='wait', wait_minutes=60,
                        restart=False)
    return move.cmd(cfg, a, out)


def start(cfg, machine, popen=subprocess.Popen):
    """The dashboard's one click: the move started detached (this dashboard stops during it and the page reconnects to the
    new main machine through the client), exactly as `tutti user main-machine <m>` runs it."""
    check_target(cfg, machine)
    argv = [sys.executable, os.path.join(config.ROOT, 'bin', 'tutti'), '--config', cfg['_path'], 'user', 'main-machine', machine]
    os.makedirs(move.move_dir(cfg), exist_ok=True)
    with open(os.path.join(move.move_dir(cfg), 'move.log'), 'a', encoding='utf8') as f:
        popen(argv, stdout=f, stderr=f, stdin=subprocess.DEVNULL, start_new_session=True)


def post(collector, headers, body, popen=subprocess.Popen):
    """POST /api/user/main-machine {"machine": m} (or {"action": "plan", "machine": m}): the server's own check first."""
    import json
    who_fn = getattr(collector, 'who', None)
    ok, why = allowed(collector.cfg, who_fn(headers) if callable(who_fn) else None)
    if not ok:
        return 403, {'error': why}
    try:
        data = json.loads(body or b'{}')
    except ValueError:
        data = None
    data = data if isinstance(data, dict) else {}
    try:
        if data.get('action') == 'plan':
            check_target(collector.cfg, data.get('machine'))
            plan = move.make_plan(collector.cfg, data['machine'], move.Move(collector.cfg, data['machine']).facts())
            return 200, {'plan': move.describe(plan), 'blocked': bool(plan['blockers'])}
        start(collector.cfg, data.get('machine'), popen)
    except MainMachineError as e:
        return 400, {'error': str(e)}
    except OSError as e:
        return 500, {'error': f'the move could not start ({type(e).__name__}: {e.strerror or e}): `tutti user main-machine '
                              f'{data.get("machine")}` in a terminal says more'}
    return 202, {'ok': True, 'started': 'main-machine', 'to': data.get('machine')}


KNOWN_DAYS = 14   # a client seen within this is the laptop the user sits at; an older status file is a retired one


def present(cfg, clock=None):
    """Whether this user is at their main machine's Tutti. A host with a client seen lately (its status file younger than
    KNOWN_DAYS: the laptop they sit at) counts them present only while one is connected, so a laptop that leaves makes
    them away (docs/HOUSEHOLD.md). A Tutti with no such client is where its user sits: present; a retired laptop's old
    file never makes them away for good."""
    import glob
    import time
    now = (clock or time.time)()
    try:
        known = [p for p in glob.glob(os.path.join(move.bridge.folder(cfg), '*.json'))
                 if now - os.path.getmtime(p) < KNOWN_DAYS * 86400]
        return not known or bool(move.bridge.clients(cfg))
    except (OSError, ValueError, KeyError):
        return True
