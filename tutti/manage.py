"""Managing a member (docs/HOUSEHOLD.md, *Managing a member*): the admin may be the only technical person at home, so
everything a member would do for their own setup the admin can do for them from the dashboard, and the member's own
Tutti carries it out on the member's computer.

- The admin's dashboard (Manage <name>) queues an action at the coordinator (`/hh/act`); the coordinator lets the admin
  act for anyone and a member only for themself, and writes the audit line "<action> by admin X for Y".
- The member's Tutti claims its actions every dashboard round (`tick`) and runs them here, as the member: the schedule
  and mode (away, vacation), the main agent, the main machine (tutti/mainmachine.py), `tutti connect`, pairing a device,
  using a shared machine, pausing or resuming one of its machines, `tutti update`. The result goes back to the panel.
- What only the member can do (a Google sign-in in their own browser, a tap on their phone) is never asked of the admin
  as technical work: the panel shows the one step to hand them (`HAND_OVER`), or a QR code of the link.

The dashboard's own check (`allowed`): with sign-in (docs/AUTH.md, `collector.who(headers)`), an admin may act for any
member and a member only for themself; without sign-in, the dashboard's own login is its owner."""
import json
import os
import subprocess
import sys

from . import config, coordinator, household, telemetry, users

API = '/api/users/manage'   # the Users tab (admin-only; a member may act for themself)
HAND_OVER = {
    'pair-device': 'Hand {name} their phone: Settings, Developer options, Wireless debugging, "Pair device with pairing code", '
                   'and type the six digits their computer shows (the dashboard there, Devices card)',
    'join': 'Send {name} this link (or let them scan the QR code): it installs Tutti if needed and joins the household; '
            'they sign in with Google once in their own browser',
    'connect': 'If an agent asks {name} to allow Tutti (a one-time prompt in that agent), they click Allow',
}


def is_self(cfg, who, member):
    """Whether the member acted for is the one asking. With sign-in that is the signed-in Google user's own member id,
    never this Tutti's: the owner's id must not let a viewer act for the owner. Without sign-in the dashboard's user is
    this login's owner."""
    if who is None:
        return member == users.identity(cfg)['hash']
    return bool(who.get('sub')) and users.id_hash(who['sub']) == member


def allowed(cfg, who, target_is_self):
    """(ok, why) for the dashboard's own check before anything is queued."""
    if who is None or who.get('role') == 'admin':
        return True, ''
    if target_is_self:
        return True, ''
    return False, 'only an admin can manage another member; you can change your own setup under Me'


def act_post(collector, headers, body, client=None):
    """POST /api/household/manage {"member": <hash>, "act": <action>, "args": {...}} or {"list": true}."""
    try:
        data = json.loads(body or b'{}')
    except ValueError:
        data = None
    data = data if isinstance(data, dict) else {}
    client = client or coordinator.client_of(collector.cfg)
    who_fn = getattr(collector, 'who', None)
    who = who_fn(headers) if callable(who_fn) else None
    if not client:   # a household of one: this user's own row, and their actions run here at once
        return alone(collector.cfg, who, data)
    try:
        if data.get('list'):
            return 200, client.call('/hh/act', {'action': 'list'})
        ok, why = allowed(collector.cfg, who, is_self(collector.cfg, who, data.get('member')))
        if not ok:
            return 403, {'error': why}
        if data.get('act') in ('remove', 'make-admin', 'weight'):
            if who is not None and who.get('role') != 'admin':
                return 403, {'error': 'only an admin can remove a member, make one admin or change a member\'s share'}
            client.call('/hh/act', {'action': data['act'], 'member': data.get('member'),
                                    **({'value': (data.get('args') or {}).get('weight')} if data['act'] == 'weight' else {})})
            audit_here(collector.cfg, who, data)
            return 200, {'ok': True}
        aid = client.call('/hh/act', {'action': 'queue', 'member': data.get('member'), 'act': data.get('act'),
                                      'args': data.get('args') or {}})['id']
    except household.HouseholdError as e:
        return 403 if 'only' in str(e) else 400, {'error': str(e)}
    except coordinator.Unreachable as e:
        return 503, {'error': f'{e}: the household coordinator must be running (its dashboard) to reach the member'}
    audit_here(collector.cfg, who, data)
    telemetry.record('feature', 'household.manage', act=str(data.get('act'))[:32], outcome='queued')
    hand = HAND_OVER.get(data.get('act'))
    return 202, {'id': aid, 'hand_over': hand}


