"""Scouts: helpers that are not workers. A scout never touches a repository or a diff; it is asked a question (research,
web browsing, watching changelogs and docs, triage, monitoring, drafting, reproducing a bug in a browser) and its answer
comes back as a comment on a GitHub issue. Today's one kind is Grok Bot's webhook routine (`grok-bot-webhook`): Tutti opens
the issue, POSTs {task, reply_to} to the routine's URL with `Authorization: Bearer <key>`, the routine posts its answer on the
issue, and Tutti reads it back through `gh`. The URL and key come from environment variables (or stdin) and are never
written, logged or printed. The plan of what to send when is pure: the scout's own account (its weekly allowance) decides,
and nothing is sent once the allowance is used, so a scout never runs into on-demand spend."""
import contextlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from . import durable, pacing
from .review import documents, gh

try:
    import fcntl
except ImportError:   # Windows: no flock
    fcntl = None

KINDS = ('grok-bot-webhook',)
SKILLS = ('research', 'web-browse', 'triage', 'monitoring', 'drafting', 'browser-repro', 'review')
FILE = 'scouts.json'
LABEL = 'tutti-scout'
WATCH_LABEL = 'tutti-scout-watch'
ENV_RX = r'[A-Z_][A-Z0-9_]*'
EVERY_RX = r'(\d+)([hdw])'
UNITS = {'h': 'hours', 'd': 'days', 'w': 'weeks'}
DEFAULTS = {'timeout_minutes': 20, 'poll_seconds': 30, 'auto': True, 'max_open': 5, 'per_tick': 2, 'give_up_hours': 48,
            'skills': list(SKILLS[:5]), 'watches': {}}
ANSWER_CHARS = 20000
RETRY_SECONDS = 3600
REVIEW_MARKER = 'grok-scout'   # the first line of a scout's review comment on a pull request
LOOPBACK = ('127.0.0.1', 'localhost', '::1')
SETUP = (
    'Easiest: run `tutti scout setup grok` in a terminal; it walks you through every step below and tests it. By hand:',
    'Open the Grok Bot app (Cursor Pro includes it; it has its own weekly allowance).',
    'Create a bot named "Tutti scout" (new bot, name it, keep it personal: Only me).',
    'In "Connect apps" (sidebar) connect GitHub, and allow it the repository named in scouts.<name>.reply.github_repo.',
    'In the bot\'s chat ask: "Create a routine named Tutti task with a webhook trigger. Instructions: the request body is JSON '
    'with task and reply_to. Do the task (research, browse, triage, monitor or draft as it asks). Then post the full result as '
    'one comment on the GitHub issue reply_to.url (repository reply_to.repo, issue number reply_to.issue). Never change code, '
    'open pull requests, or close the issue. If you cannot do the task, post a comment that says why."',
    'Open the routine (click the bot\'s name, Details, the routine) and find its Webhook section.',
    'Copy the "POST to" URL into the environment variable named by url_env (e.g. in ~/.profile: export TUTTI_GROK_URL=...).',
    'Copy the key (the part after "Bearer ") into the variable named by key_env (export TUTTI_GROK_KEY=...); never into the '
    'config, a file in a repository, or a chat.',
    'Restart the dashboard from a shell that has both variables, then run: tutti scout ask <name> "Say hello" to test it.',
    'Optional: read Grok Bot\'s weekly meter with the reading helper (accounts.<account>.usage_url: the Cursor spending page, '
    'helper_label: Grok Bot) so scouts are paced against its allowance.',
)


class ScoutError(Exception):
    """A scout command cannot be done; the message says what happened, why, and what to do."""


# ---- config

def settings(cfg, name):
    sc = (cfg.get('scouts') or {}).get(name)
    if sc is None:
        known = ', '.join(cfg.get('scouts') or {}) or 'none (add scouts: to the config, docs/SCOUTS.md)'
        raise ScoutError(f'no scout "{name}" under scouts: (known: {known})')
    acc = (cfg.get('accounts') or {}).get(sc.get('account')) if isinstance(sc, dict) else None
    extra = {}
    if acc is not None and cfg.get('project'):   # where a sent question's spend estimate goes (its account's usage log)
        from . import state  # state imports config, which imports this module
        extra = {'_usage_log': state.usage_log_path(cfg), '_task_pct': float((acc or {}).get('task_pct', pacing.TASK_PCT))}
        extra['_cost'] = measured(cfg, name, sc.get('account'), extra['_usage_log'], extra['_task_pct'])
    # Sentry triage watches, one per project with a sentry: entry (triage builds on this module)
    from . import triage
    out = {**DEFAULTS, **sc, 'name': name, **extra}
    from . import aiassist  # AI assist's research and design watches (spare quota only)
    out['watches'] = {**triage.watches(cfg, out), **aiassist.watches(cfg, out), **(out.get('watches') or {})}
    return out


