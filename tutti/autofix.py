"""Review and security auto-fix (docs/AUTOFIX.md): how far Tutti goes by itself with the tasks that reviews and security
reports become (tutti/depbots.py reads them), and what is left for a person.

The admin's one setting, `autofix.level`:

- `suggest`: a new review or security task waits as a suggestion (`tutti backlog accept <id>`); nothing is fixed by itself.
- `fix` (the default, Tutti's behaviour before this setting): the task is queued, fixed and landed by its repo's `land`
  policy (a pull request a person merges, unless the repo says merge). Never released by itself.
- `full_until_review`: fixed, landed as a pull request, checked and reviewed; then it stops for the person's final review
  (the Needs attention tab, or `tutti autofix approve <id>`), and on approval it is merged and the repo's release runs.
- `full`: fixed, landed and merged by itself (a change to its own gates still stops at its pull request), then released.

A release is the repo's `autopilot.repos.<repo>.release` command, else its project's `self_improve.release_command`; one
release per repo at a time, the fixes that land meanwhile ride on the next one. Everything here is pure except `approve`,
`start_release` and `tick`, which take the backlog's lock (the one lock for backlog.json and autofix.json)."""
import os
import re
import subprocess
import time

from . import backlog, config, github, telemetry

FILE = 'autofix.json'
LEVELS = ('suggest', 'fix', 'full_until_review', 'full')
BOT_LEVELS = ('off',) + LEVELS   # the bots' issues and pull requests can also be left alone
SOURCES = ('security', 'review')   # a task's source prefix (security:<scanners>, review:<reviewer>)
BOT_SOURCES = ('depbot', 'botpr', 'botissue')   # a bot's pull request (a dependency bump or another) or issue
# the reviewers whose review threads become tasks (logins, any case, with or without [bot]); people with write access to the
# repository count too while `humans` is on; nobody else (a stranger's comment on a public repository is not work)
REVIEWERS = ['coderabbitai[bot]', 'greptile-apps[bot]', 'copilot-pull-request-reviewer[bot]', 'Copilot', 'cursor[bot]']
# whose issues and pull requests are bots' work besides any `[bot]` or `app/` login (a refresh or release bot under a
# plain account name goes here)
BOT_AUTHORS = ['dependabot[bot]', 'renovate[bot]', 'cursor[bot]', 'github-actions[bot]', 'sentry-io[bot]']
DEFAULTS = {'level': 'fix', 'reviews': True, 'humans': True, 'reviewers': REVIEWERS, 'triage_scout': '', 'notify': True,
            'bots_level': 'fix', 'bot_authors': BOT_AUTHORS}
TRUSTED = ('OWNER', 'MEMBER', 'COLLABORATOR')


def settings(cfg):
    return {**DEFAULTS, **(cfg.get('autofix') or {})}


def level(cfg):
    """The security and review level."""
    lvl = settings(cfg)['level']
    return lvl if lvl in LEVELS else 'fix'


def bots_level(cfg):
    """The level of the bots' issues and pull requests: off, or one of LEVELS (default fix)."""
    lvl = settings(cfg)['bots_level']
    return lvl if lvl in BOT_LEVELS else 'fix'


def kind_of(task):
    """'security' for a review or security task, 'bots' for a bot's issue or pull request, else None."""
    head = (task.get('source') or '').split(':', 1)[0]
    return 'security' if head in SOURCES else 'bots' if head in BOT_SOURCES else None


def level_for(cfg, task):
    """The level that governs this task (its kind's setting), None for the others."""
    kind = kind_of(task)
    return level(cfg) if kind == 'security' else bots_level(cfg) if kind == 'bots' else None


def is_bot(login, cfg=None):
    """A bot's login: `x[bot]`, `app/x`, or one named in autofix.bot_authors."""
    from .depbots import norm
    raw = (login or '').lower()
    if raw.endswith('[bot]') or raw.startswith('app/'):
        return True
    return norm(login) in {norm(a) for a in settings(cfg or {})['bot_authors']}


