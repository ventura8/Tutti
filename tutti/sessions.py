"""Always-on and cloud agent sessions that keep working when a laptop sleeps or closes.

Sessions are persisted atomically in `<state>/sessions.json` (tutti/durable.py), can be started, steered,
paused, resumed, handed over and stopped from any device (CLI or dashboard `/api/sessions`), preserve their
session state across handovers, and enforce user-defined quotas, privacy (`local_only` by default) and
explicit `costs_money` opt-in with clear cost notices."""
import os
import time

from . import config, durable, presence

ACTIVE = ('running', 'paused')
ACTIONS = ('pause', 'resume', 'stop', 'complete', 'steer', 'handover')
PRIVACY_MODES = ('local_only', 'cloud_allowed')


class SessionError(ValueError):
    """A session request was rejected (quota, privacy, missing cost opt-in, or unknown id/action)."""


def settings(cfg):
    """User-defined session policy from `sessions:` in config, with safe defaults."""
    raw = cfg.get('sessions') if isinstance(cfg.get('sessions'), dict) else {}
    return {
        'max_sessions': raw.get('max_sessions'),
        'privacy': raw.get('privacy') or 'local_only',
        'default_host': raw.get('default_host'),
    }


def state_path(cfg):
    return os.path.join(config.state_dir(cfg), 'sessions.json')


def read(cfg):
    """Reads `<state>/sessions.json`, returning a fresh store when absent or unreadable."""
    try:
        with open(state_path(cfg), encoding='utf8') as f:
            import json
            data = json.load(f)
    except (OSError, ValueError):
        return {'next_id': 1, 'sessions': []}
    if not isinstance(data, dict) or not isinstance(data.get('sessions'), list):
        return {'next_id': 1, 'sessions': []}
    return {'next_id': int(data.get('next_id') or len(data['sessions']) + 1), 'sessions': data['sessions']}


def write(cfg, data):
    """Persists the session store atomically and durably so it survives sleep or power-off."""
    os.makedirs(config.state_dir(cfg), exist_ok=True)
    durable.dump_json(state_path(cfg), data, indent=2)


def is_cloud(host_cfg):
    """True when a machine entry represents a cloud sandbox rather than a household host."""
    return bool((host_cfg or {}).get('cloud'))


def is_always_on(name, host_cfg, pst=None):
    """True for a non-paused, non-laptop, non-cloud household host."""
    if not host_cfg or host_cfg.get('paused') or host_cfg.get('laptop') or is_cloud(host_cfg):
        return False
    rec = ((pst or {}).get('targets') or {}).get(f'machine:{name}')
    return presence.always_on(rec) >= 0.5 if rec else True


def cost_notice(host_name, host_cfg, worker_name, worker_cfg):
    """Clear human-readable cost notice for any paid host or worker, or '' when free/local."""
    parts = []
    if (host_cfg or {}).get('costs_money'):
        note = host_cfg.get('cost_note') or 'billed by the cloud provider while the session runs'
        parts.append(f'host {host_name} costs money ({note})')
    if (worker_cfg or {}).get('costs_money'):
        note = worker_cfg.get('cost_note') or 'pay-per-use API or premium requests'
        parts.append(f'worker {worker_name} costs money ({note})')
    return '; '.join(parts)


def check_cost(host_name, host_cfg, worker_name, worker_cfg, opt_in_cost):
    """Requires explicit user opt-in whenever the chosen host or worker has `costs_money: true`."""
    notice = cost_notice(host_name, host_cfg, worker_name, worker_cfg)
    if not notice:
        return ''
    host_ok = not (host_cfg or {}).get('costs_money') or (host_cfg or {}).get('enabled') is True
    worker_ok = not (worker_cfg or {}).get('costs_money') or (worker_cfg or {}).get('enabled') is True
    if opt_in_cost or (host_ok and worker_ok):
        return notice
    raise SessionError(
        f'cost opt-in required: {notice}. Pass --accept-cost (or opt_in_cost=true) or set enabled: true in config.'
    )


