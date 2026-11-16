"""Welcome back: when a user returns after being away, Tutti tells them, in plain words, what happened since they left.
On for every user by default (`welcome.enabled`; each user may turn theirs off), shown once per return as the dashboard's
banner and at any time with "What did I miss?" (GET /api/welcome), `tutti welcome`, and the MCP tool `welcome_summary`.

Back is noticed from the signals Tutti already has: the owner's effective mode (their first prompt, the end of a scheduled
away time or a vacation: tutti/owner.py), their main machine or laptop present again (tutti/presence.py), and a user's first
dashboard visit after a gap of `away_hours` (default: pacing.away_after_hours, 3). One summary per return.

The summary covers the time since they left, newest first, grouped by project priority:
  1. shipped: releases, merged pull requests, finished tasks;  2. running now;  3. failed or parked, and why;
  4. usage: where each account will end by its reset;  5. needs you: only what only they can do (a decision on a parked
  task or a larger parameter change, a sign-in, a sudo line, a tap on a phone, a usage reading), each with its exact step
  and a link to the place that does it.
A local model writes the opening paragraph from those facts (tutti/aiassist.py's ask_local, when AI assist lets it); the
template writes it otherwise. The needs-you list is always built from the facts, never by a model.

Each item has a scope: `admin` (the owner's projects, accounts and setup), `household` (shared machines' work) or a user's
own id. A viewer sees their own and the household's; an admin sees everything."""
import os
import re
import threading
import time
from datetime import datetime, timezone

from . import backlog, config, telemetry

FILE = 'welcome.json'
API = '/api/welcome'
DEFAULTS = {'enabled': True, 'away_hours': None, 'ai': True}
OWNER = 'owner'   # the local admin's key (sign-in off, or Tutti's own signals about its owner)
KEEP = 10


def settings(cfg):
    s = cfg.get('welcome') or {}
    out = {**DEFAULTS, **(s if isinstance(s, dict) else {})}
    if out['away_hours'] is None:
        out['away_hours'] = float((cfg.get('pacing') or {}).get('away_after_hours') or 3)
    return out


def path_of(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


LOCK = threading.RLock()   # the dashboard's loop, its request threads and the rewrite thread share welcome.json


def read(cfg):
    return telemetry.read_json(path_of(cfg), {'users': {}})


def write(cfg, data):
    telemetry.write_json(path_of(cfg), data)


def user_of(data, key):
    return data.setdefault('users', {}).setdefault(key, {})


def enabled_for(cfg, data, key):
    v = (data.get('users') or {}).get(key, {}).get('enabled')
    return bool(settings(cfg)['enabled']) if v is None else bool(v)


# ---- away -> back (pure on the state data)

def observe(cfg, data, key, here, signal, now):
    """One signal about user `key`: here (True) or away (False). Returns the period {since, signal} when this is a return
    after at least away_hours, else None. A return yields one summary: the away mark is cleared."""
    u = user_of(data, key)
    if not here:
        u.setdefault('away_since', now)
        return None
    since = u.pop('away_since', None)
    u['last_here'] = now
    if since is None or now - since < settings(cfg)['away_hours'] * 3600:
        return None
    return {'since': since, 'signal': signal}


def visit(cfg, data, key, now):
    """A dashboard visit: a return when the last sign of this user is away_hours old or more."""
    u = user_of(data, key)
    last = max(float(u.get('last_here') or 0), float(u.get('last_visit') or 0))
    u['last_visit'] = now
    if 'away_since' in u:
        return observe(cfg, data, key, True, 'dashboard', now)
    if last and now - last >= settings(cfg)['away_hours'] * 3600:
        u['last_here'] = now
        return {'since': last, 'signal': 'dashboard'}
    u['last_here'] = now
    return None


# ---- the facts

def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='minutes')


def when(at):
    from . import tz
    return tz.show(at)


def at_text(ts):
    return when(datetime.fromtimestamp(ts, timezone.utc))


def parse_reset(text):
    """A reset time, or None when it is missing or unreadable (`Z` suffix included)."""
    try:
        at = datetime.fromisoformat(str(text).replace('Z', '+00:00')) if text else None
    except ValueError:
        return None
    return at if at is None or at.tzinfo else at.replace(tzinfo=timezone.utc)


