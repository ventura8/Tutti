"""Keeps devices, machines and workers busy while work waits for them (docs/DISPATCH.md).

The pending-work model: a project declares its test suites (`projects.<p>.tests`), the target kinds that can run each one
(an Android device, a machine) and how it shards. Pending work is every shard not passed on the project's latest commit,
the failed shards with retries left and, for workers, the queued backlog tasks. The dispatcher pulls a pending shard to
every free compatible target on each tick (the dashboard's probe round) and whenever a shard ends; a run holds a lease with
a heartbeat, so a run that crashed frees its target. An **Idle with work** event is logged when a target sits idle for
more than `dispatch.idle_minutes` while compatible work waits (reason, dashboard list, auto-fix for Keep busy /
autopilot left off).

A test run changes no code, so a project that is read-only for Tutti still gets test runs; the results stay in the state
folder (`tutti dispatch status`, the dashboard), never in a commit. Pure planning here; spawning and the run wrapper at the end."""
import contextlib
import json
import os
import re
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:   # Windows: no flock; the dashboard is the only writer there
    fcntl = None

from . import calls, config, controls, devices, durable, power, presence, priority, shards, telemetry

FILE = 'dispatch.json'
TARGET_KINDS = ('android', 'tv', 'machine')   # tv: a device of kind tv (a Cast receiver); only suites that name it use it
DEVICE_KINDS = ('android', 'tv')
SUITE_KINDS = ('e2e', 'unit', 'lint', 'other')
LIGHT = ('unit', 'lint')            # on an Android device these are opt-in (its battery and heat): `tutti optin device <d> on`
COMMIT_EVERY = 300                  # seconds between two looks at a project's latest commit
ANY = 'any'                         # a shard moved to "the best available target": any target of its kinds
OFF = "dispatch is off: the dashboard's Keep busy switch or `tutti dispatch on` turns it on"


# ---- settings and the config's schema


def settings(cfg, folder=None):
    """dispatch: from the config, with the dashboard's switch (controls.json settings.dispatch_enabled) over `enabled`."""
    s = dict(cfg.get('dispatch') or {})
    chosen = controls.read(folder or config.state_dir(cfg))['settings'].get('dispatch_enabled')
    if isinstance(chosen, bool):
        s['enabled'] = chosen
    return s


def set_enabled(cfg, on):
    controls.set_setting(config.state_dir(cfg), 'dispatch_enabled', bool(on))


def suites(cfg):
    """Every declared suite, with its project and the defaults filled in."""
    retries = (cfg.get('dispatch') or {}).get('retries', 2)
    out = []
    for pname, p in (cfg.get('projects') or {}).items():
        for sname, s in ((p or {}).get('tests') or {}).items():
            s = s or {}
            out.append({'project': pname, 'name': sname, 'kind': s.get('kind', 'e2e'), 'targets': list(s.get('targets') or ['machine']),
                        'run': s.get('run', ''), 'shards': shards.names(s), 'shard_opts': shards.options(s),
                        'shards_from': s.get('shards_from'), 'prepare': s.get('prepare'), 'verdict': s.get('verdict'),
                        'requires': s.get('requires'), 'pair': s.get('pair'), 'runner': s.get('runner') or (p or {}).get('runner'),
                        'started': s.get('started'), 'requeue_on': list(s.get('requeue_on') or []),
                        'dir': s.get('dir'), 'desktop': s.get('desktop'), 'devices': s.get('devices'), 'machines': s.get('machines'),
                        'pause': s.get('pause'), 'resume': s.get('resume'),
                        'retries': s.get('retries', retries), 'minutes': float(s.get('minutes') or 0)})
    return out


def _where_errors(cfg, key, v):
    if not isinstance(v, dict) or not isinstance(v.get('run'), str) or not v['run'].strip():
        return [f'{key} is {v!r}: write it as {{run: <a command that prints it>, where: <a machine>}}']
    if v.get('where', 'local') != 'local' and v['where'] not in cfg['machines']:
        return [f'{key}.where names "{v["where"]}", which is not under machines: (or use local)']
    return []


def _names_errors(cfg, key, names, section):
    if names is None:
        return []
    if not isinstance(names, list):
        return [f'{key} is {names!r}: a list of names under {section}:']
    return [f'{key} names "{n}", which is not under {section}:' for n in names if n not in (cfg.get(section) or {})]


def _suite_errors(cfg, key, s):
    if not isinstance(s, dict):
        return [f'{key} is {s!r}: write it as {{run: <command>, targets: [android], shards: [...]}}']
    has_run = isinstance(s.get('run'), str) and s['run'].strip()
    errs = [] if has_run else [f'{key}.run is missing: the command a shard runs ({{shard}} is its name)']
    errs += [f'{key}.targets names "{t}": use {" or ".join(TARGET_KINDS)}' for t in s.get('targets') or [] if t not in TARGET_KINDS]
    if s.get('kind', 'e2e') not in SUITE_KINDS:
        errs.append(f'{key}.kind is {s["kind"]!r}: use one of {", ".join(SUITE_KINDS)}')
    if 'shards' in s and not isinstance(s['shards'], list):
        errs.append(f'{key}.shards is {s["shards"]!r}: a list of shard names, e.g. [player.spec, search.spec]')
    else:
        errs += shards.suite_errors(cfg, key, s, _where_errors)
    if 'shards_from' in s:
        errs += _where_errors(cfg, f'{key}.shards_from', s['shards_from'])
    from . import winsession
    errs += winsession.errors(f'{key}.desktop', s.get('desktop'))
    errs += [f'{key}.{k} is {s[k]!r}: a command (run with ANDROID_SERIAL set when a phone call comes)' for k in ('pause', 'resume')
             if k in s and not (isinstance(s[k], str) and s[k].strip())]
    if 'retries' in s and (isinstance(s['retries'], bool) or not isinstance(s['retries'], int) or s['retries'] < 0):
        errs.append(f'{key}.retries is {s["retries"]!r}: a whole number, 0 or more')
    return errs + _names_errors(cfg, f'{key}.devices', s.get('devices'), 'devices') + \
        _names_errors(cfg, f'{key}.machines', s.get('machines'), 'machines')


def errors(cfg):
    errs = []
    d = cfg.get('dispatch') or {}
    if 'enabled' in d and not isinstance(d['enabled'], bool):
        errs.append(f'dispatch.enabled is {d["enabled"]!r}: use true or false')
    for k, floor in (('idle_minutes', 0), ('lease_minutes', 0), ('call_poll_seconds', 0), ('resume_after_call_seconds', -1),
                     ('requeue_cooldown_minutes', -1)):
        v = d.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v <= floor):
            errs.append(f'dispatch.{k} is {v!r}: use a number ' + ('of 0 or more' if floor < 0 else 'above 0'))
    if 'pause_on_owner_use' in d and not isinstance(d['pause_on_owner_use'], bool):
        errs.append(f'dispatch.pause_on_owner_use is {d["pause_on_owner_use"]!r}: use true or false')
    ps = cfg.get('projects') or {}
    for pname, p in (ps.items() if isinstance(ps, dict) else ()):   # a projects: that is not a mapping is config.py's to name
        p = p if isinstance(p, dict) else {}
        if 'commit' in p:
            errs += _where_errors(cfg, f'projects.{pname}.commit', p['commit'])
        if 'report' in p:
            errs += shards.report_errors(cfg, f'projects.{pname}.report', p['report'])
        tests = p.get('tests') or {}
        if not isinstance(tests, dict):
            errs.append(f'projects.{pname}.tests is {tests!r}: a mapping of suite name to {{run, targets, shards}}')
            continue
        for sname, s in tests.items():
            errs += _suite_errors(cfg, f'projects.{pname}.tests.{sname}', s)
    return errs


