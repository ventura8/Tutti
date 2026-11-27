"""Household improvement loop (backlog t250): Tutti reviews its own telemetry and outcomes on a schedule and proposes
plus applies improvements across household/SB aspects (routing, pacing, parallel limits, …), not only code
self-improvement. On by default; anonymised sharing of kept improvements is admin opt-in (default OFF). Builds on
self-improvement (tutti/selfimprove.py) and AI assist auto-tune (tutti/aiassist.py).

Each improvement carries evidence, expected gain and safe bounds. Within `improvements.autonomy` (propose | bounded)
a small change applies by itself; a larger one waits on the Improvements view (Needs attention). After
`measure_hours` the metric is compared and the change is kept or rolled back automatically. Pure where it can be;
config writes and needs questions stay at the edge."""
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config, insights, telemetry, transfer
from . import settings as settings_mod

FILE = 'improvements.json'
DEFAULTS = config.DEFAULTS['improvements']
AUTONOMY = ('propose', 'bounded')
DAY = 86400
ASPECTS = ('routing', 'pacing', 'parallel', 'crashes')
_running = threading.Lock()


def settings(cfg):
    return {**DEFAULTS, **(cfg.get('improvements') or {})}


def path_of(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


def read_state(cfg):
    return telemetry.read_json(path_of(cfg), {})


def write_state(cfg, data):
    telemetry.write_json(path_of(cfg), data)


@contextmanager
def _locked(cfg):
    path = path_of(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.lock', 'a+b') as lock:
        transfer.lock_file(lock)
        yield


def enabled(cfg):
    """Dashboard switch when set, else improvements.enabled (on by default)."""
    on = read_state(cfg).get('enabled')
    return settings(cfg)['enabled'] if on is None else bool(on)


def set_enabled(cfg, on):
    with _locked(cfg):
        data = read_state(cfg)
        data['enabled'] = bool(on)
        write_state(cfg, data)
    telemetry.record('feature', 'improve.switch', on=bool(on))
    return data


def set_share(cfg, on):
    """Admin opt-in for anonymised improvement reports (default OFF). Logged; revocable."""
    with _locked(cfg):
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
        'lo': 5, 'hi': 60, 'step': 5, 'metric': 'wasted_minutes', 'better': 'lower', 'review_only': True,
        'evidence': f'{stalls} stall events in 7 days at stall_minutes {old:g}',
        'expected_gain': 'fewer false stalls while a quiet or night window pauses work',
    }


def _crashes(cfg, summary, min_count):
    """High crash count: reduce parallel tasks to increase stability."""
    crashes = metric_of(summary, 'crashes')
    old = current(cfg, ('autopilot', 'max_parallel'))
    if not isinstance(old, int) or crashes < min_count or old <= 1:
        return None
    return {
        'aspect': 'crashes', 'path': ('autopilot', 'max_parallel'), 'value': old - 1,
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
    rows = '\n'.join(_review_row(summary, item) for item in items)
    return (
        'You review Tutti household improvement proposals. For each proposal, answer one line exactly:\n'
        'REVIEW: <aspect> || verdict: keep|drop || value: <number or same>\n'
        'Only use listed aspects. Keep changes within their bounds.\n\n'
        f'{rows}'
    )


def _review_row(summary, item):
    metric = item['metric']
    return (
        f'{item["aspect"]}: set {".".join(item["path"])} to {item["value"]} '
        f'(range {item["lo"]} to {item["hi"]}, max step {item["step"]}). '
        f'Metric {metric}: {metric_of(summary, metric)}. Evidence: {item["evidence"]}. '
        f'Expected gain: {item["expected_gain"]}.'
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
    m = re.fullmatch(r'-?\d{1,3}', value)
    if m:
        out['value'] = int(value)
    return out


def _reviewed_item(raw, item):
    if str(raw.get('verdict')).lower() != 'keep':
        return None
    out = _apply_review_value(dict(item), raw)
    old = item['old']
    same_direction = (out['value'] - old) * (item['value'] - old) > 0
    within_step = abs(out['value'] - old) <= out['step']
    return out if same_direction and within_step and out['lo'] <= out['value'] <= out['hi'] else None


def _collect_reviewed(items, answer):
    by_aspect = {i['aspect'].lower(): i for i in items}
    out, seen = [], set()
    for line in str(answer).splitlines():
        row = _parse_review_line(line)
        if not row or row['aspect'] not in by_aspect or row['aspect'] in seen:
            continue
        seen.add(row['aspect'])
        item = _reviewed_item(row, by_aspect[row['aspect']])
        if not item:
            continue
        out.append(item)
    return out, seen


def review(cfg, summary, items, ask=None):
    """Optional local-AI review over rule proposals; falls back to rules when AI is off or unparsable."""
    if not items:
        return []
    from . import aiassist
    if not aiassist.switch(cfg, 'local', independent=True):
        return items
    prompt = _review_prompt(summary, items)
    answer, who = (ask or aiassist.ask_local)(cfg, prompt, 'self-improve', online=True, independent_local=True)
    if answer is None:
        return items
    out, seen = _collect_reviewed(fill_old(cfg, items), answer)
    if not seen:
        return items
    telemetry.record(
        'feature', 'improve.review', reviewer=str(who)[:80],
        kept=len(out), dropped=max(0, len(items) - len(out)),
    )
    return out + [item for item in items if item['aspect'] not in seen]


def verdict(item, autonomy):
    """'auto', 'propose' or 'drop' for one improvement under the admin's autonomy level."""
    old = item.get('old')
    if old is None or old == item['value']:
        return 'drop'
    delta = abs(item['value'] - old)
    inside = item['lo'] <= item['value'] <= item['hi'] and delta <= item['step']
    if autonomy == 'propose' or item.get('review_only') or not inside:
        return 'propose'
    return 'auto'


def fill_old(cfg, items):
    for it in items:
        it['old'] = current(cfg, it['path'])
    return items


def auto_room(log, now, limit):
    return max(0, int(limit) - sum(1 for e in log if now - e['at'] < DAY and e.get('by') == 'auto'))


def log_entry(item, status, now, by, before=None):
    return {
        'id': f'i{int(now)}-{item["aspect"][:3]}', 'at': now, 'aspect': item['aspect'],
        'path': '.'.join(item['path']), 'old': item.get('old'), 'new': item['value'],
        'evidence': item['evidence'], 'expected_gain': item['expected_gain'],
        'bounds': {'lo': item['lo'], 'hi': item['hi'], 'step': item['step']},
        'metric': item['metric'], 'better': item['better'], 'status': status, 'by': by,
        'before': before, 'measure_after': None, 'outcome': None,
    }


def _store_change(cfg, path, expected, value, store):
    if store is not settings_mod.store:
        store(cfg, [(path, value)])
        return
    text = settings_mod.read_file(cfg)
    fresh = config.normalise(config.parse(text, cfg['_path']))
    if current(fresh, path) != expected:
        raise settings_mod.SettingsError(409, 'setting changed: reload Improvements and review the current value')
    store(cfg, [(path, value)], tag=settings_mod.etag(text))


def apply_one(cfg, st, item, summary, now, by, store):
    """Writes one improvement into the config and logs it for later measure/rollback. Returns (ok, event)."""
    key, old = '.'.join(item['path']), item.get('old')
    try:
        _store_change(cfg, list(item['path']), old, item['value'], store)
    except (settings_mod.SettingsError, config.ConfigError, OSError) as e:
        return False, f'improvements: {key} not applied: {e}'
    before = metric_of(summary, item['metric'])
    entry = log_entry(item, 'applied', now, by, before)
    entry['before_samples'] = summary.get('work_samples')
    entry['measure_after'] = now + float(settings(cfg)['measure_hours']) * 3600
    st.setdefault('log', []).append(entry)
    telemetry.record('feature', 'improve.apply', aspect=item['aspect'], by=by)
    return True, f'improvements: {item["aspect"]} {key} {old} -> {item["value"]} ({item["evidence"]})'


def propose_one(st, item, now):
    key = '.'.join(item['path'])
    waiting = [p for p in st.get('proposals') or [] if _pending(p, now)]
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
    _store_change(cfg, entry['path'].split('.'), entry['new'], val, store)


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
    except (settings_mod.SettingsError, config.ConfigError, OSError) as err:
        return f'improvements: {e["path"]} not rolled back: {err}'
    e['outcome'], e['status'] = 'rolled_back', 'rolled_back'
    telemetry.record('feature', 'improve.rollback', aspect=e['aspect'])
    return f'improvements: rolled back {e["path"]} ({e["metric"]} did not improve)'


def due_for_measure(e, now):
    return e.get('status') == 'applied' and not e.get('outcome') and e.get('measure_after') and now >= float(e['measure_after'])


def _resolve_due(cfg, st, e, summary, share, store, now):
    after = metric_of(summary, e['metric'])
    e['measured_at'] = now
    e['after_samples'] = summary.get('work_samples')
    if _same_as_applied(cfg, e) and _trial_improved(e, summary, after):
        return keep_entry(st, e, after, share)
    e['after'] = after
    return roll_entry(cfg, e, store)


def _trial_improved(entry, summary, after):
    after_samples = summary.get('work_samples', 1)
    before_samples = entry.get('before_samples')
    before = entry.get('before')
    if not after_samples or before is None or before_samples == 0:
        return False
    if before_samples:
        return improved(before * after_samples, after * before_samples, entry.get('better', 'lower'))
    return improved(before, after, entry.get('better', 'lower'))


def measure(cfg, st, summary, now, store=None):
    """Keep or roll back applied improvements past measure_hours. Returns event lines."""
    store, share = store or settings_mod.store, share_on(cfg)
    return [_resolve_due(cfg, st, e, summary, share, store, now)
            for e in st.get('log') or [] if due_for_measure(e, now)]


def maybe_share(st, entry, on):
    """Anonymised report of a kept improvement (setting, evidence, gain; no hosts, paths, personal data)."""
    if not on:
        return
    gain = None
    if entry.get('before') is not None and entry.get('after') is not None:
        gain = {'metric': entry['metric'], 'before': entry['before'], 'after': entry['after']}
        gain.update(before_samples=entry.get('before_samples'), after_samples=entry.get('after_samples'))
    st.setdefault('reports', []).append({
        'aspect': entry['aspect'], 'setting': entry['path'], 'old': entry.get('old'), 'new': entry.get('new'),
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


def _resolve_proposal(cfg, st, proposal, status):
    for entry in st.get('log', []):
        if entry['id'] == proposal['id'] and entry['status'] == 'proposed':
            entry['outcome'] = status
    _drop_proposal(cfg, st, proposal['path'])


def decide(cfg, path, apply, store=None, now=None, proposal_id=None):
    """Admin answer to a proposed improvement: apply (logged, measured) or dismiss. Returns (ok, why)."""
    with _locked(cfg):
        return _decide(cfg, path, apply, store, now or time.time(), proposal_id)


def _decide(cfg, path, apply, store, now, proposal_id):
    st = read_state(cfg)
    p = next((x for x in st.get('proposals') or [] if x['path'] == path), None)
    if not p or (proposal_id is not None and p['id'] != proposal_id):
        return False, f'the proposal for {path} changed or is no longer pending: reload the page'
    if not apply:
        _resolve_proposal(cfg, st, p, 'dismissed')
        return True, ''
    return _approve(cfg, st, p, store, now)


def _approve(cfg, st, p, store, now):
    cfg = _fresh_config(cfg, store)
    path = p['path']
    if not _pending(p, now) or current(cfg, path.split('.')) != p.get('old'):
        return False, f'improvement for {path} expired or the setting changed: review the next proposal'
    ok, line = apply_one(
        cfg, st, _item_from_proposal(p, path), _baseline(cfg, now),
        now, 'admin', store or settings_mod.store)
    if ok:
        _resolve_proposal(cfg, st, p, 'approved')
    return ok, line


def cached_summary(cfg):
    from . import aiassist
    return aiassist.cached_summary(cfg)


def _recent_path(log, key, now):
    return any(e.get('path') == key and _blocks_trial(e, now) for e in log)


def _blocks_trial(entry, now):
    return entry.get('status') == 'applied' or now - entry.get('measured_at', entry['at']) < DAY


def _should_wait(cfg, item, autonomy, log, now, limit):
    return (verdict(item, autonomy) == 'propose' or auto_room(log, now, limit) <= 0
            or not settings_mod.writable(cfg))


def _pending(proposal, now):
    return now - proposal['at'] < 7 * DAY


def _awaiting_review(st, key, now):
    return any(p['path'] == key and _pending(p, now) for p in st.get('proposals', []))


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
        if not _awaiting_review(st, '.'.join(item['path']), now):
            events.extend(_enact_one(cfg, st, item, summary, now, store, autonomy, limit, log))
    return events


def _window(events, start, end):
    start, end = telemetry.now_iso(start), telemetry.now_iso(end)
    window = [e for e in events if start <= str(e.get('ts', '')) < end]
    summary = insights.summary(window, 0, datetime.fromisoformat(start))
    summary['work_samples'] = sum(e.get('name') in ('autopilot.attempt', 'run.delegated') for e in window)
    return summary


def _baseline(cfg, now, events=None):
    if events is None:
        events = telemetry.read_events(telemetry.folder_of(config.state_dir(cfg)))
    duration = float(settings(cfg)['measure_hours']) * 3600
    return _window(events, now - duration, now)


def _measure_windows(cfg, st, events, now, store):
    store, share = store or settings_mod.store, share_on(cfg)
    results = []
    for entry in st.get('log') or []:
        if due_for_measure(entry, now):
            after = _window(events, entry['at'], entry['measure_after'])
            results.append(_resolve_due(cfg, st, entry, after, share, store, now))
    return results


def _round_due(cfg, st, now):
    return now - float(st.get('last_round') or 0) >= float(settings(cfg)['every_hours']) * 3600


def _has_work(st, summary, items, now):
    if items or (items is not None and summary.get('events')):
        return True
    return any(due_for_measure(entry, now) for entry in st.get('log', []))


def tick(cfg, now=None, store=None, summary=None):
    """Measure due trials and review telemetry on the configured schedule."""
    return _review_round(cfg, now or time.time(), store, summary, live=now is None)


def _review_round(cfg, now, store, summary, live):
    records = telemetry.read_events(telemetry.folder_of(config.state_dir(cfg))) if summary is None else None
    if summary is None:
        summary = insights.summary(records, 7, datetime.fromtimestamp(now, timezone.utc))
    st = read_state(cfg)
    items = propose(cfg, summary) if enabled(cfg) and _round_due(cfg, st, now) else None
    if not _has_work(st, summary, items, now):
        return []
    with _locked(cfg):
        if live and items:
            now = time.time()
            records = telemetry.read_events(telemetry.folder_of(config.state_dir(cfg)))
        return _finish_round(cfg, now, store, summary, records, items)


def _finish_round(cfg, now, store, summary, records, items):
    from . import failover
    fresh = _fresh_config(cfg, store)
    if not failover.may_write(fresh):
        return []
    if not enabled(fresh):
        items = None
    if items is not None:
        items = [item for item in items if current(fresh, item['path']) == current(cfg, item['path'])]
    return _tick(fresh, now, store, summary, records, items)


def tick_in_background(cfg):
    """Run one dashboard round at a time without blocking probes on model availability."""
    if not _running.acquire(blocking=False):
        return False

    def go():
        try:
            tick(cfg)
        except Exception as err:
            telemetry.capture_exception(type(err), err, err.__traceback__, where='improvements')
        finally:
            _running.release()

    threading.Thread(target=go, name='tutti-improvements', daemon=True).start()
    return True


def _fresh_config(cfg, store):
    return config.load(cfg['_path']) if store is None and settings_mod.writable(cfg) else cfg


def _tick(cfg, now, store, summary, records, items):
    st = read_state(cfg)
    events = measure(cfg, st, summary, now, store) if records is None else _measure_windows(cfg, st, records, now, store)
    if items is not None and _round_due(cfg, st, now):
        baseline = summary if records is None else _baseline(cfg, now, records)
        events.extend(enact(cfg, st, items, baseline, now, store))
        st['last_round'] = now
        _trim_log(st)
    write_state(cfg, st)
    return events


def _trim_log(st):
    log = st.get('log') or []
    st['log'] = [entry for entry in log[:-200] if entry.get('status') == 'applied'] + log[-200:]


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
    proposals = [p for p in st.get('proposals') or [] if _pending(p, now)]
    log = st.get('log') or []
    return {
        'enabled': enabled(cfg), 'autonomy': settings(cfg)['autonomy'], 'share': share_on(cfg),
        'aspects': list(ASPECTS), 'proposals': proposals, 'log': log[-30:][::-1],
        'reports': (st.get('reports') or [])[-10:][::-1] if share_on(cfg) else [], 'last_round': st.get('last_round'),
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


def _save_decision(collector, data):
    ok, why = decide(collector.cfg, data['proposal'], data['apply'], proposal_id=data.get('id'))
    if ok and getattr(collector, 'reload', None):
        collector.reload()
    return (200, {'ok': True}) if ok else (409, {'error': why})


def save(collector, raw):
    """POST /api/improvements (admin): switch, share, or apply/dismiss a proposal."""
    data = _save_body(raw)
    if data is None:
        return 400, {'error': 'send JSON such as {"enabled": true}, {"share": true} or {"proposal": "path", "apply": true}'}
    if isinstance(data.get('proposal'), str) and isinstance(data.get('apply'), bool):
        return _save_decision(collector, data)
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
    for k, minimum in (('every_hours', 1), ('measure_hours', 1), ('max_auto_per_day', 0), ('min_count', 1)):
        v = s.get(k, DEFAULTS[k])
        if not _valid_number(v, minimum):
            errs.append(f'improvements.{k} is {v!r}: use a finite number, {minimum} or more')
    return errs


def _valid_number(value, minimum):
    return type(value) in (int, float) and minimum <= value < math.inf


def errors(cfg):
    s = cfg.get('improvements')
    if s is None:
        return []
    if not isinstance(s, dict):
        return [f'improvements is {s!r}: a mapping such as {{enabled: true, autonomy: bounded}}']
    errs = [f'improvements has {k}: use {", ".join(DEFAULTS)}' for k in sorted(set(s) - set(DEFAULTS))]
    return errs + _errors_bools(s) + _errors_numbers(s)
