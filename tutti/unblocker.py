"""The unblocker (backlog t81, t244): every round Tutti looks for work that is blocked or failing and tries to clear it
itself, local first, so only true decisions reach a person.

What it finds (`find`, pure):
- **limit-only**: a task whose recent failures are all usage limits (the worker's account, not the task): its attempts are
  forgiven (resume_from) and its backoff cleared, so a worker with room takes it now.
- **failing**: the same real failure again and again (exit 1, no report, stuck, a check that does not run). A local model
  (tutti/brain.py: the best free one in the household) reads the last failure and writes a diagnosis and a fix for the next
  attempt; the task is queued again with that note, so its next attempt edits a worktree and reruns the task's check. When
  no local model answers, or the task is size L, an online agent diagnoses (brain.ask's fallback, within pacing).
- **parked**: a needs-person task. Classified first (person-only vs Tutti-resolvable). Person-only stays for the person.
  Tutti-resolvable: a rule may pick (e.g. retry after usage limits), else a local model, else an online account with room;
  if those give no option (or the model defers), Tutti takes the question's default. Each decision is logged and undoable.
- **conflict**: a result that no longer rebases onto main: queued again from scratch on the current main.
- **idle machines with queued work**: work held back by usage-limit failures is the limit-only case above; any other
  backoff (pacing, a quota window) is left alone on purpose.
- **red-ci**: a failed check run on an open pull request of `ai_assist.unblock_repos`: its failed jobs are rerun once per
  head commit (a second red on the same commit is left to the task's own retry).

Every attempt is logged (`unblock.json`: what, by local, online or rule, the action, the outcome) for the dashboard. It acts
only on the active main machine (failover lease), at most `MAX_PER_ROUND` items a round, each item at most once per
COOLDOWN; parked Tutti-resolvable items clear by rule with no cooldown. It never kills a process (the autopilot's watchdog
ends stuck runs by their exact pid). Autopilot ticks call `resolve_parked` so the needs queue is re-checked every round;
the dashboard's unblocker round asks a model for what still needs judgement."""
import json
import os
import re
import subprocess
import threading
import time

from . import backlog, config, telemetry

FILE = 'unblock.json'
KEEP = 300
MAX_PER_ROUND = 3
MAX_PARKED_RULES = 40   # usage-limit parks clear in bulk on an autopilot tick; models stay under MAX_PER_ROUND
COOLDOWN = 6 * 3600
PARKED_COOLDOWN = 30 * 60   # a Tutti-resolvable parked item the model left: tried again within the half hour
RECENT = 3
LIMIT_RX = re.compile(r'usage limit|quota (?:reached|exceeded)|RESOURCE_EXHAUSTED|Too Many Requests|(?:HTTP|status|code)[ :]*429\b', re.I)
REAL_RX = re.compile(r'exited|without a report|stuck:|changed nothing|refused|rc=127|failed', re.I)
REBASE_RX = re.compile(r'does not rebase|rebase conflict|could not apply', re.I)
ACTION_RX = re.compile(r'^\s*ACTION:\s*(\S+)', re.I | re.M)
WHY_RX = re.compile(r'^\s*(?:WHY|DIAGNOSIS):\s*(.+)', re.I | re.M)
FIX_RX = re.compile(r'^\s*FIX:\s*(.+)', re.I | re.M)
# Only a person: payments, terms, credentials, sign-ins, physical actions, risky/irreversible (owner 2026-11-17, t244).
PERSON_RX = re.compile(
    r'(?i)\b(payment|billing|invoice|credit[\s-]?card|spend[\s-]?limit|accept(?:ing)?\s+(?:the\s+)?terms|'
    r'terms\s+of\s+service|licen[cs]e(?:\s+agreement)?|credential|password|api[\s-]?key|secret\s+key|sign[\s-]?ins?|'
    r'log[\s-]?ins?|oauth|2fa|mfa|physical|plug[\s-]?in|press\s+(?:the\s+)?button|irreversible|force[\s-]?push|'
    r'drop\s+(?:the\s+)?(?:database|db)|delete\s+production|not for autopilot|needs the \S+ session|'
    r'retire .{0,60} key)\b')