def errors(cfg):
    s = cfg.get('autofix')
    if s is None:
        return []
    if not isinstance(s, dict):
        return [f'autofix is {s!r}: a mapping such as {{level: fix}} (docs/AUTOFIX.md)']
    errs = [f'autofix.{k} is not a setting: use {", ".join(DEFAULTS)}' for k in s if k not in DEFAULTS]
    if 'level' in s and s['level'] not in LEVELS:
        errs.append(f'autofix.level is {s["level"]!r}: one of {", ".join(LEVELS)} (docs/AUTOFIX.md)')
    if 'bots_level' in s and s['bots_level'] not in BOT_LEVELS:
        errs.append(f'autofix.bots_level is {s["bots_level"]!r}: one of {", ".join(BOT_LEVELS)} (docs/AUTOFIX.md)')
    if 'bot_authors' in s and (not isinstance(s['bot_authors'], list) or not all(isinstance(x, str) and x for x in s['bot_authors'])):
        errs.append(f'autofix.bot_authors is {s["bot_authors"]!r}: a list of GitHub logins such as renovate[bot]')
    for k in ('reviews', 'humans', 'notify'):
        if k in s and not isinstance(s[k], bool):
            errs.append(f'autofix.{k} is {s[k]!r}: true or false')
    if 'reviewers' in s and (not isinstance(s['reviewers'], list) or not all(isinstance(x, str) and x for x in s['reviewers'])):
        errs.append(f'autofix.reviewers is {s["reviewers"]!r}: a list of GitHub logins such as coderabbitai[bot]')
    scout = s.get('triage_scout')
    if scout and scout not in (cfg.get('scouts') or {}):
        errs.append(f'autofix.triage_scout names "{scout}", which is not under scouts: (docs/SCOUTS.md), or leave it empty')
    return errs


def is_autofix(task):
    """A task this module governs: a review or security task, or a bot's issue or pull request."""
    return kind_of(task) is not None


def new_status(cfg, task=None):
    """The status a new task of ours starts in: a suggestion at `suggest` (and for a bot's work at `off`), else queued."""
    lvl = level_for(cfg, task) if task is not None else level(cfg)
    return 'candidate' if lvl in ('suggest', 'off') else 'queued'


def land_policy(cfg, task, repo_land):
    """The land policy of one result: the repo's own for other tasks and at suggest/fix; at full_until_review a pull request
    for the person (never a merge by itself); at full a merge. `branch` (never auto-reviewed) always stays."""
    if not is_autofix(task) or repo_land == 'branch':
        return repo_land
    return {'full_until_review': 'pr', 'full': 'merge'}.get(level_for(cfg, task), repo_land)


def release_command(cfg, repo):
    """The repo's release command: autopilot.repos.<repo>.release, else its project's self_improve.release_command."""
    own = (((cfg.get('autopilot') or {}).get('repos') or {}).get(repo) or {}).get('release')
    if own:
        return own
    from . import selfimprove
    pol = selfimprove.policy_of_repo(cfg, repo)
    return (pol or {}).get('release_command') or ''


# ---- state (<state>/autofix.json, under the backlog's lock)