# ---- the state: <project.state>/dispatch.json


def folder(cfg):
    out = os.path.join(config.state_dir(cfg), 'dispatch')
    os.makedirs(out, exist_ok=True)
    return out


def read_state(cfg):
    try:
        with open(os.path.join(config.state_dir(cfg), FILE), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    for k in ('projects', 'leases', 'idle', 'warned'):
        data.setdefault(k, {})
    data.setdefault('warnings', [])
    data.setdefault('events', [])
    return data


@contextlib.contextmanager
def locked(cfg):
    """The state read and written back under one lock: the dashboard's tick and a run that just ended never mix."""
    path = os.path.join(config.state_dir(cfg), FILE)
    with open(path + '.lock', 'w', encoding='utf8') as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        data = read_state(cfg)
        try:
            yield data
        finally:   # a lease taken before a later step failed is still written: its run is going
            durable.dump_json(path, data, indent=1)


def event(st, now, text):
    st['events'] = ([{'at': now, 'text': text}] + st['events'])[:50]


# ---- the pending-work model


def sync(st, project, commit, shard_lists, now):
    """A new commit queues every shard again; a suite's new shards join the queue. shard_lists: {suite: [names]}."""
    p = st['projects'].setdefault(project, {'commit': None, 'shards': {}})
    if commit and commit != p.get('commit'):
        p.update(commit=commit, since=now, shards={})
    for suite, names in shard_lists.items():
        for n in names or ['all']:
            p['shards'].setdefault(f'{suite}/{n}', {'status': 'queued', 'attempts': 0})


def pending(cfg, st, now=None):
    """The shards waiting for a target, queued ones first, then failed ones with retries left (fewest attempts first)."""
    by = {(s['project'], s['name']): s for s in suites(cfg)}
    out = []
    for pname, p in st['projects'].items():
        for key, sh in p['shards'].items():
            suite, _, shard = key.partition('/')
            s = by.get((pname, suite))
            if not s or sh['status'] in ('running', 'passed'):
                continue
            if sh['status'] == 'failed' and sh['attempts'] > s['retries']:
                continue
            opt = s['shard_opts'].get(shard) or {}
            out.append({**s, 'suite': suite, 'shard': shard, 'key': key, 'attempts': sh['attempts'],
                        'devices': opt.get('devices', s['devices']), 'extra': opt.get('extra', ''), 'holds': list(opt.get('holds') or []),
                        'wait': shards.prepare_wait(p, s, now or time.time()), 'pin': sh.get('pin'), 'bump': bool(sh.get('bump'))})
    return sorted(out, key=lambda w: (not w['bump'], w['attempts'], w['project'], w['key']))   # an admin's bump goes first


def compatible(target, w):
    if target['kind'] not in w['targets']:
        return False
    pair = w.get('pair') if isinstance(w.get('pair'), dict) else {}
    if pair.get('sender') and 'cast-sender' not in (target.get('roles') or []):
        return False   # a Cast suite runs on a sender: a device or a machine with Chrome or the desktop sender
    if w.get('pin'):   # an admin moved it (tutti/jobmove.py): to this target, or to any of its kind, over the suite's own list
        return w['pin'] in (ANY, target['id'])
    names = w['devices'] if target['kind'] in DEVICE_KINDS else w['machines']
    return not names or target['name'] in names


def dedicated_refuses(cfg, target, w):
    """A dedicated machine (machines.<m>.dedicated: a Windows desktop for one project's UI runs) takes only the suites that
    name it under `machines`, never a suite open to every machine."""
    m = (cfg.get('machines') or {}).get(target['name']) if target['kind'] == 'machine' else None
    return bool(m and m.get('dedicated')) and target['name'] not in (w.get('machines') or [])


def light_refused(cfg, target, w):
    """Why an Android device may not take this lint or unit shard ('' when it may): those are opt-in per device."""
    if target['kind'] != 'android' or w['kind'] not in LIGHT:
        return ''
    if ((cfg.get('devices') or {}).get(target['name']) or {}).get('enabled') is True:
        return ''
    return (f'{w["kind"]} runs on an Android device are opt-in: turn them on for {target["name"]} (Controls, opt-in) or '
            f'`tutti optin device {target["name"]} on`')


# ---- targets: devices and machines as the probes see them


def targets(devices_rows, machine_rows, st, held=None, gone=None):
    """Every device and machine as a target: busy (a run holds it), or free, with what blocks it ('' when nothing does).
    `held`: {device: why} for a phone call or its owner (tutti/calls.py). A machine is busy once our leases on it fill its
    free slots (the probe may not count a run that just started). `gone`: {target id: card text} of the targets presence
    says are away or asleep: blocked with that text and marked absent (the idle watchdog never flags them)."""
    leased = [lease['target'] for lease in st['leases'].values()]
    leased += [f'device:{n}' for lease in st['leases'].values() for n in lease.get('holds') or []]
    out = []
    for d in devices_rows or []:
        tid = f'device:{d["name"]}'
        busy = tid in leased or d.get('status') == 'busy'
        shown = 'paused' if d.get('status') == 'held' else d.get('status')
        why = (held or {}).get(d['name']) or ('' if d.get('status') == 'ready' else f'{d["name"]} is {shown}: {d.get("reason")}')
        kind = 'tv' if d.get('kind') == 'tv' else 'android'
        out.append(_absent({'id': tid, 'kind': kind, 'name': d['name'], 'busy': busy, 'blocked': '' if busy else why}, gone))
    for m in machine_rows or []:
        tid = f'machine:{m["name"]}'
        busy = m.get('status') == 'busy' or leased.count(tid) >= max(m.get('free') or 0, 1)
        why = '' if m.get('status') == 'free' else f'{m["name"]} is {m.get("status")}: {m.get("reason")}'
        pw = m.get('power') or {}
        out.append(_absent({'id': tid, 'kind': 'machine', 'name': m['name'], 'busy': busy, 'blocked': '' if busy else why,
                            'zone': m.get('zone') or '', 'power': pw.get('score', 0), 'room': pw.get('room', 1.0),
                            'effective': pw.get('effective', 0)}, gone))
    return out


def _absent(t, gone):
    if (gone or {}).get(t['id']):
        t.update(absent=True, busy=False, blocked=f'{t["name"]}: {gone[t["id"]]}')
    return t


def blocked_by(cfg, target, work, on, quiet):
    if not on:
        return OFF
    if target['blocked']:
        return target['blocked']
    quiet = quiet(target) if callable(quiet) else quiet   # a machine's quiet hours are in its own zone
    if quiet:
        return quiet
    refused = [light_refused(cfg, target, w) or w.get('wait') for w in work]
    return refused[0] if all(refused) else ''


def holds_free(w, free, taken):
    """The other devices a shard holds (TabletCastUITest casts to the tablet) are free, unblocked and not taken this round."""
    return all(f'device:{n}' in free and f'device:{n}' not in taken for n in w.get('holds') or [])


def pick(cfg, work, running):
    """The shard a target takes: the first in the weighted fair share of the projects' priority (tutti/priority.py,
    config.project_priority: the higher priority first, no project starves), fewest attempts first within a project."""
    bumped = [w for w in work if w.get('bump')]   # an admin's priority bump (tutti/jobmove.py) goes before the fair share
    return bumped[0] if bumped else priority.fair_order(cfg, work, lambda w: w['project'], running)[0]


def long_work(cfg, w):
    return w.get('minutes', 0) >= float(presence.settings(cfg)['long_minutes'])


def staying(cfg, t, work, place):
    """(work this target may take, why it is blocked or ''): a target likely to leave or sleep soon takes no long shard."""
    leaving = (place.get(t['id']) or {}).get('leaving')
    if not leaving or t['blocked']:
        return work, ''
    short = [w for w in work if not (long_work(cfg, w) and leaving(w['minutes']))]
    if work and not short and not t['busy']:
        return [], f'{t["name"]} usually leaves or sleeps soon: long shards wait for a target that stays'
    return short, ''


def ready_work(cfg, t, work, free, taken):
    """(the shards this target can start now, why none can or '')."""
    ready = [x for x in work if not light_refused(cfg, t, x) and not x.get('wait') and holds_free(x, free - {t['id']}, taken)]
    if ready:
        return ready, ''
    return [], 'its shards also need ' + ', '.join(sorted({n for x in work for n in x.get('holds') or []})) + ', which is busy or held'


def choices(cfg, t, pend, place, taken, free, on, quiet, best=0):
    """(the shards this target may start now, why it is blocked or ''); (None, '') when it has nothing to do (busy, or no
    compatible work). By measured power (tutti/power.py): a strong machine takes heavy shards first, a weak one light ones."""
    work, why = staying(cfg, t, [w for w in pend if compatible(t, w) and not dedicated_refuses(cfg, t, w)
                                 and (w['project'], w['key']) not in taken], place)
    if not why and (t['busy'] or not work or t['id'] in taken):
        return None, ''
    why = why or blocked_by(cfg, t, work, on, quiet)
    if why:
        return [], why
    ready, why = ready_work(cfg, t, work, free, taken)
    return power.suited(t, ready, best) if ready else (ready, why)


def plan(cfg, st, tgts, pend, on, quiet='', place=None):
    """{assign: [(target, shard)], blocked: {target id: why}}: each free target pulls a compatible pending shard, the
    projects taking turns by their weighted fair share. `place` (presence.placement): the most always-on targets choose
    first, and a long shard (suite `minutes` >= presence.long_minutes) never goes to a target likely to leave or sleep soon."""
    out, taken = {'assign': [], 'blocked': {}}, set()
    place = place or {}
    # the most always-on first, then the strongest by measured power and headroom (tutti/power.py)
    tgts = sorted(tgts, key=lambda t: (-round((place.get(t['id']) or {}).get('stay', 1.0), 1), -float(t.get('effective') or 0)))
    running = {}
    for lease in st['leases'].values():
        running[lease['project']] = running.get(lease['project'], 0) + 1
    free = {t['id'] for t in tgts if not t['busy'] and not t['blocked']}
    # the strongest machine Tutti may use (busy or free): a machine under half of it is weak (tutti/power.py)
    best = max([float(t.get('power') or 0) for t in tgts if t['kind'] == 'machine' and not t['blocked']] or [0])
    for t in tgts:
        ready, why = choices(cfg, t, pend, place, taken, free, on, quiet, best)
        if ready is None:
            continue
        if why:
            out['blocked'][t['id']] = why
            continue
        w = pick(cfg, ready, running)
        taken |= {(w['project'], w['key']), t['id'], *(f'device:{n}' for n in w.get('holds') or [])}
        running[w['project']] = running.get(w['project'], 0) + 1
        t['why'] = power.pick_why(t, w, best)
        out['assign'].append((t, w))
    return out


def describe(cfg, work):
    """'RoRadio has 14 e2e shards pending' per project and suite kind, the highest priority first."""
    counts = {}
    for w in work:
        counts[(w['project'], w['kind'])] = counts.get((w['project'], w['kind']), 0) + 1
    order = sorted(counts.items(), key=lambda x: (config.project_priority(cfg, x[0][0]), -x[1]))
    parts = [f'{p} has {n} {k} shard{"s" if n != 1 else ""} pending' for (p, k), n in order]
    return '; '.join(parts)


def fix_for(target, why):
    """The dashboard's fix button for what blocks a target."""
    kind = 'device' if target['kind'] in DEVICE_KINDS else target['kind']
    if why == OFF:
        return {'action': 'dispatch_on', 'label': 'Turn Keep busy on'}
    if 'paused' in why and kind in ('device', 'machine', 'worker'):
        return {'action': 'resume', 'kind': kind, 'name': target['name'], 'label': f'Resume {target["name"]}'}
    if 'are opt-in' in why:
        return {'action': 'optin', 'kind': 'device', 'name': target['name'], 'label': f'Allow lint and unit runs on {target["name"]}'}
    if 'autopilot is off' in why:
        return {'action': 'autopilot_on', 'label': 'Turn the autopilot on'}
    return {'action': 'dispatch_now', 'label': 'Check again now'}


AUTO_FIX = frozenset({'dispatch_on', 'autopilot_on'})   # system switches left off while work waits; pause/opt-in stay for the owner


def apply_fix(cfg, fix):
    """Applies a Keep-busy fix the idle watchdog offered (dispatch on, autopilot on). Pause and opt-in stay for the owner."""
    from . import autopilot
    action = fix['action']
    if action == 'dispatch_on':
        set_enabled(cfg, True)
    elif action == 'autopilot_on':
        autopilot.set_enabled(cfg, True)


def fix_idle_resources(cfg, st, warnings, now):
    """Turns Keep busy / the autopilot back on when an idle target with eligible work was blocked only by that switch.
    Returns True when at least one switch was turned back on."""
    fixed = False
    for w in warnings:
        fix = w.get('fix') or {}
        if fix.get('action') not in AUTO_FIX:
            continue
        apply_fix(cfg, fix)
        fixed = True
        line = f'Idle with work: auto-fixed {w["target"]} ({fix["label"]})'
        event(st, now, line)
        telemetry.record('feature', 'dispatch.idle_fix', target=w['target'], action=fix['action'], outcome='applied')
    return fixed


def watch(cfg, st, tgts, pend, p, now, worker_rows=()):
    """The idle watchdog: a target idle past dispatch.idle_minutes while compatible work waits gets a warning row, a log
    line and one telemetry event per idle stretch. Targets that are busy or have nothing to do are not idle."""
    limit = float((cfg.get('dispatch') or {}).get('idle_minutes', 5)) * 60
    sent = {a[0]['id'] for a in p['assign']}
    warnings = []
    for t in [*tgts, *worker_rows]:
        work = t.get('work') or [w for w in pend if compatible(t, w)]
        if t['busy'] or t.get('absent') or not work or t['id'] in sent:   # away or asleep is not idle
            st['idle'].pop(t['id'], None)
            st['warned'].pop(t['id'], None)
            continue
        since = st['idle'].setdefault(t['id'], now)
        if now - since < limit:
            continue
        why = t.get('why') or p['blocked'].get(t['id']) or ''
        what = t.get('what') or describe(cfg, work)
        minutes = int((now - since) // 60)
        from .devicecard import plain_minutes
        text = f'Idle for {plain_minutes(minutes)} while {what}: ' + (f'blocked by {why}' if why else 'dispatching')
        warnings.append({'target': t['id'], 'kind': t['kind'], 'name': t['name'], 'minutes': minutes, 'pending': len(work),
                         'what': what, 'state': 'blocked' if why else 'dispatching', 'reason': why, 'text': text,
                         'fix': fix_for(t, why) if why else None})
        if t['id'] not in st['warned']:
            st['warned'][t['id']] = now
            event(st, now, text)
            telemetry.record('feature', 'dispatch.idle', target_kind=t['kind'], minutes=minutes, pending=len(work),
                             reason=why or 'dispatching', outcome='blocked' if why else 'dispatching')
    st['warnings'] = warnings
    return warnings


def reclaim_idle(cfg, st, tgts, pend, p, now, rows, quiet, place):
    """Idle with work: log it, auto-fix Keep busy / autopilot left off, then re-plan so free targets take work this tick."""
    warnings = watch(cfg, st, tgts, pend, p, now, rows)
    if not fix_idle_resources(cfg, st, warnings, now):
        return p, warnings
    on = settings(cfg).get('enabled', True) is not False
    p = plan(cfg, st, tgts, pend, on, quiet, place)
    return p, watch(cfg, st, tgts, pend, p, now, rows)


def quiet_check(cfg, now, low):
    """Callable for plan(): full-disk or quiet-hours reason, or '' when a target may start work."""
    def quiet(t):
        return low[0] if low else quiet_now(cfg, now, t.get('zone') or '')
    return quiet


def idle_with_work(warnings):
    """Idle-with-eligible-work rows: a free target sat unused while compatible work waited. Empty is the healthy case."""
    return list(warnings or [])


# ---- workers: queued backlog tasks and a ladder step with room


def worker_rows(cfg, accounts, machines):
    """Each autopilot step (a worker or an account) with room while backlog tasks are queued, as a watchdog target; the
    autopilot is what dispatches to it, so its own plan says what blocks it."""
    from . import autopilot, backlog  # autopilot imports modules that import this one
    tasks = backlog.load(cfg)['tasks']
    repos = autopilot.settings(cfg)['repos']
    queued = [t for t in tasks if t['status'] == 'queued' and t['repo'] in repos]
    if not queued:
        return []
    used = autopilot.spent_units(tasks, accounts)
    last = (autopilot.read_state(cfg).get('last') or {})
    on = autopilot.enabled(cfg)
    why = '' if on and last.get('state') == 'dispatching' else (last.get('reason') or 'the autopilot has not planned yet')
    why = why if on else 'the autopilot is off, so queued backlog tasks wait'
    out = []
    for name in dict.fromkeys([*autopilot.ladder(cfg), *autopilot.trigger_accounts(cfg)]):
        room, _ = autopilot.step_room(cfg, name, accounts, used, machines or ())
        if room > 0 and not used.get(name):
            out.append({'id': f'worker:{name}', 'kind': 'worker', 'name': name, 'busy': False, 'why': why,
                        'work': queued, 'what': f'{len(queued)} backlog task{"s" if len(queued) != 1 else ""} wait for a worker'})
    return out


# ---- I/O: commits, shard lists, leases and runs


def ask(cfg, spec):
    """The first line a {run, where} command prints, or None (the machine did not answer)."""
    from . import remote
    where = spec.get('where', 'local')
    machine = {'transport': 'local', 'name': 'local'} if where == 'local' else cfg['machines'][where]
    try:
        r = remote.sh(machine, spec['run'], capture=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def refresh(cfg, st, now, ask_fn=ask):
    """Each project's latest commit and its suites' shard lists (`shards_from` asked with it), every COMMIT_EVERY seconds."""
    by_project = {}
    for s in suites(cfg):
        by_project.setdefault(s['project'], []).append(s)
    for pname, ss in by_project.items():
        p = st['projects'].get(pname) or {}
        due = now - (p.get('checked') or 0) >= COMMIT_EVERY
        commit = p.get('commit')
        spec = (cfg['projects'].get(pname) or {}).get('commit')
        if spec and due:
            got = (ask_fn(cfg, spec) or '').strip()
            commit = got.splitlines()[0] if got else commit
        lists = {}
        for s in ss:
            names = list(s['shards'])
            if s['shards_from']:   # discovered names join the listed ones; a listed shard keeps its own options (devices, holds)
                known = sorted({k.partition('/')[2] for k in (p.get('shards') or {}) if k.startswith(s['name'] + '/')})
                got = ask_fn(cfg, s['shards_from']) if due else None
                found = [x.strip() for x in got.splitlines() if x.strip()] if got is not None else known
                names += [n for n in found if n not in names]
                if not names:
                    continue   # its machine did not answer yet: no list, no guessed `all` shard
            lists[s['name']] = names
        sync(st, pname, commit, lists, now)
        if due:
            st['projects'][pname]['checked'] = now


def paths(cfg, lid):
    base = os.path.join(folder(cfg), lid)
    return {'log': base + '.log', 'beat': base + '.beat', 'rc': base + '.rc'}


def beat_age(cfg, lid, now):
    try:
        return now - os.path.getmtime(paths(cfg, lid)['beat'])
    except OSError:
        return None


STOPPED_FOR_CALL = 'call'


def read_rc(cfg, lid):
    """The run's exit code, STOPPED_FOR_CALL when a phone call stopped it, or None while it has none."""
    try:
        with open(paths(cfg, lid)['rc'], encoding='utf8') as f:
            text = f.read().strip()
    except OSError:
        return None
    if text == STOPPED_FOR_CALL or text.startswith(shards.REQUEUE):
        return text
    try:
        return int(text)
    except ValueError:
        return None


def requeue(st, lease, why, now):
    """The shard back in the queue with no attempt counted: a phone call is not a failure."""
    sh = ((st['projects'].get(lease['project']) or {}).get('shards') or {}).get(lease['key'])
    if sh and sh.get('lease') == lease['id']:
        sh.update(status='queued', why=why, at=now, lease=None)


def finish(st, lease, ok, why, now):
    p = st['projects'].get(lease['project']) or {}
    sh = (p.get('shards') or {}).get(lease['key'])
    if not sh or sh.get('lease') != lease['id'] or p.get('commit') != lease.get('commit'):
        return   # a newer commit queued it again: this result is for an older one
    sh.update(status='passed' if ok else 'failed', attempts=sh['attempts'] + (0 if ok else 1), why=why, at=now, lease=None,
              log=lease.get('log'))


def stop_pid(pid):
    """A lease's run stopped (its target left): its process group gets SIGTERM; it is gone already when that fails."""
    import signal
    with contextlib.suppress(OSError, AttributeError):
        os.killpg(pid, signal.SIGTERM)


def interrupted(lease, rc, alive, gone, boot):
    """Why the lease's run is interrupted, not failed, or '': its target left (away or asleep), or its host went down under it
    (an earlier boot: a power-off or reboot). Interrupted shards are queued again with no attempt counted."""
    if lease['target'] in (gone or {}) and rc != 0:
        return f'{lease["name"]} left mid-run ({gone[lease["target"]]}): queued again, not counted as a failure'
    if rc is None and not alive and durable.died_with_host(lease.get('boot'), boot):
        return 'interrupted by a power-off or reboot of this host: queued again, not counted as a failure'
    return ''


def no_result_why(cfg, lid, alive, now, age_fn):
    """A run with no result yet: None while it lives with a fresh heartbeat, else why it counts as failed."""
    limit = float((cfg.get('dispatch') or {}).get('lease_minutes', 10)) * 60
    age = age_fn(cfg, lid, now)
    if alive and age is not None and age < limit:
        return None
    if not alive:
        return 'the run died without a result (crash): retried'
    return f'no heartbeat for {int((age or limit) // 60)} min: retried'


def ended_why(cfg, st, lid, lease, rc, alive, cut, now, age_fn):
    """Why the lease's run ended, or None while it still runs with a fresh heartbeat."""
    if rc is None and not cut:
        return no_result_why(cfg, lid, alive, now, age_fn)
    if rc == STOPPED_FOR_CALL:
        return 'stopped for a phone call: queued again, not counted as a failure'
    if isinstance(rc, str):
        if lease.get('kind') in DEVICE_KINDS:   # the device was locked or in use: let it be for a while
            cool = float((cfg.get('dispatch') or {}).get('requeue_cooldown_minutes', 10)) * 60
            st.setdefault('cooldown', {})[lease['name']] = now + cool
        return rc[len(shards.REQUEUE):].strip()
    if cut:
        return cut
    return 'passed' if rc == 0 else f'failed (exit {rc}): see {paths(cfg, lid)["log"]}'


def recover(cfg, st, now, is_alive, rc_fn=read_rc, age_fn=beat_age, gone=None, boot=None, stop_fn=stop_pid):
    """Ends every lease whose run finished (its rc file) or died (no live pid, or a heartbeat older than lease_minutes): the
    shard is passed, failed or (crashed) failed with its retries counted, and its target is free again. A run whose target
    left (`gone`: presence's away and asleep targets) or whose host rebooted is interrupted: queued again, nothing counted."""
    ended = []
    for lid, lease in list(st['leases'].items()):
        rc = rc_fn(cfg, lid)
        alive = is_alive(lease['pid'])
        cut = interrupted(lease, rc, alive, gone, boot)
        why = ended_why(cfg, st, lid, lease, rc, alive, cut, now, age_fn)
        if why is None:
            continue
        if cut and alive and rc is None:
            stop_fn(lease['pid'])
        if isinstance(rc, str) or cut:
            requeue(st, lease, why, now)
        else:
            finish(st, lease, rc == 0, why, now)
        del st['leases'][lid]
        ended.append(f'{lease["project"]} {lease["key"]} on {lease["target"]}: {why}')
        event(st, now, ended[-1])
    return ended


def start(cfg, st, target, w, now, spawn=subprocess.Popen):
    """Leases the shard to the target and starts `tutti dispatch run <lease>` in the background."""
    from . import remote
    lid = f'{int(now)}-{os.getpid()}-{len(st["leases"])}-{target["name"]}'
    p = paths(cfg, lid)
    with open(p['beat'], 'w', encoding='utf8'):
        pass
    with open(p['log'], 'a', encoding='utf8') as log:
        log.write(f'dispatch: {w["project"]} {w["key"]} on {target["id"]} (attempt {w["attempts"] + 1})\n')
        child = spawn([sys.executable, remote.BIN, '--config', cfg['_path'], 'dispatch', 'run', lid], stdout=log, stderr=log,
                      stdin=subprocess.DEVNULL, start_new_session=True)
    commit = st['projects'][w['project']].get('commit')
    st['leases'][lid] = {'id': lid, 'project': w['project'], 'key': w['key'], 'commit': commit, 'target': target['id'],
                         'kind': target['kind'], 'name': target['name'], 'pid': child.pid, 'started': now,
                         'holds': list(w.get('holds') or []), 'why': target.get('why', ''), 'extra': w.get('extra', ''), 'log': p['log'],
                         'boot': durable.boot_id()}
    st['projects'][w['project']]['shards'][w['key']].update(status='running', lease=lid, target=target['id'], at=now)
    st['idle'].pop(target['id'], None)
    st['warned'].pop(target['id'], None)
    event(st, now, f'{w["project"]} {w["key"]} sent to {target["id"]}')
    return lid


def quiet_now(cfg, now, zone=''):
    """Why nothing new starts now ('' when it may): dispatch.quiet_hours, read in `zone` (a remote machine's own, read over
    its ssh) or this computer's (time.zone), daylight saving included."""
    from datetime import datetime, timezone

    from . import planner, tz
    hours = (cfg.get('dispatch') or {}).get('quiet_hours') or []
    z = zone if tz.valid(zone) else tz.zone()
    if hours and planner.quiet(datetime.fromtimestamp(now, timezone.utc), hours, z):
        where = f'{zone} time' if tz.valid(zone) else 'local time'
        return f'quiet hours ({", ".join(hours)} {where}, dispatch.quiet_hours): nothing new starts'
    return ''


def waiting_since(st, tgts, pend):
    """{target id: since when compatible work waits for it} (the oldest pending shard's queue time)."""
    out = {}
    for t in tgts:
        ats = [((st['projects'].get(w['project']) or {}).get('shards') or {}).get(w['key'], {}).get('at') or
               (st['projects'].get(w['project']) or {}).get('since') or 0 for w in pend if compatible(t, w)]
        if ats:
            out[t['id']] = min(ats)
    return out


def tick(cfg, devices_rows, machine_rows, accounts=None, now=None, is_alive=None, spawn=subprocess.Popen, ask_fn=ask,
         presence_io=presence.round_io, members=None, models=None, stop_fn=stop_pid):
    """One round: presence from the probes, end finished, dead or interrupted leases, refresh the pending work, pull it to
    free targets that are here, watch for idle ones, and reconnect or wake absent ones."""
    now = now or time.time()
    is_alive = is_alive or controls.alive
    on = settings(cfg).get('enabled', True) is not False
    rows = worker_rows(cfg, accounts or {}, machine_rows) if accounts is not None else []
    pst = presence.read(cfg)
    changes = presence.update(cfg, pst, devices_rows, machine_rows, now, members, models)
    off = presence.offset(cfg)
    gone = {tid: presence.label(r, off) for tid, r in presence.absent(pst).items()}
    planned = controls.planned_round(cfg, pst, now, stop_fn=lambda kind, name: hand_over_now(cfg, kind, name))
    planned += controls.blocked_round(cfg, now, stop_fn=lambda kind, name: hand_over_now(cfg, kind, name))
    with locked(cfg) as st:
        for line in planned:
            event(st, now, line)
        for tid, change, r in changes:
            event(st, now, f'{tid} {"is back" if change == "returned" else presence.label(r, off).lower()}')
        restore_left(cfg, st, devices_rows, now)
        before = {lid: lease['project'] for lid, lease in st['leases'].items()}
        recover(cfg, st, now, is_alive, gone=gone, stop_fn=stop_fn)
        touched = {pr for lid, pr in before.items() if lid not in st['leases']}
        refresh(cfg, st, now, ask_fn)
        touched |= builds(cfg, st, now, is_alive, spawn)
        pend = pending(cfg, st, now)
        held = {**cooling(st, now), **calls.holds(cfg, st, devices_rows, now)}
        tgts = targets(devices_rows, machine_rows, st, held, gone)
        pend = follow_devices(cfg, st, now, pend, devices_rows, gone)
        pend = runners_free(pend, machine_rows)
        with contextlib.suppress(Exception):   # Cast roles of the machines (tutti/castroles.py), asked once a day
            from . import castroles
            mroles = castroles.machine_roles(cfg, now)
            droles = {d['name']: d.get('roles') or [] for d in devices_rows or []}
            for t in tgts:
                t['roles'] = (mroles if t['kind'] == 'machine' else droles).get(t['name'], [])
        from . import diskroom
        low = diskroom.problems(cfg) if '_disk' not in cfg else cfg['_disk']   # a full disk: nothing new starts, as in quiet hours
        quiet, place = quiet_check(cfg, now, low), presence.placement(cfg, pst, now)
        p = plan(cfg, st, tgts, pend, on, quiet, place)
        for line in presence_io(cfg, pst, now, waiting_since(st, [t for t in tgts if t.get('absent')], pend)):
            event(st, now, line)
        kept = [w for w in st['warnings'] if w['kind'] == 'worker'] if accounts is None else []
        p, warnings = reclaim_idle(cfg, st, tgts, pend, p, now, rows, quiet, place)
        st['warnings'] = warnings + kept   # a round without the accounts' readings keeps the workers' rows
        for t, w in p['assign']:
            try:
                start(cfg, st, t, w, now, spawn)
            except OSError as e:
                event(st, now, f'{w["project"]} {w["key"]} could not start on {t["id"]} ({e}): it stays queued')
        st['last'] = {'at': now, 'on': on, 'sent': len(p['assign']), 'pending': len(pend), 'blocked': p['blocked']}
        for project in touched & set(st['projects']):   # the results where the project's own sessions read them (projects.<p>.report)
            shards.write_report(cfg, project, st['projects'][project], now)
    presence.write(cfg, pst)
    telemetry.record('feature', 'dispatch.tick', sent=len(p['assign']), pending=len(pend), blocked=len(p['blocked']))
    return p


def released(text):
    """The devices a run's output says were released (every user done: tutti/prep.py release_script)."""
    return re.findall(r'^tutti: device released (\S+)', text or '', re.M)


def announce_free(cfg, names, now=None, st=None):
    """Tells the other users a device is free: an event on the dashboard (Keep busy) and a line in
    <state>/devices/free.jsonl that sessions and the coordinator read."""
    if not names:
        return
    now = now or time.time()
    with contextlib.suppress(KeyError, OSError):   # the notice is a courtesy: a missing state folder never fails a run
        out = os.path.join(config.state_dir(cfg), 'devices')
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, 'free.jsonl'), 'a', encoding='utf8') as f:
            for n in names:
                f.write(json.dumps({'device': n, 'at': now, 'what': 'free: every user is done; its own settings are back'}) + '\n')

    def tell(state):
        for n in names:
            event(state, now, f'{n} is free: every user is done, its own settings are back (it sleeps again)')
    if st is not None:   # called with the state already held (the dispatch round): never take its lock twice
        tell(st)
        return
    with locked(cfg) as held:
        tell(held)


def hand_over_now(cfg, kind, name):
    """A planned absence started: the target's running jobs go to the next machine, device or worker (handover)."""
    from . import handover
    return handover.stop_now(config.load(cfg['_path']), kind, name)


AWAY_STATUS = {'held': 'paused or away', 'person': 'a person is using it', 'offline': 'not reachable'}


def runners_free(pend, machine_rows):
    """A suite on a runner machine (`runner:`) waits while that machine yields (its yield flag: the PC is the owner's or
    another project's), is held, paused or off; its devices stay free for the others meanwhile."""
    rows = {m['name']: m for m in machine_rows or []}
    out = []
    for w in pend:
        m = rows.get(w.get('runner')) if w.get('runner') else None
        if m and (m.get('paused') or m.get('status') in ('held', 'paused', 'off', 'offline')):
            why = 'its yield flag is up' if m.get('paused') else f'it is {m.get("status")}'
            w = {**w, 'wait': w.get('wait') or f'its runner {m["name"]} is not available ({why}): it waits'}
        out.append(w)
    return out


def follow_devices(cfg, st, now, pend, devices_rows, gone):
    """The pending shards of devices that went away move to devices that can run them (tutti/reroute.py); the dashboard
    and the projects are told when that changes (an event, <state>/devices/moves.jsonl)."""
    from . import reroute
    away = {d['name']: AWAY_STATUS[d['status']] for d in devices_rows or [] if d.get('status') in AWAY_STATUS}
    away.update({tid.split(':', 1)[1]: 'away' for tid in (gone or {}) if tid.startswith('device:')})
    pend, report = reroute.plan(cfg, pend, away)
    roles = {d['name']: d.get('roles') or [] for d in devices_rows or []}
    pend = [reroute.paired(cfg, w, away, roles) for w in pend]
    lines = reroute.summary(cfg, report, away)
    if lines != st.get('reroute', []):
        for line in lines or ['every device is back: its shards are its own again']:
            event(st, now, line)
        with contextlib.suppress(KeyError, OSError):
            out = os.path.join(config.state_dir(cfg), 'devices')
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, 'moves.jsonl'), 'a', encoding='utf8') as f:
                f.write(json.dumps({'at': now, 'lines': lines, 'report': report}) + '\n')
    st['reroute'] = lines
    return pend