# ---- finding and classifying (pure)

def recent_whys(t, n=RECENT):
    a = (t.get('attempts') or [])[int(t.get('resume_from') or 0):]
    return [str(x.get('why') or '') for x in a if not x.get('ok')][-n:]


def need_text(t, q=None):
    q = q or {}
    return ' '.join(str(x or '') for x in (q.get('question'), q.get('why'), t.get('title'), t.get('parked')))


def need_kind(text):
    """'person-only' when only a person may decide (payments, terms, credentials, sign-ins, physical, irreversible); else
    'tutti' (Tutti tries to clear it)."""
    return 'person-only' if PERSON_RX.search(str(text or '')) else 'tutti'


def open_question(questions, tid):
    return next((q for q in questions if q.get('task') == tid and q.get('status') == 'open'), None)


def rule_pick(t, q):
    """An option Tutti can pick without a model, or '' (person-only, or needs judgement)."""
    if need_kind(need_text(t, q)) == 'person-only':
        return ''
    opts = {o.lower(): o for o in q.get('options') or []}
    blob = ' '.join([str(t.get('parked') or ''), str(q.get('why') or ''), *recent_whys(t, 5)])
    if LIMIT_RX.search(blob) and 'retry' in opts:
        return opts['retry']
    return ''


def classify(t, questions=()):
    """The kind of block a task is in, or ''."""
    st = t.get('status')
    if st == 'needs-person':
        return 'parked' if open_question(questions, t['id']) else ''
    if st == 'review' and REBASE_RX.search(str(t.get('publish_note') or '')):
        return 'conflict'
    if st != 'queued':
        return ''
    whys = recent_whys(t)
    if len(whys) >= RECENT and all(LIMIT_RX.search(w) for w in whys):
        return 'limit-only'
    if len(whys) >= RECENT and all(REAL_RX.search(w) for w in whys):
        return 'failing'
    return ''


LIMIT_COOLDOWN = 24 * 3600   # attempts forgiven at most once a day: forgiving faster only feeds a loop on a capped account
LIMIT_SETTLE = 3600   # and only an hour after the last limit, so the account (or its per-model cap) had time to move on


def due(t, kind, tried, now, questions=()):
    """Whether the item may be tried now: its cooldown is over (and a usage-limit one has settled)."""
    if kind == 'parked':
        q = open_question(questions, t['id']) or {}
        if rule_pick(t, q):
            return True   # a clear Tutti rule: every autopilot tick may clear a batch
        return now - tried >= PARKED_COOLDOWN
    if kind != 'limit-only':
        return now - tried >= COOLDOWN
    last = max((float(a.get('at') or 0) for a in t.get('attempts') or []), default=0)
    return now - tried >= LIMIT_COOLDOWN and now - last >= LIMIT_SETTLE


def find(data, now, log=(), limit=MAX_PER_ROUND):
    """[{kind, id, title}] to try this round: rule-clearable parks first (bulk), then others under `limit`."""
    tried = {e.get('id'): float(e.get('at') or 0) for e in log}
    qs = data.get('questions') or []
    rules, rest = [], []
    for t in data.get('tasks') or []:
        kind = classify(t, qs)
        if not (kind and due(t, kind, tried.get(t['id'], 0), now, qs)):
            continue
        item = {'kind': kind, 'id': t['id'], 'title': str(t.get('title') or '')[:120]}
        q = open_question(qs, t['id']) or {}
        if kind == 'parked' and (rule_pick(t, q) or need_kind(need_text(t, q)) == 'person-only'):
            rules.append(item)
        else:
            rest.append(item)
    return rules[:MAX_PARKED_RULES] + rest[:limit]


