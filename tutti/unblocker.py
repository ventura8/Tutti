"""The unblocker (backlog t81): every round Tutti looks for work that is blocked or failing and tries to clear it itself,
local first, so only true decisions reach a person.

What it finds (`find`, pure):
- **limit-only**: a task whose recent failures are all usage limits (the worker's account, not the task): its attempts are
  forgiven (resume_from) and its backoff cleared, so a worker with room takes it now.
- **failing**: the same real failure again and again (exit 1, no report, stuck, a check that does not run). A local model
  (tutti/brain.py: the best free one in the household) reads the last failure and writes a diagnosis and a fix for the next
  attempt; the task is queued again with that note, so its next attempt edits a worktree and reruns the task's check. When
  no local model answers, or the task is size L, an online agent diagnoses (brain.ask's fallback, within pacing).
- **parked**: a task waiting on a person's answer. Each round, parked tasks are reviewed oldest first. The local main
  model decides first; when it cannot, the online main agent (current or auto) decides within pacing. Only true owner
  choices stay for a person.
- **conflict**: a result that no longer rebases onto main: queued again from scratch on the current main.
- **idle machines with queued work**: work held back by usage-limit failures is the limit-only case above; any other
  backoff (pacing, a quota window) is left alone on purpose.
- **red-ci**: a failed check run on an open pull request of `ai_assist.unblock_repos`: its failed jobs are rerun once per
  head commit (a second red on the same commit is left to the task's own retry).

Every attempt is logged (`unblock.json`: what, by local, online, person or rule, the decision tier, the question, the
answer when one was picked, and the outcome) for the dashboard. It acts
only on the active main machine (failover lease), at most `MAX_PER_ROUND` items a round, each item at most once per
COOLDOWN; it never kills a process (the autopilot's watchdog ends stuck runs by their exact pid)."""
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone

from . import backlog, config, telemetry

FILE = 'unblock.json'
KEEP = 300
MAX_PER_ROUND = 3
COOLDOWN = 6 * 3600
RECENT = 3
LIMIT_RX = re.compile(r'usage limit|quota (?:reached|exceeded)|RESOURCE_EXHAUSTED|Too Many Requests|(?:HTTP|status|code)[ :]*429\\b', re.I)
REAL_RX = re.compile(r'exited|without a report|stuck:|changed nothing|refused|rc=127|failed', re.I)
REBASE_RX = re.compile(r'does not rebase|rebase conflict|could not apply', re.I)
ACTION_RX = re.compile(r'^\s*ACTION:\s*(\S+)', re.I | re.M)
WHY_RX = re.compile(r'^\s*(?:WHY|DIAGNOSIS):\s*(.+)', re.I | re.M)
FIX_RX = re.compile(r'^\s*FIX:\s*(.+)', re.I | re.M)


# ---- finding (pure)

def recent_whys(t, n=RECENT):
    a = (t.get('attempts') or [])[int(t.get('resume_from') or 0):]
    return [str(x.get('why') or '') for x in a if not x.get('ok')][-n:]


def classify(t, questions=()):
    """The kind of block a task is in, or ''."""
    st = t.get('status')
    if st == 'needs-person':
        return 'parked' if any(q.get('task') == t['id'] and q.get('status') == 'open' for q in questions) else ''
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


def due(t, kind, tried, now):
    """Whether the item may be tried now: its cooldown is over (and a usage-limit one has settled)."""
    if kind != 'limit-only':
        return now - tried >= COOLDOWN
    last = max((float(a.get('at') or 0) for a in t.get('attempts') or []), default=0)
    return now - tried >= LIMIT_COOLDOWN and now - last >= LIMIT_SETTLE


def find(data, now, log=(), limit=MAX_PER_ROUND):
    """[{kind, id, title}] to try this round: never one tried within its cooldown."""
    tried = {e.get('id'): float(e.get('at') or 0) for e in log}
    parked, out = [], []
    for t in data.get('tasks') or []:
        kind = classify(t, data.get('questions') or [])
        if kind and due(t, kind, tried.get(t['id'], 0), now):
            item = {'kind': kind, 'id': t['id'], 'title': str(t.get('title') or '')[:120], 'added': _added_time(t)}
            (parked if kind == 'parked' else out).append(item)
    parked.sort(key=lambda x: (x['added'], x['id']))
    return [*parked, *out][:limit]


def _added_time(task):
    try:
        return float(task.get('added') or 0)
    except (TypeError, ValueError):
        return 0.0