def own_row(cfg):
    """This user's own row, as the coordinator would send it: identity, presence, setup (main machine, agents, mode,
    schedule, consumption), joined (a household of one has nothing to join), admin."""
    import time
    from datetime import datetime, timezone

    from . import owner, pacing, sharing, state
    me = users.identity(cfg)
    try:
        entries = pacing.read_log(state.usage_log_path(cfg))
    except (OSError, ValueError):
        entries = []
    view = owner.view(cfg, config.state_dir(cfg), entries, datetime.now(timezone.utc))
    return {'hash': me['hash'], 'name': me['name'], 'me': True, 'admin': True, 'agent': telemetry.tool_of(me['main'] or ''),
            'online': True, 'away': not sharing._present(cfg), 'active': False, 'last_seen': time.time(), 'joined': None,
            'household_of_one': True, 'setup': sharing.setup_of(cfg, view)}


def alone(cfg, who, data):
    """The Users tab without a household: the admin's own row, complete; an action for themself runs here at once."""
    if data.get('list'):
        return 200, {'members': [own_row(cfg)], 'acts': [], 'audit': [], 'admin': True, 'admins': 1}
    if data.get('member') != users.identity(cfg)['hash']:
        return 409, {'error': 'this Tutti is not in a household yet: invite someone from Invites first'}
    ok, why = allowed(cfg, who, is_self(cfg, who, data.get('member')))
    if not ok:
        return 403, {'error': why}
    if data.get('act') in ('remove', 'make-admin'):
        return 409, {'error': 'you are the only admin: make another member admin first'}
    ok, note = run(cfg, {'action': data.get('act'), 'args': data.get('args') or {}, 'by': users.identity(cfg)['name']})
    audit_here(cfg, who, data)
    return (200 if ok else 400), ({'ok': True, 'note': note} if ok else {'error': note})


def audit_here(cfg, who, data):
    """The dashboard's own audit (docs/AUTH.md) when sign-in is on: who asked for what, for whom."""
    try:
        from . import auth
    except ImportError:
        return
    if who:
        auth.audit(config.state_dir(cfg), who, f'household {data.get("act")}', {'sub': data.get('member'), 'email': ''})


# ---- the member's side: run what was asked for, here


def run_schedule(cfg, args):
    from . import owner
    code, answer = owner.change(cfg, config.state_dir(cfg), {k: args[k] for k in ('mode', 'days', 'window', 'hours') if k in args})
    if code != 200:
        raise household.HouseholdError(answer.get('error', 'the schedule was refused'))
    return f'mode {answer.get("mode", args.get("mode"))}' if 'mode' in args else 'schedule saved'


def run_main_agent(cfg, args):
    worker, note = users.set_main_agent(cfg, str(args.get('kind') or ''))
    return f'main agent {worker}' + (f' ({note})' if note else '')


def run_main_machine(cfg, args):
    from . import mainmachine
    mainmachine.start(cfg, str(args.get('machine') or ''))
    return f'moving to {args.get("machine")}: this Tutti continues there'


def run_connect(cfg, _args):
    try:
        from . import connect
    except ImportError:
        return 'tutti connect is not in this Tutti yet: it updates by itself'
    results = connect.connect_all(cfg['_path'])
    return '; '.join(f'{r.get("agent")}: {r.get("status")}' for r in results) or 'no agent installed'


def run_machine_use(cfg, args):
    import io
    from types import SimpleNamespace

    from . import householdcmd
    out = io.StringIO()
    householdcmd.use_machine(cfg, str(args.get('machine') or ''), SimpleNamespace(user=None), out,
                             coordinator.client_of(cfg), None)
    return out.getvalue().strip()


