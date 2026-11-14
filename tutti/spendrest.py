"""Spend the rest: a scout's allowance that is behind its pace near its reset is used by Tutti itself on scout work that has
value, never on backlog tasks (a scout answers questions; it takes no bounded tasks). On by default, `scouts.<name>.spend_rest:
false` turns it off; `{hours: 24, stop_at: 95, min_gap_hours: 3}` tune it.

Inside the last `hours` before the allowance's target, while it is behind (or late) and under `stop_at` %, every round sends,
in the order of the projects' `priority` (config.project_priority: 0 first; a watch without `project:` after the ranked
ones) and then by kind:
  1. Sentry triage of every project with Sentry (the `sentry-*` watches);
  2. dependency and changelog notes (a watch whose name says dep, changelog, release or notes);
  3. the other watches (live-site checks and the like);
  4. research questions from the backlog: a queued task whose title starts with `scout:`;
then, with the room left, a review of each open pull request the review trial still takes (`scouts.<name>.review_trial`;
GitHub is asked outside the scouts' lock, so reviews come last in a round).
A watch runs at most once more in that stretch, and only `min_gap_hours` after its last run. The scout's own caps still hold:
`per_tick` per round, `max_open` waiting, and the points left before `stop_at` (one question costs the account's task_pct)."""
import json
import re
import time

from . import backlog, config, pacing, scouts

DEFAULTS = {'enabled': True, 'hours': 24, 'stop_at': 95, 'min_gap_hours': 3}
RANK = {'sentry': 0, 'notes': 1, 'check': 2, 'research': 3}
PREFIX = 'scout:'
NOTES_RX = re.compile(r'dep|changelog|release|notes', re.I)


def settings(sc):
    v = sc.get('spend_rest', True)
    if isinstance(v, bool):
        return {**DEFAULTS, 'enabled': v}
    return {**DEFAULTS, **(v if isinstance(v, dict) else {})}


def errors(key, v):
    if v is None or isinstance(v, bool):
        return []
    if not isinstance(v, dict):
        return [f'{key} is {v!r}: true, false, or {{hours: 24, stop_at: 95, min_gap_hours: 3}}']
    errs = [f'{key}.{k} is {v[k]!r}: a number above 0' for k in ('hours', 'stop_at', 'min_gap_hours')
            if k in v and (isinstance(v[k], bool) or not isinstance(v[k], (int, float)) or v[k] <= 0)]
    return errs + [f'{key}.{k} is not a spend_rest key: use enabled, hours, stop_at, min_gap_hours' for k in v if k not in DEFAULTS]


def active(sc, acc, now):
    """(on, why) for the scout's account state `acc` at `now` (epoch seconds)."""
    s = settings(sc)
    if not s['enabled']:
        return False, f'off (scouts.{sc["name"]}.spend_rest: false)'
    if not acc or acc.get('state') != 'ok' or acc.get('used') is None:
        return False, 'no reading of its allowance in this window'
    if acc.get('panic') or acc.get('verdict') not in ('behind', 'late'):
        return False, f'{acc.get("verdict")}: not behind its pace'
    if acc['used'] >= s['stop_at']:
        return False, f'{acc["used"]:g} % used: it stops at {s["stop_at"]:g} %'
    start = pacing.parse_time(acc['target']).timestamp() - s['hours'] * 3600
    if now < start or now >= pacing.parse_time(acc['reset']).timestamp():
        return False, f'not in the last {s["hours"]:g} h before its reset'
    return True, ''