def diagnose_prompt(t):
    a = (t.get('attempts') or [{}])[-1]
    whys = '\n'.join(f'- {w[:200]}' for w in recent_whys(t, 5))
    return ('A coding task keeps failing in an automated loop. Read the failures and say what blocks it.\n'
            f'TASK: {str(t.get("title"))[:400]}\nCHECK: {str(t.get("check") or "")[:300]}\nFILES: {", ".join(t.get("scope") or [])[:300]}\n'
            f'RECENT FAILURES:\n{whys}\nLAST OUTPUT (tail):\n{str(a.get("tail") or "")[-2500:]}\n\n'
            'Answer in exactly three lines:\nACTION: retry (a fix the next attempt can make) | person (only a person can decide)\n'
            'WHY: <one sentence>\nFIX: <one sentence for the next attempt>')


def decide_prompt(t, q):
    return (f'A task is parked with a question. Pick an option only if the history clearly decides it, else answer person.\n'
            f'TASK: {str(t.get("title"))[:300]}\nQUESTION: {q.get("question")}\nWHY: {q.get("why")}\n'
            f'OPTIONS: {", ".join(q.get("options") or [])}\nRECENT FAILURES:\n' + '\n'.join(f'- {w[:200]}' for w in recent_whys(t, 5)) +
            '\n\nAnswer in two lines:\nACTION: <one option exactly, or person>\nWHY: <one sentence>')


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
        needs.ask(cfg, t, f'{item["id"]} keeps failing: {verdict.get("why") or "no diagnosis"}', verdict.get('fix') or 'see its runs',
                  ['retry', 'retry-from-scratch', 'drop'], 'retry', now=now)
        return 'a person decides: asked in the needs queue'


def _questions(cfg):
    from . import needs
    return needs.load(cfg).get('questions') or []


def answer_parked(cfg, item, verdict, now):
    from . import needs
    q = next((x for x in _questions(cfg) if x.get('task') == item['id'] and x.get('status') == 'open'), None)
    if not q:
        return {'outcome': 'no open question any more', 'question': '', 'answer': ''}
    pick = next((o for o in q.get('options') or [] if o.lower() == verdict.get('action')), None)
    if not pick:
        return {'outcome': 'a true decision: left for the person', 'question': q.get('question') or '', 'answer': ''}
    ok, why = needs.answer(cfg, q['id'], pick, role='admin', key='tutti-unblocker', now=now)
    if ok:
        return {'outcome': f'decided "{pick}": {verdict.get("why") or ""}'.strip(), 'question': q.get('question') or '', 'answer': pick}
    return {'outcome': why, 'question': q.get('question') or '', 'answer': pick}


# ---- the round

def task_of(cfg, tid):
    return next((x for x in backlog.load(cfg)['tasks'] if x['id'] == tid), {})


def needs_model(item):
    return item['kind'] in ('failing', 'parked')


def handle(cfg, item, ask, ask_online, now):
    """One item: the model when it needs judgement (local main first, online main next, then person)."""
    by, tier, verdict = 'rule', 'rule', {}
    if needs_model(item):
        answer, who = judge(cfg, item, ask, ask_online)
        if not answer:
            return {'by': 'none', 'tier': 'none', 'outcome': f'no model answered ({who}): left as it is', 'question': '',
                    'answer': '', 'attempts': len(task_of(cfg, item['id']).get('attempts') or []), 'last_failure': _last_failure(cfg, item)}
        verdict = parse(answer)
        by = 'online' if who in _cli_workers(cfg) else 'local'
        tier = 'online-main' if by == 'online' else 'local-main'
    if item['kind'] == 'parked':
        parked = answer_parked(cfg, item, verdict, now)
        if not parked['answer'] and by != 'rule':
            by, tier = 'person', 'person'
        return {'by': by, 'tier': tier, 'outcome': parked['outcome'], 'why': verdict.get('why', ''),
                'question': parked['question'], 'answer': parked['answer'],
                'attempts': len(task_of(cfg, item['id']).get('attempts') or []), 'last_failure': _last_failure(cfg, item)}
    outcome = apply(cfg, item, verdict, now)
    return {'by': by, 'tier': tier, 'outcome': outcome, 'why': verdict.get('why', ''), 'question': '', 'answer': '',
            'attempts': len(task_of(cfg, item['id']).get('attempts') or []), 'last_failure': _last_failure(cfg, item)}


