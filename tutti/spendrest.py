"""The scout's week (tutti/grokweek.py) and spending the rest: a scout's weekly allowance is used all week on scout work that has
value, never on backlog tasks (a scout answers questions; it takes no bounded tasks). On by default, `scouts.<name>.spend_rest:
false` turns it off; `{hours: 6, stop_at: 95, min_gap_hours: 3, pace_gap_hours: 6, burst_per_tick: 10, burst_max_open: 30,
review_repos: [owner/name]}` tune it.

Every round sends up to the runs the week's even line allows now (`grokweek.budget`: the measured cost of one run, `stop_at` x
the window gone); in the last `hours` before the target (the burst) whatever is left goes out as fast as the caps allow. In
the order of the projects' `priority` (config.project_priority: 0 first; a watch without `project:` after the ranked ones)
and then by kind:
  1. Sentry triage of every project with Sentry (the `sentry-*` watches);
  2. dependency and changelog notes (a watch whose name says dep, changelog, release or notes);
  3. the other watches (live-site checks and the like);
  4. research questions from the backlog: a queued task whose title starts with `scout:` (or `research:`);
  5. second opinions on autopilot tasks that failed twice or more, before they climb the ladder again (tutti/autopilot.py
     waits up to `second_wait_minutes` for the answer, which joins the next attempt's brief);
  6. while the scout is behind its pace (the allowance would be lost at the reset), a scouting question for each queued
     backlog task not started yet: files, risk, approach and a check for the coding agent that takes it;
then, with the room left, a review of each open pull request of `review_repos` (default: the projects' `repo`, the review
trial's repositories) not reviewed by the scout since it last changed (GitHub is asked outside the scouts' lock, so reviews
come last in a round), and while behind its pace an audit of each pull request merged in the last 7 days. A watch
runs again `pace_gap_hours` after its last run, `min_gap_hours` while the scout is behind its pace or in the burst (once
more at most in the burst). The Do line says how many items are ready now and how many only later. The caps still hold:
`per_tick` per round and `max_open` waiting (`burst_per_tick` and `burst_max_open` in the burst), and the points left
before `stop_at`."""
import json
import re
import time

from . import autopilot, backlog, config, grokweek, pacing, scouts

DEFAULTS = {'enabled': True, 'hours': 6, 'stop_at': 95, 'min_gap_hours': 3, 'pace_gap_hours': 6, 'burst_per_tick': 10,
            'burst_max_open': 30, 'review_repos': None, 'second_wait_minutes': 30}
NUMBERS = ('hours', 'stop_at', 'min_gap_hours', 'pace_gap_hours', 'burst_per_tick', 'burst_max_open', 'second_wait_minutes')
RANK = {'sentry': 0, 'notes': 1, 'check': 2, 'research': 3, 'second': 4, 'scouting': 5}
SCOUTING = ('Before a coding agent starts this task in {repo}: "{title}".\n\nLook at the repository and name: the files it will touch, '
            'the risk (what could break), the simplest approach, and a check that proves it works. Plain words, short.')
AUDIT = ('Audit pull request {url} ({repo}#{number}), merged in the last {days} days. Read its diff: name a bug, a missing test, '
         'a security or performance problem, or a doc that is now wrong, each with the file and line and a concrete fix. '
         'Say "No findings." when there is none. Answer on that pull request.')
AUDIT_DAYS = 7
PREFIX = 'scout:'
NOTES_RX = re.compile(r'dep|changelog|release|notes', re.I)
SECOND = ('Second opinion before a coding agent retries. Task in {repo}: "{title}". It failed {n} time(s); the last failure:\n'
          '{why}\n\nName the likely cause, the file to look at and a concrete fix the next attempt should make. Plain words, short.')


def settings(sc):
    v = sc.get('spend_rest', True)
    if isinstance(v, bool):
        return {**DEFAULTS, 'enabled': v}
    return {**DEFAULTS, **(v if isinstance(v, dict) else {})}


def errors(key, v):
    if v is None or isinstance(v, bool):
        return []
    if not isinstance(v, dict):
        return [f'{key} is {v!r}: true, false, or {{hours: 6, stop_at: 95, min_gap_hours: 3}}']
    errs = [f'{key}.{k} is {v[k]!r}: a number above 0' for k in NUMBERS
            if k in v and (isinstance(v[k], bool) or not isinstance(v[k], (int, float)) or v[k] <= 0)]
    rr = v.get('review_repos')
    if rr is not None and (not isinstance(rr, list) or not all(scouts.is_repo(x) for x in rr)):
        errs.append(f'{key}.review_repos is {rr!r}: a list of owner/name repositories')
    return errs + [f'{key}.{k} is not a spend_rest key: use {", ".join(DEFAULTS)}' for k in v if k not in DEFAULTS]


