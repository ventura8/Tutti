"""Self-improvement: crashes and failures that Tutti sees (Sentry issues through a scout's triage, the local Insights) become
backlog tasks that the autopilot fixes, a pull request, AI reviews, and, where the owner gave Tutti that authority, a merge
and a batched patch release. `self_improve.enabled` is on by default and switched on the dashboard (or
`tutti selfimprove on|off`); every project decides how far it goes:

    projects:
      Tutti: {repo: octo/tutti, self_improve: {tasks: true, autopilot_repo: tutti, merge: true, release: true,
              release_every_hours: 24, release_command: bash scripts/release-patch.sh}}
      My App: {repo: octo/my-app, self_improve: {tasks: true, autopilot_repo: app, base: develop}}   # stops at a ready PR
      Other: {sentry: {org: me, project: other}}                                                    # issues only

The gates (pure, tested): a task only for a project with `tasks` and an autopilot repo; a pull request only to a base that is
not protected (main, master: ship.protected) unless the project has `merge`; a merge only with `merge`, all checks green
(CI, Sonar), no review asking for changes, no open review thread, the AI reviews done, never with --admin; a release only
with `release`, merged fixes waiting and the last release `release_every_hours` ago. Dedupe: one task per Sentry issue or
insight; at most `max_new_per_day` new tasks; the autopilot's no-progress guard parks what does not move."""
import json
import os
import subprocess
import sys
import time

from . import backlog, config, github, insights, remote, telemetry

FILE = 'self-improve.json'
SOURCES = ('sentry', 'insight', 'report')
DEFAULTS = {'enabled': True, 'auto_accept': True, 'every_hours': 6, 'max_new_per_day': 3, 'min_count': 3, 'home': ''}
POLICY = {'tasks': False, 'autopilot_repo': '', 'merge': False, 'release': False, 'release_every_hours': 24, 'release_command': '',
          'base': ''}
DAY = 86400


def settings(cfg):
    return {**DEFAULTS, **(cfg.get('self_improve') or {})}