def scope_of(t):
    if t.get('user'):
        return f'user:{t["user"]}'
    return 'household' if t.get('household') else 'admin'


def project_of(cfg, t):
    return t.get('project') or config.project_of_repo(cfg, t.get('repo')) or t.get('repo') or 'other'


def last_at(t):
    return max([float(a.get('at') or 0) for a in t.get('attempts') or []] + [float(t.get('added') or 0)])


def task_facts(cfg, tasks, since):
    """(shipped, running, failed, needs) from the backlog."""
    shipped, running, failed, needs = [], [], [], []
    for t in tasks:
        at, base = last_at(t), {'project': project_of(cfg, t), 'scope': scope_of(t)}
        if t['status'] == 'done' and at >= since:
            what = f'pull request {t["pr"]}' if t.get('pr') else 'done'
            shipped.append({**base, 'at': at, 'text': f'{t["title"][:120]} ({what})', 'link': t.get('pr')})
        elif t['status'] == 'running':
            running.append({**base, 'at': at, 'text': f'{t["title"][:120]} (attempt {len(t.get("attempts") or []) + 1}, '
                                                       f'on {t.get("step") or "a worker"})'})
        elif t['status'] == 'needs-person':
            why = t.get('parked') or 'it stopped making progress'
            failed.append({**base, 'at': at, 'text': f'{t["title"][:120]}: parked, {why}'})
            if t.get('question'):   # its question is listed from the needs-input queue (question_needs)
                continue
            needs.append({**base, 'kind': 'decision', 'title': f'Decide on task {t["id"]}: {t["title"][:80]}',
                          'step': f'Retry it, change its brief or drop it: tutti backlog retry {t["id"]} (or tutti backlog rm {t["id"]})',
                          'why': why, 'link': '/#autopilot'})
        elif t.get('attempts') and not t['attempts'][-1].get('ok') and at >= since:
            a = t['attempts'][-1]
            cls = f' ({a["class"]})' if a.get('class') else ''
            failed.append({**base, 'at': at, 'text': f'{t["title"][:120]}: attempt {len(t["attempts"])} failed{cls}, '
                                                      f'{(a.get("summary") or a.get("why") or "")[:160]}; Tutti retries it'})
    return shipped, running, failed, needs


def release_facts(cfg, since):
    from . import selfimprove
    st = selfimprove.read_state(cfg)
    out = [{'project': name, 'scope': 'admin', 'at': at, 'text': f'{name}: released'}
           for name, at in (st.get('released') or {}).items() if at >= since]
    out += [{'project': m['project'], 'scope': 'admin', 'at': m['at'], 'text': f'{m["project"]}: merged {m["pr"]}'}
            for m in st.get('merged') or [] if m['at'] >= since]
    return out


def usage_facts(accounts):
    out = []
    for a in accounts or []:
        end, used = a.get('projected_end'), a.get('used')
        if used is None:
            continue
        at_end = f', on track to end at {end:g} %' if isinstance(end, (int, float)) else ''
        reset = f' by its reset {when(parsed)}' if (parsed := parse_reset(a.get('reset'))) else ''
        out.append({'scope': 'admin', 'text': f'{a["name"]}: {used:g} % used{at_end}{reset} ({a.get("verdict") or "unknown"})'})
    return out


def reading_needs(accounts):
    return [{'scope': 'admin', 'kind': 'reading', 'project': '', 'title': f'Read {a["name"]}\'s usage',
             'step': a.get('action') or f'tutti usage log {a["name"]} <percent>', 'why': 'Tutti paces it blind without a reading',
             'link': '/#usage'}
            for a in accounts or [] if a.get('state') == 'no reading' and not a.get('auto')]


def setup_needs(cfg):
    from . import setupcmd
    report = setupcmd.read_report(cfg) or {}
    return [{'scope': 'admin', 'kind': s.get('kind'), 'project': '', 'title': s.get('title'),
             'step': s.get('command') or ('a tap on the phone' if s.get('kind') == 'phone' else ''), 'why': s.get('why') or '',
             'link': '/#setup'} for s in report.get('steps') or [] if isinstance(s, dict)]