def restore_left(cfg, st, rows, now, sh=None):
    """A device whose prep values were saved but never put back (its run's machine rebooted, the device was gone) gets
    them back once it answers again and nothing holds it (tutti/prep.py)."""
    from . import prep, remote
    sh = sh or remote.sh
    leased = {lease['name'] for lease in st['leases'].values()} | {n for lease in st['leases'].values() for n in lease.get('holds') or []}
    done = []
    for d in rows or []:
        if d.get('prep_left') != 1 or d.get('status') != 'ready' or d['name'] in leased:
            continue
        device = cfg['devices'][d['name']]
        stale = int(float(device.get('external_lock_stale_minutes', devices.EXTERNAL_STALE_MINUTES)) * 60)
        ext = devices.external_lock(device)
        try:
            r = sh(cfg['machines'][device['machine']], prep.release_script(device, ext, stale), capture=True, timeout=120)
            if r.returncode == 0 and 'stays in use' in (r.stdout or ''):
                continue   # another session or tool still uses it: the last one's release puts the values back
            ok = r.returncode == 0
            if ok:
                announce_free(cfg, released(r.stdout), now, st)
        except (OSError, subprocess.SubprocessError):
            ok = False
        event(st, now, f'{d["name"]}: the settings a run changed were put back after it ended uncleanly' if ok else
              f'{d["name"]}: putting back the settings a run changed failed; the next round tries again')
        done.append(d['name'])
    return done