def path_of(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


def read_state(cfg):
    return telemetry.read_json(path_of(cfg), {})


def write_state(cfg, data):
    telemetry.write_json(path_of(cfg), data)


def enabled(cfg):
    """The dashboard's switch when set, else self_improve.enabled (on by default)."""
    on = read_state(cfg).get('enabled')
    return settings(cfg)['enabled'] if on is None else bool(on)


def auto_accept(cfg):
    """A Sentry or scout triage task joins the queue by itself (no `tutti backlog accept`): self-improvement is on and
    self_improve.auto_accept (on by default) is not turned off."""
    return enabled(cfg) and bool(settings(cfg)['auto_accept'])


def accept_waiting(cfg):
    """Queues every self-improvement candidate (one suggested while self-improvement or auto_accept was off) of a project that
    may take tasks, so fill_room and the autopilot pick it up. No task is added: the day's cap and the dedupe held when it was
    suggested. Returns event lines."""
    if not auto_accept(cfg):
        return []
    events = []
    with backlog.locked(cfg) as data:
        for t in [t for t in data['tasks'] if t['status'] == 'candidate' and t.get('source') in SOURCES]:
            if may_add(policy(cfg, t.get('project')), cfg)[0]:
                t['status'] = 'queued'
                events.append(f'self-improvement: {t["id"]} accepted by itself into the {t["repo"]} queue ({t["title"][:60]})')
    return events


def set_enabled(cfg, on):
    data = read_state(cfg)
    data['enabled'] = bool(on)
    write_state(cfg, data)
    telemetry.record('feature', 'selfimprove.switch', on=bool(on))
    return data


def projects(cfg):
    ps = cfg.get('projects') or {}
    return ps if isinstance(ps, dict) else {}


def policy(cfg, project):
    """The project's policy ({**POLICY, **projects.<name>.self_improve, name, repo}) or None for an unknown project."""
    p = projects(cfg).get(project)
    if not isinstance(p, dict):
        return None
    return {**POLICY, **(p.get('self_improve') or {}), 'name': project, 'repo': p.get('repo') or ''}


def policy_of_repo(cfg, autopilot_repo):
    for name in projects(cfg):
        pol = policy(cfg, name)
        if pol and pol['autopilot_repo'] == autopilot_repo:
            return pol
    return None


# ---- the gates (pure)

def may_add(pol, cfg):
    """(ok, why): a project takes tasks only when it opts in and names a repo the autopilot knows."""
    if not pol or not pol['tasks']:
        return False, 'the project takes issues only (self_improve.tasks is off)'
    if pol['autopilot_repo'] not in ((cfg.get('autopilot') or {}).get('repos') or {}):
        return False, f'self_improve.autopilot_repo "{pol["autopilot_repo"]}" is not under autopilot.repos'
    return True, ''


def pr_plan(cfg, task, repo):
    """(mode 'draft'|'ready'|None, base or None, why) of a passed task's pull request. A self-improvement task gets a ready
    pull request; its base must not be a protected branch unless the project may merge (then the default branch is fine)."""
    if task.get('source') not in SOURCES or not task.get('pr_mode'):
        return ('draft', repo.get('base') or None, '') if repo.get('pr') == 'draft' else (None, None, '')
    pol = policy_of_repo(cfg, task['repo']) or {**POLICY}
    base = pol['base'] or repo.get('base') or ''
    protected = set((cfg.get('ship') or {}).get('protected') or ['main', 'master'])
    if pol['merge']:
        return task['pr_mode'], base or None, ''
    if not base or base in protected:
        return None, None, (' (no pull request: this project may not merge, so it needs self_improve.base, a work branch that '
                            'is not ' + '/'.join(sorted(protected)) + ')')
    return task['pr_mode'], base, ''


def checks_green(checks):
    """Every check finished and passed (or was skipped/neutral), and at least one passed."""
    states = [(c.get('conclusion') or c.get('state') or '').upper() for c in checks or []]
    return 'SUCCESS' in states and all(s in ('SUCCESS', 'NEUTRAL', 'SKIPPED') for s in states)


def may_merge(pol, pr, reviewed):
    """(ok, why) for merging a self-improvement pull request. `pr`: gh pr view's JSON plus `open_threads`."""
    if not pol or not pol['merge']:
        return False, 'this project stops at a pull request: the owner merges'
    if pr.get('state') != 'OPEN' or pr.get('isDraft'):
        return False, 'not an open, ready pull request'
    if not reviewed:
        return False, 'waiting for the AI reviews (tutti pr review)'
    if pr.get('reviewDecision') == 'CHANGES_REQUESTED':
        return False, 'a reviewer asked for changes: the fix goes back to the autopilot'
    if pr.get('reviewDecision') == 'REVIEW_REQUIRED':
        return False, 'the base branch requires an approving review'
    if pr.get('open_threads'):
        return False, f'{pr["open_threads"]} review thread(s) still open'
    if pr.get('mergeable') != 'MERGEABLE':
        return False, 'GitHub has not found it mergeable (a conflict, or not computed yet)'
    if not checks_green(pr.get('statusCheckRollup') or []):
        return False, 'checks (CI, Sonar) are not all green yet'
    return True, ''


def may_release(pol, last_release, merged_since, now):
    """(ok, why) for a batched patch release."""
    if not pol or not pol['release'] or not pol['release_command']:
        return False, 'this project is never released by Tutti: the owner tags it'
    if merged_since <= 0:
        return False, 'no merged fix since the last release'
    wait = float(pol['release_every_hours']) * 3600 - (now - float(last_release or 0))
    if wait > 0:
        return False, f'the next release may go in {wait / 3600:.1f} h (release_every_hours)'
    return True, ''


def room_today(tasks, now, limit):
    """How many more self-improvement tasks may be added in the last 24 hours."""
    recent = sum(1 for t in tasks if t.get('source') in SOURCES and now - float(t.get('added') or 0) < DAY)
    return max(0, int(limit) - recent)


# ---- the local analysis: insights into task candidates (pure)

def candidates(summary, min_count):
    """[{title, sig, why}] from the Insights summary: crashes, commands that fail often, frequent stalls, wasted quota."""
    out = []
    for c in summary['crashes']:
        out.append({'sig': f'crash:{c["where"]}', 'title': f'Fix the crash {c["where"]} (seen {c["count"]} time(s) in Tutti)',
                    'why': 'a crash recorded by telemetry'})
    for f in summary['most_failing']:
        if f['failed'] >= min_count and f['failed'] * 2 >= f['runs']:
            out.append({'sig': f'failing:{f["name"]}', 'why': 'a command that fails more often than not',
                        'title': f'Find and fix why `tutti {f["name"]}` fails ({f["failed"]} of {f["runs"]} runs)'})
    for s in summary['stalls']:
        if s['count'] >= min_count:
            out.append({'sig': f'stall:{s["cause"]}', 'why': 'repeated stalls',
                        'title': f'Find why `tutti wait {s["cause"]}` jobs stall ({s["count"]} stalls) and make them end or recover'})
    for w in summary['wasted']:
        if w['runs'] >= min_count:
            out.append({'sig': f'wasted:{w["who"]}', 'why': 'quota spent on failed runs',
                        'title': f'Cut failed runs on {w["who"]} ({w["runs"]} runs, {w["minutes"]} min wasted): better briefs or routing'})
    return out


def add_candidates(cfg, items, source, now, accept=None):
    """Adds items to the home project's repo, deduped by signature, within the day's room. Returns how many were added."""
    s = settings(cfg)
    pol = policy(cfg, s['home'])
    ok, _why = may_add(pol, cfg)
    if not ok:
        return 0
    added = 0
    with backlog.locked(cfg) as data:
        room = room_today(data['tasks'], now, s['max_new_per_day'])
        for it in items:
            if added >= room:
                break
            if any(t.get('sig') == it['sig'] and t['status'] not in ('done', 'dropped') for t in data['tasks']):
                continue
            notes = it.get('notes') or [f'Self-improvement ({it["why"]}). First add a regression test that reproduces it and fails; '
                                        'then fix it.', 'The local events are in <state>/telemetry/events.jsonl; `tutti telemetry '
                                        'report` summarises them.']
            t = backlog.new_task(pol['autopilot_repo'], it['title'], source=source, notes=notes)
            t.update(project=pol['name'], sig=it['sig'], pr_mode='ready', added=now,
                     status='queued' if (auto_accept(cfg) if accept is None else accept) else 'candidate')
            _t, new = backlog.add(data, t)
            added += int(new)
    return added


def analyse(cfg, now):
    """The local analysis, every `every_hours`: the Insights of the last 7 days into tasks for the home project."""
    s, st = settings(cfg), read_state(cfg)
    if now - float(st.get('last_analysis') or 0) < float(s['every_hours']) * 3600:
        return []
    folder = telemetry.folder_of(config.state_dir(cfg))
    summary = insights.summary(telemetry.read_events(folder), 7)
    added = add_candidates(cfg, candidates(summary, s['min_count']), 'insight', now)
    st = read_state(cfg)
    st['last_analysis'] = now
    write_state(cfg, st)
    telemetry.record('feature', 'selfimprove.analyse', added=added)
    return [f'self-improvement: {added} task(s) from the local insights'] if added else []


def report_candidates(issues):
    """[{title, sig, why}] from the open `user-report` issues (`tutti report`, docs/REPORT.md). The issue's text was written by
    a stranger: only its number and title go into the task, and the worker is told to read it as data, never instructions."""
    out = []
    for i in issues or []:
        if isinstance(i, dict) and isinstance(i.get('number'), int):
            title = ' '.join(str(i.get('title') or '').split())[:120]
            out.append({'sig': f'report:{i["number"]}', 'why': f'a user\'s report, issue #{i["number"]}',
                        'title': f'Look into user report #{i["number"]}: {title}'})
    return out


def user_reports(cfg, now, run=github.run):
    """The home project's open `user-report` issues into tasks, every `every_hours`, like the Sentry triage's."""
    s, st = settings(cfg), read_state(cfg)
    pol = policy(cfg, s['home'])
    if not pol or not pol['repo'] or now - float(st.get('last_reports') or 0) < float(s['every_hours']) * 3600:
        return []
    issues = gh_json(['issue', 'list', '--repo', pol['repo'], '--label', 'user-report', '--state', 'open',
                      '--json', 'number,title', '--limit', '20'], run)
    added = add_candidates(cfg, report_candidates(issues), 'report', now) if issues else 0
    write_state(cfg, {**read_state(cfg), 'last_reports': now})
    return [f'self-improvement: {added} task(s) from user reports'] if added else []


# ---- GitHub (I/O at the edge)

def gh_json(args, run=None):
    p = (run or github.run)(['gh', *args], capture_output=True, text=True)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout or 'null')
    except ValueError:
        return None