def question_needs(cfg):
    """The open questions (tutti/needs.py), one line each with its default; answered in the Chat or the Me card."""
    from . import needs as needs_mod
    out = []
    for q in needs_mod.load(cfg)['questions']:
        if q['status'] != 'open':
            continue
        out.append({'scope': 'admin' if q['user'] == needs_mod.OWNER else f'user:{q["user"]}', 'kind': 'decision',
                    'project': q.get('project') or '', 'at': q['at'], 'title': f'{q["id"]}: {q["question"]}',
                    'why': q.get('why') or '', 'link': '/#chat',
                    'step': f'In the Chat: "{q["id"]} {q["default"]}" (default {q["default"]}; options {", ".join(q["options"])})'})
    return out


def tune_needs(cfg):
    from . import aiassist
    return [{'scope': 'admin', 'kind': 'decision', 'project': 'Tutti',
             'title': f'Approve or ignore {p["path"]}: {p["old"]} -> {p["value"]}',
             'step': f'Settings: set {p["path"]} to {p["value"]}, or leave it', 'why': p['why'], 'link': '/#settings'}
            for p in aiassist.live_proposals(aiassist.read_state(cfg))] if aiassist.switch(cfg, 'enabled') else []


ACTIONABLE = ('decision', 'sign-in', 'sudo', 'phone', 'reading')


def actionable(item):
    """Only what only the person can do, with a step to do it."""
    return item.get('kind') in ACTIONABLE and bool(item.get('step')) and bool(item.get('title'))


def facts(cfg, since, accounts=None, tasks=None):
    tasks = backlog.load(cfg)['tasks'] if tasks is None else tasks
    shipped, running, failed, needs = task_facts(cfg, tasks, since)
    needs += question_needs(cfg) + reading_needs(accounts) + setup_needs(cfg) + tune_needs(cfg)
    return {'shipped': release_facts(cfg, since) + shipped, 'running': running, 'failed': failed, 'usage': usage_facts(accounts),
            'needs': [n for n in needs if actionable(n)]}


def visible(item, role, sub):
    return role == 'admin' or item['scope'] == 'household' or item['scope'] == f'user:{sub}'


def for_viewer(cfg, f, role, sub):
    """The facts this viewer may see, newest first within each group, groups by project priority."""
    def order(items):
        items = [i for i in items if visible(i, role, sub)]
        items.sort(key=lambda i: -float(i.get('at') or 0))
        return sorted(items, key=lambda i: config.project_priority(cfg, i.get('project')))
    return {k: order(v) for k, v in f.items()}


# ---- the words

def template(f, since):
    n = {k: len(v) for k, v in f.items()}
    if not any(n.values()):
        return f'Welcome back. Nothing happened since {at_text(since)} that needs telling.'
    parts = [f'{n["shipped"]} shipped' if n['shipped'] else '', f'{n["running"]} running' if n['running'] else '',
             f'{n["failed"]} failed or parked' if n['failed'] else '', f'{n["needs"]} need you' if n['needs'] else '']
    return 'Welcome back. Since ' + at_text(since) + ': ' + ', '.join(p for p in parts if p) + '.'


def ai_intro(cfg, f, since, ask=None):
    """The opening paragraph by a local model, or None (AI off, or no model answered)."""
    from . import aiassist
    if not settings(cfg)['ai'] or not aiassist.switch(cfg, 'local'):
        return None
    lines = [f'{k}: {i["text"] if "text" in i else i["title"]}' for k in ('shipped', 'running', 'failed') for i in f[k][:12]]
    prompt = ('Write a short welcome-back note (at most 4 sentences, plain words, no lists, no markdown) for a person returning '
              f'to their AI coding helper Tutti after being away since {iso(since)}. Say what shipped, what is running and what '
              'failed, from these facts only:\n' + '\n'.join(lines or ['nothing happened']))
    answer, _who = (ask or (lambda c, p: aiassist.ask_local(c, p, 'welcome')))(cfg, prompt)
    return answer.strip()[:1200] if answer else None


def render(intro, f):
    """Plain text: the intro, the groups, then the numbered needs-you list with each exact step."""
    out = [intro]
    for key, title in (('shipped', 'Shipped'), ('running', 'Running now'), ('failed', 'Failed or parked'), ('usage', 'Usage')):
        if f[key]:
            out += ['', f'{title}:'] + [f'- {i["text"]}' for i in f[key][:15]]
    if f['needs']:
        out += ['', f'Needs you ({len(f["needs"])}): only you can do these.']
        out += [f'{n}. {i["title"]}: {i["why"]}\n   {i["step"]}' for n, i in enumerate(f['needs'], 1)]
    return '\n'.join(out)