def cooling(st, now):
    """{device: why} for a device whose run was queued again because it was locked or in use: left alone for a while."""
    out = {}
    for name, until in list((st.get('cooldown') or {}).items()):
        if until <= now:
            st['cooldown'].pop(name)
            continue
        out[name] = (f'{name}\'s last run never started or another app came in front (locked or in use): it is left alone '
                     f'for {int((until - now) // 60) + 1} more min (dispatch.requeue_cooldown_minutes)')
    return out


REUSE_SCRIPT = ('n=0; f=; for p in {paths}; do [ -e "$p" ] || continue; t=$(stat -c %Y "$p"); '
                '[ "$t" -gt "$n" ] && {{ n=$t; f=$p; }}; done; c=$({newer_than}); echo "newest=$n $f"; echo "commit=$c"')


def reuse_or_guard(cfg, p, s, now, sh=None, yield_fn=None):
    """Before a suite's build (`prepare`), "newest artifact, else a guarded build": with `prepare.reuse: {paths, newer_than}`
    an artifact newer than the commit (newer_than prints its time) is used as it is, nothing is built; a build that is
    needed waits while its machine yields, in `prepare.guard.hours` (e.g. 20-24, the owner's evening) or while
    `prepare.guard.yield` machines yield. Returns why nothing was started now ('' to build), recorded as the build's state."""
    import shlex

    from . import hours, remote
    pr = s.get('prepare') or {}
    where = cfg['machines'].get(pr.get('where')) if pr.get('where') else None
    reuse = pr.get('reuse') or {}
    if reuse.get('paths') and reuse.get('newer_than') and where:
        script = REUSE_SCRIPT.format(paths=' '.join(shlex.quote(x) for x in reuse['paths']), newer_than=reuse['newer_than'])
        try:
            out = (sh or remote.sh)(where, script, capture=True, timeout=60).stdout or ''
        except (OSError, subprocess.SubprocessError):
            out = ''
        newest = next((x[7:] for x in out.splitlines() if x.startswith('newest=')), '0 ')
        commit = next((x[7:] for x in out.splitlines() if x.startswith('commit=')), '').strip()
        t, _, path = newest.partition(' ')
        if path and commit.isdigit() and int(t) >= int(commit):
            p.setdefault('prepare', {})[s['name']] = {'commit': p.get('commit'), 'status': 'passed', 'at': now, 'reused': path}
            return f'reusing {path} (newer than commit {(p.get("commit") or "")[:12]}): nothing built'
    guard = pr.get('guard') or {}
    why = ''
    for spec in guard.get('hours') or []:
        if hours.window(spec, None, now):
            why = f'no build in {spec} (prepare.guard.hours)'
    for name in [*(guard.get('yield') or []), *([pr['where']] if pr.get('where') else [])]:
        m = cfg['machines'].get(name)
        if not why and m and m.get('yield_file') and (yield_fn or remote.yield_note)(m) is not None:
            why = f'no build while {name} yields (its yield flag is up)'
    if why:
        p.setdefault('prepare', {})[s['name']] = {'commit': p.get('commit'), 'status': 'failed', 'at': now, 'why': why}
        return f'the build waits: {why}'
    return ''