THREADS_Q = ('query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){reviewThreads(first:100)'
             '{nodes{isResolved}}}}}')


def pr_state(repo, number, run=github.run):
    pr = gh_json(['pr', 'view', str(number), '--repo', repo, '--json',
                  'state,isDraft,reviewDecision,mergeable,statusCheckRollup,baseRefName,headRefOid'], run)
    if pr is None:
        return None
    owner, name = repo.split('/', 1)
    t = gh_json(['api', 'graphql', '-f', f'query={THREADS_Q}', '-F', f'o={owner}', '-F', f'r={name}', '-F', f'n={number}'], run)
    try:
        nodes = t['data']['repository']['pullRequest']['reviewThreads']['nodes']
        pr['open_threads'] = sum(1 for x in nodes if not x['isResolved'])
    except (TypeError, KeyError):
        pr['open_threads'] = 1   # unknown counts as open: never merge blind
    return pr


def pr_number(url):
    tail = str(url or '').rstrip('/').rsplit('/', 1)[-1]
    return int(tail) if tail.isdigit() else None


def review_files(cfg, repo, number):
    base = os.path.join(config.state_dir(cfg), f'self-improve-review-{repo.replace("/", "-")}-{number}')
    return base + '.log', base + '.rc'


def request_review(cfg, repo, number, spawn=subprocess.Popen):
    """`tutti pr review <n>` in the background: asks the AI reviewers and falls back to a cheap worker's review; its exit
    code lands in the .rc file once it has finished."""
    log, rc = review_files(cfg, repo, number)
    if os.path.exists(rc):
        os.remove(rc)
    argv = [sys.executable, remote.BIN, '--config', cfg['_path'], 'pr', 'review', str(number), '--repo', repo]
    script = 'rc=0; "$@" || rc=$?; echo $rc > "$RC_FILE"'
    with open(log, 'a', encoding='utf8') as f:
        spawn(['sh', '-c', script, 'review', *argv], stdout=f, stderr=f, stdin=subprocess.DEVNULL, start_new_session=True,
              env={**os.environ, 'RC_FILE': rc})