def diagnose_prompt(t):
    a = (t.get('attempts') or [{}])[-1]
    # Keep it smaller
    whys = '\n'.join(f'- {w[:100]}' for w in recent_whys(t, 3))
    return ('A coding task keeps failing. Read the failures and say what blocks it.\n'
            f'TASK: {str(t.get("title"))[:200]}\n'
            f'RECENT FAILURES:\n{whys}\n'
            f'OUTPUT (tail): {str(a.get("tail") or "")[-1000:]}\n\n'
            'Answer in exactly three lines:\nACTION: retry | person\n'
            'WHY: <one sentence>\nFIX: <one sentence>')


def decide_prompt(t, q):
    # Keep it smaller
    return (f'A task is parked. Tutti resolves it unless only a person may (payment, terms, credentials, sign-in, physical, '
            'irreversible). Prefer an option; answer person only for those.\n'
            f'TASK: {str(t.get("title"))[:200]}\nQUESTION: {q.get("question")[:100]}\n'
            f'OPTIONS: {", ".join(q.get("options") or [])}\n'
            'Answer in two lines:\nACTION: <option or person>\nWHY: <one sentence>')


def parse(answer):
    """{action, why, fix} from a model's answer; action '' when it gave none."""
    text = str(answer or '')
    m, w, f = ACTION_RX.search(text), WHY_RX.search(text), FIX_RX.search(text)
    return {'action': m.group(1).strip().lower().rstrip('.') if m else '', 'why': w.group(1).strip()[:300] if w else '',
            'fix': f.group(1).strip()[:300] if f else ''}


# ---- acting (on the backlog, inside its lock)

def requeue(t, now, note, fresh=False):
    t['status'], t['parked'] = 'queued', None
    t['resume_from'] = len(t.get('attempts') or [])
    t['not_before'] = now
    if fresh:
        t['fresh'] = True
    t['notes'] = [*t.get('notes', []), note][-20:]


def apply(cfg, item, verdict, now):
    """Writes the action into the backlog; returns the outcome line."""
    with backlog.locked(cfg) as data:
        t = next((x for x in data['tasks'] if x['id'] == item['id']), None)
        if t is None or classify(t, _questions(cfg)) != item['kind']:
            return 'changed meanwhile: left as it is'
        kind = item['kind']
        if kind == 'limit-only':
            requeue(t, now, 'unblocker: the recent failures were usage limits only (the account, not the task): attempts forgiven')
            return 'attempts forgiven, queued for a worker with room'
        if kind == 'conflict':
            requeue(t, now, 'unblocker: the earlier result no longer rebases onto main: redo it on the current main', fresh=True)
            return 'queued again from scratch on the current main'
        if kind == 'failing' and verdict.get('action') == 'retry':
            requeue(t, now, f'unblocker diagnosis: {verdict["why"]} Fix for this attempt: {verdict["fix"]}')
            return 'diagnosed; queued again with the fix (its next attempt reruns the check)'
        if kind != 'failing':
            return 'nothing to do'
        from . import needs
        q = needs.ask(cfg, t, f'{item["id"]} keeps failing: {verdict.get("why") or "no diagnosis"}',
                      verdict.get('fix') or 'see its runs', ['retry', 'retry-from-scratch', 'drop'], 'retry', now=now)
        needs.mark_tried(cfg, q['id'])   # the model already said person: show it, do not hide behind Tutti resolve
        return 'a person decides: asked in the needs queue'


def _questions(cfg):
    from . import needs
    return needs.load(cfg).get('questions') or []


def match_option(q, action):
    return next((o for o in q.get('options') or [] if o.lower() == str(action or '').lower()), None)


def leave_person(cfg, qid):
    from . import needs
    if qid:
        needs.mark_tried(cfg, qid)
    return 'person-only: left for the person', {}