def builds(cfg, st, now, is_alive, spawn=subprocess.Popen):
    """Each suite's `prepare` (the build) once per commit, before its shards: finished ones collected, due ones started.
    Returns the projects whose build ended."""
    out, ended = [], set()
    for s in suites(cfg):
        p = st['projects'].get(s['project'])
        if not p or not s.get('prepare'):
            continue
        done = shards.collect_prepare(p, now, is_alive)
        out += [f'{s["project"]} {line}' for line in done]
        ended |= {s['project']} if done else set()
        left = any(sh['status'] != 'passed' for k, sh in p['shards'].items() if k.startswith(s['name'] + '/'))
        if left and shards.prepare_due(p, s, now):
            skip = reuse_or_guard(cfg, p, s, now)
            if skip:
                event(st, now, f'{s["project"]} {s["name"]}: {skip}')
                continue
            shards.start_prepare(cfg, p, s, folder(cfg), now, spawn)
            event(st, now, f'{s["project"]} {s["name"]}: building commit {(p.get("commit") or "")[:12]}')
    for line in out:
        event(st, now, line)
    return ended


def live_tick(cfg, is_alive=None, spawn=subprocess.Popen):
    """A tick with fresh probes (a run that just ended, `tutti dispatch now`): no dashboard snapshot to lean on."""
    from . import keepon, remote, state
    rows = devices.devices_state(cfg, keepon.readings_with_revive(cfg, remote.device_readings))
    return tick(cfg, rows, state.machines_state(cfg, remote.probe), is_alive=is_alive, spawn=spawn)