def review_finished(cfg, repo, number):
    """True once `tutti pr review` ended well (its .rc file says 0)."""
    try:
        with open(review_files(cfg, repo, number)[1], encoding='utf8') as f:
            return f.read().strip() == '0'
    except OSError:
        return False


def pushed_since_review(reviews, key, pr):
    """True when the open pull request's head is not the commit the reviews were asked on."""
    asked = reviews[key] if isinstance(reviews[key], dict) else {}
    return pr.get('state') == 'OPEN' and bool(pr.get('headRefOid')) and asked.get('sha') != pr['headRefOid']


def ask_reviews(cfg, t, pol, number, key, reviews, now, run, spawn):
    """Asks the AI reviews once, remembering the commit they see. Returns the event, or None when gh cannot be read."""
    pr = pr_state(pol['repo'], number, run)
    if pr is None:
        return None
    request_review(cfg, pol['repo'], number, spawn)
    reviews[key] = {'at': now, 'sha': pr.get('headRefOid') or ''}   # the commit the reviewers see
    return f'{t["id"]}: AI reviews asked on {key}'


def advance(cfg, t, st, now, run=github.run, spawn=subprocess.Popen):
    """One self-improvement task in review with a pull request: ask for reviews once, then merge when the gates allow."""
    pol = policy_of_repo(cfg, t['repo'])
    number = pr_number(t.get('pr'))
    if not pol or not pol['repo'] or not number:
        return None
    key = f'{pol["repo"]}#{number}'
    reviews = st.setdefault('reviews', {})
    if key not in reviews:
        return ask_reviews(cfg, t, pol, number, key, reviews, now, run, spawn)
    pr = pr_state(pol['repo'], number, run)
    if pr is None:
        return None
    if pushed_since_review(reviews, key, pr):
        reviews.pop(key, None)   # commits pushed after the review are never merged unreviewed: the next round asks again
        t['self_improve'] = 'new commits since the AI reviews: they are asked again'
        return None
    ok, why = may_merge(pol, pr, review_finished(cfg, pol['repo'], number))
    t['self_improve'] = why or 'merging'
    if pr.get('state') == 'MERGED':
        t['status'] = 'done'
        return None
    if not ok or not pr.get('headRefOid'):
        if pr.get('reviewDecision') == 'CHANGES_REQUESTED' and t['status'] == 'review':
            t['status'], t['notes'] = 'queued', [*t.get('notes', []), f'Changes requested on {key}: read the review and fix them.']
            reviews.pop(key, None)   # the fix pushed onto this pull request is reviewed again before any merge
        return None
    if not pr.get('headRefOid'):
        t['self_improve'] = 'GitHub did not say which commit the pull request is at: not merged; the next round looks again'
        return None
    return merge_now(t, st, pol, number, key, pr['headRefOid'], now, run)