def path_of(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


def read_state(cfg):
    return telemetry.read_json(path_of(cfg), {})


def write_state(cfg, st):
    telemetry.write_json(path_of(cfg), st)


def want_release(cfg, st, t, now):
    """A merged fix of a repo with a release command waits for the next release. Returns an event line or ''."""
    if not release_command(cfg, t['repo']):
        return ''
    pend = st.setdefault('pending', {}).setdefault(t['repo'], [])
    if t['id'] not in pend:
        pend.append(t['id'])
    return f'autofix: {t["id"]} merged; the {t["repo"]} release follows'


def after_land(cfg, t, now=None):
    """Under the backlog's lock, after a result of a review or security task landed (`fill.record`): at full_until_review
    its pull request waits for the person's final review; at full a merged one waits for the release. Returns event lines."""
    now = now or time.time()
    if not is_autofix(t) or t['status'] != 'done' or not t.get('pr'):
        return []
    lvl = level_for(cfg, t)
    if lvl == 'full_until_review' and not t.get('merged'):
        t['final_review'] = {'state': 'waiting', 'pr': t['pr'], 'head': t.get('head') or '', 'at': now}
        return [f'autofix: {t["id"]} is ready for your final review: {t["pr"]}']
    if t.get('merged'):
        lines = [mark_close(t, f'resolved by {t["pr"]}')]
        if lvl in ('full', 'full_until_review'):
            st = read_state(cfg)
            lines.append(want_release(cfg, st, t, now))
            write_state(cfg, st)
        return [x for x in lines if x]
    return []


# ---- the source (a bot's issue or pull request) closed once its work is resolved

def source_url(t):
    """The GitHub issue or pull request a bot task came from, else ''."""
    ref = t.get('depbot_ref') or ''
    return ref if kind_of(t) == 'bots' and ref.startswith('https://') and re.search(r'/(issues|pull)/\d+$', ref) else ''


def mark_close(t, reason):
    """The task's source waits to be closed with `reason` (closed by tick, outside the lock). Returns an event line or ''."""
    url = source_url(t)
    if not url or t.get('source_closed'):
        return ''
    t['close_source'] = reason[:300]
    return f'autofix: {t["id"]}: {url} will be closed ({reason[:120]})'


def close_one(url, reason, gh=github.run):
    """(closed?, why): the issue or pull request closed with a comment that says why. A closed one counts as closed."""
    kind = 'pr' if '/pull/' in url else 'issue'
    body = f'Closed by Tutti: {reason}'
    done = gh(['gh', kind, 'close', url, '--comment', body], capture_output=True, text=True)
    if done.returncode == 0:
        return True, ''
    state = gh(['gh', kind, 'view', url, '--json', 'state', '--jq', '.state'], capture_output=True, text=True)
    if (state.stdout or '').strip() in ('CLOSED', 'MERGED'):
        return True, ''
    return False, (done.stderr or done.stdout or 'no output').strip()[:200]


def close_round(cfg, gh=github.run):
    """Every task's source that waits to be closed, closed (network outside the lock, the result recorded under it)."""
    todo = [(t['id'], source_url(t), t['close_source']) for t in backlog.load(cfg)['tasks']
            if t.get('close_source') and not t.get('source_closed') and source_url(t)]
    events, results = [], {}
    for tid, url, reason in todo:
        results[tid] = close_one(url, reason, gh)
        ok, why = results[tid]
        events.append(f'autofix: closed {url} ({reason[:80]})' if ok else f'autofix: closing {url} failed ({why}); tried again later')
    if results:
        with backlog.locked(cfg) as data:
            for t in data['tasks']:
                ok, why = results.get(t['id'], (None, ''))
                if ok:
                    t['source_closed'] = True
                    t.pop('close_source_error', None)
                elif ok is False:
                    t['close_source_error'] = why
            backlog.save(cfg, data)
    return events


def close_dupes(cfg, gh=github.run):
    """The bots' duplicate issues the intake found (state `to_close`), closed with a comment naming the one kept."""
    with backlog.locked(cfg):
        todo = dict(read_state(cfg).get('to_close') or {})
    events, closed = [], []
    for url, reason in todo.items():
        ok, why = close_one(url, reason, gh)
        closed += [url] if ok else []
        events.append(f'autofix: closed {url} ({reason[:80]})' if ok else f'autofix: closing {url} failed ({why}); tried again later')
    if closed:
        with backlog.locked(cfg):
            st = read_state(cfg)
            st['to_close'] = {k: v for k, v in (st.get('to_close') or {}).items() if k not in closed}
            write_state(cfg, st)
    return events


def green_round(cfg, gh=github.run, now=None):
    """A bot's green pull request needs no worker: at full it is merged here (pinned to the commit that was green), at
    full_until_review it waits for the person's final review. Fix and suggest leave it to a worker (or a person)."""
    now = now or time.time()
    lvl = bots_level(cfg)
    if lvl not in ('full', 'full_until_review'):
        return []
    todo = [t for t in backlog.load(cfg)['tasks'] if kind_of(t) == 'bots' and t['status'] == 'queued' and t.get('bot_green')
            and '/pull/' in (t.get('depbot_ref') or '') and not t.get('attempts')]
    events, merged = [], {}
    for t in todo:
        if lvl == 'full':
            ok, why = merge_pr(t['depbot_ref'], t.get('bot_head') or '', None, gh)
            merged[t['id']] = (ok, why)
            events.append(f'autofix: merged the green {t["depbot_ref"]}' if ok else
                          f'autofix: merging the green {t["depbot_ref"]} failed ({why}); a worker takes it')
    with backlog.locked(cfg) as data:
        for t in data['tasks']:
            if t['id'] not in {x['id'] for x in todo} or t['status'] != 'queued':
                continue
            if lvl == 'full_until_review':
                t['status'] = 'done'
                t['pr'] = t['depbot_ref']
                t['final_review'] = {'state': 'waiting', 'pr': t['depbot_ref'], 'head': t.get('bot_head') or '', 'at': now}
                events.append(f'autofix: the green {t["depbot_ref"]} waits for your final review')
            elif merged.get(t['id'], (False, ''))[0]:
                t['status'], t['merged'], t['source_closed'] = 'done', True, True   # merging closed it
                t.setdefault('notes', []).append(f'merged by itself: its checks were green ({t["depbot_ref"]})')
                st = read_state(cfg)
                line = want_release(cfg, st, t, now)
                write_state(cfg, st)
                events += [line] if line else []
            else:
                t['bot_green'] = False   # a worker brings it in and fixes what the merge refused
        backlog.save(cfg, data)
    return events


def close_obsolete(cfg, task_id, reason, now=None):
    """`tutti autofix close <id> "<why>"`: a bot's issue or pull request that is obsolete or a duplicate: the task is
    dropped and its source closed with the reason at the next round. Returns (ok, message)."""
    if not (reason or '').strip():
        return False, 'say why it is obsolete or a duplicate: tutti autofix close <id> "<why>"'
    with backlog.locked(cfg) as data:
        t = next((x for x in data['tasks'] if x['id'] == task_id), None)
        if t is None or not source_url(t):
            return False, f'{task_id} is not a task from a bot\'s issue or pull request: `tutti autofix` lists them'
        t['status'] = 'dropped'
        t.setdefault('notes', []).append(f'closed as obsolete: {reason.strip()[:300]}')
        mark_close(t, reason.strip())
        backlog.save(cfg, data)
    return True, f'{task_id} dropped; {source_url(t)} is closed at the next round with your reason'


def merge_pr(url, head, cwd, gh=github.run):
    """(merged?, why) of a squash merge pinned to the reviewed commit; a non-zero exit is checked against the PR's state."""
    args = ['gh', 'pr', 'merge', url, '--squash', *(['--match-head-commit', head] if head else [])]
    done = gh(args, cwd=cwd, capture_output=True, text=True)
    if done.returncode == 0:
        return True, ''
    state = gh(['gh', 'pr', 'view', url, '--json', 'state', '--jq', '.state'], cwd=cwd, capture_output=True, text=True)
    if (state.stdout or '').strip() == 'MERGED':
        return True, ''
    return False, (done.stderr or done.stdout or 'no output').strip()[:200]


def approve(cfg, task_id, gh=github.run, now=None, actor='the owner'):
    """The person's final review: the task's pull request merged (pinned to the reviewed commit) and its release queued.
    Returns (ok, message)."""
    now = now or time.time()
    data = backlog.load(cfg)
    t = next((x for x in data['tasks'] if x['id'] == task_id), None)
    fr = (t or {}).get('final_review') or {}
    if t is None:
        return False, f'no task {task_id}: `tutti autofix` lists the ones waiting for your final review'
    if fr.get('state') != 'waiting':
        return False, f'{task_id} is not waiting for a final review ({fr.get("state") or "it never was"})'
    path = (((cfg.get('autopilot') or {}).get('repos') or {}).get(t['repo']) or {}).get('path')
    ok, why = merge_pr(fr['pr'], fr.get('head'), config.expand(path) if path else None, gh)
    with backlog.locked(cfg) as live:
        lt = backlog.find(live, task_id)
        if not ok:
            lt['final_review'] = {**fr, 'error': f'the merge failed ({why}): look at {fr["pr"]}, then approve again'}
            backlog.save(cfg, live)
            return False, lt['final_review']['error']
        lt['final_review'] = {**fr, 'state': 'approved', 'by': actor, 'approved_at': now, 'error': ''}
        lt['merged'] = True
        if fr['pr'] == source_url(lt):
            lt['source_closed'] = True   # the bot's own pull request: merging it closed it
        mark_close(lt, f'resolved by {fr["pr"]}')
        st = read_state(cfg)
        line = want_release(cfg, st, lt, now)
        write_state(cfg, st)
        backlog.save(cfg, live)
    telemetry.record('feature', 'autofix.approve')
    return True, f'{task_id}: merged {fr["pr"]}' + (f'; {line.split("; ", 1)[1]}' if line else '')


# ---- releases

def release_files(cfg, repo):
    base = os.path.join(config.state_dir(cfg), f'autofix-release-{repo}')
    return base + '.log', base + '.rc'


def start_release(cfg, repo, spawn=subprocess.Popen):
    path = (((cfg.get('autopilot') or {}).get('repos') or {}).get(repo) or {}).get('path')
    log, rc = release_files(cfg, repo)
    if os.path.exists(rc):
        os.remove(rc)
    with open(log, 'a', encoding='utf8') as f:
        spawn(['sh', '-c', 'rc=0; bash -c "$1" || rc=$?; echo $rc > "$RC_FILE"', 'release', release_command(cfg, repo)],
              cwd=config.expand(path) if path else None, stdout=f, stderr=f, stdin=subprocess.DEVNULL, start_new_session=True,
              env={**os.environ, 'RC_FILE': rc})
    return log


def release_round(cfg, st, now, spawn=subprocess.Popen):
    """Finished releases recorded (failed ones for a person), waiting fixes released, one release per repo at a time."""
    events = []
    for repo, going in list((st.get('releasing') or {}).items()):
        log, rc_file = release_files(cfg, repo)
        try:
            with open(rc_file, encoding='utf8') as f:
                rc = f.read().strip()
        except OSError:
            continue   # still running
        del st['releasing'][repo]
        if rc == '0':
            st.setdefault('released', {})[repo] = now
            st.setdefault('failed', {}).pop(repo, None)
            events.append(f'autofix: the {repo} release with {len(going["tasks"])} fix(es) is done')
        else:
            st.setdefault('failed', {})[repo] = {'at': now, 'rc': rc, 'log': log, 'tasks': going['tasks']}
            events.append(f'autofix: the {repo} release failed (exit {rc}): read {log}; its fixes wait for the next one')
    for repo, ids in list((st.get('pending') or {}).items()):
        if not ids or repo in (st.get('releasing') or {}) or not release_command(cfg, repo):
            continue
        if repo in (st.get('failed') or {}):
            continue   # a failed release waits for a person (Needs attention: retry it with `tutti autofix release <repo>`)
        log = start_release(cfg, repo, spawn)
        st.setdefault('releasing', {})[repo] = {'at': now, 'tasks': ids}
        st['pending'][repo] = []
        telemetry.record('feature', 'autofix.release', fixes=len(ids))
        events.append(f'autofix: the {repo} release started with {len(ids)} fix(es); log {log}')
    return events


def retry_release(cfg, repo):
    """`tutti autofix release <repo>`: a failed release is tried again at the next round, with its fixes."""
    with backlog.locked(cfg):
        st = read_state(cfg)
        failed = (st.get('failed') or {}).pop(repo, None)
        if failed is None:
            return False, f'no failed release of {repo}: `tutti autofix` shows the releases'
        pend = st.setdefault('pending', {}).setdefault(repo, [])
        pend += [i for i in failed.get('tasks') or [] if i not in pend]
        write_state(cfg, st)
    return True, f'the {repo} release runs again at the next round'


# ---- what needs a person (the Needs attention tab, the notification)

def attention(cfg, data=None, st=None):
    """[{key, kind, tone, title, note, task?, pr?}]: what only a person can move on, from the backlog and the releases."""
    data = data if data is not None else backlog.load(cfg)
    st = st if st is not None else read_state(cfg)
    items = []
    for t in data['tasks']:
        fr = t.get('final_review') or {}
        if fr.get('state') == 'waiting':
            items.append({'key': f'final:{t["id"]}', 'kind': 'final-review', 'tone': 'warn', 'task': t['id'], 'pr': fr['pr'],
                          'title': t['title'], 'note': fr.get('error') or f'{t["id"]} · {t["repo"]}: checked and reviewed; '
                          f'approve to merge {fr["pr"]}' + (' and release' if release_command(cfg, t['repo']) else '')})
        elif t['status'] == 'needs-person' and is_autofix(t):
            kind = 'bot-work' if kind_of(t) == 'bots' else 'security' if t['source'].startswith('security') else 'review-fix'
            items.append({'key': f'parked:{t["id"]}', 'kind': kind, 'tone': 'bad', 'task': t['id'], 'title': t['title'],
                          'note': f'{t["id"]} · {t["repo"]}: not fixed by itself: {t.get("parked") or "parked after its attempts"}'
                                  + (f'; when it is obsolete: `tutti autofix close {t["id"]} "<why>"`' if source_url(t) else '')})
        elif t['status'] == 'candidate' and is_autofix(t):
            setting = 'autofix.bots_level' if kind_of(t) == 'bots' else 'autofix.level'
            items.append({'key': f'suggested:{t["id"]}', 'kind': 'suggested', 'tone': 'ok', 'task': t['id'], 'title': t['title'],
                          'note': f'{t["id"]} · {t["repo"]}: suggested ({setting} suggest): `tutti backlog accept {t["id"]}` '
                                  'queues it'})
        elif t.get('close_source_error') and not t.get('source_closed'):
            items.append({'key': f'close:{t["id"]}', 'kind': 'close-failed', 'tone': 'warn', 'task': t['id'], 'title': t['title'],
                          'note': f'{t["id"]} · {t["repo"]}: resolved, but closing {source_url(t)} failed '
                                  f'({t["close_source_error"]}): close it by hand, or check `gh auth status`'})
    for repo, f in sorted((st.get('failed') or {}).items()):
        items.append({'key': f'release:{repo}:{f.get("at")}', 'kind': 'release-failed', 'tone': 'bad', 'repo': repo,
                      'title': f'The {repo} release failed (exit {f.get("rc")})',
                      'note': f'read {f.get("log")}, fix it, then `tutti autofix release {repo}` runs it again with its '
                              f'{len(f.get("tasks") or [])} fix(es)'})
    return items


def notice_text(items):
    lines = [f'{len(items)} new item(s) need you (the dashboard\'s Needs attention tab):']
    lines += [f'- {i["title"][:160]}: {i["note"][:240]}' for i in items]
    return '\n'.join(lines)


def deliver_chat(cfg, items):
    """One message in the owner's "Needs attention" conversation (made once), like the questions' own (tutti/needs.py)."""
    from . import chat, needs
    store, sub = chat.Store(config.state_dir(cfg)), needs.chat_sub(needs.OWNER)
    conv = next((c for c in store.load(sub)['conversations'] if c.get('title') == 'Needs attention'), None)
    cid = (conv or store.create(sub, title='Needs attention'))['id']
    store.put_message(sub, cid, {'id': chat.new_id('m'), 'role': 'assistant', 'text': notice_text(items), 'at': chat.now_iso(),
                                 'status': 'done', 'model': 'tutti', 'cards': []})


def fresh(items, notified):
    """The items not told yet (by key): a key that went away and came back is told again."""
    return [i for i in items if i['key'] not in set(notified)]


def tick(cfg, now=None, spawn=subprocess.Popen, send=None, gh=None):
    """The dashboard's round: releases finished and started, and a notification for each new item that needs a person.
    Returns event lines."""
    now = now or time.time()
    send = send or deliver_chat
    events = []
    g = gh or github.run   # GitHub work (network, only when there is some): never under the backlog's lock
    for step in (lambda: green_round(cfg, g, now), lambda: close_round(cfg, g), lambda: close_dupes(cfg, g)):
        try:
            events += step()
        except (OSError, ValueError, KeyError) as e:   # one failed step never stops the releases and the notification
            events.append(f'autofix: a GitHub step failed ({type(e).__name__}: {e}); the next round tries again')
    with backlog.locked(cfg) as data:
        st = read_state(cfg)
        events += release_round(cfg, st, now, spawn)
        items = attention(cfg, data, st)
        new = fresh(items, st.get('notified') or [])
        st['notified'] = [i['key'] for i in items]
        write_state(cfg, st)
    if new and settings(cfg)['notify']:
        try:
            send(cfg, new)
            events.append(f'autofix: {len(new)} new item(s) for a person, told in the Needs attention chat')
        except Exception as e:  # never lost: the keys are forgotten so the next round tells them again
            with backlog.locked(cfg):
                st = read_state(cfg)
                st['notified'] = [k for k in st.get('notified') or [] if k not in {i['key'] for i in new}]
                write_state(cfg, st)
            events.append(f'autofix: the notification failed ({type(e).__name__}: {e}); the next round tries again')
    return events


def view(cfg):
    st = read_state(cfg)
    return {'level': level(cfg), 'levels': list(LEVELS), 'bots_level': bots_level(cfg), 'bot_levels': list(BOT_LEVELS),
            'items': attention(cfg, st=st),
            'releasing': sorted(st.get('releasing') or {}), 'pending': {k: v for k, v in (st.get('pending') or {}).items() if v}}


def describe(v):
    lines = [f'security and reviews: {v["level"]} (one of {", ".join(v["levels"])}; Settings, Autopilot, or autofix.level)',
             f'bots\' issues and pull requests: {v["bots_level"]} (one of {", ".join(v["bot_levels"])}; autofix.bots_level)']
    lines += [f'releasing: {", ".join(v["releasing"])}'] if v['releasing'] else []
    lines += [f'waiting for a release: {r} ({", ".join(ids)})' for r, ids in v['pending'].items()]
    lines += [f'needs you: {i["title"][:100]}: {i["note"]}' for i in v['items']] or ['nothing needs you']
    return '\n'.join(lines)