def pick_for_parked(q, verdict, by):
    """(pick, why, by) for a Tutti-resolvable parked question: matched option, else the default (never the person)."""
    pick, why = match_option(q, verdict.get('action')), verdict.get('why') or ''
    if pick:
        return pick, why, by
    pick = q.get('default') or (q.get('options') or [''])[0]
    if str(verdict.get('action') or '').lower() == 'person':
        why = (why + '; ' if why else '') + 'Tutti-resolvable: model deferred, took the default'
    return pick, why or 'Tutti-resolvable: took the default', ('rule' if by == 'none' else by)


def answer_parked(cfg, item, verdict, now, by='local'):
    """Applies a parked decision; returns (outcome, undo fields). Person-only stays; Tutti-resolvable always picks or defaults."""
    from . import needs
    q = open_question(_questions(cfg), item['id'])
    if not q:
        return 'no open question any more', {}
    if need_kind(need_text(task_of(cfg, item['id']), q)) == 'person-only':
        return leave_person(cfg, q['id'])
    pick, why, by = pick_for_parked(q, verdict, by)
    if not pick:
        return leave_person(cfg, q['id'])
    ok, note = needs.answer(cfg, q['id'], pick, role='admin', key='tutti-unblocker', now=now)
    undo = {'undoable': True, 'qid': q['id'], 'choice': pick, 'task': item['id']} if ok else {}
    return (f'decided "{pick}" ({by}): {why}'.strip() if ok else note), undo


def person_only_result(cfg, qid):
    outcome, _ = leave_person(cfg, qid)
    return {'by': 'rule', 'outcome': outcome, 'why': 'only a person may decide'}


def rule_result(cfg, item, pick, now):
    why = 'rule: usage limit or clear Tutti case'
    outcome, undo = answer_parked(cfg, item, {'action': pick, 'why': why}, now, by='rule')
    return {'by': 'rule', 'outcome': outcome, 'why': why, **undo}


def handle_parked(cfg, item, ask, now):
    """Classify, rule, local model, online; Tutti-resolvable always ends with a pick or the default."""
    t, q = task_of(cfg, item['id']), open_question(_questions(cfg), item['id']) or {}
    if need_kind(need_text(t, q)) == 'person-only':
        return person_only_result(cfg, q.get('id'))
    pick = rule_pick(t, q)
    if pick:
        return rule_result(cfg, item, pick, now)
    answer, who = judge(cfg, item, ask)
    by = 'none' if not answer else ('online' if who in _cli_workers(cfg) else 'local')
    verdict = parse(answer) if answer else {'action': '', 'why': f'no model answered ({who})'}
    outcome, undo = answer_parked(cfg, item, verdict, now, by=by)
    return {'by': by if by != 'none' else 'rule', 'outcome': outcome, 'why': verdict.get('why', ''), **undo}


# ---- the round

def task_of(cfg, tid):
    return next((x for x in backlog.load(cfg)['tasks'] if x['id'] == tid), {})


def needs_model(item):
    return item['kind'] == 'failing'


def handle(cfg, item, ask, now):
    """One item: parked goes through classify/rule/model; failing uses a model (local first, online when local fails or L)."""
    if item['kind'] == 'parked':
        return handle_parked(cfg, item, ask, now)
    by, verdict = 'rule', {}
    if needs_model(item):
        answer, who = judge(cfg, item, ask)
        if not answer:
            return {'by': 'none', 'outcome': f'no model answered ({who}): left as it is'}
        verdict = parse(answer)
        by = 'online' if who in _cli_workers(cfg) else 'local'
    return {'by': by, 'outcome': apply(cfg, item, verdict, now), 'why': verdict.get('why', '')}


def judge(cfg, item, ask):
    """(answer, who): local first; online only when local failed (asked once more, online only) or the task is size L."""
    t = task_of(cfg, item['id'])
    if item['kind'] == 'failing':
        prompt = diagnose_prompt(t)
    else:
        prompt = decide_prompt(t, open_question(_questions(cfg), item['id']) or {})
    big = t.get('size') == 'L'
    answer, who = ask(cfg, prompt, 'unblock', online=big)
    if not answer and not big:
        answer, who = ask(cfg, prompt, 'unblock', online=True, local_first=False)
    return answer, who