def merge_now(t, st, pol, number, key, head, now, run):
    """The squash merge of the reviewed, green head; the event, or None when GitHub refused it."""
    # --match-head-commit: a commit pushed after the gates were read is never merged with them
    done = run(['gh', 'pr', 'merge', str(number), '--repo', pol['repo'], '--squash', '--delete-branch', '--match-head-commit',
                str(head)], capture_output=True, text=True)
    if done.returncode != 0:
        t['self_improve'] = f'merge refused: {(done.stderr or done.stdout).strip()[:160]}'
        return None
    t['status'] = 'done'
    st.setdefault('merged', []).append({'project': pol['name'], 'pr': key, 'at': now, 'task': t['id']})
    telemetry.record('feature', 'selfimprove.merge', source=t.get('source'))
    return f'{t["id"]}: merged {key}'


def release_files(cfg, name):
    base = os.path.join(config.state_dir(cfg), f'self-improve-release-{name.replace(" ", "-")}')
    return base + '.log', base + '.rc'


def release_ended(cfg, st, name):
    """A started release that has finished: counted as released only when its command exited 0. Returns an event or None."""
    going = (st.get('releasing') or {}).get(name)
    if not going:
        return None
    try:
        with open(release_files(cfg, name)[1], encoding='utf8') as f:
            rc = f.read().strip()
    except OSError:
        return None   # still running
    del st['releasing'][name]
    if rc == '0':
        st.setdefault('released', {})[name] = going['at']
        return f'self-improvement: release of {name} done'
    st.setdefault('release_failed', {})[name] = going['at']
    log = release_files(cfg, name)[0]
    return f'self-improvement: the release of {name} failed (exit {rc}): read {log}; its fixes wait for the next one'


def start_release(cfg, pol, name, spawn):
    path = ((cfg.get('autopilot') or {}).get('repos') or {}).get(pol['autopilot_repo'], {}).get('path')
    log, rc = release_files(cfg, name)
    if os.path.exists(rc):
        os.remove(rc)
    with open(log, 'a', encoding='utf8') as f:
        spawn(['sh', '-c', 'rc=0; bash -c "$1" || rc=$?; echo $rc > "$RC_FILE"', 'release', pol['release_command']],
              cwd=config.expand(path) if path else None, stdout=f, stderr=f, stdin=subprocess.DEVNULL, start_new_session=True,
              env={**os.environ, 'RC_FILE': rc})
    return log


