"""Household improvement loop (backlog t250): Tutti reviews its own telemetry and outcomes on a schedule and proposes
plus applies improvements across household/SB aspects (routing, pacing, parallel limits, …), not only code
self-improvement. On by default; anonymised sharing of kept improvements is admin opt-in (default OFF). Builds on
self-improvement (tutti/selfimprove.py) and AI assist auto-tune (tutti/aiassist.py).

Each improvement carries evidence, expected gain and safe bounds. Within `improvements.autonomy` (propose | bounded)
a small change applies by itself; a larger one waits on the Improvements view (Needs attention). After
`measure_hours` the metric is compared and the change is kept or rolled back automatically. Pure where it can be;
config writes and needs questions stay at the edge."""
import os
import re
import time

from . import config, insights, settings as settings_mod, telemetry

FILE = 'improvements.json'
DEFAULTS = {
    'enabled': True, 'autonomy': 'bounded', 'every_hours': 6, 'measure_hours': 48,
    'max_auto_per_day': 2, 'share': False, 'min_count': 3,
}
AUTONOMY = ('propose', 'bounded')
DAY = 86400
ASPECTS = ('routing', 'pacing', 'parallel', 'crashes')


def settings(cfg):
    return {**DEFAULTS, **(cfg.get('improvements') or {})}