def measured(cfg, name, account, log, fallback):
    """What one run of the scout costs of its account, from the real readings around its runs (tutti/grokweek.py)."""
    from . import grokweek
    try:
        entries = pacing.read_log(log)
        requests = load(cfg)['requests']
    except (OSError, ValueError):
        return {'pct': fallback, 'runs': 0, 'rise': 0.0, 'measured': False}
    return grokweek.cost(entries, requests, account, name, fallback)


def run_pct(sc):
    """The points one run costs: measured when known, else the account's task_pct."""
    return float((sc.get('_cost') or {}).get('pct') or sc.get('_task_pct') or pacing.TASK_PCT)


def spend_estimate(sc, r, now=None):
    """After a question went out: the account's newest reading plus task_pct, logged as an estimate (source `estimate`), so
    the meter moves between readings. An estimate never counts as a fresh reading; the next real one replaces it."""
    if not sc.get('_usage_log') or not sc.get('account'):
        return None
    at = datetime.fromtimestamp(now or time.time(), timezone.utc)
    entries = pacing.read_log(sc['_usage_log'])
    last = pacing.latest(entries, sc['account'])
    if not last:
        return None
    reset = pacing.reading_anchor(entries, sc['account'])
    reset_passed = reset and pacing.parse_time(last['at']) < reset <= at   # the newest reading is from before the reset: from 0
    used = min(100.0, (0.0 if reset_passed else float(last['used'])) + run_pct(sc))
    return pacing.append_log(sc['_usage_log'], sc['account'], used, at, f'estimate: scout {sc["name"]} {r["id"]} sent '
                             f'(+{run_pct(sc):g} points)', source='estimate')


def _every_errors(key, v):
    return [] if isinstance(v, str) and re.fullmatch(EVERY_RX, v) else [f'{key} is {v!r}: write it as 1d, 7d, 12h or 2w']


def _watch_errors(key, watches):
    if not isinstance(watches, dict):
        return [f'{key} is {watches!r}: a mapping such as {{deps: {{every: 7d, task: "..."}}}}']
    errs = []
    for n, w in watches.items():
        if not isinstance(w, dict) or not isinstance(w.get('task'), str) or not w['task'].strip():
            errs.append(f'{key}.{n} needs task: what the scout should do each time, in plain words')
            continue
        errs += _every_errors(f'{key}.{n}.every', w.get('every'))
        if 'repo' in w and not is_repo(w['repo']):
            errs.append(f'{key}.{n}.repo is {w["repo"]!r}: the GitHub repository as owner/name')
    return errs


def is_repo(v):
    return isinstance(v, str) and re.fullmatch(r'[\w.-]+/[\w.-]+', v) is not None


def _env_errors(key, sc):
    # the value is never echoed: a pasted URL or key here is exactly the mistake this catches
    return [f'{key}.{k} is not an environment variable name: write the NAME (e.g. TUTTI_GROK_{k[:-4].upper()}), never the value'
            for k in ('url_env', 'key_env') if not (isinstance(sc.get(k), str) and re.fullmatch(ENV_RX, sc[k]))]


def _account_errors(cfg, key, sc):
    acc = sc.get('account')
    if acc is None:
        return []
    if acc not in cfg['accounts']:
        return [f'{key}.account names "{acc}", which is not under accounts: (add it with window: 7d, or remove the key)']
    if (cfg['accounts'][acc] or {}).get('costs_money'):
        return [f'{key}.account "{acc}" costs money per use: a scout only spends an included allowance, never on-demand']
    return []


def _triage_errors(key, sc):
    if 'sentry_triage' in sc and not isinstance(sc['sentry_triage'], bool):
        return [f'{key}.sentry_triage is {sc["sentry_triage"]!r}: true or false']
    return []


