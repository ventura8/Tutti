"""The dashboard's telemetry: the page's script errors and the panels and controls it uses (POST /api/telemetry), the consent
switch, the Self-improvement switch (POST /api/self-improve), and what /api/state shows (status, Insights, the lane).
Everything the page sends is reduced to categories and scrubbed text here, on the server, before it is recorded."""
import json
import os
import re
import time

from . import config, insights, scrub, selfimprove, telemetry

PANELS = ('usage', 'attention', 'running', 'machines', 'autopilot', 'main', 'workers', 'projects', 'scouts', 'controls', 'jobs',
          'insights', 'selfimprove', 'tiles', 'appbar', 'helper', 'page')
LOCATION_RX = re.compile(r'(\S+):(\d{1,7}):(\d{1,7})')
_cache = {}


def body(raw):
    try:
        data = json.loads(raw or b'null')
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def js_frame(line):
    """One stack line (`at fn (url:line:col)` in Chromium, `fn@url:line:col` in Firefox) as a frame, or None."""
    line = line.strip().removeprefix('at ')
    fn, _, where = line.rpartition('@') if '@' in line else line.rpartition(' ')
    m = LOCATION_RX.fullmatch(where.strip('()'))
    if not m:
        return None
    name = scrub.category(fn.strip() or '?')
    return {'function': name if name != scrub.OTHER else '?', 'filename': scrub.url_path(m.group(1)), 'lineno': int(m.group(2)),
            'colno': int(m.group(3)), 'in_app': True}


def js_frames(stack):
    frames = [f for f in (js_frame(x) for x in str(stack or '')[:4000].splitlines()) if f]
    return frames[::-1][-30:]   # Sentry wants the oldest frame first


def js_error_event(err):
    """A page error as a Sentry event: its type, scrubbed message and frames (dashboard files only)."""
    name = scrub.category(str(err.get('type') or 'Error'))
    ev = telemetry.base_event('error', {'where': 'dashboard', 'kind': err.get('kind') or 'error'})
    ev['platform'] = 'javascript'
    ev['exception'] = {'values': [{'type': name if name != scrub.OTHER else 'Error',
                                   'value': scrub.text(err.get('message'), hosts=telemetry._ctx['hosts'], limit=300),
                                   'stacktrace': {'frames': js_frames(err.get('stack'))}}]}
    return ev


def save(collector, raw):
    """(status, answer) for POST /api/telemetry: {"consent": bool} | {"error": {...}} | {"use": {"panel", "control"}}."""
    data = body(raw)
    if data is None:
        return 400, {'error': 'send a JSON object: {"consent": true}, {"error": {...}} or {"use": {"panel": "usage"}}'}
    folder = telemetry.folder_of(config.state_dir(collector.cfg))
    if isinstance(data.get('consent'), bool):
        telemetry.set_consent(folder, data['consent'], 'dashboard')
        return 200, {'ok': True, 'send': data['consent']}
    if isinstance(data.get('error'), dict):
        telemetry.report(js_error_event(data['error']), 'crash', 'dashboard')
        return 200, {'ok': True}
    if isinstance(data.get('use'), dict):
        panel = data['use'].get('panel')
        if panel not in PANELS:
            return 400, {'error': f'panel must be one of {", ".join(PANELS)}'}
        telemetry.record('dashboard', panel, control=scrub.category(data['use'].get('control')))
        return 200, {'ok': True}
    return 400, {'error': 'send {"consent": true|false}, {"error": {...}} or {"use": {"panel": ..., "control": ...}}'}


def save_self_improve(collector, raw):
    data = body(raw)
    if not data or not isinstance(data.get('enabled'), bool):
        return 400, {'error': 'send {"enabled": true} or {"enabled": false} as JSON'}
    selfimprove.set_enabled(collector.cfg, data['enabled'])
    return 200, {'ok': True, 'enabled': data['enabled']}


def cached_insights(folder, days=7):
    """The Insights summary, read again only when the events file changed (the page polls every few seconds)."""
    path = os.path.join(folder, telemetry.EVENTS)
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        stamp = 0
    hit = _cache.get(folder)
    if hit and hit[0] == stamp and time.time() - hit[1] < 300:
        return hit[2]
    s = insights.summary(telemetry.read_events(folder, insights.since_iso(days)), days)
    _cache[folder] = (stamp, time.time(), s)
    return s


def view(cfg):
    folder = telemetry.folder_of(config.state_dir(cfg))
    st = telemetry.status(folder, cfg)
    st.pop('folder', None)
    st['events'] = os.path.basename(st['events'])
    return {'status': st, 'insights': cached_insights(folder), 'self_improve': selfimprove.lane(cfg)}


def tick(cfg):
    """After each probe: self-improvement's round, then a send of the queue when due. Returns event lines."""
    events = selfimprove.tick(cfg)
    folder = telemetry.folder_of(config.state_dir(cfg))
    telemetry.flush(folder, cfg)
    return events