def path_of(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


def read_state(cfg):
    return telemetry.read_json(path_of(cfg), {})


def write_state(cfg, data):
    telemetry.write_json(path_of(cfg), data)


def enabled(cfg):
    """Dashboard switch when set, else improvements.enabled (on by default)."""
    on = read_state(cfg).get('enabled')
    return settings(cfg)['enabled'] if on is None else bool(on)


def set_enabled(cfg, on):
    data = read_state(cfg)
    data['enabled'] = bool(on)
    write_state(cfg, data)
    telemetry.record('feature', 'improve.switch', on=bool(on))
    return data


def set_share(cfg, on):
    """Admin opt-in for anonymised improvement reports (default OFF). Logged; revocable."""
    data = read_state(cfg)
    data['share'] = bool(on)
    data.setdefault('share_log', []).append({'at': time.time(), 'on': bool(on)})
    data['share_log'] = data['share_log'][-50:]
    write_state(cfg, data)
    telemetry.record('feature', 'improve.share', on=bool(on))
    return data


def share_on(cfg):
    st = read_state(cfg)
    if 'share' in st:
        return bool(st['share'])
    return bool(settings(cfg)['share'])


def current(cfg, path):
    v = cfg
    for k in path:
        v = (v or {}).get(k) if isinstance(v, dict) else None
    return v


def metric_of(summary, name):
    tables = {
        'stalls': lambda: sum(s['count'] for s in summary.get('stalls') or []),
        'escalations': lambda: int(summary.get('escalations') or 0),
        'parks': lambda: sum(p['count'] for p in summary.get('parked') or []),
        'crashes': lambda: sum(c['count'] for c in summary.get('crashes') or []),
    }
    if name in tables:
        return tables[name]()
    return round(sum(w['minutes'] for w in summary.get('wasted') or []), 1)


def _routing(cfg, summary, min_count):
    """Fewer no-progress parks and wasted quota: stop a stuck task one attempt sooner."""
    parked = {p['cause']: p['count'] for p in summary.get('parked') or []}
    wasted = metric_of(summary, 'wasted_minutes')
    n = parked.get('no_progress', 0)
    old = current(cfg, ('autopilot', 'no_progress'))
    if not isinstance(old, int) or n < min_count or wasted < 30:
        return None
    return {
        'aspect': 'routing', 'path': ('autopilot', 'no_progress'), 'value': old - 1,
        'lo': 2, 'hi': 6, 'step': 1, 'metric': 'wasted_minutes', 'better': 'lower',
        'evidence': f'{n} tasks parked with no progress and {wasted:g} min wasted in 7 days',
        'expected_gain': f'cut wasted quota by stopping no-progress work one attempt sooner (now {old})',
    }


def _pacing(cfg, summary, min_count):
    """Many escalations: allow one more attempt before parking, within bounds."""
    esc = metric_of(summary, 'escalations')
    old = current(cfg, ('autopilot', 'max_attempts'))
    if not isinstance(old, int) or esc < min_count:
        return None
    return {
        'aspect': 'pacing', 'path': ('autopilot', 'max_attempts'), 'value': old + 1,
        'lo': 4, 'hi': 20, 'step': 2, 'metric': 'escalations', 'better': 'lower',
        'evidence': f'{esc} escalations in 7 days at max_attempts {old}',
        'expected_gain': 'fewer parks from early escalation; one more attempt on the same step',
    }


def _parallel(cfg, summary, min_count):
    """Repeated stalls: wait longer before a job counts as stalled (scheduling vs presence/quiet)."""
    stalls = metric_of(summary, 'stalls')
    old = current(cfg, ('dashboard', 'stall_minutes'))
    if not isinstance(old, (int, float)) or stalls < min_count:
        return None
    return {
        'aspect': 'parallel', 'path': ('dashboard', 'stall_minutes'), 'value': int(old) + 5,
        'lo': 5, 'hi': 60, 'step': 5, 'metric': 'stalls', 'better': 'lower',
        'evidence': f'{stalls} stall events in 7 days at stall_minutes {old:g}',
        'expected_gain': 'fewer false stalls while a quiet or night window pauses work',
    }


def _crashes(cfg, summary, min_count):
    """High crash count: reduce parallel tasks to increase stability."""
    crashes = metric_of(summary, 'crashes')
    old = current(cfg, ('autopilot', 'parallel'))
    if not isinstance(old, int) or crashes < min_count or old <= 1:
        return None
    return {
        'aspect': 'crashes', 'path': ('autopilot', 'parallel'), 'value': old - 1,
        'lo': 1, 'hi': 5, 'step': 1, 'metric': 'crashes', 'better': 'lower',
        'evidence': f'{crashes} crashes in 7 days',
        'expected_gain': 'improve stability by reducing parallel tasks',
    }


RULES = (_routing, _pacing, _parallel, _crashes)
REVIEW = 'REVIEW:'


def propose(cfg, summary, min_count=None):
    """[{aspect, path, value, lo, hi, step, metric, better, evidence, expected_gain}] from telemetry, one per aspect."""
    min_count = settings(cfg)['min_count'] if min_count is None else int(min_count)
    out = []
    for rule in RULES:
        item = rule(cfg, summary, min_count)
        if item and item['lo'] <= item['value'] <= item['hi']:
            out.append(item)
    return review(cfg, summary, out)


def _review_prompt(summary, items):
    rows = [{
        'aspect': i['aspect'],
        'path': '.'.join(i['path']),
        'value': i['value'],
        'bounds': {'lo': i['lo'], 'hi': i['hi'], 'step': i['step']},
        'metric': i['metric'],
        'evidence': i['evidence'],
        'expected_gain': i['expected_gain'],
    } for i in items]
    return (
        'You review Tutti household improvement proposals. For each proposal, answer one line exactly:\n'
        'REVIEW: <aspect> || verdict: keep|drop || value: <number or same> || evidence: <short text> || gain: <short text>\n'
        'Only use listed aspects. Keep changes within each bounds.\n\n'
        f'Insights summary:\n{summary}\n\nProposals:\n{rows}'
    )


def _parse_review_line(line):
    text = line.strip().lstrip('-*0123456789.) ').strip()
    if not text.upper().startswith(REVIEW):
        return None
    body = text[len(REVIEW):].strip()
    parts = [p.strip() for p in body.split('||')]
    if not parts:
        return None
    out = {'aspect': parts[0].lower()}
    for p in parts[1:]:
        key, _, val = p.partition(':')
        out[key.strip().lower()] = val.strip()
    return out


def _apply_review_value(out, raw):
    value = str(raw.get('value') or 'same').strip().lower()
    if value in ('same', ''):
        return out
    m = re.fullmatch(r'-?\d+', value)
    if m:
        out['value'] = int(value)
    return out


def _reviewed_item(raw, item):
    if str(raw.get('verdict') or 'keep').lower() == 'drop':
        return None
    out = _apply_review_value(dict(item), raw)
    if raw.get('evidence'):
        out['evidence'] = str(raw['evidence'])[:180]
    if raw.get('gain'):
        out['expected_gain'] = str(raw['gain'])[:180]
    return out if out['lo'] <= out['value'] <= out['hi'] else None


def _ask_review(ask, cfg, prompt):
    try:
        return ask(cfg, prompt, 'self-improve', online=False)
    except TypeError:
        return ask(cfg, prompt)


def _collect_reviewed(items, answer):
    by_aspect = {i['aspect'].lower(): i for i in items}
    out, seen = [], set()
    for line in str(answer).splitlines():
        row = _parse_review_line(line)
        if not row or row['aspect'] not in by_aspect or row['aspect'] in seen:
            continue
        item = _reviewed_item(row, by_aspect[row['aspect']])
        if not item:
            continue
        out.append(item)
        seen.add(row['aspect'])
    return out


def review(cfg, summary, items, ask=None):
    """Optional local-AI review over rule proposals; falls back to rules when AI is off or unparsable."""
    if not items:
        return []
    from . import aiassist
    if not (aiassist.switch(cfg, 'enabled') and aiassist.switch(cfg, 'local')):
        return items
    answer, who = _ask_review(ask or aiassist.ask_local, cfg, _review_prompt(summary, items))
    if answer is None:
        return items
    out = _collect_reviewed(items, answer)
    if not out:
        return items
    telemetry.record(
        'feature', 'improve.review', reviewer=str(who)[:80],
        kept=len(out), dropped=max(0, len(items) - len(out)),
    )
    return out


def verdict(item, autonomy):
    """'auto', 'propose' or 'drop' for one improvement under the admin's autonomy level."""
    old = item.get('old')
    if old is None or old == item['value']:
        return 'drop'
    delta = abs(item['value'] - old)
    inside = item['lo'] <= item['value'] <= item['hi'] and delta <= item['step']
    if autonomy == 'propose' or not inside:
        return 'propose'
    return 'auto'


def fill_old(cfg, items):
    for it in items:
        it['old'] = current(cfg, it['path'])
    return items


def auto_room(log, now, limit):
    return max(0, int(limit) - sum(1 for e in log if e.get('status') == 'applied' and now - e['at'] < DAY
                                   and e.get('by') == 'auto'))


def log_entry(item, status, now, by, before=None):
    return {
        'id': f'i{int(now)}-{item["aspect"][:3]}', 'at': now, 'aspect': item['aspect'],
        'path': '.'.join(item['path']), 'old': item.get('old'), 'new': item['value'],
        'evidence': item['evidence'], 'expected_gain': item['expected_gain'],
        'bounds': {'lo': item['lo'], 'hi': item['hi'], 'step': item['step']},
        'metric': item['metric'], 'better': item['better'], 'status': status, 'by': by,
        'before': before, 'measure_after': None, 'outcome': None,
    }


def apply_one(cfg, st, item, summary, now, by, store):
    """Writes one improvement into the config and logs it for later measure/rollback. Returns (ok, event)."""
    key, old = '.'.join(item['path']), item.get('old')
    try:
        store(cfg, [(list(item['path']), item['value'])])
    except settings_mod.SettingsError as e:
        return False, f'improvements: {key} not applied: {e}'
    before = metric_of(summary, item['metric'])
    entry = log_entry(item, 'applied', now, by, before)
    entry['measure_after'] = now + float(settings(cfg)['measure_hours']) * 3600
    st.setdefault('log', []).append(entry)
    telemetry.record('feature', 'improve.apply', aspect=item['aspect'], by=by)
    return True, f'improvements: {item["aspect"]} {key} {old} -> {item["value"]} ({item["evidence"]})'


def propose_one(st, item, now):
    key = '.'.join(item['path'])
    waiting = [p for p in st.get('proposals') or [] if now - p['at'] < 7 * DAY and p['path'] != key]
    entry = log_entry(item, 'proposed', now, 'loop')
    waiting.append({**entry, 'path': key, 'value': item['value']})
    st['proposals'] = waiting
    st.setdefault('log', []).append(entry)
    telemetry.record('feature', 'improve.propose', aspect=item['aspect'])
    return f'improvements: {item["aspect"]} {key} proposed ({item["evidence"]})'


def improved(before, after, better):
    if before is None or after is None:
        return False
    return after < before if better == 'lower' else after > before


def rollback_one(cfg, entry, store):
    from . import yamledit
    val = yamledit.DELETE if entry['old'] is None else entry['old']
    store(cfg, [(entry['path'].split('.'), val)])


def _same_as_applied(cfg, entry):
    path = tuple(entry['path'].split('.'))
    return current(cfg, path) == entry.get('new')


def keep_entry(st, e, after, share):
    e['after'], e['outcome'], e['status'] = after, 'kept', 'kept'
    maybe_share(st, e, share)
    telemetry.record('feature', 'improve.kept', aspect=e['aspect'])
    return f'improvements: kept {e["path"]} ({e["metric"]} {e.get("before")} -> {after})'


def roll_entry(cfg, e, store):
    if not _same_as_applied(cfg, e):
        e['outcome'], e['status'] = 'skipped_changed', 'kept'
        telemetry.record('feature', 'improve.rollback_skip', aspect=e['aspect'])
        return f'improvements: kept {e["path"]} (changed after apply; rollback skipped)'
    try:
        rollback_one(cfg, e, store)
    except settings_mod.SettingsError as err:
        return f'improvements: {e["path"]} not rolled back: {err}'
    e['outcome'], e['status'] = 'rolled_back', 'rolled_back'
    telemetry.record('feature', 'improve.rollback', aspect=e['aspect'])
    return f'improvements: rolled back {e["path"]} ({e["metric"]} did not improve)'


def due_for_measure(e, now):
    return e.get('status') == 'applied' and not e.get('outcome') and e.get('measure_after') and now >= float(e['measure_after'])


def _resolve_due(cfg, st, e, summary, share, store):
    after = metric_of(summary, e['metric'])
    if improved(e.get('before'), after, e.get('better') or 'lower'):
        return keep_entry(st, e, after, share)
    e['after'] = after
    return roll_entry(cfg, e, store)


def measure(cfg, st, summary, now, store=None):
    """Keep or roll back applied improvements past measure_hours. Returns event lines."""
    store, share = store or settings_mod.store, share_on(cfg)
    return [_resolve_due(cfg, st, e, summary, share, store)
            for e in st.get('log') or [] if due_for_measure(e, now)]


def maybe_share(st, entry, on):
    """Anonymised report of a kept improvement (setting, evidence, gain; no hosts, paths, personal data)."""
    if not on:
        return
    gain = None
    if entry.get('before') is not None and entry.get('after') is not None:
        gain = {'metric': entry['metric'], 'before': entry['before'], 'after': entry['after']}
    st.setdefault('reports', []).append({
        'at': entry['at'], 'aspect': entry['aspect'], 'setting': entry['path'],
        'evidence': entry.get('evidence', ''), 'expected_gain': entry.get('expected_gain', ''), 'gain': gain,
    })
    st['reports'] = st['reports'][-100:]


def _item_from_proposal(p, path):
    return {
        'aspect': p['aspect'], 'path': tuple(path.split('.')), 'value': p['value'], 'old': p.get('old'),
        'lo': p['bounds']['lo'], 'hi': p['bounds']['hi'], 'step': p['bounds']['step'],
        'metric': p['metric'], 'better': p['better'], 'evidence': p['evidence'],
        'expected_gain': p['expected_gain'],
    }


def _drop_proposal(cfg, st, path):
    st['proposals'] = [x for x in st.get('proposals') or [] if x['path'] != path]
    write_state(cfg, st)


def decide(cfg, path, apply, store=None, now=None):
    """Admin answer to a proposed improvement: apply (logged, measured) or dismiss. Returns (ok, why)."""
    st, now = read_state(cfg), now or time.time()
    p = next((x for x in st.get('proposals') or [] if x['path'] == path), None)
    if not p:
        return False, f'no proposed improvement for {path}: reload the page'
    if not apply:
        _drop_proposal(cfg, st, path)
        return True, ''
    ok, line = apply_one(
        cfg, st, _item_from_proposal(p, path), cached_summary(cfg) or {},
        now, 'admin', store or settings_mod.store)
    if ok:
        _drop_proposal(cfg, st, path)
    return ok, line


def cached_summary(cfg):
    from . import aiassist
    return aiassist.cached_summary(cfg)


def _recent_path(log, key, now):
    statuses = ('applied', 'proposed', 'kept')
    return any(e.get('path') == key and now - e['at'] < DAY and e.get('status') in statuses for e in log)


def _should_wait(cfg, item, autonomy, log, now, limit):
    return (verdict(item, autonomy) == 'propose' or auto_room(log, now, limit) <= 0
            or not settings_mod.writable(cfg))


def _enact_one(cfg, st, item, summary, now, store, autonomy, limit, log):
    key = '.'.join(item['path'])
    if _recent_path(log, key, now) or verdict(item, autonomy) == 'drop':
        return []
    if _should_wait(cfg, item, autonomy, log, now, limit):
        return [propose_one(st, item, now)]
    ok, line = apply_one(cfg, st, item, summary, now, 'auto', store)
    return [line] if ok else [line, propose_one(st, item, now)]


def enact(cfg, st, items, summary, now, store):
    """Apply or propose each item under autonomy and the day's room. Returns event lines."""
    store = store or settings_mod.store
    autonomy, limit = settings(cfg)['autonomy'], settings(cfg)['max_auto_per_day']
    log = st.setdefault('log', [])
    events = []
    for item in fill_old(cfg, items):
        events.extend(_enact_one(cfg, st, item, summary, now, store, autonomy, limit, log))
    return events


def _finish_round(cfg, st, summary, now, store, events):
    events = events + enact(cfg, st, propose(cfg, summary), summary, now, store)
    st['last_round'] = now
    st['log'] = (st.get('log') or [])[-200:]
    write_state(cfg, st)
    telemetry.record(
        'feature', 'improve.tick',
        proposed=sum(1 for e in events if 'proposed' in e),
        applied=sum(1 for e in events if '->' in e and 'proposed' not in e),
    )
    return events


def tick(cfg, now=None, store=None, summary=None):
    """One round: measure/rollback due items, then propose/apply across aspects when every_hours has passed."""
    if not enabled(cfg):
        return []
    now = now or time.time()
    st = read_state(cfg)
    folder = telemetry.folder_of(config.state_dir(cfg))
    summary = summary if summary is not None else insights.summary(telemetry.read_events(folder), 7)
    events = measure(cfg, st, summary, now, store)
    due = now - float(st.get('last_round') or 0) >= float(settings(cfg)['every_hours']) * 3600
    if not due:
        write_state(cfg, st)
        return events
    return _finish_round(cfg, st, summary, now, store, events)


def _log_counts(log):
    return {
        'applied': sum(1 for e in log if e.get('status') == 'applied'),
        'kept': sum(1 for e in log if e.get('outcome') == 'kept'),
        'rolled_back': sum(1 for e in log if e.get('outcome') == 'rolled_back'),
        'skipped_changed': sum(1 for e in log if e.get('outcome') == 'skipped_changed'),
    }


def view(cfg, summary=None):
    """The dashboard's Improvements view: proposed, applied, kept, rolled back, and share status."""
    st = read_state(cfg)
    summary = summary if summary is not None else (cached_summary(cfg) or {})
    now = time.time()
    proposals = [p for p in st.get('proposals') or [] if now - float(p.get('at') or 0) < 7 * DAY]
    log = st.get('log') or []
    return {
        'enabled': enabled(cfg), 'autonomy': settings(cfg)['autonomy'], 'share': share_on(cfg),
        'aspects': list(ASPECTS), 'proposals': proposals, 'log': log[-30:][::-1],
        'reports': (st.get('reports') or [])[-10:][::-1], 'last_round': st.get('last_round'),
        'waiting': len(proposals), **_log_counts(log),
        'metrics': {a: metric_of(summary, m) for a, m in (
            ('routing', 'wasted_minutes'), ('pacing', 'escalations'), ('parallel', 'stalls'))},
    }


def _save_body(data):
    import json
    try:
        out = json.loads(data or b'{}')
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def save(collector, raw):
    """POST /api/improvements (admin): switch, share, or apply/dismiss a proposal."""
    data = _save_body(raw)
    if data is None:
        return 400, {'error': 'send JSON such as {"enabled": true}, {"share": true} or {"proposal": "path", "apply": true}'}
    if 'proposal' in data:
        ok, why = decide(collector.cfg, str(data['proposal']), data.get('apply') is True)
        if ok and getattr(collector, 'reload', None):
            collector.reload()
        return (200, {'ok': True}) if ok else (409, {'error': why})
    if isinstance(data.get('share'), bool):
        set_share(collector.cfg, data['share'])
        return 200, {'ok': True, 'share': data['share']}
    if isinstance(data.get('enabled'), bool):
        set_enabled(collector.cfg, data['enabled'])
        return 200, {'ok': True, 'enabled': data['enabled']}
    return 400, {'error': 'send {"enabled": bool}, {"share": bool} or {"proposal": path, "apply": bool}'}


def _errors_bools(s):
    errs = []
    if 'enabled' in s and not isinstance(s['enabled'], bool):
        errs.append(f'improvements.enabled is {s["enabled"]!r}: true or false')
    if 'share' in s and not isinstance(s['share'], bool):
        errs.append(f'improvements.share is {s["share"]!r}: true or false (admin opt-in, default false)')
    if s.get('autonomy', 'bounded') not in AUTONOMY:
        errs.append(f'improvements.autonomy is {s.get("autonomy")!r}: {" or ".join(AUTONOMY)}')
    return errs


def _errors_numbers(s):
    errs = []
    for k in ('every_hours', 'measure_hours', 'max_auto_per_day', 'min_count'):
        v = s.get(k, DEFAULTS[k])
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            errs.append(f'improvements.{k} is {v!r}: a number, 0 or more')
    return errs


def errors(cfg):
    s = cfg.get('improvements')
    if s is None:
        return []
    if not isinstance(s, dict):
        return [f'improvements is {s!r}: a mapping such as {{enabled: true, autonomy: bounded}}']
    errs = [f'improvements has {k}: use {", ".join(DEFAULTS)}' for k in sorted(set(s) - set(DEFAULTS))]
    return errs + _errors_bools(s) + _errors_numbers(s)