def _cli_workers(cfg):
    return {n for n, w in (cfg.get('workers') or {}).items() if isinstance(w, dict) and w.get('kind') == 'cli'}


def red_ci(cfg, run=subprocess.run, log=()):
    """[{kind: red-ci, id, title, run}] of failed check runs on open PRs of ai_assist.unblock_repos, once per head commit."""
    from . import aiassist
    seen = {e.get('id') for e in log}
    out = []
    for repo in aiassist.settings(cfg).get('unblock_repos') or []:
        heads = open_heads(repo, run)
        try:
            r = run(['gh', 'run', 'list', '--repo', repo, '--status', 'failure', '--limit', '10', '--json',
                     'databaseId,headSha,event,displayTitle'], capture_output=True, text=True, timeout=60)
            rows = json.loads(r.stdout or '[]') if r.returncode == 0 else []
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        for x in rows:
            key = f'ci:{repo}:{x.get("headSha", "")[:12]}'
            if x.get('event') == 'pull_request' and x.get('headSha') in heads and key not in seen:
                seen.add(key)
                out.append({'kind': 'red-ci', 'id': key, 'title': str(x.get('displayTitle') or '')[:120], 'repo': repo,
                            'run': x.get('databaseId')})
    return out


def open_heads(repo, run=subprocess.run):
    """The head commits of the repository's open pull requests: only their runs are rerun (not merged, closed or replaced ones)."""
    try:
        r = run(['gh', 'pr', 'list', '--repo', repo, '--state', 'open', '--json', 'headRefOid'], capture_output=True, text=True, timeout=60)
        return {x.get('headRefOid') for x in json.loads(r.stdout or '[]')} if r.returncode == 0 else set()
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()


def rerun(item, run=subprocess.run):
    r = run(['gh', 'run', 'rerun', str(item['run']), '--failed', '--repo', item['repo']], capture_output=True, text=True, timeout=60)
    return 'failed jobs rerun once' if r.returncode == 0 else f'rerun refused ({(r.stderr or "").strip()[:120]})'


def record(cfg, item, res, now):
    entry = {'at': now, **{k: item[k] for k in ('kind', 'id', 'title')}, **res}
    write_log(cfg, entry)
    telemetry.record('feature', 'unblocker.try', block=item['kind'], by=res.get('by'))
    return f'unblocker: {item["id"]} ({item["kind"]}, {res.get("by")}): {res.get("outcome")}'


def tick(cfg, now=None, ask=None, run=subprocess.run, may_write=None):
    """One round. Returns event lines. Nothing on a machine that is not the active main (the failover lease)."""
    from . import aiassist, brain, failover
    if not aiassist.settings(cfg).get('unblock', True) or not (may_write or failover.may_write)(cfg):
        return []
    now = now or time.time()
    ask = ask or brain.ask
    log = read_log(cfg)
    items = find(backlog.load(cfg) | {'questions': _questions(cfg)}, now, log)
    items += red_ci(cfg, run, log)[:max(0, MAX_PER_ROUND - len([i for i in items if i['kind'] != 'parked']))]
    events = []
    for item in items:
        try:
            res = {'by': 'rule', 'outcome': rerun(item, run)} if item['kind'] == 'red-ci' else handle(cfg, item, ask, now)
        except Exception as e:  # one item's surprise must not end the round: logged, the next items go on
            res = {'by': 'none', 'outcome': f'{type(e).__name__}: {e}'[:200]}
        events.append(record(cfg, item, res, now))
    return events


def try_auto_clear(t, park, now):
    """True when a would-be park is Tutti-resolvable and only usage limits: requeue in place, no question asked."""
    if need_kind(f'{t.get("title")} {park}') == 'person-only' or not LIMIT_RX.search(str(park or '')):
        return False
    requeue(t, now, 'unblocker: strongest step hit usage limits only: queued again, no person asked')
    return True