def test_argv(cfg, lease, s, shard):
    """The `tutti test` a shard runs: on its device (ANDROID_SERIAL, the device lock) or on its machine."""
    from . import remote
    where = ['--device', lease['name'], '--wait-minutes', '5'] if lease['kind'] in DEVICE_KINDS else ['--on', lease['name']]
    tree = config.expand(s['dir']) if s.get('dir') else os.path.join(folder(cfg), 'tree')
    os.makedirs(tree, exist_ok=True)
    track = f'dispatch-{s["project"]}-{s["name"]}'.lower().replace(' ', '-')
    holds = [x for n in lease.get('holds') or [] for x in ('--hold-device', n)]
    command = s['run'].replace('{shard}', shard).replace('{extra}', lease.get('extra') or '')
    if s.get('desktop'):   # a Windows UI run: in the logged-on user's interactive session, not ssh's session 0
        from . import winsession
        dk = s['desktop']
        command = winsession.wrap(command, dk['user'], dk.get('dir', ''), (dk.get('before') or '').replace('{shard}', shard))
    empty = shards.empty_filter(command)
    if empty:
        raise config.ConfigError(f'{s["project"]} {s["name"]}/{shard}: {empty}')
    from . import deps
    command = deps.prefix(deps.for_suite(cfg, s['project'], s['name'])) + command   # declared needs installed first
    if s.get('runner') and lease['kind'] in DEVICE_KINDS:   # the suite runs on its runner machine, the devices over leases
        names = [lease['name'], *(lease.get('holds') or [])]
        # options before the names: argparse refuses a `-- <command>` after options that follow a nargs='*' positional
        return [sys.executable, remote.BIN, '--config', cfg['_path'], 'devices', 'lease', '--runner', s['runner'],
                '--who', f'dispatch-{s["project"]}', *names, '--', command]
    return [sys.executable, remote.BIN, '--config', cfg['_path'], 'test', '--dir', tree, *where, *holds, track, command]