def _one_errors(cfg, name, sc):
    key = f'scouts.{name}'
    if not isinstance(sc, dict):
        return [f'{key} is {sc!r}: write it as a mapping with kind, url_env, key_env and reply']
    if sc.get('kind') not in KINDS:
        return [f'{key}.kind is {sc.get("kind")!r}: use {", ".join(KINDS)}']
    errs = _env_errors(key, sc) + _account_errors(cfg, key, sc) + _watch_errors(f'{key}.watches', sc.get('watches') or {})
    reply = sc.get('reply') or {}
    if not is_repo(reply.get('github_repo')):
        errs.append(f'{key}.reply.github_repo is {reply.get("github_repo")!r}: the repository its answers go to, as owner/name')
    if 'issue_label' in reply and not (isinstance(reply['issue_label'], str) and reply['issue_label'].strip()):
        errs.append(f'{key}.reply.issue_label is {reply["issue_label"]!r}: a label name such as {LABEL}')
    from . import spendrest, trial  # they build on this module
    errs += trial.errors(f'{key}.review_trial', sc.get('review_trial')) + _triage_errors(key, sc)
    errs += spendrest.errors(f'{key}.spend_rest', sc.get('spend_rest'))
    bad = [s for s in sc.get('skills') or [] if s not in SKILLS]
    if bad:
        errs.append(f'{key}.skills has {", ".join(map(str, bad))}: use any of {", ".join(SKILLS)}')
    for k in ('timeout_minutes', 'poll_seconds', 'max_open', 'per_tick', 'give_up_hours'):
        v = sc.get(k, DEFAULTS[k])
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            errs.append(f'{key}.{k} is {v!r}: use a number above 0')
    return errs


def errors(cfg):
    """Config problems in `scouts:`, each naming its key and the fix."""
    s = cfg.get('scouts') or {}
    if not isinstance(s, dict):
        return [f'scouts is {s!r}: a mapping such as scouts: {{grok: {{kind: grok-bot-webhook, ...}}}}']
    from . import triage
    return [e for name, sc in s.items() for e in _one_errors(cfg, name, sc)] + triage.errors(cfg)


def missing_env(sc, env=None):
    """The environment variables a scout still needs (names only)."""
    env = os.environ if env is None else env
    return [sc[k] for k in ('url_env', 'key_env') if not env.get(sc[k], '').strip()]


def secrets(sc, env=None, key_stdin=None):
    """(url, key) from the environment; the key may come from stdin instead. Raises with the variable's name, never a value."""
    env = os.environ if env is None else env
    url = env.get(sc['url_env'], '').strip()
    key = (key_stdin if key_stdin is not None else env.get(sc['key_env'], '')).strip()
    if not url or not key:
        gone = [sc['url_env']] if not url else []
        gone += [] if key else [sc['key_env'] + (' (or --key-stdin)' if key_stdin is None else ' (stdin was empty)')]
        raise ScoutError(f'scout {sc["name"]} is not set up: {", ".join(gone)} not set in this shell; follow `tutti scout setup '
                         f'{sc["name"]}` (the routine\'s webhook URL and key go into these variables)')
    check_url(url, sc)
    return url, key[len('Bearer '):].strip() if key.startswith('Bearer ') else key


def check_url(url, sc):
    try:
        u = urlparse(url)
        host = u.hostname
    except ValueError:
        u, host = None, None
    if u is None:
        raise ScoutError(f'{sc["url_env"]} does not hold a web address: copy the routine\'s "POST to" URL into it again')
    if u.scheme == 'https' and host:
        return
    if u.scheme == 'http' and host in LOOPBACK:   # a local fake for tests and dry runs
        return
    raise ScoutError(f'{sc["url_env"]} does not hold an https:// address: copy the routine\'s "POST to" URL into it again')


# ---- the webhook

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is never followed: it would carry the Authorization header to an address check_url never saw."""

    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def post(url, key, body, timeout=30):
    """POSTs one task; returns the HTTP status (200: a run started). The key goes only into the Authorization header."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                 headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json',
                                          'User-Agent': 'tutti-scout'})
    try:
        with OPENER.open(req, timeout=timeout) as r:   # the URL is the owner's own routine, checked by check_url
            return r.status
    except urllib.error.HTTPError as e:
        e.close()
        return e.code