def judge(cfg, item, ask, ask_online):
    """(answer, who): local main first; online main only when local did not answer."""
    t = task_of(cfg, item['id'])
    if item['kind'] == 'failing':
        prompt = diagnose_prompt(t)
    else:
        q = next((x for x in _questions(cfg) if x.get('task') == item['id'] and x.get('status') == 'open'), {})
        prompt = decide_prompt(t, q)
    answer, who = ask(cfg, prompt, 'unblock', online=False)
    if not answer:
        answer, who = ask_online(cfg, prompt)
    return answer, who


def _last_failure(cfg, item):
    tries = task_of(cfg, item['id']).get('attempts') or []
    failed = [a for a in tries if not a.get('ok')]
    return str((failed[-1] if failed else {}).get('why') or '')[:200]


def pick_online_main(cfg, available=None, room=None):
    """(worker name, args) for the online main agent with room (current/auto order), else (None, why)."""
    from . import brain, conduct, review, workers
    name = online_main_worker(cfg)
    w = (cfg.get('workers') or {}).get(name) or {}
    if not name or not w:
        return None, 'no online main agent has room now'
    available = available or (lambda worker: workers.available(worker, timeout=1))
    if room is None:
        from . import pacing, state
        now = datetime.now(timezone.utc)
        try:
            entries = pacing.read_log(state.usage_log_path(cfg))
        except (OSError, ValueError):
            entries = []
        def room(worker_name):
            return review.worker_room(cfg, worker_name, entries, now)
    args, why = brain.online_room(w, name, available, room)
    return (name, args) if args is not None else (None, f'main agent {name}: {why}')


def online_main_worker(cfg):
    """The first CLI main-agent worker with room in current/auto order, else ''."""
    from . import conduct
    states = conduct.account_states(cfg) if isinstance(cfg.get('accounts'), dict) else []
    check_room = bool(states) and isinstance(cfg.get('accounts'), dict)
    names = conduct.order(conduct.current(cfg), (cfg.get('main') or {}).get('fallback') or [])
    for name in names:
        w = (cfg.get('workers') or {}).get(name) or {}
        if w.get('kind') != 'cli':
            continue
        if check_room and conduct.out_reason(cfg, name, states):
            continue
        return name
    return ''


def ask_online_main(cfg, prompt, ask_online_fn=None, pick=None):
    """The unblocker's online tier: the current/auto main agent, within pacing."""
    from . import brain
    return (ask_online_fn or brain.ask_online)(cfg, prompt, pick=pick or pick_online_main)


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


def tick(cfg, now=None, ask=None, ask_online=None, run=subprocess.run, may_write=None):
    """One round. Returns event lines. Nothing on a machine that is not the active main (the failover lease)."""
    from . import aiassist, brain, failover
    if not aiassist.settings(cfg).get('unblock', True) or not (may_write or failover.may_write)(cfg):
        return []
    now = now or time.time()
    ask = ask or brain.ask
    ask_online = ask_online or ask_online_main
    log = read_log(cfg)
    items = find(backlog.load(cfg) | {'questions': _questions(cfg)}, now, log)
    items += red_ci(cfg, run, log)[:max(0, MAX_PER_ROUND - len(items))]
    events = []
    for item in items:
        try:
            res = {'by': 'rule', 'tier': 'rule', 'outcome': rerun(item, run), 'question': '', 'answer': '',
                   'attempts': 0, 'last_failure': ''} if item['kind'] == 'red-ci' else handle(cfg, item, ask, ask_online, now)
        except Exception as e:  # one item's surprise must not end the round: logged, the next items go on
            res = {'by': 'none', 'tier': 'none', 'outcome': f'{type(e).__name__}: {e}'[:200], 'question': '', 'answer': '',
                   'attempts': 0, 'last_failure': ''}
        entry = {'at': now, **{k: item[k] for k in ('kind', 'id', 'title')}, **res}
        write_log(cfg, entry)
        telemetry.record('feature', 'unblocker.try', block=item['kind'], by=res['by'])
        events.append(f'unblocker: {item["id"]} ({item["kind"]}, {res["tier"]}): {res["outcome"]}')
    return events


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


def write_log(cfg, entry):
    log = (read_log(cfg) + [entry])[-KEEP:]
    tmp = f'{path_of(cfg)}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(log, f)
    os.replace(tmp, path_of(cfg))


def view(cfg):
    log = read_log(cfg)
    return {'recent': log[-10:][::-1], 'cleared': sum(1 for e in log if e.get('by') != 'none' and 'left' not in str(e.get('outcome'))),
            'tries': len(log)}
