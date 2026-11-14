"""The block resolver: when work waits and no worker takes it, Tutti tries the automatic fixes in order and logs each one on
the autopilot card; only when none helps does it offer the owner one clear action (a button on the dashboard).

The order: give tasks files (and steps) so a local model can take them; route to any local worker on any machine; lower the
model tier; borrow a household member's spare share; wake an idle machine's model server; then the owner's one action.
Pure: attempts, action. The I/O is the caller's (autopilot.tick stores the result, the dashboard's POST applies the action)."""
import time

ALLOW_DAYS = 7


def attempts(cfg, plan, tasks, rooms, events=()):
    """[{fix, done, note}] of the automatic fixes for a stopped plan, in the order tried."""
    from . import autopilot, fleet
    queued = [t for t in tasks if t['status'] == 'queued']
    no_files = [t['id'] for t in queued if not autopilot.has_files(t)]
    guessed = [e for e in events if 'so a local model can take it' in e]
    out = [{'fix': 'split or resize', 'done': bool(guessed),
            'note': (f'files picked for {len(guessed)} task(s); a larger task is worked in steps of a few files'
                     if guessed else f'{len(no_files)} task(s) name no file Tutti can find from their title')}]
    locals_ = [n for n in autopilot.ladder(cfg) if autopilot.is_local(cfg, n)]
    local_why = '; '.join((rooms.get(n) or (0, f'{n}: not planned'))[1] or f'{n}: free, but no queued task fits it' for n in locals_)
    out.append({'fix': 'any local worker', 'done': False, 'note': local_why or 'no machine has a local model (tutti fleet)'})
    cheap = [n for n in cfg['accounts'] if (cfg['accounts'][n].get('tiers') or {}).get('cheap')]
    out.append({'fix': 'lower the model tier', 'done': False,
                'note': (f'{", ".join(cheap)} already use their cheap tier while ahead' if cheap else 'no account has a cheap tier')})
    spare = list(cfg.get('_spare_accounts') or ())
    out.append({'fix': 'borrow household spare', 'done': bool(spare),
                'note': f'spare room on {", ".join(spare)}' if spare else 'no household member shares spare room now'})
    woke = [e for e in events if 'tunnel' in e]
    out.append({'fix': 'wake an idle machine', 'done': bool(woke),
                'note': '; '.join(woke) or 'every local model server answers or its machine is not up'})
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
