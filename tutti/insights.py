"""`tutti telemetry report` and the dashboard's Insights card: what the local events say, with nothing sent anywhere.
Pure: a list of events (telemetry.read_events) in, a summary out."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

TOP = 5
FAILED = ('failed', 'error', 'crash', 'config_error', 'interrupted', 'timeout', 'stall')


def since_iso(days, now=None):
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=days)).isoformat(timespec='seconds')


def median(values):
    s = sorted(values)
    if not s:
        return 0
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def p90(values):
    s = sorted(values)
    return s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))] if s else 0


def command_name(e):
    return f'{e["name"]} {e["action"]}' if isinstance(e.get('action'), str) and e.get('action') else str(e.get('name'))


def commands(events):
    """Per command: runs, failures, median and p90 seconds."""
    runs, fails, took = Counter(), Counter(), defaultdict(list)
    for e in events:
        if e.get('kind') != 'command':
            continue
        n = command_name(e)
        runs[n] += 1
        if e.get('outcome') in FAILED or (isinstance(e.get('rc'), int) and e['rc'] not in (0, None)):
            fails[n] += 1
        if isinstance(e.get('seconds'), (int, float)):
            took[n].append(e['seconds'])
    return runs, fails, took


def wasted(events):
    """Quota spent on nothing: autopilot attempts and delegated runs that failed, per account or worker kind, with minutes."""
    out = defaultdict(lambda: {'runs': 0, 'minutes': 0.0})
    for e in events:
        if e.get('kind') == 'feature' and e.get('name') in ('autopilot.attempt', 'run.delegated') and e.get('outcome') != 'passed':
            k = str(e.get('account') or e.get('worker_kind') or 'unknown')
            out[k]['runs'] += 1
            out[k]['minutes'] += float(e.get('minutes') or 0)
    return sorted(({'who': k, **v, 'minutes': round(v['minutes'], 1)} for k, v in out.items()), key=lambda x: -x['minutes'])


def causes(events, names, field):
    return Counter(str(e.get(field) or 'unknown') for e in events if e.get('kind') == 'feature' and e.get('name') in names)


def summary(events, days=7, now=None):
    """The report: totals, most used, slowest, most failing, wasted quota, stall and park causes, crashes, dashboard use."""
    cutoff = since_iso(days, now)
    events = [e for e in events if str(e.get('ts', '')) >= cutoff]
    runs, fails, took = commands(events)
    slow = sorted(((n, median(v), p90(v), len(v)) for n, v in took.items() if v), key=lambda x: -x[1])
    failing = sorted(((n, fails[n], runs[n]) for n in fails), key=lambda x: (-x[1] / max(x[2], 1), -x[1]))
    crashes = Counter(f'{e.get("name")} in {e.get("frame") or "?"}' for e in events if e.get('kind') == 'crash')
    panels = Counter(f'{e.get("name")}' + (f' {e["control"]}' if e.get('control') else '') for e in events if e.get('kind') == 'dashboard')
    return {
        'days': days, 'events': len(events), 'commands': sum(runs.values()),
        'most_used': [{'name': n, 'runs': c} for n, c in runs.most_common(TOP)],
        'slowest': [{'name': n, 'median_s': round(m, 1), 'p90_s': round(p, 1), 'runs': k} for n, m, p, k in slow[:TOP]],
        'most_failing': [{'name': n, 'failed': f, 'runs': r} for n, f, r in failing[:TOP]],
        'wasted': wasted(events)[:TOP],
        'stalls': [{'cause': c, 'count': k} for c, k in causes(events, ('wait.stall', 'job.stall'), 'cause').most_common(TOP)],
        'parked': [{'cause': c, 'count': k} for c, k in causes(events, ('autopilot.park',), 'cause').most_common(TOP)],
        'escalations': sum(1 for e in events if e.get('name') == 'autopilot.escalate'),
        'crashes': [{'where': w, 'count': k} for w, k in crashes.most_common(TOP)],
        'dashboard': [{'name': n, 'uses': k} for n, k in panels.most_common(TOP)],
    }


def lines(s):
    """The report as text lines."""
    out = [f'Tutti telemetry, last {s["days"]} day(s): {s["events"]} event(s), {s["commands"]} command(s). Nothing here was sent.']

    def block(title, rows, fmt):
        out.append(f'{title}:')
        out.extend(f'  {fmt(r)}' for r in rows) if rows else out.append('  (none)')
    block('Most used', s['most_used'], lambda r: f'{r["name"]}: {r["runs"]}')
    block('Slowest (median, p90)', s['slowest'], lambda r: f'{r["name"]}: {r["median_s"]} s, {r["p90_s"]} s over {r["runs"]} run(s)')
    block('Most failing', s['most_failing'], lambda r: f'{r["name"]}: {r["failed"]} of {r["runs"]}')
    block('Wasted quota (failed runs)', s['wasted'], lambda r: f'{r["who"]}: {r["runs"]} run(s), {r["minutes"]} min')
    block('Stall causes', s['stalls'], lambda r: f'{r["cause"]}: {r["count"]}')
    block('Parked tasks', s['parked'], lambda r: f'{r["cause"]}: {r["count"]}')
    out.append(f'Escalations up the ladder: {s["escalations"]}')
    block('Crashes', s['crashes'], lambda r: f'{r["where"]}: {r["count"]}')
    block('Dashboard use', s['dashboard'], lambda r: f'{r["name"]}: {r["uses"]}')
    return out