def build(cfg, period, role, sub, accounts=None, tasks=None, ask=None, ai=True):
    """The summary; ai=False writes the template now (a page request: a model may take minutes; `rewrite` follows)."""
    f = for_viewer(cfg, facts(cfg, period['since'], accounts, tasks), role, sub)
    intro = (ai and ai_intro(cfg, f, period['since'], ask)) or template(f, period['since'])
    return {'by': 'model' if ai and not intro.startswith('Welcome back. ') else 'template', 'since': period['since'],
            'signal': period['signal'], 'at': time.time(), 'role': role, 'intro': intro, **f,
            'text': render(intro, f)}


def rewrite(cfg, key, at, ask=None):
    """The stored summary of `key` made at `at` gets the local model's intro (in a thread: the page is never kept waiting)."""
    def go():
        try:
            with LOCK:
                data = read(cfg)
            s = next((x for x in user_of(data, key).get('summaries') or [] if x['at'] == at), None)
            f = {k: s[k] for k in ('shipped', 'running', 'failed', 'usage', 'needs')} if s else None
            intro = ai_intro(cfg, f, s['since'], ask) if s else None
            if not intro:
                return
            with LOCK:
                data = read(cfg)   # read again: the page may have marked it read meanwhile
                for x in user_of(data, key).get('summaries') or []:
                    if x['at'] == at:
                        x.update(intro=intro, by='model', text=render(intro, f))
                write(cfg, data)
        except Exception as e:  # a model problem never breaks the dashboard: recorded as a crash
            telemetry.capture_exception(type(e), e, e.__traceback__, where='welcome')
    threading.Thread(target=go, name='tutti-welcome', daemon=True).start()


def remember(data, key, summary):
    u = user_of(data, key)
    u['summaries'] = [*(u.get('summaries') or []), summary][-KEEP:]
    u['unread'] = True


# ---- the edge: the dashboard's loop, its GET and POST, the CLI

def accounts_now(cfg, now):
    from . import state
    try:
        return state.usage_state(cfg, datetime.fromtimestamp(now, timezone.utc))['accounts']
    except (OSError, ValueError, KeyError):
        return []


def owner_here(cfg, now):
    """(here?, signal) from the owner's effective mode and their laptop or main machine's presence."""
    from . import owner, pacing, presence, state
    away = presence.owner_away(cfg)
    if away:
        return False, 'presence'
    try:
        mode = owner.view(cfg, config.state_dir(cfg), pacing.read_log(state.usage_log_path(cfg)),
                          datetime.fromtimestamp(now, timezone.utc))['effective']
    except (OSError, ValueError, KeyError):
        return True, 'unknown'
    return mode not in ('away', 'vacation'), 'owner-mode'


def _tick(cfg, now=None, ask=None):
    """The dashboard's loop: the owner's signals; a return makes their summary. Returns event lines."""
    now = now or time.time()
    data = read(cfg)
    if not enabled_for(cfg, data, OWNER):
        return []
    here, signal = owner_here(cfg, now)
    period = observe(cfg, data, OWNER, here, signal, now)
    if period:
        summary = build(cfg, period, 'admin', OWNER, accounts_now(cfg, now), ai=False)
        remember(data, OWNER, summary)
        telemetry.record('feature', 'welcome.summary', signal=signal, role='admin')
    write(cfg, data)
    if period:
        rewrite(cfg, OWNER, summary['at'], ask)
    return ['welcome: a summary is waiting for the owner'] if period else []


def viewer(collector, headers):
    """(role, key) of who asks; None with sign-in on and no signed-in user (never the owner's summary by default)."""
    who = getattr(collector, 'who', None)
    w = who(headers) if callable(who) else None
    if w:
        return w['role'], w['sub']
    return None if getattr(collector, 'signed', False) else ('admin', OWNER)


ANON = (401, {'error': 'sign in first: open the dashboard'})