def check_privacy(host_name, host_cfg, privacy):
    """Blocks cloud hosts when privacy is `local_only`."""
    if privacy not in PRIVACY_MODES:
        raise SessionError(f'sessions.privacy is {privacy!r}: use one of {", ".join(PRIVACY_MODES)}')
    if is_cloud(host_cfg) and privacy != 'cloud_allowed':
        raise SessionError(
            f'privacy blocks cloud host {host_name}: sessions.privacy is {privacy!r}; '
            'use a household always-on host or set privacy to cloud_allowed.'
        )


def check_quota(cfg, sessions):
    """Enforces `sessions.max_sessions` against currently active (running or paused) sessions."""
    limit = settings(cfg)['max_sessions']
    if limit is None:
        return
    active = sum(1 for s in sessions if s.get('status') in ACTIVE)
    if active >= limit:
        raise SessionError(
            f'session quota reached ({active}/{limit} active): stop or complete a session, or raise sessions.max_sessions.'
        )


def _rank_hosts(cfg, pst):
    """Household always-on hosts first (highest always_on score), then cloud hosts."""
    machines = cfg.get('machines') or {}
    always, cloud = [], []
    for name, m in machines.items():
        if (m or {}).get('paused'):
            continue
        if is_cloud(m):
            cloud.append((name, m))
        elif is_always_on(name, m, pst):
            rec = ((pst or {}).get('targets') or {}).get(f'machine:{name}')
            always.append((presence.always_on(rec) if rec else 1.0, name, m))
    always.sort(key=lambda x: (-x[0], x[1]))
    return [(n, m) for _, n, m in always], sorted(cloud)


def pick_host(cfg, privacy, opt_in_cost, explicit=None, pst=None):
    """Chooses an always-on household host first, or a cloud sandbox when privacy and cost opt-in allow."""
    machines = cfg.get('machines') or {}
    if explicit:
        if explicit not in machines:
            raise SessionError(f'unknown host {explicit!r}: configure it under machines: in tutti.config.yaml')
        return explicit, machines[explicit]
    always, cloud = _rank_hosts(cfg, pst)
    if always:
        return always[0]
    if cloud and privacy == 'cloud_allowed':
        return cloud[0]
    if cloud:
        raise SessionError(
            f'no always-on household host is available and privacy ({privacy}) blocks cloud host {cloud[0][0]}: '
            'add a non-laptop household machine or allow cloud_allowed.'
        )
    raise SessionError('no always-on host is available: configure a non-laptop machine under machines: in tutti.config.yaml')


def _worker_cfg(cfg, worker):
    if not worker:
        return {}
    if worker in (cfg.get('workers') or {}):
        return cfg['workers'][worker] or {}
    for off in cfg.get('off') or []:
        if off.get('kind') == 'worker' and off.get('name') == worker:
            return {'costs_money': bool(off.get('costs_money')), 'enabled': False}
    return {}


def start(cfg, task, worker=None, host=None, device='local', repo='', track='', privacy=None, opt_in_cost=False, state=None, now=None):
    """Starts and persists a new always-on or cloud agent session."""
    if not (task or '').strip():
        raise SessionError('task is empty: provide a task description to start a session')
    store = read(cfg)
    check_quota(cfg, store['sessions'])
    eff_privacy = privacy or settings(cfg)['privacy']
    pst = presence.read(cfg)
    host_name, host_cfg = pick_host(cfg, eff_privacy, opt_in_cost, explicit=host or settings(cfg)['default_host'], pst=pst)
    check_privacy(host_name, host_cfg, eff_privacy)
    notice = check_cost(host_name, host_cfg, worker or '', _worker_cfg(cfg, worker), opt_in_cost)
    ts = time.time() if now is None else now
    sid = f's{store["next_id"]}'
    store['next_id'] += 1
    session = {
        'id': sid, 'task': task.strip(), 'worker': worker or '', 'host': host_name,
        'host_kind': 'cloud' if is_cloud(host_cfg) else 'always_on',
        'status': 'running', 'device': device or 'local', 'repo': repo or '', 'track': track or sid,
        'privacy': eff_privacy, 'cost_opted_in': bool(opt_in_cost or not notice), 'cost_notice': notice,
        'state': dict(state or {}), 'created_at': ts, 'updated_at': ts,
        'history': [{'at': ts, 'action': 'start', 'device': device or 'local', 'host': host_name}],
    }
    store['sessions'].append(session)
    write(cfg, store)
    return session


