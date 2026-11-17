"""Main-session continuity (docs/CONDUCT.md, *When a session runs out of tokens*): a task a main session claimed (a Claude Code
window, a Codex, Cursor or agy session connected through `tutti mcp`) does not stall when that session hits its usage limit.

- **State.** Each claimed task carries a living handoff note (`task['handoff']`: done, next, blockers, branch, worktree, how
  to verify), kept with `tutti handoff --task <id> ...` or the MCP tool `task_handoff`. Each session's liveness and limit
  events live in `<state>/sessions.json`: every MCP call that names the session (`who`) is a heartbeat, `session_heartbeat`
  says which account it spends and where its transcript is, and `usage_report` with `state: spent|panic` is a limit event.
- **Detection.** A claimed task is *orphaned by quota* when its session reported a limit and has been quiet since, when the
  session's transcript ends with a usage-limit message, or when the session has been silent for `main.continuity.
  silent_minutes` while its account is in panic or spent (the pacing readings). Silence alone never orphans a task.
- **Takeover.** Tutti runs `tutti conduct` in the task's worktree with the next main agent that has room
  (`main.continuity.takeover_order`, else the main order: tutti/mainorder.py), its goal made of the task, the note and the
  claim and lease context. The claim moves to `tutti-conduct:<agent>`; the session's live leases for the task move with it
  (an expired one is dropped, so it is claimed again). The owner is told (the chat's "From Tutti") and every step is an event
  (tutti/events.py) the Needs attention list reads; a conductor's `NEEDS HUMAN:` note becomes a `needs-attention` event.
- **Hand-back.** When the original session claims the task again and its account has room, the conductor is asked to stop
  (its stop file: it writes its handoff and ends), the claim and the leases go back, and the claim's answer is the note.
  One owner at a time: while the account is still out the claim is refused with the note, and nobody else may claim it."""
import contextlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import config, durable, routing

try:
    import fcntl
except ImportError:   # Windows: no flock
    fcntl = None

FILE = 'sessions.json'
CONDUCTOR = 'tutti-conduct:'
LISTS = ('done', 'next', 'blockers')
FIELDS = ('branch', 'worktree', 'verify')
SECTIONS = LISTS + FIELDS
KEEP_ITEMS = 30
TRANSCRIPT_BYTES = 64_000
NEEDS_HUMAN = 'NEEDS HUMAN'
DEFAULTS = {'enabled': True, 'silent_minutes': 20, 'takeover_order': [], 'limit_grace_minutes': 2}


class ContinuityError(Exception):
    """A handoff or hand-back cannot be done; the message says why and what to do."""


def settings(cfg):
    return {**DEFAULTS, **(((cfg.get('main') or {}).get('continuity')) or {})}


def errors(m):
    """Config errors of main.continuity (a mapping in main)."""
    c = m.get('continuity')
    if c is None:
        return []
    if not isinstance(c, dict):
        return [f'main.continuity is {c!r}: a mapping such as {{enabled: true, silent_minutes: 20, takeover_order: [agy]}}']
    errs = []
    if not isinstance(c.get('enabled', True), bool):
        errs.append(f'main.continuity.enabled is {c["enabled"]!r}: true or false')
    for key in ('silent_minutes', 'limit_grace_minutes'):
        v = c.get(key, DEFAULTS[key])
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            errs.append(f'main.continuity.{key} is {v!r}: a number of minutes, 0 or more')
    order = c.get('takeover_order', [])
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        errs.append(f'main.continuity.takeover_order is {order!r}: a list of workers, e.g. [agy, cursor]')
    return errs


def is_conductor(who):
    return str(who or '').startswith(CONDUCTOR)


# ---- sessions: liveness and limit events