def pct(sc):
    """What one run costs: measured (sc['_cost'], set by the round from the real readings), else the account's task_pct."""
    c = sc.get('_cost') or {}
    return float(c.get('pct') or sc.get('_task_pct') or pacing.TASK_PCT)


def week(sc, acc, now):
    s = settings(sc)
    return grokweek.budget(acc, now, pct(sc), s['stop_at'], s['hours'])


def active(sc, acc, now):
    """(on, why) for the scout's account state `acc` at `now` (epoch seconds): on while the week's line has room for a run now,
    or in the burst before the reset."""
    s = settings(sc)
    if not s['enabled']:
        return False, f'off (scouts.{sc["name"]}.spend_rest: false)'
    if not acc or acc.get('state') != 'ok' or acc.get('used') is None:
        return False, 'no reading of its allowance in this window'
    if acc.get('panic') or acc['used'] >= s['stop_at']:
        return False, f'{acc["used"]:g} % used: it stops at {s["stop_at"]:g} %'
    if now >= pacing.parse_time(acc['reset']).timestamp():
        return False, 'past its reset: waiting for a reading of the new week'
    b = week(sc, acc, now)
    if b['mode'] == 'ahead' or not b['now']:
        return False, f'{acc["used"]:g} % used, at its even line ({b["line"]:g} % by the next hour): the next run waits for it'
    return True, ''


def burst(sc, acc, now):
    return week(sc, acc, now)['mode'] == 'burst'