def run_pause(cfg, args, paused):
    from . import controls
    name = str(args.get('machine') or '')
    if name not in cfg['machines']:
        raise household.HouseholdError(f'no machine "{name}" here')
    controls.set_paused(config.state_dir(cfg), 'machine', name, paused, source=f'household admin {args.get("_by", "")}'.strip())
    return f'{name} {"paused" if paused else "resumed"}'


def run_detached(cfg, argv_tail, what):
    argv = [sys.executable, os.path.join(config.ROOT, 'bin', 'tutti'), '--config', cfg['_path'], *argv_tail]
    log = os.path.join(config.state_dir(cfg), 'household', f'{what}.log')
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, 'a', encoding='utf8') as f:
        subprocess.Popen(argv, stdout=f, stderr=f, stdin=subprocess.DEVNULL, start_new_session=True)
    return f'{" ".join(argv_tail)} started'


def run_notify(cfg, args):
    from . import needs
    ok, res = needs.set_user(cfg, needs.OWNER, {k: args[k] for k in ('notify', 'batch', 'quiet') if k in args})
    if not ok:
        raise household.HouseholdError(res)
    return f'questions: {res["notify"]}, batch {res["batch"]}, quiet {res["quiet"] or "none"}'


RUNNERS = {
    'notify': run_notify,
    'schedule': run_schedule,
    'main-agent': run_main_agent,
    'main-machine': run_main_machine,
    'connect': run_connect,
    'machine-use': run_machine_use,
    'pause': lambda cfg, a: run_pause(cfg, a, True),
    'resume': lambda cfg, a: run_pause(cfg, a, False),
    'update': lambda cfg, a: run_detached(cfg, ['update'], 'update'),
    'pair-device': lambda cfg, a: run_detached(cfg, ['devices', 'pair'], 'pair'),
}


def run(cfg, act):
    """(ok, note) of one action, run here as this member."""
    try:
        return True, RUNNERS[act['action']](cfg, {**act.get('args', {}), '_by': act.get('by', '')})
    except (household.HouseholdError, users.UserError, KeyError, OSError, ValueError, RuntimeError) as e:
        return False, str(e)


def tick(cfg, client=None, runners=None):
    """The member's dashboard round: claim the actions asked for this member and run each; the results go back."""
    client = client or coordinator.client_of(cfg)
    if not client:
        return []
    try:
        acts = client.call('/hh/act', {'action': 'claim'})['acts']
    except (coordinator.Unreachable, household.HouseholdError):
        return []
    resend(cfg, client)
    done = []
    for act in acts:
        ok, note = (runners or {}).get(act['action'], run)(cfg, act) if runners else run(cfg, act)
        result = {'action': 'finish', 'id': act['id'], 'ok': ok, 'note': note}
        try:
            client.call('/hh/act', result)
        except (coordinator.Unreachable, household.HouseholdError):
            keep(cfg, result)   # sent again next round; meanwhile the act is reclaimed only after ACT_RECLAIM
        done.append((act['id'], ok, note))
    return done


def _pending(cfg):
    return os.path.join(config.state_dir(cfg), 'household', 'acts-pending.json')


def keep(cfg, result):
    path = _pending(cfg)
    try:
        with open(path, encoding='utf8') as f:
            items = json.load(f)
    except (OSError, ValueError):
        items = []
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf8') as f:
        json.dump([*items, result][-50:], f)


def resend(cfg, client):
    """Results the coordinator missed: sent again; the ones it took leave the file."""
    path = _pending(cfg)
    try:
        with open(path, encoding='utf8') as f:
            items = json.load(f)
    except (OSError, ValueError):
        return
    left = []
    for r in items:
        try:
            client.call('/hh/act', r)
        except coordinator.Unreachable:
            left.append(r)
        except household.HouseholdError:
            pass   # the coordinator no longer knows it: nothing to keep
    with open(path, 'w', encoding='utf8') as f:
        json.dump(left, f)
