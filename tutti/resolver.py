"""The block resolver: when work waits and no worker takes it, Tutti tries the automatic fixes in order and logs each one on
the autopilot card; only when none helps does it offer the owner one clear action (a button on the dashboard).

The order: give tasks files (and steps) so a local model can take them; route to any local worker on any machine; lower the
model tier; borrow a household member's spare share; wake an idle machine's model server; then the owner's one action.
Pure: attempts, action. The I/O is the caller's (autopilot.tick stores the result, the dashboard's POST applies the action)."""
import time

ALLOW_DAYS = 7


def attempts(cfg, plan, tasks, rooms, events=(), now=None):
    """[{fix, done, note}] of the automatic fixes for a stopped plan, in the order tried. Each one is applied by the round
    itself (the scope guess, the local routing and the tiers in the plan, the household's spare share, the tunnels in the
    dashboard's probe); this lists what each found, from the tasks' and Tutti's own state."""
    from . import autopilot, fleet
    now = time.time() if now is None else now
    queued = [t for t in tasks if t['status'] == 'queued']
    given = [t['id'] for t in queued if not t.get('scope') and t.get('local_scope')]
    no_files = [t['id'] for t in queued if not autopilot.has_files(t)]
    out = [{'fix': 'split or resize', 'done': bool(given),
            'note': (f'files picked for {len(given)} task(s), worked in steps of a few files; ' if given else '') +
                    (f'{len(no_files)} task(s) name no file Tutti can find from their title' if no_files else 'every task has files')}]
    locals_ = [n for n in autopilot.ladder(cfg) if autopilot.is_local(cfg, n)]
    local_why = '; '.join((rooms.get(n) or (0, f'{n}: not planned'))[1] or f'{n}: free, but no queued task fits its models'
                          for n in locals_)
    out.append({'fix': 'any local worker', 'done': False, 'note': local_why or 'no machine has a local model (tutti fleet)'})
    cheap = [n for n in cfg['accounts'] if (cfg['accounts'][n].get('tiers') or {}).get('cheap')]
    out.append({'fix': 'lower the model tier', 'done': False,
                'note': (f'checked: {", ".join(cheap)} already switch to their cheap tier while ahead, and still have no room'
                         if cheap else 'checked: no account has a cheap tier (accounts.<n>.tiers)')})
    spare = list(cfg.get('_spare_accounts') or ())
    out.append({'fix': 'borrow household spare', 'done': bool(spare),
                'note': f'spare room on {", ".join(spare)}' if spare else 'checked: no household member shares spare room now'})
    woke = fleet.read_state(cfg).get('woke') or {}
    recent = woke.get('at', 0) > now - 900
    out.append({'fix': 'wake an idle machine', 'done': recent,
                'note': (f'tunnels started for {", ".join(woke["workers"])}' if recent else
                         'checked: every local model server answers, or its machine is not up')})
    return out


def action(cfg, states, now=None):
    """The owner's one action, or None: an account of the ladder that is not a trigger account and still has room by its pace
    may take first attempts for ALLOW_DAYS days (one click), else a pointer to name files for the waiting tasks."""
    from . import autopilot, routing
    trig = autopilot.trigger_accounts(cfg, now)
    for n in autopilot.ladder(cfg):
        s = states.get(n) or {}
        if n in trig or n not in cfg['accounts'] or not autopilot.step_of(cfg, n) or routing.account_unfit(s):
            continue
        if s.get('verdict') in ('behind', 'on pace', 'late'):
            return {'kind': 'allow', 'account': n, 'label': f'Allow {n} for autopilot first attempts for {ALLOW_DAYS} days?',
                    'why': f'{n} is {s.get("verdict")} and may start new tasks; today it only takes retries'}
    return {'kind': 'files', 'label': 'Name the files of the waiting tasks',
            'why': 'no online account has room and a local model edits only the files it is given: '
                   '`tutti backlog add --scope <file>` or add the file paths to the task titles'}


def allow(cfg, name, now=None):
    """The dashboard's one click: `name` takes first attempts for ALLOW_DAYS days (autopilot state `allow`)."""
    from . import autopilot
    if name not in cfg['accounts']:
        raise ValueError(f'no account "{name}" in the config: nothing was allowed')
    now = time.time() if now is None else now
    with autopilot.state_locked(cfg) as st:
        st.setdefault('allow', {})[name] = now + ALLOW_DAYS * 86400
    return {'allowed': name, 'until': now + ALLOW_DAYS * 86400}