def list_sessions(cfg, status=None):
    """All persisted sessions, optionally filtered by status."""
    items = read(cfg)['sessions']
    return [s for s in items if s.get('status') == status] if status else items


def get_session(cfg, session_id):
    """Returns a single session dict by id, or raises SessionError."""
    for s in read(cfg)['sessions']:
        if s.get('id') == session_id:
            return s
    raise SessionError(f'unknown session {session_id!r}: run `tutti session list` to see active sessions')


def _find_mut(store, session_id):
    for s in store['sessions']:
        if s.get('id') == session_id:
            return s
    raise SessionError(f'unknown session {session_id!r}: run `tutti session list` to see active sessions')


def _apply_handover(cfg, s, to_host, device, note, opt_in_cost, ts):
    pst = presence.read(cfg)
    target, host_cfg = pick_host(cfg, s['privacy'], opt_in_cost, explicit=to_host, pst=pst)
    check_privacy(target, host_cfg, s['privacy'])
    notice = check_cost(target, host_cfg, s.get('worker', ''), _worker_cfg(cfg, s.get('worker')), opt_in_cost or s.get('cost_opted_in'))
    prev_host, prev_dev = s['host'], s['device']
    s.update(host=target, host_kind='cloud' if is_cloud(host_cfg) else 'always_on',
             device=device or prev_dev, status='running', cost_notice=notice or s.get('cost_notice', ''))
    s['history'].append({'at': ts, 'action': 'handover', 'from_host': prev_host, 'to_host': target,
                         'from_device': prev_dev, 'to_device': s['device'], 'note': note})


def control(cfg, session_id, action, device='remote', note='', to_host=None, state_patch=None, opt_in_cost=False, now=None):
    """Controls an existing session from any device, preserving and merging session state."""
    if action not in ACTIONS:
        raise SessionError(f'unknown action {action!r}: use one of {", ".join(ACTIONS)}')
    store = read(cfg)
    s = _find_mut(store, session_id)
    ts = time.time() if now is None else now
    if state_patch:
        s['state'] = {**(s.get('state') or {}), **state_patch}
    if action == 'handover':
        _apply_handover(cfg, s, to_host, device, note, opt_in_cost, ts)
    else:
        status_map = {'pause': 'paused', 'resume': 'running', 'stop': 'stopped', 'complete': 'completed'}
        if action in status_map:
            s['status'] = status_map[action]
        s['device'] = device or s['device']
        s['history'].append({'at': ts, 'action': action, 'device': s['device'], 'note': note})
    s['updated_at'] = ts
    write(cfg, store)
    return s


def sleep_handoff(cfg, sleeping_host=None, device='laptop', opt_in_cost=False, now=None):
    """Called when a laptop sleeps/closes: hands running sessions to an always-on host or pauses with state preserved."""
    store = read(cfg)
    ts = time.time() if now is None else now
    pst = presence.read(cfg)
    changed = []
    for s in store['sessions']:
        if s.get('status') != 'running':
            continue
        if sleeping_host and s.get('host') != sleeping_host:
            continue
        changed.append(_handoff_on_sleep(cfg, s, device, opt_in_cost, pst, ts))
    if changed:
        write(cfg, store)
    return changed