def release(cfg, st, now, spawn=subprocess.Popen):
    """A batched patch release of every project allowed to release, when its fixes wait and its interval has passed. A
    release counts only once its command exited 0; a failed one leaves its fixes for the next (and spaces the retry)."""
    events = []
    for name in projects(cfg):
        ended = release_ended(cfg, st, name)
        if ended:
            events.append(ended)
        if name in (st.get('releasing') or {}):
            continue
        pol = policy(cfg, name)
        last = (st.get('released') or {}).get(name)
        waiting = [m for m in st.get('merged') or [] if m['project'] == name and m['at'] > float(last or 0)]
        tried = max(float(last or 0), float((st.get('release_failed') or {}).get(name) or 0))
        ok, _why = may_release(pol, tried, len(waiting), now)
        if not ok:
            continue
        log = start_release(cfg, pol, name, spawn)
        st.setdefault('releasing', {})[name] = {'at': now}
        telemetry.record('feature', 'selfimprove.release', fixes=len(waiting))
        events.append(f'self-improvement: release of {name} started with {len(waiting)} fix(es); log {log}')
    return events


def tick(cfg, now=None, run=github.run, spawn=subprocess.Popen):
    """One round (the dashboard's loop): the local analysis when due, reviews and merges, batched releases."""
    now = now or time.time()
    if not enabled(cfg):
        return []
    events = analyse(cfg, now) + user_reports(cfg, now, run) + accept_waiting(cfg)
    st = read_state(cfg)
    with backlog.locked(cfg) as data:
        for t in [t for t in data['tasks'] if t.get('source') in SOURCES and t['status'] == 'review' and t.get('pr')]:
            line = advance(cfg, t, st, now, run, spawn)
            if line:
                events.append(line)
    events += release(cfg, st, now, spawn)
    kept = ('reviews', 'merged', 'released', 'releasing', 'release_failed')
    write_state(cfg, {**read_state(cfg), **{k: st[k] for k in kept if k in st}})
    return events


def lane(cfg):
    """The dashboard's Self-improvement lane: issue or insight -> task -> pull request -> merge -> release."""
    st = read_state(cfg)
    released = st.get('released') or {}
    merged = {m['task']: m for m in st.get('merged') or []}
    items = []
    for t in backlog.load(cfg)['tasks']:
        if t.get('source') not in SOURCES:
            continue
        m = merged.get(t['id'])
        rel = m and released.get(m['project'], 0) >= m['at']
        items.append({'id': t['id'], 'source': t['source'], 'ref': t.get('sentry') or t.get('sig') or '', 'title': t['title'][:160],
                      'project': t.get('project'), 'status': t['status'], 'pr': t.get('pr'), 'gate': t.get('self_improve', ''),
                      'merged': bool(m), 'released': bool(rel)})
    return {'enabled': enabled(cfg), 'items': items[-30:][::-1], 'last_analysis': st.get('last_analysis')}


def errors(cfg):
    errs = []
    s = cfg.get('self_improve') or {}
    if not isinstance(s, dict):
        return [f'self_improve is {s!r}: a mapping such as {{enabled: true, home: Tutti}}']
    for k in ('enabled', 'auto_accept'):
        if k in s and not isinstance(s[k], bool):
            errs.append(f'self_improve.{k} is {s[k]!r}: true or false')
    for k in ('every_hours', 'max_new_per_day', 'min_count'):
        v = s.get(k, DEFAULTS[k])
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            errs.append(f'self_improve.{k} is {v!r}: a number, 0 or more')
    if s.get('home') and s['home'] not in projects(cfg):
        errs.append(f'self_improve.home names "{s["home"]}", which is not under projects: (the project Tutti\'s own fixes go to)')
    for name, p in projects(cfg).items():
        errs += project_errors(cfg, name, p)
    return errs


def project_errors(cfg, name, p):
    si = p.get('self_improve') if isinstance(p, dict) else None
    if si is None:
        return []
    key = f'projects.{name}.self_improve'
    if not isinstance(si, dict):
        return [f'{key} is {si!r}: a mapping such as {{tasks: true, autopilot_repo: app}}']
    errs = []
    bad = sorted(set(si) - set(POLICY))
    if bad:
        errs.append(f'{key} has {", ".join(bad)}: use {", ".join(POLICY)}')
    if si.get('tasks') and si.get('autopilot_repo') not in ((cfg.get('autopilot') or {}).get('repos') or {}):
        errs.append(f'{key}.autopilot_repo must name a repo under autopilot.repos when tasks is on')
    if (si.get('merge') or si.get('release')) and not p.get('repo'):
        errs.append(f'{key}: merge and release need projects.{name}.repo (owner/name)')
    return errs
