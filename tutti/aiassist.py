"""AI assist: Tutti uses AI, local and online, to make itself better. Off by default; an admin opts in (`ai_assist.enabled`,
Settings, Telemetry and self-improvement, or the dashboard's "Tutti is improving itself" card), with separate switches:

    ai_assist: {enabled: true, local: true, online: true, online_code: false, auto_tune: true}

- **Self-analysis** (local): a local model (an ollama worker) reads the Insights summary (telemetry: failures, stalls, parks,
  wasted quota, slow commands; categories and numbers only) and writes ranked proposals; each becomes a self-improvement
  task with its evidence and a regression-test-first brief (selfimprove.add_candidates: the day's cap and the dedupe hold).
- **Smarter decisions** (local): a failed autopilot attempt is classified (flaky, environment, real: a flaky or environment
  failure retries on the same step, a real one escalates) and summarised for the retry's brief; a queued task gets
  acceptance criteria and a size (S, M, L: the model tier that takes it). Each has a cheap fallback with no AI (regular
  expressions, a heuristic), used when AI is off or every local model is down.
- **Auto-tuning**: measured outcomes propose changes to Tutti's own parameters (TUNABLE). A small one (inside its bounds
  and step) is applied by itself, logged with the old value and undoable; a larger one waits as a proposal for the admin.
- **Research and design** (online): generated scout watches (`tutti-ai-*`) ask the online scout (Grok Bot) about the agent
  CLIs' release notes, new models, prices and usage APIs, and for a design review of the scrubbed insights. They are
  `spare_only`: sent only while the scout's account is behind its pace (spare quota the planner would lose), never from the
  owner's reserve. Their TUTTI-TASK lines become tasks in the home project.
- **Privacy**: what goes online is scrubbed (tutti/scrub.py, docs/TELEMETRY.md) and is never code, a prompt or a brief;
  code may go online only with `online_code: true` (a separate switch). Briefs and failure output stay on local models.

Pure where it can be (the parsers, the fallbacks, the tuning rules); the model calls and the config writes are at the edge."""
import hashlib
import json
import os
import re
import threading
import time

from . import backlog, config, insights, scrub, telemetry

FILE = 'ai-assist.json'
DEFAULTS = {'enabled': False, 'local': True, 'online': False, 'online_code': False, 'auto_tune': True, 'every_hours': 6,
            'max_proposals': 5, 'local_worker': '', 'scout': '', 'research_every': '7d', 'online_worker': '', 'preempt': True,
            'unblock': True, 'unblock_repos': []}   # online_worker: a CLI worker, or auto
SWITCHES = ('enabled', 'local', 'online', 'online_code', 'auto_tune')
CLASSES = ('flaky', 'environment', 'real')
WATCH_PREFIX = 'tutti-ai-'
MARK = 'PROPOSAL:'
DAY = 86400
# The parameters auto-tuning may change: (min, max, the largest change applied by itself)
TUNABLE = {
    ('autopilot', 'no_progress'): (2, 6, 1),
    ('autopilot', 'max_attempts'): (4, 20, 2),
    ('dashboard', 'stall_minutes'): (5, 60, 5),
    ('dashboard', 'probe_seconds'): (15, 120, 15),
    ('self_improve', 'every_hours'): (2, 24, 2),
}
MAX_AUTO_PER_DAY = 2
# Narrow phrases from the network and the machine, never words a test name or an assertion uses; a rate limit is not
# flaky: the ladder moves on to another account (the reviewer's finding on #77)
FLAKY_RX = re.compile(r'\b(ECONNRESET|ETIMEDOUT|EAI_AGAIN|ECONNREFUSED)\b|Connection reset by peer|Temporary failure in name '
                      r'resolution|503 Service Unavailable|Read timed out|net::ERR_[A-Z_]+')
ENV_RX = re.compile(r': command not found\b|is not on PATH|^ModuleNotFoundError: |No space left on device|\bENOSPC\b|'
                    r'Cannot connect to the Docker daemon|\bOut of memory\b|\bOOM[- ]kill|Killed\s*$', re.M)
RATE_RX = re.compile(r'\b429\b|rate[ -]?limit|quota (exceeded|exhausted)|usage limit', re.I)
SWITCH_DOWN = 1800   # after no local model answered, decisions wait this long before asking again
PER_ROUND = 5   # failures examined and tasks sharpened per round
_running = threading.Lock()


# ---- switches