def handle_parked_rules(cfg, item, now):
    """Classify and apply only rules (no model): person-only marked, usage-limit retries cleared."""
    t, q = task_of(cfg, item['id']), open_question(_questions(cfg), item['id']) or {}
    if need_kind(need_text(t, q)) == 'person-only':
        return person_only_result(cfg, q.get('id'))
    pick = rule_pick(t, q)
    if not pick:
        return {'by': 'rule', 'outcome': 'needs a model: left for the dashboard round', 'why': ''}
    return rule_result(cfg, item, pick, now)


def rule_parked_items(cfg, now):
    """Parked items due for a rule clear (person-only mark or a Tutti rule pick)."""
    data = backlog.load(cfg) | {'questions': _questions(cfg)}
    qs = data.get('questions') or []
    out = []
    for item in find(data, now, read_log(cfg)):
        if item['kind'] != 'parked':
            continue
        t, q = task_of(cfg, item['id']), open_question(qs, item['id']) or {}
        if rule_pick(t, q) or need_kind(need_text(t, q)) == 'person-only':
            out.append(item)
    return out


def resolve_parked(cfg, now=None, may_write=None):
    """Every autopilot tick: rule-clear Tutti-resolvable parks and mark person-only (models stay on the dashboard round)."""
    from . import aiassist, failover
    if not aiassist.settings(cfg).get('unblock', True) or not (may_write or failover.may_write)(cfg):
        return []
    now = now or time.time()
    events = []
    for item in rule_parked_items(cfg, now):
        try:
            res = handle_parked_rules(cfg, item, now)
        except Exception as e:
            res = {'by': 'none', 'outcome': f'{type(e).__name__}: {e}'[:200]}
        events.append(record(cfg, item, res, now))
    return events


def undo(cfg, entry_at):
    """Re-opens a Tutti-answered parked question and parks the task again. (ok, why)."""
    from . import needs
    log = read_log(cfg)
    entry = next((e for e in reversed(log) if e.get('at') == entry_at and e.get('undoable') and not e.get('undone')), None)
    if not entry:
        return False, f'no undoable unblocker decision at {entry_at}: pick one from the AI assist card'
    ok, why = needs.reopen(cfg, entry['qid'], entry['task'], now=time.time())
    if not ok:
        return ok, why
    entry['undone'] = time.time()
    _save_log(cfg, log)
    telemetry.record('feature', 'unblocker.undo', task=entry['task'])
    return True, why


_running = threading.Lock()
_last = [0.0]
EVERY = 600   # seconds between rounds


def tick_in_background(cfg, clock=time.time):
    """The dashboard's loop: a round every EVERY seconds, in its own thread (a model may take a minute)."""
    if clock() - _last[0] < EVERY or not _running.acquire(blocking=False):
        return False
    _last[0] = clock()

    def go():
        try:
            tick(cfg)
        except Exception as e:  # a model or file problem must not end the dashboard's loop: recorded as a crash
            telemetry.capture_exception(type(e), e, e.__traceback__, where='unblocker')
        finally:
            _running.release()
    threading.Thread(target=go, name='tutti-unblocker', daemon=True).start()
    return True


# ---- the log and the card

def path_of(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


def read_log(cfg):
    try:
        with open(path_of(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save_log(cfg, log):
    tmp = f'{path_of(cfg)}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(log[-KEEP:], f)
    os.replace(tmp, path_of(cfg))


def write_log(cfg, entry):
    _save_log(cfg, read_log(cfg) + [entry])


def view(cfg):
    log = read_log(cfg)
    return {'recent': log[-10:][::-1], 'cleared': sum(1 for e in log if e.get('by') != 'none' and 'left' not in str(e.get('outcome'))),
            'tries': len(log)}