def refused(status, sc):
    if status == 401 or status == 403:
        why = f'the key was refused: copy the routine\'s current key into {sc["key_env"]} again'
    elif status == 404:
        why = f'no routine at that URL: copy the "POST to" URL into {sc["url_env"]} again'
    elif status == 429:
        why = 'Grok Bot\'s rate limit (200 runs an hour per routine): wait and send it again'
    elif 300 <= status < 400:
        why = f'it answered with a redirect, which is never followed (the key would go elsewhere): check {sc["url_env"]}'
    else:
        why = 'confirm the routine is not paused (Grok Bot: the bot\'s Details tab) and the key is current'
    return f'the scout\'s webhook answered {status}, so no run started: {why}'


# ---- state

def path_of(cfg):
    from . import config  # config imports this module for its checks
    return os.path.join(config.state_dir(cfg), FILE)


def load(cfg):
    try:
        with open(path_of(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    data.setdefault('next', 1)
    data.setdefault('requests', [])
    return data


@contextlib.contextmanager
def locked(cfg):
    p = path_of(cfg)
    with open(p + '.lock', 'w', encoding='utf8') as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        data = load(cfg)
        yield data
        durable.dump_json(p, data, indent=1)


def find(data, rid):
    for r in data['requests']:
        if r['id'] == rid:
            return r
    raise ScoutError(f'no scout request {rid}: `tutti scout list` shows them')


def new_request(data, sc, task, kind='ask', watch=None, repo=None, skill=None, now=None):
    if not task.strip():
        raise ScoutError('a scout task needs words: what to find out, watch or draft')
    rid = f's{data["next"]}'
    data['next'] += 1
    r = {'id': rid, 'scout': sc['name'], 'kind': kind, 'watch': watch, 'task': task.strip()[:4000], 'skill': skill,
         'repo': repo or sc['reply']['github_repo'], 'status': 'queued', 'added': now or time.time()}
    data['requests'].append(r)
    return r


# ---- GitHub: the issue and its answer

def label_of(sc):
    return (sc.get('reply') or {}).get('issue_label') or LABEL


def ensure_label(repo, name, gh_fn=gh):
    rc, _o, err = gh_fn(['label', 'create', name, '--repo', repo, '--color', '5319E7', '--force',
                         '--description', 'A question for a Tutti scout; its answer is a comment'])
    if rc != 0:
        raise ScoutError(f'cannot create the label {name} in {repo} ({err}): check `gh auth status` and that you may write there')


def open_issue(sc, r, gh_fn=gh):
    labels = [label_of(sc)] + ([WATCH_LABEL] if r['kind'] == 'watch' else [])
    for name in labels:
        ensure_label(r['repo'], name, gh_fn)
    title = f'Scout {r["scout"]}: ' + (f'{r["watch"]} ' if r['watch'] else '') + re.sub(r'\s+', ' ', r['task'])[:70]
    body = (f'{r["task"]}\n\n---\nAsked by Tutti ({r["id"]}, scout {r["scout"]}, {r["kind"]}). The scout posts its answer as a '
            'comment here; Tutti reads the first comment after this question.')
    args = ['issue', 'create', '--repo', r['repo'], '--title', title, '--body', body]
    for name in labels:
        args += ['--label', name]
    rc, out, err = gh_fn(args)
    m = re.search(r'/issues/(\d+)', out or '')
    if rc != 0 or not m:
        raise ScoutError(f'cannot open the scout\'s issue in {r["repo"]} ({err or out}): check `gh auth status`')
    return int(m.group(1)), out.strip().splitlines()[-1]


TRUSTED = ('OWNER', 'MEMBER', 'COLLABORATOR')   # GitHub's author_association: then the account's permission is asked
WRITE = ('admin', 'maintain', 'write')


def writer(repo, who, gh_fn, seen):
    """Whether `who` may write to the repository (GitHub's collaborator permission), asked once per login."""
    if who not in seen:
        rc, out, _ = gh_fn(['api', f'repos/{repo}/collaborators/{who}/permission', '--jq', '.permission'])
        seen[who] = rc == 0 and out.strip() in WRITE
    return seen[who]


def trusted(c, repo, gh_fn, login=None, seen=None):
    """A comment a scout's answer may come from: by the account the scout posts as (scouts.<name>.github_login), or by an
    owner, member or collaborator whose permission on the repository is write or more (an organisation member or a
    collaborator may be read-only). In a public repository anyone can comment, and an answer becomes backlog work."""
    who = (c.get('user') or {}).get('login', '')
    if login and who == login:
        return True
    return c.get('author_association') in TRUSTED and bool(who) and writer(repo, who, gh_fn, {} if seen is None else seen)


def answer_of(repo, issue, since, gh_fn=gh, marker=None, login=None, skipped=None):
    """The first comment on the issue (or pull request) made after `since` (epoch seconds) by a trusted author (`trusted`),
    and holding `marker` when one is given (a review's "grok-scout" tag: other reviewers comment there too): (text, author)
    or None."""
    rc, text, err = gh_fn(['api', '--paginate', f'repos/{repo}/issues/{issue}/comments?per_page=100'])
    if rc != 0:
        raise ScoutError(f'reading the comments of {repo}#{issue} failed ({err}): check `gh auth status`')
    items = [c for page in documents(text) for c in (page if isinstance(page, list) else [page])]
    seen = {}
    for c in sorted(items, key=lambda c: c.get('created_at') or ''):
        at = pacing.parse_time(c['created_at']).timestamp() if c.get('created_at') else 0
        if at >= since - 60 and (c.get('body') or '').strip() and (not marker or marker in c['body']) and \
                trusted(c, repo, gh_fn, login, seen):
            return c['body'].strip()[:ANSWER_CHARS], (c.get('user') or {}).get('login', '')
        body = (c.get('body') or '').strip()
        if skipped is not None and at >= since - 60 and body and (not marker or marker in body):
            skipped.append((c.get('user') or {}).get('login', '') or '?')
    return None


def send(sc, r, url, key, gh_fn=gh, post_fn=post, now=None):
    """Opens the issue and fires the webhook for one request; it is then `waiting`, or `failed` with why."""
    if r['kind'] != 'review':   # a review answers on its pull request, whose number and url the request already has
        r['issue'], r['url'] = open_issue(sc, r, gh_fn)
    r['sent'] = now or time.time()
    how = (f'post the findings as ONE comment on this pull request whose first line is "{REVIEW_MARKER}"' if r['kind'] == 'review'
           else 'post the result as one comment on this issue')
    body = {'task': r['task'], 'skill': r.get('skill'), 'from': 'tutti', 'id': r['id'],
            'reply_to': {'repo': r['repo'], 'issue': r['issue'], 'url': r['url'], 'how': how}}
    try:
        status = post_fn(url, key, body)
    except OSError as e:
        status, why = None, f'the scout\'s webhook could not be reached ({type(e).__name__}): check the network and {sc["url_env"]}'
    else:
        why = '' if status == 200 else refused(status, sc)
    if why:
        r.update(status='failed', why=why)
        if r['kind'] == 'review':
            return r
        gh_fn(['issue', 'close', str(r['issue']), '--repo', r['repo'], '--comment', f'Tutti: not sent. {why}'])
        return r
    r['status'] = 'waiting'
    spend_estimate(sc, r, r['sent'])
    return r


def collect(sc, r, gh_fn=gh, now=None):
    """Looks once for the answer of a waiting request; gives up after give_up_hours. Returns the request."""
    now = now or time.time()
    skipped = []
    found = answer_of(r['repo'], r['issue'], r['sent'], gh_fn, REVIEW_MARKER if r['kind'] == 'review' else None,
                      sc.get('github_login'), skipped)
    if skipped and not found:   # say why the answer does not count, once per author
        r['ignored'] = (f'a comment by {", ".join(sorted(set(skipped)))} was not taken as the answer: that account cannot write '
                        f'to {r["repo"]}; if the scout posts as it, set scouts.{sc.get("name", "<name>")}.github_login: <that login>')
    if found:
        r.update(status='answered', answer=found[0], answered_by=found[1], answered=now)
        if r['kind'] == 'ask':   # a watch's issue stays open for the orchestrator to triage
            gh_fn(['issue', 'close', str(r['issue']), '--repo', r['repo'], '--reason', 'completed'])
    elif now - r['sent'] > float(sc['give_up_hours']) * 3600:
        r.update(status='timeout', why=f'no answer in {sc["give_up_hours"]} h: look at the Grok Bot chat for the run\'s error')
    return r


def wait(sc, r, timeout_s, gh_fn=gh, clock=time.monotonic, sleep=time.sleep):
    end = clock() + timeout_s
    while True:
        collect(sc, r, gh_fn)
        if r['status'] != 'waiting' or clock() >= end:
            return r
        sleep(float(sc['poll_seconds']))


# ---- the plan: what to send now, paced by the scout's allowance

def every_seconds(every):
    m = re.fullmatch(EVERY_RX, every)
    return timedelta(**{UNITS[m.group(2)]: int(m.group(1))}).total_seconds()


def last_fired(requests, scout, watch, failed=False):
    """When the watch last went out (failed=False) or last failed to go out (failed=True)."""
    times = [r.get('sent') or r['added'] for r in requests if r['scout'] == scout and r.get('watch') == watch
             and r['status'] != 'queued' and (r['status'] == 'failed') == failed]
    return max(times) if times else None


def due(sc, requests, name, w, acc, now):
    """A watch is due once its period has passed (half of it when early and the allowance is behind its pace); a failed
    send waits RETRY_SECONDS, so a wrong key never opens an issue every round."""
    failed = last_fired(requests, sc['name'], name, failed=True)
    if failed is not None and now - failed < RETRY_SECONDS:
        return False
    if w.get('spare_only') and not behind(acc):   # AI assist's research: only quota the planner says is spare
        return False
    last, gap = last_fired(requests, sc['name'], name), every_seconds(w['every'])
    return last is None or now - last >= gap or bool(w.get('early') and behind(acc) and now - last >= gap / 2)


def allowance(acc):
    """(may send, why) from the scout's account state; no account or no reading yet: may send."""
    if not acc:
        return True, 'no meter configured'
    used = acc.get('used')
    if acc.get('panic') or acc.get('verdict') == 'spent' or (used is not None and used >= 100):
        reset = acc.get('reset') or 'its reset'
        pct = f' ({used:g} %)' if used is not None else ''
        return False, f'its allowance is used{pct}: nothing is sent until {reset}, never on-demand spend'
    return True, acc.get('verdict') or 'unknown'


def guard(cfg, sc, accounts=None, now=None):
    """Refuses a direct send (ask, a watch now, a review) while the scout's allowance is used: never on-demand spend."""
    if not sc.get('account'):
        return
    if accounts is None:
        from . import state  # state imports config, which imports this module
        now_dt = datetime.fromtimestamp(now or time.time(), timezone.utc)
        accounts = {a['name']: a for a in state.usage_state(cfg, now_dt)['accounts']}
    ok, why = allowance(accounts.get(sc['account']))
    if not ok:
        raise ScoutError(f'scout {sc["name"]} sends nothing now: {why}. `tutti scout ask --queue` keeps a question for after the reset')


def behind(acc):
    return bool(acc) and acc.get('verdict') in ('behind', 'late')


def plan(sc, requests, acc, now):
    """{'send': [('watch', name) | ('queued', id)], 'why': text}. Due watches first, then queued asks; watches marked early go
    ahead of time while the allowance is behind its pace (use it or lose it). At most per_tick, never past max_open waiting."""
    ok, why = allowance(acc)
    if not ok:
        return {'send': [], 'why': why}
    mine = [r for r in requests if r['scout'] == sc['name']]
    room = min(int(sc['per_tick']), int(sc['max_open']) - sum(1 for r in mine if r['status'] == 'waiting'))
    if room <= 0:
        return {'send': [], 'why': f'{sc["max_open"]} questions are still waiting for answers'}
    out = [('watch', name) for name, w in (sc.get('watches') or {}).items() if due(sc, requests, name, w, acc, now)]
    out += [('queued', r['id']) for r in mine if r['status'] == 'queued']
    return {'send': out[:room], 'why': why if out else 'nothing due'}


def tick(cfg, accounts, env=None, gh_fn=gh, post_fn=post, now=None):
    """One round for every scout with auto on and its variables set: collect answers, then send what the plan allows.
    Returns event lines (never a secret)."""
    now, events = now or time.time(), []
    for name in cfg.get('scouts') or {}:
        sc = settings(cfg, name)
        if not sc['auto'] or missing_env(sc, env):
            continue
        url, key = secrets(sc, env)
        acc = accounts.get(sc.get('account'))
        from . import selfimprove, spendrest, triage
        with locked(cfg) as data:
            before = len(events)
            events += _round(sc, data, acc, url, key, gh_fn, post_fn, now)
            sent = sum(1 for e in events[before:] if ' waiting: ' in e)
            extra, n = spendrest.round_(cfg, sc, data, acc, url, key, sent, gh_fn, post_fn, now) if acc else ([], 0)
            events += extra
            events += triage.feed_answered(cfg, data, selfimprove.auto_accept(cfg))
            answers = spendrest.second_answers(data)
            from . import aiassist
            ai_answers = aiassist.answered(data)   # fed below, outside the scouts' lock
            left = spendrest.room(sc, data, acc, sent + n, now) if acc else 0
        events += spendrest.feed_seconds(cfg, answers)   # the backlog's lock: never inside the scouts' lock
        events += aiassist.feed(cfg, ai_answers, now)
        events += spendrest.reviews(cfg, name, acc, left, gh_fn, post_fn, env, now) if acc else []   # GitHub: outside the lock
    return events


def _round(sc, data, acc, url, key, gh_fn, post_fn, now):
    events = []
    for r in [r for r in data['requests'] if r['scout'] == sc['name'] and r['status'] == 'waiting']:
        try:   # one unreadable issue (deleted, no access) never stops the round
            collect(sc, r, gh_fn, now)
        except ScoutError as e:
            if now - r['sent'] > float(sc['give_up_hours']) * 3600:
                r.update(status='timeout', why=f'its issue could not be read for {sc["give_up_hours"]} h ({e})')
            else:
                events.append(f'{r["id"]} not read this round: {e}')
                continue
        if r['status'] != 'waiting':
            events.append(f'{r["id"]} {r["status"]}: {r.get("url")}')
    for what, ref in plan(sc, data['requests'], acc, now)['send']:
        if what == 'watch':
            w = sc['watches'][ref]
            r = new_request(data, sc, w['task'], 'watch', ref, w.get('repo'), w.get('skill'), now)
            if w.get('project'):
                r['project'] = w['project']
        else:
            r = find(data, ref)
        try:
            send(sc, r, url, key, gh_fn, post_fn, now)
        except ScoutError as e:
            r.update(status='failed', why=str(e))
        events.append(f'{r["id"]} {r["status"]}: {r.get("url") or r.get("why")}')
    return events


# ---- the view (dashboard and `tutti scout list`)

def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def request_view(r):
    return {k: r.get(k) for k in ('id', 'kind', 'watch', 'status', 'url', 'why', 'answered_by', 'ignored')} | {
        'task': r['task'][:200], 'answer': (r.get('answer') or '')[:600], 'at': _iso(r.get('sent') or r['added']),
        'answered_at': _iso(r.get('answered'))}


def scout_view(cfg, sc, requests, acc, env, now):
    missing = missing_env(sc, env)
    mine = [r for r in requests if r['scout'] == sc['name']]
    watches = []
    for n, w in (sc.get('watches') or {}).items():
        last = last_fired(requests, sc['name'], n)
        watches.append({'name': n, 'every': w['every'], 'task': w['task'][:160], 'last': _iso(last),
                        'next': _iso((last or now) + (every_seconds(w['every']) if last else 0))})
    p = plan(sc, requests, acc, now)
    from . import spendrest
    rest = spendrest.plan(cfg, sc, requests, acc, now) if acc else {'on': False, 'why': 'no meter', 'items': []}
    meter = {k: acc.get(k) for k in ('name', 'used', 'reset', 'verdict', 'panic', 'reading_at', 'usage_url', 'stale')} if acc else None
    return {'name': sc['name'], 'kind': sc['kind'], 'ready': not missing, 'missing': missing, 'auto': sc['auto'],
            'skills': sc['skills'], 'repo': sc['reply']['github_repo'], 'account': sc.get('account'), 'meter': meter,
            'plan': p['why'], 'watches': watches, 'waiting': sum(1 for r in mine if r['status'] == 'waiting'),
            'queued': sum(1 for r in mine if r['status'] == 'queued'),
            'recent': [request_view(r) for r in sorted(mine, key=lambda r: r['added'], reverse=True)[:6]],
            'facts': scout_facts(meter, watches, mine),
            'spend_rest': {'on': rest['on'], 'why': rest['why'], 'count': len(rest['items']), 'line': spendrest.line(rest),
                           'items': [i['ref'] for i in rest['items']]},
            'week': week_view(cfg, sc, requests, acc, rest, now)}


def week_view(cfg, sc, requests, acc, rest, now):
    """The week's plan on the scout's card: runs left and their mix, what can go now, what it did today."""
    from . import grokweek, spendrest
    b = rest.get('week') or grokweek.budget(None, now, 1, 95, 6)
    c = sc.get('_cost') or {'pct': run_pct(sc), 'runs': 0, 'rise': 0.0, 'measured': False}
    days = 0.0 if not acc or not acc.get('target') else max(0.0, (pacing.parse_time(acc['target']).timestamp() - now) / 86400)
    w = sc.get('watches') or {}
    triage_n = sum(1 for n in w if n.startswith('sentry-'))
    notes = sum(1 for n in w if spendrest.NOTES_RX.search(n) and not n.startswith('sentry-'))
    checks = len(w) - triage_n - notes
    reviews = sum(1 for i in rest.get('items', []) if i['kind'] == 'review') + 3 * max(1, len(spendrest.review_repos(cfg, sc['name'])))
    research = sum(1 for i in rest.get('items', []) if i['kind'] in ('research', 'second'))
    mixed = grokweek.mix(b['left'], days, reviews, triage_n, notes, checks, research)
    done = grokweek.today(requests, sc['name'], now)
    return {'left': b['left'], 'now': b['now'], 'mode': b['mode'], 'line': b['line'], 'cost': c, 'mix': mixed, 'today': done,
            'text': grokweek.text(b, c, mixed, done)}


def scout_facts(meter, watches, mine):
    """What the scout's account card shows instead of task room (a scout answers questions, it takes no bounded tasks): the
    allowance used and left, the next watch, the last answer."""
    used = meter.get('used') if meter else None
    nxt = min((w for w in watches if w['next']), key=lambda w: w['next'], default=None)
    answered = [r for r in mine if r['status'] == 'answered']
    last = max(answered, key=lambda r: r.get('answered') or r.get('sent') or r['added'], default=None)
    return {'used': used, 'left': None if used is None else max(0, round(100 - used, 1)),
            'next_watch': {'name': nxt['name'], 'at': nxt['next']} if nxt else None,
            'last_answer': {'at': _iso(last.get('answered') or last.get('sent') or last['added']), 'id': last['id'],
                            'text': (last.get('answer') or '')[:200], 'url': last.get('url')} if last else None}


def view(cfg, accounts, env=None, now=None):
    """The dashboard's Scouts card: every scout, its readiness (names of missing variables only), meter, watches, recent
    questions and answers; the owner's setup steps when a scout is not ready or none is configured."""
    now = now or time.time()
    requests = load(cfg)['requests'] if cfg.get('scouts') else []
    items = [scout_view(cfg, settings(cfg, n), requests, accounts.get((cfg['scouts'][n] or {}).get('account')), env, now)
             for n in cfg.get('scouts') or {}]
    return {'configured': bool(items), 'items': items, 'setup': list(SETUP) if not items or not all(i['ready'] for i in items) else []}


def describe(v):
    if not v['configured']:
        return 'no scouts configured: add scouts: to the config (docs/SCOUTS.md), then follow `tutti scout setup`'
    lines = []
    for s in v['items']:
        meter = s['meter']
        m = f'{meter["used"]:g} % of the week used, {meter["verdict"]}' if meter and meter.get('used') is not None else 'no meter reading'
        state = 'ready' if s['ready'] else f'not set up ({", ".join(s["missing"])} missing)'
        lines.append(f'{s["name"]} ({s["kind"]}): {state}; {m}; {s["waiting"]} waiting, {s["queued"]} queued; plan: {s["plan"]}')
        lines.append(f'  week: {s["week"]["text"]}')
        lines += [f'  watch {w["name"]}: every {w["every"]}, last {w["last"] or "never"}' for w in s['watches']]
        lines += [f'  {r["id"]} {r["status"]} {r["kind"]}: {r["task"][:70]} {r["url"] or r["why"] or ""}' for r in s['recent']]
    return '\n'.join(lines)