def settings(cfg):
    s = cfg.get('ai_assist') or {}
    return {**DEFAULTS, **(s if isinstance(s, dict) else {})}


def path_of(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


def read_state(cfg):
    return telemetry.read_json(path_of(cfg), {})


def write_state(cfg, data):
    telemetry.write_json(path_of(cfg), data)


def switch(cfg, name):
    """A switch: false in the config always wins (the admin's off); else the state folder's (a config the dashboard
    cannot write) over the config's. Every switch is off while `enabled` is."""
    def one(k):
        if (cfg.get('_ai_assist_file') or {}).get(k) is False:   # written in the file, not a default
            return False
        v = (read_state(cfg).get('switches') or {}).get(k)
        return bool(settings(cfg)[k]) if v is None else bool(v)
    return one('enabled') and (name == 'enabled' or one(name))


def may_send_online(cfg, kind):
    """Whether `kind` ('telemetry' or 'code') may go to an online model: online on, and for code also online_code."""
    if not switch(cfg, 'online'):
        return False
    return kind != 'code' or switch(cfg, 'online_code')


def online_text(cfg, text, quotes=True):
    """Text for an online model, scrubbed the way telemetry is (no paths under home, hosts, tokens, e-mails, and quoted text
    unless `quotes` is False: a JSON summary of categories keeps its quotes)."""
    hosts = [m.get('host') for m in (cfg.get('machines') or {}).values() if isinstance(m, dict) and m.get('host')]
    return scrub.text(text, hosts=hosts, limit=6000, quotes=quotes)


# ---- models: every call goes through tutti/brain.py (the best free local model in the household, then online)

def ask_local(cfg, prompt, purpose='self-improve', **kw):
    """(answer, who) from tutti/brain.py's ask: the one helper for Tutti's own model use (backlog t80)."""
    from . import brain
    return brain.ask(cfg, prompt, purpose, **kw)


# ---- smarter decisions: the cheap fallbacks (pure)

def classify_quick(text):
    """flaky, environment or real, from the failing output alone."""
    if ENV_RX.search(text or ''):
        return 'environment'
    if FLAKY_RX.search(text or ''):
        return 'flaky'
    return 'real'


def size_quick(task):
    """S, M or L from the task's scope and title: a heuristic for when no model answers."""
    scope, words = len(task.get('scope') or []), len((task.get('title') or '').split())
    if scope > 4 or words > 40:
        return 'L'
    return 'M' if scope > 1 or words > 18 else 'S'


def parse_class(answer):
    m = re.search(r'^\W*CLASS:\s*(flaky|environment|real)\b', answer or '', re.I | re.M)
    return m.group(1).lower() if m else None


def attempt_class(cfg, ok, text):
    """The class of a finished attempt for the autopilot: None when it passed or AI assist is off (the ladder as before)."""
    return None if ok or not switch(cfg, 'enabled') else classify_quick(text)


def parse_size(answer):
    m = re.search(r'\bSIZE:\s*([SML])\b', answer or '', re.I)
    return m.group(1).upper() if m else None


def criteria_lines(answer, limit=5):
    out = [re.sub(r'^\s*(?:[-*]|\d+[.)])\s*', '', ln).strip() for ln in (answer or '').splitlines()]
    return [ln[:200] for ln in out if ln and not ln.endswith(':')][:limit]


def failure_prompt(tail):
    return ('A coding agent\'s attempt failed. Its failing output follows. Answer in two lines: first CLASS: flaky, environment '
            'or real (flaky: a dropped connection or a DNS failure; environment: a missing tool, disk, memory or permission; '
            'a rate limit or a used-up quota is real, never flaky; '
            'real: the code or the test is wrong). Then SUMMARY: the cause in one sentence a developer can act on.\n\n'
            + (tail or '')[-4000:])


def brief_prompt(task):
    return ('Write 3 to 5 acceptance criteria, one per line, for this coding task: what must be true when it is done, '
            'testable, no preamble. Then one line SIZE: S, M or L (S: one file, M: a few files, L: a feature).\n\nTask: '
            + task['title'] + ('\nScope: ' + ', '.join(task['scope']) if task.get('scope') else '')
            + ''.join(f'\nNote: {n}' for n in (task.get('notes') or [])[:5]))


def examine_attempt(cfg, t, ask):
    """The last failed attempt of `t` classified and summarised by a local model (in place). True when it changed."""
    last = (t.get('attempts') or [None])[-1]
    if not last or last.get('ok') or last.get('class_by') == 'ai' or not last.get('tail'):
        return False
    answer, _who = ask(cfg, failure_prompt(last['tail']))
    if answer is None:
        return False
    quick = classify_quick(last['tail'])
    # a rate limit is never flaky, whatever the model says: the ladder moves on to another account
    last['class'] = 'real' if RATE_RX.search(last['tail']) else parse_class(answer) or last.get('class') or quick
    m = re.search(r'SUMMARY:\s*(.+)', answer)
    if m:
        last['summary'] = m.group(1).strip()[:400]
    last['class_by'] = 'ai'
    return True


def sharpen_task(cfg, t, ask):
    """A queued task with no attempt yet gets acceptance criteria and a size from a local model (in place); the size
    heuristic when none answers. True when it changed."""
    if t['status'] != 'queued' or t.get('attempts') or t.get('criteria') is not None:
        return False
    answer, _who = ask(cfg, brief_prompt(t))
    if answer is None:   # no model: the heuristic size now; criteria stay unasked and are asked again later
        if t.get('sized'):
            return False
        t['size'], t['sized'] = size_quick(t), 'heuristic'
        return True
    t['criteria'] = criteria_lines(re.sub(r'(?im)^\s*SIZE:.*$', '', answer))
    if t.get('sized') != 'ai':
        t['size'] = parse_size(answer) or (t['size'] if t.get('sized') else size_quick(t))
        t['sized'] = 'ai' if parse_size(answer) else 'heuristic'
    return True


# ---- self-analysis (pure parts)

def analysis_prompt(summary, limit):
    return ('You improve Tutti, a tool that paces AI coding agents\' quotas and runs their tasks. Below is its own telemetry of '
            'the last days (categories and numbers only). Find the improvements with the most value: failures, stalls, '
            'parked tasks, wasted quota, idle capacity, slow commands. Write at most ' + str(limit) + ' lines, the most '
            'valuable first, exactly: PROPOSAL: <what to change, one line> || why: <the evidence, with numbers> || test: <the '
            'regression test that fails before the fix>. Optionally, to change a parameter, a line TUNE: <key> = <number> || '
            'why: <evidence>, where key is one of ' + ', '.join('.'.join(p) for p in TUNABLE) + '. Nothing else.\n\n'
            + json.dumps(summary, sort_keys=True)[:6000])


def fields(rest):
    parts = [p.strip() for p in rest.split('||')]
    out = {'title': re.sub(r'\s+', ' ', parts[0])[:200]}
    for p in parts[1:]:
        k, _, v = p.partition(':')
        out[k.strip().lower()] = v.strip()[:300]
    return out


def parse_proposals(answer, limit):
    """[{title, why, test}] from PROPOSAL: lines, in the order given (ranked), at most `limit`."""
    out = []
    for line in (answer or '').splitlines():
        body = line.strip().lstrip('-*0123456789.) ').strip('`')
        if body.upper().startswith(MARK):
            it = fields(body[len(MARK):])
            if it['title']:
                out.append(it)
    return out[:limit]


def parse_tunes(answer):
    """[{path, value, why}] from TUNE: lines naming a TUNABLE key and a number."""
    out = []
    for line in (answer or '').splitlines():
        m = re.match(r'^\W*TUNE:\s*([a-z_.]+)\s*=\s*(-?\d+(?:\.\d+)?)\s*(?:\|\|\s*why:\s*(.*))?$', line.strip(), re.I)
        if m and tuple(m.group(1).split('.')) in TUNABLE and float(m.group(2)).is_integer():   # every tunable key is a whole number
            out.append({'path': tuple(m.group(1).split('.')), 'value': int(float(m.group(2))),
                        'why': (m.group(3) or 'proposed by the self-analysis').strip()[:200]})
    return out


def sig_of(title):
    return 'ai:' + hashlib.sha256(re.sub(r'\W+', ' ', title.lower()).strip().encode()).hexdigest()[:12]


def as_candidates(proposals, by):
    return [{'sig': sig_of(p['title']), 'title': p['title'], 'why': f'AI self-analysis by {by}',
             'notes': [f'Self-improvement proposed by {by} from Tutti\'s own telemetry. Evidence: {p.get("why") or "see the insights"}.',
                       'First add a regression test that reproduces it and fails'
                       + (f' (suggested: {p["test"]})' if p.get('test') else '') + '; then make it pass.',
                       '`tutti telemetry report` summarises the local events.']} for p in proposals]


# ---- auto-tuning (pure rules, edge writes)

def current(cfg, path):
    v = cfg
    for k in path:
        v = (v or {}).get(k) if isinstance(v, dict) else None
    return v


def rule_tunes(cfg, summary):
    """Proposals from measured outcomes: many parks with no progress and wasted quota stop a task sooner; repeated stalls
    of one cause wait longer before calling a job stalled."""
    out = []
    parked = {p['cause']: p['count'] for p in summary.get('parked') or []}
    wasted = sum(w['minutes'] for w in summary.get('wasted') or [])
    np = current(cfg, ('autopilot', 'no_progress'))
    if isinstance(np, int) and parked.get('no_progress', 0) >= 3 and wasted >= 60:
        out.append({'path': ('autopilot', 'no_progress'), 'value': np - 1, 'metric': 'wasted_minutes',
                    'why': f'{parked["no_progress"]} tasks parked with no progress and {wasted:g} min of quota wasted in 7 days'})
    stalls = max((s['count'] for s in summary.get('stalls') or []), default=0)
    sm = current(cfg, ('dashboard', 'stall_minutes'))
    if isinstance(sm, (int, float)) and stalls >= 5:
        out.append({'path': ('dashboard', 'stall_minutes'), 'value': sm + 5, 'metric': 'stalls',
                    'why': f'{stalls} stalls of one cause in 7 days: a longer wait before a job counts as stalled'})
    return out


def judge(cfg, tune):
    """'auto' (inside the bounds and the step: applied by itself), 'propose' (for the admin) or 'drop' (no change)."""
    lo, hi, step = TUNABLE[tuple(tune['path'])]
    old = current(cfg, tune['path'])
    if old == tune['value']:
        return 'drop'
    if not isinstance(old, (int, float)) or not lo <= tune['value'] <= hi or abs(tune['value'] - old) > step:
        return 'propose'
    return 'auto'


def metric(summary, name):
    if name == 'stalls':
        return sum(s['count'] for s in summary.get('stalls') or [])
    return round(sum(w['minutes'] for w in summary.get('wasted') or []), 1)


def auto_room(log, now):
    return MAX_AUTO_PER_DAY - sum(1 for e in log if e.get('by') == 'auto' and now - e['at'] < DAY)


def apply_tune(cfg, st, t, summary, now, by, store):
    """Writes one tune into the config (with its backup) and logs it with its old value for undo. Returns (ok, event line)."""
    from . import settings as settings_mod
    log, key, old = st.setdefault('tuned', []), '.'.join(t['path']), current(cfg, t['path'])
    try:
        store(cfg, [(list(t['path']), t['value'])])
    except settings_mod.SettingsError as e:
        return False, f'ai-assist: {key} not tuned: {e}'
    log.append({'id': f'u{len(log) + 1}', 'at': now, 'path': key, 'old': old, 'new': t['value'], 'why': t['why'], 'by': by,
                'metric': t.get('metric'), 'before': metric(summary, t.get('metric'))})
    telemetry.record('feature', 'aiassist.tune', key=key, by=by)
    return True, f'ai-assist: {key} {old} -> {t["value"]} ({t["why"]}); undo on the dashboard'


def propose(cfg, st, t, now):
    key = '.'.join(t['path'])
    props = [p for p in st.get('tune_proposals') or [] if now - p['at'] < 7 * DAY and p['path'] != key]   # stale ones go
    st['tune_proposals'] = props + [{'path': key, 'value': t['value'], 'old': current(cfg, t['path']), 'why': t['why'], 'at': now,
                                     'metric': t.get('metric')}]


def tune(cfg, st, tunes, summary, now, store=None):
    """Applies the small tunes (at most MAX_AUTO_PER_DAY a day, one per key a day; one past the day's room waits for the
    next round) and keeps the larger ones as proposals the admin applies or dismisses. Returns event lines."""
    if not switch(cfg, 'auto_tune'):
        return []
    from . import settings as settings_mod
    store = store or settings_mod.store
    events = []
    for t in tunes:
        verdict, key = judge(cfg, t), '.'.join(t['path'])
        if verdict == 'drop' or any(e['path'] == key and now - e['at'] < DAY for e in st.get('tuned') or []):
            continue
        if verdict == 'propose':
            propose(cfg, st, t, now)
        elif auto_room(st.get('tuned') or [], now) > 0 and settings_mod.writable(cfg):
            events.append(apply_tune(cfg, st, t, summary, now, 'auto', store)[1])
    return events


def decide(cfg, path, apply, store=None, now=None):
    """The admin's answer to a proposed tune: apply it (logged, undoable) or dismiss it. Returns (ok, why)."""
    from . import settings as settings_mod
    st, now = read_state(cfg), now or time.time()
    p = next((x for x in st.get('tune_proposals') or [] if x['path'] == path), None)
    if not p:
        return False, f'no proposed change to {path}: reload the page'
    ok, line = apply_tune(cfg, st, {**p, 'path': tuple(path.split('.'))}, cached_summary(cfg) or {}, now, 'admin',
                          store or settings_mod.store) if apply else (True, '')
    if ok:   # a failed apply keeps the proposal, so the admin can try again
        st['tune_proposals'] = [x for x in st['tune_proposals'] if x['path'] != path]
        write_state(cfg, st)
    return ok, line


def live_proposals(st, now=None):
    """The proposed tunes younger than 7 days (older ones go from the card and the welcome-back list)."""
    now = now or time.time()
    return [p for p in st.get('tune_proposals') or [] if now - float(p.get('at') or 0) < 7 * DAY]


def undo(cfg, tune_id, store=None):
    """Puts a tuned key back to its old value (removed when it was not in the file). Returns (ok, why)."""
    from . import settings as settings_mod
    from . import yamledit
    store = store or settings_mod.store
    st = read_state(cfg)
    e = next((x for x in st.get('tuned') or [] if x['id'] == tune_id), None)
    if not e or e.get('undone'):
        return False, f'no tuned change {tune_id} to undo: reload the page'
    try:
        store(cfg, [(e['path'].split('.'), yamledit.DELETE if e['old'] is None else e['old'])])
    except settings_mod.SettingsError as err:
        return False, f'{e["path"]} could not be put back: {err}'
    e['undone'] = time.time()
    write_state(cfg, st)
    telemetry.record('feature', 'aiassist.undo', key=e['path'])
    return True, ''


# ---- research and design (online, through the scout)

def scout_name(cfg):
    names = list(cfg.get('scouts') or {})
    chosen = settings(cfg)['scout']
    if chosen in names:
        return chosen
    return next((n for n in names if 'research' in ((cfg['scouts'][n] or {}).get('skills') or ())), names[0] if names else '')


def research_text(cfg):
    clis = sorted({w['command'][0] for w in (cfg.get('workers') or {}).values() if isinstance(w, dict) and w.get('kind') == 'cli'
                   and w.get('command')})
    return ('Research for Tutti, a tool that paces quotas of AI coding agents and routes tasks to them. For these agent CLIs: '
            + ', '.join(clis or ['claude', 'cursor-agent']) + ', and local models served by ollama: read the release notes, '
            'model launches and price or plan changes of the last 7 days. Report what Tutti should adopt: a new cheaper or '
            'stronger model, a new flag, a new usage or quota API that would remove a manual reading, a breaking change. End '
            'with one line per change worth making, exactly: TUTTI-TASK: <the change to Tutti, one line> || check: <a test '
            'that shows it>. Write no TUTTI-TASK line when nothing is worth it.')


def failing_titles(cfg):
    """The titles of failed and parked tasks: text the owner wrote, sent online only with online_code."""
    tasks = backlog.load(cfg)['tasks']
    return [t['title'][:160] for t in tasks if t['status'] == 'needs-person'
            or (t.get('attempts') and not t['attempts'][-1].get('ok'))][-10:]


def design_text(cfg, summary):
    return ('Design review for Tutti, a tool that paces quotas of AI coding agents. Its telemetry of the last 7 days '
            '(categories and numbers only) follows. Name the two or three changes to its design that would cut failures, '
            'stalls, wasted quota or idle capacity the most, each with the evidence. End with one line per change, exactly: '
            'TUTTI-TASK: <the change, one line> || check: <the regression test>.\n\n'
            + online_text(cfg, json.dumps(summary, sort_keys=True), quotes=False)
            + focus_text(cfg)
            + (('\n\nThe tasks that failed or were parked (titles):\n' + online_text(cfg, '\n'.join(failing_titles(cfg))))
               if may_send_online(cfg, 'code') else ''))


def focus_text(cfg):
    """The scout's question, drafted by Tutti's own AI (draft_focus): the questions a local model found in the numbers."""
    focus = [str(x)[:200] for x in (read_state(cfg).get('scout_focus') or [])][:3]
    return ('\n\nLook first at:\n' + online_text(cfg, '\n'.join(f'- {x}' for x in focus))) if focus else ''


def draft_focus(cfg, summary, ask, now):
    """Scout drafting through Tutti's own AI: the best free local model reads the week's numbers and writes the three
    questions the online design review should answer first (stored; the watch's text adds them). Local only: drafting a
    question for an online model is not worth an online call. Returns event lines."""
    prompt = ('Tutti\'s telemetry of the last 7 days (categories and numbers) follows. Write the three questions a design '
              'reviewer should answer first to cut failures, stalls and wasted quota, one per line starting with Q:.\n\n'
              + json.dumps(summary, sort_keys=True)[:6000])
    answer, _who = ask(cfg, prompt, 'scout', online=False)
    qs = [m.group(1).strip() for m in re.finditer(r'^\s*Q:\s*(.+)$', str(answer or ''), re.M)][:3]
    if not qs:
        return []
    st = read_state(cfg)
    st.update(scout_focus=qs, scout_focus_at=now)
    write_state(cfg, st)
    return [f'ai-assist: {len(qs)} question(s) drafted for the design review']


def watches(cfg, sc, summary_fn=None):
    """The generated watches of the AI-assist scout, when online AI is on: release notes and a design review, both
    spare_only (sent only while the scout's account has spare quota)."""
    if not may_send_online(cfg, 'telemetry') or sc.get('name') != scout_name(cfg):
        return {}
    home = (cfg.get('self_improve') or {}).get('home') or None
    every = settings(cfg)['research_every']
    out = {WATCH_PREFIX + 'release-notes': {'every': every, 'task': research_text(cfg), 'skill': 'research', 'spare_only': True,
                                            'project': home, 'spend_kind': 'notes'}}
    summary = (summary_fn or cached_summary)(cfg)
    if summary and summary.get('events'):
        out[WATCH_PREFIX + 'design'] = {'every': every, 'task': design_text(cfg, summary), 'skill': 'research',
                                        'spare_only': True, 'project': home}
    return out


def cached_summary(cfg):
    from . import dashtelemetry
    return dashtelemetry.cached_insights(telemetry.folder_of(config.state_dir(cfg)))


def answered(data):
    """Every answered tutti-ai-* watch not fed yet, marked fed (inside the scouts' lock); `feed` turns them into tasks."""
    out = []
    for r in data['requests']:
        if r.get('kind') == 'watch' and str(r.get('watch') or '').startswith(WATCH_PREFIX) and r.get('status') == 'answered' \
                and not r.get('fed'):
            r['fed'] = {'at': time.time()}
            out.append(dict(r))
    return out


def feed(cfg, requests, now=None):
    """The TUTTI-TASK lines of answered research watches into the home project (the backlog's lock: never inside the
    scouts'). Returns event lines."""
    from . import selfimprove, triage
    events, now = [], now or time.time()
    if not may_send_online(cfg, 'telemetry'):   # answers after online AI was turned off are dropped
        return [f'{r["id"]} {r["watch"]}: not read, online AI is off' for r in requests]
    for r in requests:
        items = [{'sig': sig_of(it['title']), 'title': it['title'], 'why': f'online research ({r["watch"]})',
                  'notes': [f'From {r["watch"]} ({r.get("url") or r["id"]}).', 'First add a test that shows the change; then make it.']
                  + (['A check command was suggested; it is not passed on (outside text, audit 2026-11 H7): write your own '
                      'test.'] if it['check'] else [])}
                 for it in triage.parse(r.get('answer'))]
        # outside text (a scout's reply): candidates the admin accepts (tutti backlog accept), never queued by itself
        added = selfimprove.add_candidates(cfg, items, 'insight', now, accept=False) if items else 0
        remember(cfg, items, f'online {r["watch"]}', now)
        events.append(f'{r["id"]} {r["watch"]}: {added} task(s) for the backlog')
    return events


# ---- the round

def remember(cfg, items, by, now):
    st = read_state(cfg)
    props = st.setdefault('proposals', [])
    for it in items:
        if not any(p['sig'] == it['sig'] for p in props):
            props.append({'sig': it['sig'], 'title': it['title'], 'by': by, 'at': now})
    st['proposals'] = props[-200:]
    write_state(cfg, st)


def self_analysis(cfg, summary, now, ask):
    from . import selfimprove
    answer, who = ask(cfg, analysis_prompt(summary, settings(cfg)['max_proposals']))
    if answer is None:
        return [], [], f'self-analysis skipped: {who}'
    proposals = parse_proposals(answer, settings(cfg)['max_proposals'])
    items = as_candidates(proposals, who)
    added = selfimprove.add_candidates(cfg, items, 'insight', now)
    remember(cfg, items, who, now)
    telemetry.record('feature', 'aiassist.analyse', proposals=len(items), added=added)
    return [f'ai-assist: {who} proposed {len(items)} improvement(s), {added} new task(s)'], parse_tunes(answer), ''


def wants_model(t):
    last = (t.get('attempts') or [None])[-1]
    failed = bool(last) and not last.get('ok') and last.get('class_by') != 'ai' and bool(last.get('tail'))
    fresh = t['status'] == 'queued' and not t.get('attempts') and t.get('criteria') is None
    return failed or fresh


def decisions(cfg, ask, now=None):
    """Up to PER_ROUND failed attempts examined and queued tasks sharpened. The model is asked outside the backlog's lock
    (copies), the results applied under it; after a round where no model answered, the next waits SWITCH_DOWN."""
    now, st = now or time.time(), read_state(cfg)
    if now < float(st.get('models_down_until') or 0):
        return []
    with backlog.locked(cfg) as data:
        work = [json.loads(json.dumps(t)) for t in data['tasks'][-200:] if wants_model(t)][:PER_ROUND]
    answered, down = [], [False]

    def asking(c, prompt):
        answer, who = ask(c, prompt)
        down[0] = down[0] or answer is None
        return answer, who
    for t in work:
        if examine_attempt(cfg, t, asking) | sharpen_task(cfg, t, asking):
            answered.append(t)
    with backlog.locked(cfg) as data:
        by_id = {t['id']: t for t in answered}
        for t in data['tasks']:
            if t['id'] in by_id and len(t.get('attempts') or []) == len(by_id[t['id']].get('attempts') or []):
                t.update({k: by_id[t['id']][k] for k in ('attempts', 'criteria', 'size', 'sized') if k in by_id[t['id']]})
    if down[0]:
        st = read_state(cfg)
        st['models_down_until'] = now + SWITCH_DOWN
        write_state(cfg, st)
    return [f'ai-assist: {len(answered)} task(s) sharpened or failures examined'] if answered else []


def tick(cfg, now=None, ask=None, store=None):
    """One round when due (every_hours): self-analysis, decisions, auto-tuning. Nothing at all while AI assist is off."""
    if not switch(cfg, 'enabled'):
        return []
    now, ask = now or time.time(), ask or ask_local
    st = read_state(cfg)
    events = decisions(cfg, ask, now)
    if now - float(st.get('last_round') or 0) < float(settings(cfg)['every_hours']) * 3600:
        return events
    summary = insights.summary(telemetry.read_events(telemetry.folder_of(config.state_dir(cfg))), 7)
    lines, ai_tunes, skipped = self_analysis(cfg, summary, now, ask) if summary['events'] else ([], [], 'no telemetry yet')
    from . import brain, triage
    if summary['events'] and ask is ask_local:
        lines += draft_focus(cfg, summary, brain.ask, now)
    lines += triage.draft_missing(cfg)
    st = read_state(cfg)
    st.update(last_round=now, skipped=skipped)
    events += lines + tune(cfg, st, rule_tunes(cfg, summary) + ai_tunes, summary, now, store)
    write_state(cfg, st)
    return events


def tick_in_background(cfg):
    """The dashboard's loop: one round at a time, in its own thread (a local model may take minutes)."""
    if not switch(cfg, 'enabled') or not _running.acquire(blocking=False):
        return False

    def go():
        try:
            tick(cfg)
        except Exception as e:  # a model or file problem must not end the dashboard's loop: recorded as a crash
            telemetry.capture_exception(type(e), e, e.__traceback__, where='ai-assist')
        finally:
            _running.release()
    threading.Thread(target=go, name='tutti-ai-assist', daemon=True).start()
    return True


# ---- the dashboard card

def gains(cfg, st, summary):
    out = []
    for e in st.get('tuned') or []:
        if e.get('metric') and not e.get('undone') and e.get('before'):
            after = metric(summary, e['metric'])
            pct = round((after - e['before']) * 100 / e['before'])
            out.append(f'{e["metric"].replace("_", " ")} {e["before"]:g} -> {after:g} ({pct:+d} %) since {e["path"]} was tuned')
    return out


def view(cfg, summary=None):
    st = read_state(cfg)
    done = {t.get('sig') for t in backlog.load(cfg)['tasks'] if str(t.get('sig') or '').startswith('ai:') and t['status'] == 'done'}
    props = st.get('proposals') or []
    return {'switches': {k: switch(cfg, k) for k in SWITCHES}, 'proposals': len(props), 'done': len(done),
            'recent': props[-5:][::-1], 'tuned': (st.get('tuned') or [])[-10:][::-1],
            'tune_proposals': live_proposals(st)[-5:],
            'gains': gains(cfg, st, summary or cached_summary(cfg) or {}), 'last_round': st.get('last_round'),
            'skipped': st.get('skipped') or '', 'brain': _brain_view(cfg), 'unblocker': _unblocker_view(cfg)}


_BRAIN_CACHE = {}


def _brain_view(cfg, ttl=60, clock=time.monotonic):
    """tutti/brain.py's card (it probes each model server, a second at most each): once a minute."""
    from . import brain
    at, v = _BRAIN_CACHE.get('v', (None, None))
    if at is None or clock() - at >= ttl:
        try:
            v = brain.view(cfg)
        except (OSError, KeyError, ValueError) as e:
            v = {'line': f"Tutti's own AI: could not read the fleet ({type(e).__name__})", 'parks': []}
        _BRAIN_CACHE['v'] = (clock(), v)
    return v


def _unblocker_view(cfg):
    from . import unblocker
    return unblocker.view(cfg)


def save(collector, raw):
    """POST /api/ai-assist (admin): {"switch": name, "on": bool} or {"undo": id}."""
    try:
        data = json.loads(raw or b'{}')
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return 400, {'error': 'send JSON such as {"switch": "enabled", "on": true} or {"undo": "u1"}'}
    if 'proposal' in data:
        ok, why = decide(collector.cfg, str(data['proposal']), data.get('apply') is True)
        if ok and getattr(collector, 'reload', None):
            collector.reload()
        return (200, {'ok': True}) if ok else (409, {'error': why})
    if 'undo' in data:
        ok, why = undo(collector.cfg, str(data['undo']))
        reload = getattr(collector, 'reload', None)
        if ok and reload:
            reload()
        return (200, {'ok': True}) if ok else (409, {'error': why})
    if data.get('switch') not in SWITCHES or not isinstance(data.get('on'), bool):
        return 400, {'error': f'"switch" is one of {", ".join(SWITCHES)} and "on" is true or false'}
    from . import dashboard
    return dashboard.stored(collector, [(['ai_assist', data['switch']], data['on'])], {'switch': data['switch'], 'on': data['on']},
                            lambda: set_state_switch(collector.cfg, data['switch'], data['on']))


def set_state_switch(cfg, name, on):
    st = read_state(cfg)
    st.setdefault('switches', {})[name] = bool(on)
    write_state(cfg, st)
    telemetry.record('feature', 'aiassist.switch', switch=name, on=bool(on))


def brain_errors(s):
    """The keys of Tutti's own AI and the unblocker (tutti/brain.py, tutti/unblocker.py)."""
    errs = []
    repos = s.get('unblock_repos', [])
    if not (isinstance(repos, list) and all(isinstance(x, str) and '/' in x for x in repos)):
        errs.append(f'ai_assist.unblock_repos is {repos!r}: a list of GitHub repositories such as [owner/name]')
    pre = s.get('preempt', True)
    if not isinstance(pre, bool) and not (isinstance(pre, list) and all(isinstance(x, str) for x in pre)):
        errs.append(f'ai_assist.preempt is {pre!r}: true, false, or a list of purposes such as [needs, welcome]')
    return errs


def errors(cfg):
    s = cfg.get('ai_assist')
    if s is None:
        return []
    if not isinstance(s, dict):
        return [f'ai_assist is {s!r}: a mapping such as {{enabled: true, local: true, online: false}}']
    errs = [f'ai_assist has {k}: use {", ".join(DEFAULTS)}' for k in sorted(set(s) - set(DEFAULTS))]
    errs += [f'ai_assist.{k} is {s[k]!r}: true or false' for k in SWITCHES if k in s and not isinstance(s[k], bool)]
    for k in ('every_hours', 'max_proposals'):
        if k in s and (isinstance(s[k], bool) or not isinstance(s[k], (int, float)) or s[k] <= 0):
            errs.append(f'ai_assist.{k} is {s[k]!r}: a number above 0')
    errs += brain_errors(s)
    if s.get('local_worker') and s['local_worker'] not in (cfg.get('workers') or {}):
        errs.append(f'ai_assist.local_worker names "{s["local_worker"]}", which is not under workers:')
    return errs