def points_left(sc, acc):
    """How many runs fit before stop_at, each costing pct(sc)."""
    cost = pct(sc)
    return max(0, int((settings(sc)['stop_at'] - float(acc.get('used') or 0)) // cost)) if cost > 0 else 0


def kind_of(name, w):
    if name.startswith('sentry-'):
        return 'sentry'
    return 'notes' if NOTES_RX.search(name) else w.get('spend_kind') or 'check'


def extra_watches(sc, requests, acc, now, until=None):
    """The watches that may run again now (or, with `until`, by then): run before, not due by their own period, and
    `pace_gap_hours` since their last run; in the burst `min_gap_hours`, and no extra run of theirs yet in this stretch."""
    s = settings(sc)
    hot = burst(sc, acc, until or now)
    since = pacing.parse_time(acc['target']).timestamp() - s['hours'] * 3600
    gap = (s['min_gap_hours'] if hot or behind(acc) else s['pace_gap_hours']) * 3600
    out = []
    for name, w in (sc.get('watches') or {}).items():
        last = scouts.last_fired(requests, sc['name'], name)
        if last is None or (until or now) - last < gap or scouts.due(sc, requests, name, w, acc, now):
            continue
        if hot and any(r['scout'] == sc['name'] and r.get('watch') == name and r.get('extra') and (r.get('sent') or r['added']) >= since
                       for r in requests):
            continue
        out.append({'kind': kind_of(name, w), 'ref': name, 'project': w.get('project')})
    return out


def behind(acc):
    """The scout's allowance would be lost at its reset: its account is behind its pace or late (pacing's verdict)."""
    return bool(acc) and acc.get('verdict') in ('behind', 'late')


def scouting(cfg, requests, name):
    """While the scout is behind its pace: a scouting question per queued backlog task not scouted yet (no attempt yet), so
    the quota that would be lost at the reset prepares the coding agents' work."""
    # a send that failed asked nothing: that task is scouted again next round
    asked = {r.get('backlog') for r in requests if r['scout'] == name and r.get('scouting') and r.get('status') != 'failed'}
    return [{'kind': 'scouting', 'ref': t['id'], 'project': config.project_of_repo(cfg, t['repo']),
             'task': SCOUTING.format(repo=t['repo'], title=t['title'][:300])}
            for t in backlog.load(cfg)['tasks']
            if t['status'] == 'queued' and not t.get('attempts') and not backlog.is_question(t) and t['id'] not in asked]


def questions(cfg):
    """The backlog's research questions for a scout: queued tasks whose title starts with `scout:` or `research:`."""
    return [{'kind': 'research', 'ref': t['id'], 'project': config.project_of_repo(cfg, t['repo']),
             'task': t['title'].split(':', 1)[1].strip()}
            for t in backlog.load(cfg)['tasks'] if backlog.is_question(t) and t['status'] == 'queued']


def seconds(cfg, requests, name):
    """Autopilot tasks that failed twice or more and have no second opinion for their latest failure yet."""
    asked = {(r.get('second_opinion') or {}).get('key') for r in requests if r['scout'] == name}
    out = []
    for t in backlog.load(cfg)['tasks']:
        fails = [a for a in t.get('attempts') or [] if not a.get('ok')]
        key = f'{t["id"]}#{len(fails)}'
        if t['status'] not in ('queued', 'needs-person') or len(fails) < 2 or key in asked:
            continue
        why = f'{fails[-1].get("why") or "no failure text recorded"}\n{fails[-1].get("tail") or ""}'.strip()[-1500:]
        out.append({'kind': 'second', 'ref': t['id'], 'key': key, 'project': config.project_of_repo(cfg, t['repo']),
                    'task': SECOND.format(repo=t['repo'], title=t['title'][:200], n=len(fails), why=why)})
    return out


def ordered(cfg, items):
    return sorted(items, key=lambda x: (config.project_priority(cfg, x.get('project')), RANK[x['kind']]))


def plan(cfg, sc, requests, acc, now):
    """{on, why, items, later, week}: what the round would send now, in order (reviews are listed by the round, which asks
    GitHub), and the week's budget."""
    on, why = active(sc, acc, now)
    if not on:
        states = autopilot.read_state(cfg)
        if autopilot.main_in_panic(cfg, states) and acc.get('verdict') != 'spent' and not acc.get('panic'):
            on, why = True, 'panic override'
    b = week(sc, acc, now) if acc else grokweek.budget(None, now, 1, 95, 6)
    if not on:
        return {'on': False, 'why': why, 'items': [], 'later': 0, 'week': b}
    items = ordered(cfg, extra_watches(sc, requests, acc, now) + questions(cfg) + seconds(cfg, requests, sc['name']))
    if behind(acc):   # the allowance would be lost at the reset: useful scouting of the backlog fills it
        items += ordered(cfg, scouting(cfg, requests, sc['name']))
    target = pacing.parse_time(acc['target']).timestamp()
    later = len(extra_watches(sc, requests, acc, now, target)) - sum(1 for i in items if i['kind'] not in ('research', 'second',
                                                                                                         'scouting'))
    return {'on': True, 'why': why, 'items': items, 'later': max(0, later), 'week': b}


def count(p, reviews=0):
    """How many runs the plan has now: free now, free later, and reviews."""
    return len(p['items']) + p.get('later', 0) + reviews


def line(p, reviews=0):
    """The scout account's Do line while the week's plan sends."""
    if not p['on']:
        return ''
    b = p.get('week') or {}
    if b.get('mode') == 'burst':
        return f'Scout: burst before the reset: {b.get("left", 0)} run(s) of allowance left, sending all the useful work that fits'
    ready, later = len(p['items']) + reviews, p.get('later', 0)
    return (f'Scout: {b.get("now", 0)} run(s) can go now on its even weekly line; {ready} useful item(s) ready now, {later} '
            f'later ({b.get("left", 0)} runs left this week)')


def room(sc, data, acc, sent, now=None):
    """Runs the round may still send: the caps (higher in the burst), the week's line, the points left before stop_at."""
    now = now or time.time()
    s = settings(sc)
    hot = burst(sc, acc, now)
    waiting = sum(1 for r in data['requests'] if r['scout'] == sc['name'] and r['status'] == 'waiting')
    per_tick = int(s['burst_per_tick'] if hot else sc['per_tick'])
    max_open = int(s['burst_max_open'] if hot else sc['max_open'])
    return max(0, min(per_tick - sent, max_open - waiting, week(sc, acc, now)['now'] - sent, points_left(sc, acc)))


def round_(cfg, sc, data, acc, url, key, sent, gh_fn=scouts.gh, post_fn=scouts.post, now=None):
    """Under the scouts lock: sends the extra watches, the backlog's questions and second opinions that fit; returns
    (events, how many sent)."""
    now = now or time.time()
    p = plan(cfg, sc, data['requests'], acc, now)
    events, n = [], 0
    for item in p['items'][:room(sc, data, acc, sent, now)]:
        if item['kind'] in ('research', 'second', 'scouting'):
            r = scouts.new_request(data, sc, item['task'], 'ask', skill='research', now=now)
            r['backlog'] = item['ref']
            r['scouting'] = item['kind'] == 'scouting'
            if item['kind'] == 'second':
                r['second_opinion'] = {'key': item['key'], 'task': item['ref']}
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
        events.append(f'scout week: {r["id"]} {item["kind"]} {r["status"]}: {r.get("url") or r.get("why")}')
    return events, n


def mark_asked(cfg, task_id, name, r):
    """A backlog question that went to a scout leaves the queue: a person reads the answer on its issue."""
    with backlog.locked(cfg) as data:
        t = backlog.find(data, task_id)
        t['status'] = 'review'
        t['notes'] = [*t.get('notes', []), f'asked scout {name} ({r["id"]}): {r.get("url")}']


def second_answers(data):
    """Under the scouts lock: [(request id, task id, note)] of answered second opinions not fed yet (nothing is changed)."""
    out = []
    for r in data['requests']:
        so = r.get('second_opinion')
        if so and r.get('status') == 'answered' and not so.get('fed'):
            out.append((r['id'], so['task'], f'second opinion ({r["id"]}, {r.get("url")}): {(r.get("answer") or "")[:1200]}'))
    return out


def feed_seconds(cfg, answers):
    """After the scouts lock is released (the backlog lock is never taken inside it): each answer joins its task's notes
    (the next attempt's brief carries them), and only then is it marked fed. Returns event lines."""
    events, done = [], []
    for rid, task_id, note in answers:
        with backlog.locked(cfg) as b:
            t = next((x for x in b['tasks'] if x['id'] == task_id), None)
            if t is not None:
                t['notes'] = [*t.get('notes', []), note]
        done.append(rid)
        events.append(f'{rid} second opinion on {task_id}: added to its notes')
    if done:
        with scouts.locked(cfg) as data:
            for r in data['requests']:
                if r['id'] in done:
                    r['second_opinion']['fed'] = True
    return events


def waiting_seconds(cfg, now=None):
    """{task id: why} of tasks whose second opinion is still on its way (held by the autopilot up to second_wait_minutes)."""
    now = now or time.time()
    out = {}
    if not cfg.get('scouts'):
        return out
    for r in scouts.load(cfg)['requests']:
        so = r.get('second_opinion')
        if not so or r.get('status') != 'waiting':
            continue
        sc = (cfg['scouts'].get(r['scout']) or {})
        wait = settings(sc if isinstance(sc, dict) else {})['second_wait_minutes'] * 60
        if now - (r.get('sent') or r['added']) < wait:
            out[so['task']] = f'waits up to {wait / 60:g} min for scout {r["scout"]}\'s second opinion ({r["id"]}) before its retry'
    return out


def open_prs(repo, gh_fn=scouts.gh):
    """[(number, url, head commit)] of the repository's open pull requests, or [] when GitHub cannot be asked."""
    rc, out, _ = gh_fn(['pr', 'list', '--repo', repo, '--state', 'open', '--json', 'number,url,headRefOid', '--limit', '30'])
    try:
        return [(int(p['number']), p['url'], p.get('headRefOid') or '') for p in json.loads(out or '[]')] if rc == 0 else []
    except (ValueError, KeyError, TypeError):
        return []


def review_repos(cfg, name):
    """Where the scout reviews every open pull request: spend_rest.review_repos, else the projects' repo and the review
    trial's repositories."""
    from . import trial
    sc = scouts.settings(cfg, name)
    given = settings(sc)['review_repos']
    if given is not None:
        return list(given)
    repos = [p['repo'] for p in sorted((cfg.get('projects') or {}).values(), key=lambda p: (p or {}).get('priority', 99))
             if isinstance(p, dict) and p.get('repo')]
    return list(dict.fromkeys(repos + list((trial.settings(cfg, name) or {}).get('repos') or [])))


def reviewed(requests, name, repo, number, head):
    """The scout already reviewed this head commit (a request from before heads were kept counts for any head: no repeat).
    Only a new commit asks again; comments, labels and CI runs do not."""
    return any(r['scout'] == name and r.get('kind') == 'review' and r.get('repo') == repo and r.get('issue') == number
               and r['status'] in ('waiting', 'answered') and r.get('head', head) == head for r in requests)


def keep_head(cfg, rid, head):
    with scouts.locked(cfg) as data:
        for r in data['requests']:
            if r['id'] == rid:
                r['head'] = head


def reviews(cfg, name, acc, left, gh_fn=scouts.gh, post_fn=scouts.post, env=None, now=None):
    """Outside the scouts lock: the open pull requests of review_repos not reviewed since they last changed, at most `left`.
    Returns events."""
    from . import trial  # trial builds on scouts
    sc = scouts.settings(cfg, name)
    if left <= 0 or not active(sc, acc, now or time.time())[0]:
        return []
    listed = name in (cfg.get('review') or {}).get('reviewers', [])
    if not listed and trial.may_review(cfg, name):
        return []
    events, requests = [], scouts.load(cfg)['requests']
    for repo in review_repos(cfg, name):
        for number, url, head in open_prs(repo, gh_fn):
            if left <= 0 or reviewed(requests, name, repo, number, head):
                continue
            try:
                r = trial.ask_review(cfg, name, repo, number, url, gh_fn, post_fn, env, {acc['name']: acc}, listed=listed)
            except scouts.ScoutError as e:
                events.append(f'scout week: review of {repo}#{number} not sent: {e}')
                continue
            left -= 1
            keep_head(cfg, r['id'], head)
            events.append(f'scout week: review {r["id"]} {r["status"]}: {url}')
    if left > 0 and behind(acc):   # still room that the reset would take: audit what merged lately
        events += audits(cfg, name, acc, left, gh_fn, post_fn, env, now)
    return events


MERGED_LIMIT = 500   # gh pr list has no --paginate: a bound well above a busy repository's merges in AUDIT_DAYS
AUDIT_RETRY_HOURS = 6   # a failed audit send waits this long before its record is sent again
AUDIT_TRIES = 3   # then it is left failed: the round's line says so and nothing piles up


def merged_prs(repo, gh_fn=scouts.gh, days=AUDIT_DAYS, now=None):
    """[(number, url)] of the pull requests merged in the last `days`, newest first, or [] when GitHub cannot be asked."""
    since = time.strftime('%Y-%m-%d', time.gmtime((now or time.time()) - days * 86400))
    rc, out, _ = gh_fn(['pr', 'list', '--repo', repo, '--state', 'merged', '--search', f'merged:>={since}', '--json', 'number,url',
                        '--limit', str(MERGED_LIMIT)])
    try:
        return [(int(p['number']), p['url']) for p in json.loads(out or '[]')] if rc == 0 else []
    except (ValueError, KeyError, TypeError):
        return []


def audits(cfg, name, acc, left, gh_fn=scouts.gh, post_fn=scouts.post, env=None, now=None):
    """An audit of each pull request merged lately that the scout has not audited, at most `left`. The request is made
    under the scouts lock and sent outside it. A failed send keeps its record: it is sent again after AUDIT_RETRY_HOURS,
    at most AUDIT_TRIES times, never as a new record each round."""
    now = now or time.time()
    sc = scouts.settings(cfg, name)
    try:
        url_, key = scouts.secrets(sc, env)
    except scouts.ScoutError as e:
        return [f'scout week: audits not sent: {e}']
    events = []
    for repo in review_repos(cfg, name):
        prior = {r.get('issue'): r for r in scouts.load(cfg)['requests']
                 if r['scout'] == name and r.get('audit') and r.get('repo') == repo}
        for number, url in merged_prs(repo, gh_fn, now=now):
            old = prior.get(number)
            if left <= 0 or (old and not retry_audit(old, now)):
                continue
            r = reserve_audit(cfg, sc, old, repo, number, url, now)
            try:
                scouts.send(sc, r, url_, key, gh_fn, post_fn, now)
            except scouts.ScoutError as e:
                r.update(status='failed', why=str(e))
            with scouts.locked(cfg) as data:
                scouts.find(data, r['id']).update(r)
            left -= r['status'] == 'waiting'
            events.append(f'scout week: audit {r["id"]} {r["status"]}: {url}')
    return events


def retry_audit(r, now):
    """A failed audit is sent again after AUDIT_RETRY_HOURS, at most AUDIT_TRIES sends in all; any other is done."""
    # a record left `queued` by a send that never happened (a crash between reserve and send) is retried the same way
    return (r.get('status') in ('failed', 'queued') and r.get('tries', 1) < AUDIT_TRIES
            and now - float(r.get('sent') or r.get('added') or 0) >= AUDIT_RETRY_HOURS * 3600)


def reserve_audit(cfg, sc, old, repo, number, url, now):
    """Under the scouts lock: the audit's record (the failed one again, or a new one); a copy to send outside the lock."""
    with scouts.locked(cfg) as data:
        if old:
            r = scouts.find(data, old['id'])
            r.update(status='queued', why=None, tries=r.get('tries', 1) + 1)
        else:
            r = scouts.new_request(data, sc, AUDIT.format(url=url, repo=repo, number=number, days=AUDIT_DAYS), 'review',
                                   repo=repo, skill='review', now=now)
            r.update(issue=number, url=url, extra=True, audit=True, tries=1)   # answered on the pull request, as a review
        return dict(r)