def points_left(sc, acc):
    """How many questions fit before stop_at, each costing the account's task_pct."""
    cost = float(sc.get('_task_pct') or pacing.TASK_PCT)
    return max(0, int((settings(sc)['stop_at'] - float(acc.get('used') or 0)) // cost)) if cost > 0 else 0


def kind_of(name, w):
    if name.startswith('sentry-'):
        return 'sentry'
    return 'notes' if NOTES_RX.search(name) else w.get('spend_kind') or 'check'


def extra_watches(sc, requests, acc, now, until=None):
    """The watches that may run once more now (or, with `until`, by then): run before, not due by their own period,
    min_gap_hours since their last run, and no extra run of theirs yet in this stretch."""
    s = settings(sc)
    since = pacing.parse_time(acc['target']).timestamp() - s['hours'] * 3600
    out = []
    for name, w in (sc.get('watches') or {}).items():
        last = scouts.last_fired(requests, sc['name'], name)
        if last is None or (until or now) - last < s['min_gap_hours'] * 3600 or scouts.due(sc, requests, name, w, acc, now):
            continue
        if any(r['scout'] == sc['name'] and r.get('watch') == name and r.get('extra') and (r.get('sent') or r['added']) >= since
               for r in requests):
            continue
        out.append({'kind': kind_of(name, w), 'ref': name, 'project': w.get('project')})
    return out


def questions(cfg):
    """The backlog's research questions for a scout: queued tasks whose title starts with `scout:`."""
    return [{'kind': 'research', 'ref': t['id'], 'project': config.project_of_repo(cfg, t['repo']),
             'task': t['title'][len(PREFIX):].strip()}
            for t in backlog.load(cfg)['tasks'] if backlog.is_question(t) and t['status'] == 'queued']


def ordered(cfg, items):
    return sorted(items, key=lambda x: (config.project_priority(cfg, x.get('project')), RANK[x['kind']]))


def plan(cfg, sc, requests, acc, now):
    """{on, why, items}: what spend-the-rest would send now, in order (reviews are listed by the round, which asks GitHub)."""
    on, why = active(sc, acc, now)
    if not on:
        return {'on': False, 'why': why, 'items': [], 'later': 0}
    items = ordered(cfg, extra_watches(sc, requests, acc, now) + questions(cfg))
    target = pacing.parse_time(acc['target']).timestamp()
    later = len(extra_watches(sc, requests, acc, now, target)) - sum(1 for i in items if i['kind'] != 'research')
    return {'on': True, 'why': '', 'items': items, 'later': later}


def count(p, reviews=0):
    """How many extra runs spend-the-rest plans before the reset: free now, free later, and reviews."""
    return len(p['items']) + p.get('later', 0) + reviews


def line(p, reviews=0):
    """The scout account's Do line while spend-the-rest is on."""
    n = count(p, reviews)
    if not p['on']:
        return ''
    if not n:
        return 'Scout: will use the remaining allowance before the reset; no extra watch is free yet (each waits min_gap_hours)'
    return f'Scout: will use the remaining allowance on {n} watch(es) before the reset'


def room(sc, data, acc, sent):
    waiting = sum(1 for r in data['requests'] if r['scout'] == sc['name'] and r['status'] == 'waiting')
    return max(0, min(int(sc['per_tick']) - sent, int(sc['max_open']) - waiting, points_left(sc, acc)))


def round_(cfg, sc, data, acc, url, key, sent, gh_fn=scouts.gh, post_fn=scouts.post, now=None):
    """Under the scouts lock: sends the extra watches and the backlog's questions that fit; returns (events, how many sent)."""
    now = now or time.time()
    p = plan(cfg, sc, data['requests'], acc, now)
    events, n = [], 0
    for item in p['items'][:room(sc, data, acc, sent)]:
        if item['kind'] == 'research':
            r = scouts.new_request(data, sc, item['task'], 'ask', skill='research', now=now)
            r['backlog'] = item['ref']
        else:
            w = sc['watches'][item['ref']]
            r = scouts.new_request(data, sc, w['task'], 'watch', item['ref'], w.get('repo'), w.get('skill'), now)
        r.update(extra=True, project=item.get('project'))
        try:
            scouts.send(sc, r, url, key, gh_fn, post_fn, now)
        except scouts.ScoutError as e:
            r.update(status='failed', why=str(e))
        if r['status'] == 'waiting' and item['kind'] == 'research':
            mark_asked(cfg, item['ref'], sc['name'], r)
        n += r['status'] == 'waiting'
        events.append(f'spend the rest: {r["id"]} {r["status"]}: {r.get("url") or r.get("why")}')
    return events, n


def mark_asked(cfg, task_id, name, r):
    """A backlog question that went to a scout leaves the queue: a person reads the answer on its issue."""
    with backlog.locked(cfg) as data:
        t = backlog.find(data, task_id)
        t['status'] = 'review'
        t['notes'] = [*t.get('notes', []), f'asked scout {name} ({r["id"]}): {r.get("url")}']


def open_prs(repo, gh_fn=scouts.gh):
    """[(number, url)] of the repository's open pull requests, or [] when GitHub cannot be asked."""
    rc, out, _ = gh_fn(['pr', 'list', '--repo', repo, '--state', 'open', '--json', 'number,url', '--limit', '20'])
    try:
        return [(int(p['number']), p['url']) for p in json.loads(out or '[]')] if rc == 0 else []
    except (ValueError, KeyError, TypeError):
        return []


def reviews(cfg, name, acc, left, gh_fn=scouts.gh, post_fn=scouts.post, env=None, now=None):
    """Outside the scouts lock: the review trial's open pull requests not reviewed yet, at most `left`. Returns events."""
    from . import trial  # trial builds on scouts
    sc = scouts.settings(cfg, name)
    if left <= 0 or not active(sc, acc, now or time.time())[0] or trial.may_review(cfg, name):
        return []
    t, events = trial.settings(cfg, name) or {'repos': []}, []
    done = {p['key'] for p in (trial.load(cfg)['scouts'].get(name) or {}).get('prs', [])}
    for repo in t['repos']:
        for number, url in open_prs(repo, gh_fn):
            if left <= 0 or trial.key_of(repo, number) in done or not trial.eligible(cfg, trial.load(cfg), name, repo):
                continue
            try:
                r = trial.ask_review(cfg, name, repo, number, url, gh_fn, post_fn, env, {acc['name']: acc})
            except scouts.ScoutError as e:
                events.append(f'spend the rest: review of {repo}#{number} not sent: {e}')
                continue
            left -= 1
            events.append(f'spend the rest: review {r["id"]} {r["status"]}: {url}')
    return events