def _handoff_on_sleep(cfg, s, device, opt_in_cost, pst, ts):
    try:
        target, host_cfg = pick_host(cfg, s['privacy'], opt_in_cost or s.get('cost_opted_in'), pst=pst)
        check_privacy(target, host_cfg, s['privacy'])
        check_cost(target, host_cfg, s.get('worker', ''), _worker_cfg(cfg, s.get('worker')), opt_in_cost or s.get('cost_opted_in'))
    except SessionError as e:
        s['status'] = 'paused'
        s['updated_at'] = ts
        s['history'].append({'at': ts, 'action': 'sleep_pause', 'device': device, 'note': str(e)})
        return s
    prev = s['host']
    s.update(host=target, host_kind='cloud' if is_cloud(host_cfg) else 'always_on', updated_at=ts)
    s['history'].append({'at': ts, 'action': 'sleep_handoff', 'from_host': prev, 'to_host': target, 'device': device})
    return s


def resume_after_wake(cfg, device='laptop', now=None):
    """Resumes sessions that were paused by laptop sleep, preserving their state."""
    store = read(cfg)
    ts = time.time() if now is None else now
    resumed = []
    for s in store['sessions']:
        if s.get('status') == 'paused' and s.get('history') and s['history'][-1].get('action') == 'sleep_pause':
            s['status'] = 'running'
            s['device'] = device
            s['updated_at'] = ts
            s['history'].append({'at': ts, 'action': 'wake_resume', 'device': device})
            resumed.append(s)
    if resumed:
        write(cfg, store)
    return resumed


def api_get(cfg):
    """GET /api/sessions: persisted sessions and current session policy."""
    return {'sessions': list_sessions(cfg), 'settings': settings(cfg)}


def api_post(cfg, body):
    """POST /api/sessions: start or control a session from any device (dashboard, mobile, remote)."""
    if not isinstance(body, dict):
        return 400, {'error': 'body must be a JSON object'}
    action = body.get('action') or 'start'
    try:
        if action == 'start':
            s = start(
                cfg, body.get('task', ''), worker=body.get('worker'), host=body.get('host'),
                device=body.get('device') or 'dashboard', repo=body.get('repo') or '',
                track=body.get('track') or '', privacy=body.get('privacy'),
                opt_in_cost=bool(body.get('opt_in_cost')), state=body.get('state'),
            )
            return 200, {'ok': True, 'session': s}
        s = control(
            cfg, body.get('id', ''), action, device=body.get('device') or 'dashboard',
            note=body.get('note') or '', to_host=body.get('to_host'),
            state_patch=body.get('state'), opt_in_cost=bool(body.get('opt_in_cost')),
        )
        return 200, {'ok': True, 'session': s}
    except SessionError as e:
        return 400, {'error': str(e)}


def cmd_session(cfg, a):
    """`tutti session <action>` CLI handler."""
    import json
    import sys
    try:
        if a.action == 'list':
            items = list_sessions(cfg, status=a.status)
            if a.json:
                print(json.dumps(items, indent=2))
            else:
                for s in items:
                    print(f'{s["id"]}\t{s["status"]}\t{s["host"]} ({s["host_kind"]})\t{s["device"]}\t{s["task"]}')
            return 0
        if a.action == 'show':
            print(json.dumps(get_session(cfg, a.id), indent=2))
            return 0
        if a.action == 'start':
            s = start(cfg, a.task or '', worker=a.worker, host=a.host, device=a.device, repo=a.repo or '',
                      track=a.track or '', privacy=a.privacy, opt_in_cost=a.accept_cost)
            print(json.dumps(s, indent=2) if a.json else f'session {s["id"]} started on {s["host"]} ({s["host_kind"]})')
            return 0
        s = control(cfg, a.id or '', a.action, device=a.device, note=a.note or '', to_host=a.host, opt_in_cost=a.accept_cost)
        print(json.dumps(s, indent=2) if a.json else f'session {s["id"]} {a.action} on {s["host"]} ({s["status"]})')
        return 0
    except SessionError as e:
        print(f'session: {e}', file=sys.stderr)
        return 2