def _get(collector, headers, now=None):
    """GET /api/welcome: this viewer's latest summary (made now when they just came back). ?now=1 is What did I miss?"""
    cfg, now = collector.cfg, now or time.time()
    who = viewer(collector, headers)
    if who is None:
        return ANON
    role, sub = who
    key = sub
    data = read(cfg)
    on = enabled_for(cfg, data, key)
    period = visit(cfg, data, key, now) if on else None
    if period:
        made = build(cfg, period, role, sub, accounts_now(cfg, now) if role == 'admin' else [], ai=False)
        remember(data, key, made)
        telemetry.record('feature', 'welcome.summary', signal='dashboard', role=role)
    write(cfg, data)
    if period:
        rewrite(cfg, key, made['at'])
    u = user_of(data, key)
    latest = (u.get('summaries') or [None])[-1]
    if role == 'admin' and key != OWNER:   # an admin signed in also gets what Tutti noticed about its owner
        own = (user_of(data, OWNER).get('summaries') or [None])[-1]
        latest = max([s for s in (latest, own) if s], key=lambda s: s['at'], default=None)
    return 200, {'enabled': on, 'unread': bool(u.get('unread')) and on, 'summary': latest}


def miss(cfg, role, sub, accounts=None, now=None):
    """What did I miss? A summary since the last one this user saw (or the last day), made now."""
    now = now or time.time()
    data = read(cfg)
    prev = (user_of(data, sub).get('summaries') or [None])[-1]
    since = prev['at'] if prev else now - 86400
    return build(cfg, {'since': since, 'signal': 'asked'}, role, sub, accounts, ai=False)


MISS_RX = re.compile(r"^\s*(what (did|have) i miss(ed)?|what happened while i was away)\s*\??\s*$", re.I)


def asks_miss(text):
    return bool(MISS_RX.match(text or ''))


def _chat_answer(cfg, user, now=None):
    """The Chat's answer to What did I miss?: the summary since this user's last one, kept as their latest."""
    key = OWNER if user.get('sub') in (None, 'local') else user['sub']
    role = user.get('role') or 'viewer'
    made = miss(cfg, role, key, accounts_now(cfg, now or time.time()) if role == 'admin' else [])
    data = read(cfg)
    remember(data, key, made)
    user_of(data, key)['unread'] = False
    write(cfg, data)
    return made['text']


def _post(collector, headers, raw):
    """POST /api/welcome {"action": "read"|"miss"|"on"|"off"}: the viewer's own setting and summary only."""
    import json
    try:
        body = json.loads(raw or b'{}')
    except ValueError:
        body = None
    action = body.get('action') if isinstance(body, dict) else None
    if action not in ('read', 'miss', 'on', 'off'):
        return 400, {'error': 'send {"action": "read"}, {"action": "miss"} (What did I miss?), {"action": "on"} or {"action": "off"}'}
    cfg = collector.cfg
    who = viewer(collector, headers)
    if who is None:
        return ANON
    role, sub = who
    data = read(cfg)
    u = user_of(data, sub)
    if action in ('on', 'off'):
        u['enabled'] = action == 'on'
    elif action == 'read':
        u['unread'] = False
    else:
        made = miss(cfg, role, sub, accounts_now(cfg, time.time()) if role == 'admin' else [])
        remember(data, sub, made)
        u['unread'] = False
    write(cfg, data)
    if action == 'miss':
        rewrite(cfg, sub, made['at'])
    return 200, {'ok': True, 'summary': (u.get('summaries') or [None])[-1], 'enabled': enabled_for(cfg, data, sub)}


def errors(cfg):
    s = cfg.get('welcome')
    if s is None:
        return []
    if not isinstance(s, dict):
        return [f'welcome is {s!r}: a mapping such as {{enabled: true}}']
    errs = [f'welcome has {k}: use {", ".join(DEFAULTS)}' for k in sorted(set(s) - set(DEFAULTS))]
    errs += [f'welcome.{k} is {s[k]!r}: true or false' for k in ('enabled', 'ai') if k in s and not isinstance(s[k], bool)]
    v = s.get('away_hours')
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0):
        errs.append(f'welcome.away_hours is {v!r}: hours above 0 (empty: pacing.away_after_hours)')
    return errs


def tick(cfg, now=None, ask=None):
    with LOCK:
        return _tick(cfg, now, ask)


def get(collector, headers, now=None):
    with LOCK:
        return _get(collector, headers, now)


def post(collector, headers, raw):
    with LOCK:
        return _post(collector, headers, raw)


def chat_answer(cfg, user, now=None):
    with LOCK:
        return _chat_answer(cfg, user, now)