def sessions_path(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


def load_sessions(cfg):
    try:
        with open(sessions_path(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@contextlib.contextmanager
def sessions_locked(cfg):
    p = sessions_path(cfg)
    with open(p + '.lock', 'w', encoding='utf8') as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        data = load_sessions(cfg)
        yield data
        durable.dump_json(p, data, indent=1)


def beat(cfg, who, account=None, transcript=None, now=None):
    """A sign of life of a session (any MCP call that names it); `account` and `transcript` are kept when given."""
    if not who or is_conductor(who):
        return None
    with sessions_locked(cfg) as data:
        s = data.setdefault(who, {})
        s['seen'] = now or time.time()
        if account:
            s['account'] = account
        if transcript:
            s['transcript'] = os.path.expanduser(transcript)
        return dict(s)


def limit_event(cfg, who, account, kind, now=None):
    """The session saw its account spent or in panic (`usage_report` with a state)."""
    now = now or time.time()
    with sessions_locked(cfg) as data:
        s = data.setdefault(who, {'seen': now})
        s['limit'] = {'at': now, 'kind': kind, 'account': account}
        s['account'] = s.get('account') or account
        return dict(s)


def transcript_limit(path, now=None):
    """Whether the end of a session's transcript (a log Tutti can read) says it hit a usage limit."""
    from . import readers
    try:
        with open(path, 'rb') as f:
            f.seek(max(0, os.path.getsize(path) - TRANSCRIPT_BYTES))
            lines = f.read().decode('utf8', 'replace').splitlines()[-40:]
    except OSError:
        return False
    return bool(readers.limit_hits(lines, now or datetime.now(timezone.utc)))


# ---- the living handoff note of a task


def set_note(task, section, text, who='', now=None):
    """Adds `text` to a list section (done, next, blockers) or sets a field (branch, worktree, verify). Returns the note."""
    if section not in SECTIONS:
        raise ContinuityError(f'handoff section "{section}": use one of {", ".join(SECTIONS)}')
    text = ' '.join(str(text or '').split())
    if not text:
        raise ContinuityError(f'handoff {section} needs the text: tutti handoff {section} --task {task["id"]} "<text>"')
    h = task.setdefault('handoff', {})
    if section in LISTS:
        h[section] = [*(h.get(section) or []), text[:500]][-KEEP_ITEMS:]
    else:
        h[section] = text[:500]
    h.update(updated=now or time.time(), by=who or h.get('by') or '')
    return h


def note_text(task):
    """The task's handoff note as Markdown (what a session that takes it over or back reads first)."""
    h = task.get('handoff') or {}
    lines = [f'Task {task["id"]} ({task.get("repo")}): {task.get("title", "")}']
    if task.get('check'):
        lines.append(f'Acceptance check: {task["check"]}')
    for f in FIELDS:
        if h.get(f):
            lines.append(f'{f.capitalize()}: {h[f]}')
    for s in LISTS:
        lines += ['', f'### {s.capitalize()}', ''] + ([f'- {x}' for x in h.get(s) or []] or ['(nothing written)'])
    if h.get('updated'):
        lines += ['', f'(last written by {h.get("by") or "?"} at {time.strftime("%Y-%m-%d %H:%M", time.localtime(h["updated"]))})']
    return '\n'.join(lines) + '\n'


# ---- detection


def by_name(states):
    return {s.get('name'): s for s in states or []}


def limit_reason(who, session, now, grace):
    lim = session.get('limit') or {}
    if not lim:
        return ''
    at = float(lim.get('at') or 0)
    quiet_since = float(session.get('seen') or 0) <= at + 60   # it said nothing after its limit report
    if quiet_since and now - at >= grace:
        return f'{who} reported {lim.get("account") or "its account"} {lim.get("kind") or "spent"} and has been quiet since'
    return ''


def orphan_reason(task, session, states, now, s, transcript_hit=False):
    """Why a claimed task is orphaned by quota, or ''. `states`: {account: pacing state}."""
    if task.get('status') != 'claimed' or is_conductor(task.get('claimed_by')):
        return ''
    who, session = task.get('claimed_by') or '?', session or {}
    seen = float(session.get('seen') or task.get('claimed_at') or now)
    grace = float(s['limit_grace_minutes']) * 60
    why = limit_reason(who, session, now, grace)
    if why:
        return why
    if transcript_hit and now - seen >= grace:
        return f'{who}\'s transcript ends with a usage-limit message'
    account = session.get('account') or task.get('claim_account')
    unfit = routing.account_unfit(states[account]) if account in states else ''
    silent = now - seen
    if unfit and silent >= float(s['silent_minutes']) * 60:
        return f'{who} has been silent {int(silent // 60)} min while {account} is out ({unfit})'
    return ''


# ---- takeover


def pick_agent(cfg, states_list, out_fn=None):
    """(the agent that takes over or None, the rest of the order, the skipped ones with why)."""
    from . import conduct, mainorder
    s = settings(cfg)
    names = [n for n in (s['takeover_order'] or mainorder.order(cfg)) if n in cfg['workers']]
    out_fn = out_fn or (lambda n: conduct.out_reason(cfg, n, states_list))
    agent, skipped = conduct.next_agent(names, [], out_fn)
    rest = [n for n in names if n != agent]
    return agent, rest, skipped


def workdir(cfg, task):
    """The folder the takeover works in: the note's worktree when it exists, else the repo's folder; '' when neither."""
    w = config.expand((task.get('handoff') or {}).get('worktree') or '')
    if w and os.path.isdir(w):
        return w
    repo = ((cfg.get('autopilot') or {}).get('repos') or {}).get(task.get('repo')) or {}
    p = config.expand(repo.get('path') or '')
    return p if p and os.path.isdir(p) else ''


def move_leases(leases, frm, to, task_id, now):
    """Moves `frm`'s live leases for the task (or with no task) to `to`; expired ones are dropped (claimed again when needed).
    Returns (moved, dropped) as `kind name` lines."""
    moved, dropped = [], []
    for key, held in list(leases.items()):
        if held.get('who') != frm or held.get('task') not in (task_id, ''):
            continue
        if held['until'] <= now:
            leases.pop(key)
            dropped.append(f'{held["kind"]} {held["name"]}')
        else:
            held.update(who=to, task=task_id, moved_from=frm)
            moved.append(f'{held["kind"]} {held["name"]}')
    return moved, dropped


def goal_text(task, frm, agent, reason, moved, dropped):
    lines = [f'Continue backlog task {task["id"]} of {task.get("repo")}: {task.get("title", "")}', '',
             f'Session {frm} claimed it and stopped: {reason}. The claim is yours now ({CONDUCTOR}{agent}); you work in this '
             'folder, on its branch. Read the handoff note below first, check the tree, then continue from "Next".',
             f'Leases moved to you: {", ".join(moved) or "none"}.' + (f' Expired, claim again before use: {", ".join(dropped)}.'
                                                                       if dropped else ''),
             f'Keep the note current with `tutti handoff done|next|blockers "<text>"` (it lands on task {task["id"]}): '
             f'{frm} may take the task back when its account resets and continues from your note.',
             'Ask-first actions (push, tag, merge, deploy, payments, settings, deleting data) stay with a person: write '
             f'`tutti handoff blockers "{NEEDS_HUMAN}: <the action and why>"` and end with BLOCKED.', '', '## Handoff note', '',
             note_text(task)]
    return '\n'.join(lines)


def launch(cfg, task, agent, rest, cwd, goal, popen=subprocess.Popen, now=None):
    """Starts `tutti conduct` detached for the task. Returns (pid, log path, stop file)."""
    from . import remote
    d = os.path.join(config.state_dir(cfg), 'continuity')
    os.makedirs(d, exist_ok=True)
    base = os.path.join(d, f'{task["id"]}-{int(now or time.time())}')
    stop = base + '.stop'
    env = dict(os.environ, TUTTI_TASK=task['id'], TUTTI_SESSION=CONDUCTOR + agent, TUTTI_STOP_FILE=stop)
    argv = [sys.executable, remote.BIN, '--config', cfg['_path'], 'conduct', '--main', agent, '--fallback', ','.join(rest),
            '--dir', cwd, goal]
    with open(base + '.log', 'w', encoding='utf8') as log:
        p = popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return p.pid, base + '.log', stop


def notify(cfg, text):
    """Tells the owner in the chat's "From Tutti" conversation (the needs queue's chat path). Never fails the caller."""
    from . import chat, needs
    try:
        store, sub = chat.Store(config.state_dir(cfg)), needs.chat_sub(needs.OWNER)
        conv = next((c for c in store.load(sub)['conversations'] if c.get('title') == 'From Tutti'), None)
        cid = (conv or store.create(sub, title='From Tutti'))['id']
        store.put_message(sub, cid, {'id': chat.new_id('m'), 'role': 'assistant', 'text': text, 'at': chat.now_iso(),
                                     'status': 'done', 'model': 'tutti', 'cards': []})
    except (OSError, ValueError, KeyError):
        return False
    return True


def mark_orphaned(cfg, t, reason, why_waiting, now):
    c = t.get('continuity') or {}
    first = c.get('state') != 'orphaned'
    t['continuity'] = {**c, 'state': 'orphaned', 'reason': reason, 'waiting': why_waiting, 'at': c.get('at') or now,
                       'from': t.get('claimed_by')}
    return first


def take_over(cfg, task_id, reason, states_list, now=None, popen=subprocess.Popen, out_fn=None, tell=None):
    """Moves an orphaned task to the next main agent with room. Returns a line for the log."""
    from . import backlog, events, intake
    now = now or time.time()
    tell = tell or notify
    with backlog.locked(cfg) as data:
        t = backlog.find(data, task_id)
        if t.get('status') != 'claimed' or is_conductor(t.get('claimed_by')):
            return f'{task_id}: no longer orphaned ({t.get("status")}, {t.get("claimed_by")})'
        frm = t['claimed_by']
        agent, rest, skipped = pick_agent(cfg, states_list, out_fn)
        cwd = workdir(cfg, t) if agent else ''
        if not agent or not cwd:
            why = ('no main agent has room: ' + '; '.join(skipped)) if not agent else \
                'no worktree to continue in (the note names none that exists, and the repo has no folder here)'
            if mark_orphaned(cfg, t, reason, why, now):
                events.emit(cfg, 'continuity.orphaned', task=task_id, session=frm, reason=reason, waiting=why)
                tell(cfg, f'Task {task_id} stalled: {reason}. Tutti cannot take it over yet: {why}.')
            return f'{task_id}: orphaned ({reason}); waiting: {why}'
        with intake.leases_locked(cfg) as leases:
            moved, dropped = move_leases(leases, frm, CONDUCTOR + agent, task_id, now)
        pid, log, stop = launch(cfg, t, agent, rest, cwd, goal_text(t, frm, agent, reason, moved, dropped), popen, now)
        session = load_sessions(cfg).get(frm) or {}
        t.update(claimed_by=CONDUCTOR + agent, claimed_at=now)
        t['continuity'] = {'state': 'taken-over', 'from': frm, 'from_account': session.get('account') or t.get('claim_account'),
                           'agent': agent, 'reason': reason, 'at': now, 'pid': pid, 'log': log, 'stop': stop, 'cwd': cwd,
                           'leases': moved, 'dropped': dropped, 'asked': []}
        t['notes'] = [*t.get('notes', []), f'taken over by {agent} from {frm}: {reason}']
    events.emit(cfg, 'continuity.takeover', task=task_id, session=frm, agent=agent, reason=reason, leases=moved)
    tell(cfg, f'Task {task_id} continues with {agent}: {frm} stopped ({reason}). It works in {cwd}; {frm} can take it back '
              'when its account resets (backlog_claim).')
    return f'{task_id}: taken over by {agent} from {frm} ({reason})'


def follow(cfg, t, alive, now):
    """A taken-over task: its conductor's NEEDS HUMAN notes become needs-attention events; a gone conductor ends the takeover.
    Returns (events to emit, a log line or None); `t` is changed in place."""
    c = t['continuity']
    h = t.get('handoff') or {}
    out = []
    for item in [*(h.get('blockers') or []), *(h.get('next') or [])]:
        if NEEDS_HUMAN in item and item not in c.setdefault('asked', []):
            c['asked'].append(item)
            out.append(('needs-attention', {'task': t['id'], 'title': t.get('title', ''), 'agent': c.get('agent'), 'ask': item}))
    if not alive(c.get('pid')):
        c.update(state='ended', ended_at=now)
        out.append(('continuity.ended', {'task': t['id'], 'agent': c.get('agent'), 'session': c.get('from')}))
        return out, f'{t["id"]}: {c.get("agent")}\'s takeover ended; {c.get("from")} or anyone may claim it again'
    return out, None


def follow_all(cfg, alive, now):
    from . import backlog, events
    lines, emitted = [], []
    with backlog.locked(cfg) as data:
        for t in data['tasks']:
            if (t.get('continuity') or {}).get('state') == 'taken-over':
                evs, line = follow(cfg, t, alive, now)
                emitted += evs
                lines += [line] if line else []
    for kind, payload in emitted:
        events.emit(cfg, kind, **payload)
    return lines


def orphans(cfg, states_list, now, sessions=None, read_transcripts=True):
    """[(task id, reason)] of the claimed tasks orphaned by quota now."""
    from . import backlog
    s, states, sessions = settings(cfg), by_name(states_list), sessions if sessions is not None else load_sessions(cfg)
    out = []
    for t in backlog.load(cfg)['tasks']:
        if t.get('status') != 'claimed' or is_conductor(t.get('claimed_by')):
            continue
        sess = sessions.get(t.get('claimed_by')) or {}
        hit = bool(read_transcripts and sess.get('transcript') and transcript_limit(sess['transcript']))
        why = orphan_reason(t, sess, states, now, s, hit)
        if why:
            out.append((t['id'], why))
    return out


def tick(cfg, states_list=None, now=None, popen=subprocess.Popen, alive=None, dry=False, tell=None):
    """One round: follow the takeovers, then take over every orphaned task (with `dry`, only list them). Returns log lines."""
    from . import conduct
    if not settings(cfg)['enabled']:
        return []
    now = now or time.time()
    states_list = conduct.account_states(cfg) if states_list is None else states_list
    lines = [] if dry else follow_all(cfg, alive or conduct.pid_alive, now)
    for task_id, why in orphans(cfg, states_list, now):
        lines.append(f'{task_id}: orphaned ({why})' if dry else take_over(cfg, task_id, why, states_list, now, popen, tell=tell))
    return lines


# ---- hand-back


def hand_back(cfg, task, who, states_list, now=None, tell=None):
    """The original session claims its task again. Returns the note to continue from, or None when the task was never taken
    over. Raises ContinuityError while the conductor still owns it (the session's account is still out, or another session
    asks); the error carries the note."""
    from . import events, intake
    c = task.get('continuity') or {}
    state = c.get('state')
    if state not in ('taken-over', 'ended'):
        return None
    if state == 'taken-over':
        if who != c.get('from'):
            raise ContinuityError(f'{task["id"]} is taken over by {task.get("claimed_by")} for {c.get("from")}: only {c.get("from")} '
                                  'takes it back (or wait until the takeover ends)')
        acct = c.get('from_account')
        st = by_name(states_list).get(acct)
        why = routing.account_unfit(st) if st else ''
        if why:
            raise ContinuityError(f'{task["id"]} stays with {c.get("agent")} while {acct} is out ({why}); its note so far:\n\n'
                                  + note_text(task))
        with open(c['stop'], 'w', encoding='utf8') as f:   # the conductor sees it at its next check, writes its handoff, ends
            f.write(who)
    now = now or time.time()
    with intake.leases_locked(cfg) as leases:
        back, _gone = move_leases(leases, task.get('claimed_by'), who, task['id'], now)
    c.update(state='handed-back', back_at=now, to=who, leases_back=back)
    task['claimed_by'] = who   # the claim itself is completed by the caller (`tutti backlog claim`)
    events.emit(cfg, 'continuity.handback', task=task['id'], session=who, agent=c.get('agent'))
    (tell or notify)(cfg, f'Task {task["id"]} is back with {who}; {c.get("agent")} stops and leaves its note.')
    return note_text(task)


# ---- the dashboard


def view(cfg, states_list, now=None):
    """The Main agent panel's rows: the tasks taken over or orphaned, newest first, and the orphans not acted on yet."""
    from . import backlog
    now = now or time.time()
    rows, tasks = [], backlog.load(cfg)['tasks']
    titles = {t['id']: t.get('title', '')[:120] for t in tasks}
    for t in tasks:
        c = t.get('continuity') or {}
        if c.get('state') in ('orphaned', 'taken-over', 'ended') and t.get('status') == 'claimed':
            rows.append({'id': t['id'], 'title': t.get('title', '')[:120], 'state': c['state'], 'from': c.get('from'),
                         'agent': c.get('agent'), 'reason': c.get('reason'), 'waiting': c.get('waiting'), 'at': c.get('at'),
                         'needs': list(c.get('asked') or [])})
    seen = {r['id'] for r in rows}
    for tid, why in orphans(cfg, states_list, now, read_transcripts=False):
        if tid not in seen:
            rows.append({'id': tid, 'title': titles.get(tid, ''), 'state': 'orphaned', 'reason': why, 'from': None, 'agent': None,
                         'at': now, 'needs': []})
    rows.sort(key=lambda r: -(r.get('at') or 0))
    return {'enabled': settings(cfg)['enabled'], 'tasks': rows}