def wait_call_out(cfg, device, beat, look=calls.look, sleep=time.sleep):
    """While the call lasts, and resume_after_call_seconds after it, the heartbeat goes on and nothing runs."""
    d = cfg.get('dispatch') or {}
    poll = float(d.get('call_poll_seconds', 3))
    while look(cfg, device) in (devices.CALL_RINGING, devices.CALL_OFFHOOK):
        os.utime(beat)
        sleep(poll)
    waited = 0.0
    while waited < float(d.get('resume_after_call_seconds', 30)):
        os.utime(beat)
        sleep(poll)
        waited += poll


def on_call(cfg, lease, s, child, beat, look=calls.look, sleep=time.sleep):
    """A call on the device mid-run: the Home key frees the screen; with the suite's pause/resume hooks the run waits for
    the call and goes on (None), else it is stopped cleanly and queued again (STOPPED_FOR_CALL)."""
    device = cfg['devices'][lease['name']]
    calls.release_focus(cfg, device)
    if s.get('pause') and calls.hook(cfg, device, s['pause']):
        wait_call_out(cfg, device, beat, look, sleep)
        if s.get('resume') and calls.hook(cfg, device, s['resume']):
            return None
    stop(child)
    return STOPPED_FOR_CALL


def stop(child, grace=20):
    """The run's process group ended cleanly: SIGTERM, then SIGKILL after `grace` seconds."""
    import signal
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
    except (OSError, AttributeError):   # gone already, or no process groups (Windows)
        child.terminate()


def read_log(path):
    try:
        with open(path, encoding='utf8', errors='replace') as f:
            return f.read()
    except OSError:
        return ''


def supervise(cfg, lease, s, child, beat, look=calls.look, sleep=time.sleep, log='', clock=time.monotonic):
    """Waits for the run, touching the heartbeat; on an Android device it looks for a call every call_poll_seconds; a run
    whose tests never start (suite.started) is stopped and queued again; the result is judged from the output."""
    d = cfg.get('dispatch') or {}
    android = lease['kind'] in DEVICE_KINDS
    # a phone call is looked for on a phone or tablet only (a TV has no call state; devices.<d>.calls: false skips it too)
    phone = lease['kind'] == 'android' and (cfg['devices'].get(lease['name']) or {}).get('calls', True) is not False
    poll = float(d.get('call_poll_seconds', 3)) if phone else 5.0 if (s.get('started') or android) else 30.0
    began = clock()
    # every device run is a device session (devices.external_wrap): its ready marker starts the 2-minute start check
    ready_at = None if android else began
    while True:
        os.utime(beat)
        try:
            return shards.judge(s, read_log(log), child.wait(timeout=poll))
        except subprocess.TimeoutExpired:
            pass
        if phone and look(cfg, cfg['devices'][lease['name']]) in (devices.CALL_RINGING, devices.CALL_OFFHOOK):
            rc = on_call(cfg, lease, s, child, beat, look, sleep)
            if rc is not None:
                return rc
        text = read_log(log) if (s.get('started') or android) else ''
        if ready_at is None and (shards.started_marker() in text or not android):
            ready_at = clock()
        late = shards.not_started(s, text, clock() - ready_at, device=android) if ready_at is not None else ''
        if late:
            stop(child)
            return late


def run(cfg, lid, popen=subprocess.Popen, after=live_tick, look=calls.look, sleep=time.sleep):
    """`tutti dispatch run <lease>`: runs the shard, touches the heartbeat while it runs (and looks for phone calls on a
    device), writes the exit code, frees the lease and dispatches the next shard at once (the target is free now)."""
    st = read_state(cfg)
    lease = st['leases'].get(lid)
    if not lease:
        return 2
    suite, _, shard = lease['key'].partition('/')
    s = next((x for x in suites(cfg) if x['project'] == lease['project'] and x['name'] == suite), None)
    p = paths(cfg, lid)
    rc = 2
    try:
        if s:
            child = popen(test_argv(cfg, lease, s, shard), stdin=subprocess.DEVNULL, start_new_session=True)
            rc = supervise(cfg, lease, s, child, p['beat'], look, sleep, log=p['log'])
    except config.ConfigError as e:   # an empty test filter: refused, never run
        with open(p['log'], 'a', encoding='utf8') as f:
            f.write(f'dispatch: refused: {e}\n')
        rc = 1
    finally:
        announce_free(cfg, released(read_log(p['log'])))
        with open(p['rc'], 'w', encoding='utf8') as f:
            f.write(f'{rc}\n')
    with contextlib.suppress(Exception):   # the next shard is a bonus: the dashboard's next tick sends it anyway
        after(cfg)
    return 0 if isinstance(rc, str) else rc   # a requeue is not this process's failure


# ---- what the dashboard and `tutti dispatch status` show


def view(cfg, now=None):
    now = now or time.time()
    st = read_state(cfg)
    s = settings(cfg)
    projects = []
    for pname, p in st['projects'].items():
        counts = {}
        for sh in p['shards'].values():
            counts[sh['status']] = counts.get(sh['status'], 0) + 1
        failed = [{'key': k, 'why': sh.get('why'), 'attempts': sh['attempts']} for k, sh in p['shards'].items() if sh['status'] == 'failed']
        projects.append({'name': pname, 'priority': config.project_priority(cfg, pname), 'commit': (p.get('commit') or '')[:12],
                         'counts': counts, 'failed': failed[:20]})
    projects.sort(key=lambda x: (x['priority'], x['name']))
    return {'enabled': s.get('enabled', True) is not False, 'idle_minutes': s.get('idle_minutes', 5), 'projects': projects,
            'warnings': st['warnings'], 'reroute': st.get('reroute', []), 'leases': list(st['leases'].values()),
            'events': st['events'][:10], 'last': st.get('last'),
            'suites': len(suites(cfg)), 'at': now}


def describe_view(v):
    lines = [f'dispatch: {"on" if v["enabled"] else "off"} (idle warning after {v["idle_minutes"]} min); {v["suites"]} suite(s) declared']
    for p in v['projects']:
        counts = ', '.join(f'{n} {k}' for k, n in sorted(p['counts'].items())) or 'no shards'
        lines.append(f'  {p["name"]} (priority {p["priority"]}) @ {p["commit"] or "(no commit: projects.<p>.commit)"}: {counts}')
        lines += [f'    failed {f["key"]} ({f["attempts"]} attempt(s)): {f["why"]}' for f in p['failed']]
    lines += [f'  running {x["project"]} {x["key"]} on {x["target"]}' + (f' ({x["why"]})' if x.get('why') else '') for x in v['leases']]
    lines += [f'  ! {w["text"]}' for w in v['warnings']]
    return '\n'.join(lines)
