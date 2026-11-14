"""`tutti pr review <number>`: ask the automated reviewers, find out when none will come, and fall back to a cheap local
review that is posted to the pull request. Reviewers are GitHub's Copilot code review and CodeRabbit; both may be absent,
rate-limited (CodeRabbit's free plan says "Review limit reached") or simply never answer. Everything goes through `gh`."""
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone

from . import pacing, routing, state, tz, workers

COPILOT_LOGINS = ('copilot-pull-request-reviewer[bot]', 'copilot-pull-request-reviewer', 'Copilot')
# the names the REST API has taken; 'Copilot' first (PRs #9-#20 of the Tutti repository). The answer is checked: GitHub
# answers 201 and drops the request for a name it does not take, and 422 "not a collaborator" for the bare bot login.
COPILOT_REQUEST_AS = ('Copilot', 'copilot-pull-request-reviewer[bot]')
CODERABBIT_LOGIN = 'coderabbitai[bot]'
CODERABBIT_LIMITED = ('rate limited by coderabbit.ai', 'review limit reached', 'review rate limited', 'reviews paused',
                      'rate limit exceeded')
PROMPT = ('You are reviewing a pull request. The full diff is in the file pr.diff in the current directory; read it. Report only real '
          'problems: bugs, wrong behaviour, missing error handling, security issues, tests that do not test what they claim, docs that '
          'contradict the code. For each finding write one Markdown bullet: `path:line` - what is wrong - the fix. At most 15 bullets, '
          'most serious first. If there is nothing worth fixing, write exactly: No findings. Do not edit any file.')


def documents(text):
    """Every JSON document in `text`, written back to back (`gh api --paginate`: one per page)."""
    decoder, out, i, text = json.JSONDecoder(), [], 0, text or ''
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return out
        doc, i = decoder.raw_decode(text, i)
        out.append(doc)


def gh(args, run=subprocess.run):
    """(exit code, stdout, stderr) of one `gh` call."""
    r = run(['gh', *args], capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr.strip()


def repo_of(pr_repo, gh_fn=gh):
    if pr_repo:
        return pr_repo
    rc, out, err = gh_fn(['repo', 'view', '--json', 'nameWithOwner', '--jq', '.nameWithOwner'])
    if rc != 0 or not out.strip():
        raise RuntimeError(f'cannot tell which GitHub repository this is ({err}): pass --repo <owner/name> or cd into its checkout')
    return out.strip()


def request(repo, number, who, gh_fn=gh):
    """Asks one reviewer. Returns '' when the request went out, else why it cannot (the reviewer then counts as absent)."""
    if who == 'coderabbit':
        rc, _out, err = gh_fn(['pr', 'comment', str(number), '--repo', repo, '--body', '@coderabbitai review'])
        return '' if rc == 0 else f'could not comment on the pull request ({err})'
    errors = []
    for login in COPILOT_REQUEST_AS:
        rc, out, err = gh_fn(['api', '-X', 'POST', f'repos/{repo}/pulls/{number}/requested_reviewers', '-f', f'reviewers[]={login}'])
        if rc == 0 and copilot_listed(out):
            return ''
        errors.append(err if rc else f'GitHub answered 201 for "{login}" but did not list Copilot as a reviewer')
    if request_by_graphql(repo, number, gh_fn):
        return ''
    return (f'GitHub did not add Copilot as a reviewer ({errors[-1]}): check that Copilot code review is on for this repository '
            '(Settings > Copilot > Code review) and that the head commit is not reviewed already')


def copilot_listed(out):
    """True when a requested_reviewers answer (REST) names Copilot. GitHub answers 201 even when it silently drops the
    request (a login it does not take as a reviewer), so the answer's list is what counts, never the status alone."""
    try:
        data = json.loads(out or '{}')
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    return any(isinstance(r, dict) and r.get('login') in COPILOT_LOGINS for r in data.get('requested_reviewers') or [])


GRAPHQL_REQUEST = ('mutation($id:ID!){requestReviewsByLogin(input:{pullRequestId:$id,botLogins:["copilot-pull-request-reviewer"],'
                   'union:true}){pullRequest{reviewRequests(first:20){nodes{requestedReviewer{... on Bot{login}}}}}}}')


def request_by_graphql(repo, number, gh_fn=gh):
    """The GraphQL way (requestReviewsByLogin with botLogins), which takes Copilot as the bot it is. True when it is listed."""
    rc, node, _err = gh_fn(['api', f'repos/{repo}/pulls/{number}', '--jq', '.node_id'])
    if rc != 0 or not node.strip():
        return False
    rc, out, _err = gh_fn(['api', 'graphql', '-f', f'id={node.strip()}', '-f', f'query={GRAPHQL_REQUEST}'])
    if rc != 0:
        return False
    try:
        nodes = json.loads(out)['data']['requestReviewsByLogin']['pullRequest']['reviewRequests']['nodes']
    except (ValueError, KeyError, TypeError):
        return False
    return any(((n or {}).get('requestedReviewer') or {}).get('login') in COPILOT_LOGINS for n in nodes)


def events(repo, number, gh_fn=gh):
    """[(login, time, text, kind)] of the pull request's reviews (kind 'review') and comments ('comment'). The time is the
    latest of created and updated: CodeRabbit edits its summary comment in place to say "Review limit reached"."""
    out = []
    for path, when, kind in ((f'repos/{repo}/pulls/{number}/reviews', 'submitted_at', 'review'),
                             (f'repos/{repo}/issues/{number}/comments', 'created_at', 'comment')):
        # --paginate prints one JSON document per page, back to back; --slurp would join them but gh before 2.62 lacks it
        rc, text, err = gh_fn(['api', '--paginate', f'{path}?per_page=100'])
        if rc != 0:
            raise RuntimeError(f'reading {path} failed ({err}): check `gh auth status`')
        pages = documents(text)
        for item in (x for page in pages for x in (page if isinstance(page, list) else [page])):
            times = [pacing.parse_time(item[k]) for k in (when, 'updated_at') if item.get(k)]
            if times:
                out.append(((item.get('user') or {}).get('login', ''), max(times), item.get('body') or '', kind))
    return out


SCOUT_MARKER = 'grok-scout'   # the first line of a scout's review comment (tutti/scouts.py REVIEW_MARKER)


def status(who, evs, since, author=None):
    """'reviewed' (a pull request review after `since`), 'limited' (it answered that it will not review now) or 'pending'.
    A comment is never a review: CodeRabbit acknowledges a request ("Action not completed", "Review triggered") in one.
    A scout (any other name) posts as the GitHub account connected to it (`author`: the owner's, or scouts.<name>.github_login):
    its review is a comment by that account whose first line is the marker; anyone else's marker counts for nothing."""
    if who not in ('copilot', 'coderabbit'):
        found = any(t >= since and body.lstrip().lower().startswith(SCOUT_MARKER) and (author is None or login == author)
                    for login, t, body, _k in evs)
        return 'reviewed' if found else 'pending'
    logins = COPILOT_LOGINS if who == 'copilot' else (CODERABBIT_LOGIN,)
    mine = [(body, kind) for login, t, body, kind in evs if login in logins and t >= since]
    if any(kind == 'review' for _b, kind in mine):
        return 'reviewed'
    if who == 'coderabbit' and any(any(k in body.lower() for k in CODERABBIT_LIMITED) for body, _k in mine):
        return 'limited'
    return 'pending'


def wait_for(repo, number, asked, since, timeout_s, poll_s, gh_fn=gh, clock=time.monotonic, sleep=time.sleep, authors=None):
    """Polls until a reviewer reviewed, or every asked reviewer is limited, or the time is up. Returns {who: status}."""
    end = clock() + timeout_s
    while True:
        evs = events(repo, number, gh_fn)
        found = {w: status(w, evs, since, (authors or {}).get(w)) for w in asked}
        if any(s == 'reviewed' for s in found.values()) or all(s == 'limited' for s in found.values()) or clock() >= end:
            return found
        sleep(poll_s)


def pick_fallback(cfg, now=None, available=None):
    """The first fallback worker that is installed and whose account is not in panic or spent; (name, args) or (None, why)."""
    now = now or datetime.now(timezone.utc)
    available = available or workers.available
    entries = pacing.read_log(state.usage_log_path(cfg))
    why = []
    for name in cfg['review']['fallback']:
        ok, note = available(cfg['workers'][name])
        args, reason = worker_room(cfg, name, entries, now) if ok else (None, note)
        if args is not None:
            return name, args
        why.append(f'{name}: {reason}')
    return None, '; '.join(why) or 'review.fallback names no worker'


def worker_room(cfg, name, entries, now):
    """(the args of the worker's account with room, '') or (None, why). A worker no account names always has room."""
    accounts = [n for n, a in cfg['accounts'].items() if n == name or (a or {}).get('worker') == name]
    if not accounts:
        return [], ''
    states = [pacing.account_state(n, cfg['accounts'][n], entries, now, *pacing.plan_args(cfg)) for n in accounts]
    best, reason = routing.pick_account(states)
    if not best:
        return None, reason
    return list((cfg['accounts'][best['name']] or {}).get('args') or []), ''


def local_review(cfg, repo, number, name, args, gh_fn=gh, run_worker=None):
    """The fallback: the pull request's diff in an empty folder, the worker asked to review it. Returns the review text."""
    rc, diff, err = gh_fn(['pr', 'diff', str(number), '--repo', repo])
    if rc != 0:
        raise RuntimeError(f'gh pr diff {number} failed ({err})')
    with tempfile.TemporaryDirectory(prefix='tutti-review-') as d:
        with open(os.path.join(d, 'pr.diff'), 'w', encoding='utf8') as f:
            f.write(diff)
        argv = workers.with_args(workers.command(cfg['workers'][name], PROMPT), args)
        run_worker = run_worker or (lambda a, cwd: subprocess.run(a, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                                                    timeout=1800, env=workers.worker_env(cfg)))
        r = run_worker(argv, d)
    text = (r.stdout or '').strip()
    if r.returncode != 0 or not text:
        raise RuntimeError(f'{name} did not finish the review (exit {r.returncode}): {(r.stderr or "")[-300:]}')
    return text


def comment(repo, number, body, gh_fn=gh):
    with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False, encoding='utf8') as f:
        f.write(body)
    try:
        rc, _out, err = gh_fn(['pr', 'comment', str(number), '--repo', repo, '--body-file', f.name])
    finally:
        os.unlink(f.name)
    if rc != 0:
        raise RuntimeError(f'posting the review to pull request {number} failed ({err})')


# ---- a reviewer whose allowance is used: skipped until its reset (its account reading at 100 % with a reset time)

LIMIT_WAIT_RX = re.compile(r'wait\W+(?:(\d+)\s*hours?)?\W*(?:and\W+)?(?:(\d+)\s*minutes?)?', re.I)
LIMIT_DEFAULT = timedelta(hours=1)


def skipped_until(entries, who, now):
    """The reset time while the reviewer's account (the account named like the reviewer: `copilot`, `coderabbit`) reads
    100 % or more and that reset is still ahead, else None: after the reset the reviewer is asked again by itself."""
    last = pacing.latest(entries, who)
    if not last or float(last.get('used') or 0) < 100 or not last.get('resets'):
        return None
    reset = pacing.parse_time(last['resets'])
    return reset if reset > now else None


def limit_reset(text, now):
    """When a rate-limit comment says the reviewer comes back ("Please wait **26 minutes and 35 seconds**"); an hour when
    it does not say."""
    m = LIMIT_WAIT_RX.search(text or '')
    if m and (m.group(1) or m.group(2)):
        return now + timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0) + 1)
    return now + LIMIT_DEFAULT


def note_limited(cfg, repo, number, who, evs, since, now):
    """Writes the reviewer's account at 100 % until the reset its rate-limit comment names, so the next pull requests skip it."""
    logins = COPILOT_LOGINS if who == 'copilot' else (CODERABBIT_LOGIN,)
    bodies = [b for login, t, b, _k in evs if login in logins and t >= since]
    reset = limit_reset(bodies[-1] if bodies else '', now)
    pacing.append_log(state.usage_log_path(cfg), who, 100, now, f'rate-limit comment on {repo}#{number}', resets=reset,
                      source='limit message', ref=f'{repo}#{number}')
    return reset


def ask_scout(cfg, repo, number, who, gh_fn=gh):
    """'' when the scout `who` was asked to review (an advisory comment on the pull request), else why not: its allowance is
    used, it is not set up. Listed in review.reviewers, it reviews whatever its trial says."""
    from . import scouts, trial  # trial builds on scouts, which imports this module
    url = f'https://github.com/{repo}/pull/{number}'
    try:
        r = trial.ask_review(cfg, who, repo, int(number), url, gh_fn, listed=True)
    except scouts.ScoutError as e:
        return str(e)
    return '' if r['status'] in ('waiting', 'answered') else (r.get('why') or f'its request {r["id"]} {r["status"]}')


def scout_author(cfg, who, gh_fn=gh):
    """The GitHub login a scout posts as: scouts.<name>.github_login, else the account `gh` is signed in with (Grok Bot posts
    through the owner's GitHub connection). An empty answer means none is known: then no comment counts as its review."""
    given = ((cfg.get('scouts') or {}).get(who) or {}).get('github_login')
    if given:
        return given
    rc, out, _ = gh_fn(['api', 'user', '--jq', '.login'])
    return out.strip() if rc == 0 and out.strip() else ''


def request_any(cfg, repo, number, who, gh_fn=gh):
    return ask_scout(cfg, repo, number, who, gh_fn) if who in (cfg.get('scouts') or {}) else request(repo, number, who, gh_fn)


def review(cfg, number, pr_repo=None, timeout_minutes=None, post=True, gh_fn=gh, say=print, **kw):
    """The whole flow. Returns 0 when a reviewer answered or the fallback review was posted, 1 otherwise."""
    r = cfg['review']
    repo = repo_of(pr_repo, gh_fn)
    since = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        entries = pacing.read_log(state.usage_log_path(cfg))
    except (OSError, ValueError):
        entries = []
    asked, absent = [], {}
    for who in r['reviewers']:
        until = skipped_until(entries, who, since)
        why = f'skipped until {tz.show(until)}: its allowance is used (account {who} at 100 %)' if until else \
            request_any(cfg, repo, number, who, gh_fn)
        if why:
            absent[who] = why
            say(f'review: {who} will not come: {why}')
        else:
            asked.append(who)
            say(f'review: asked {who}')
    timeout = float(r['timeout_minutes'] if timeout_minutes is None else timeout_minutes) * 60
    authors = {w: scout_author(cfg, w, gh_fn) for w in asked if w in (cfg.get('scouts') or {})}
    found = wait_for(repo, number, asked, since, timeout, float(r['poll_seconds']), gh_fn, authors=authors, **kw) if asked else {}
    limited = [w for w, s in found.items() if s == 'limited']
    if limited:
        evs, now = events(repo, number, gh_fn), datetime.now(timezone.utc)
        for w in limited:
            say(f'review: {w} is rate-limited: skipped until {tz.show(note_limited(cfg, repo, number, w, evs, since, now))}')
    if any(s == 'reviewed' for s in found.values()):
        say(f'review: {", ".join(w for w, s in found.items() if s == "reviewed")} reviewed pull request {number}: '
            'read and resolve its threads')
        return 0
    lines = [f'{w}: {"said it will not review now (limit reached)" if s == "limited" else f"no review within {timeout / 60:g} min"}'
             for w, s in found.items()] + [f'{w}: {why}' for w, why in absent.items()]
    say('review: no automated review will come: ' + '; '.join(lines or ['no reviewer configured (review.reviewers)']))
    name, args = pick_fallback(cfg)
    if not name:
        say(f'review: no fallback worker can review either ({args}): review the diff yourself')
        return 1
    say(f'review: a local review by {name} {" ".join(args)}'.rstrip())
    text = local_review(cfg, repo, number, name, args, gh_fn)
    body = (f'### Local review by {name}\n\nNo automated review arrived ({"; ".join(lines) or "none configured"}), so Tutti asked a '
            f'local worker. Findings are suggestions to verify, not a verdict.\n\n{text}\n')
    if post:
        comment(repo, number, body, gh_fn)
        say(f'review: posted {name}\'s review to {repo}#{number}')
    else:
        say(body)
    from . import fill  # it imports this module
    if not fill.verdict(text)[0]:
        say('review: the local review has findings: a person reads them before this pull request merges (exit 3)')
        return 3   # never "reviewed" for an automatic merge (selfimprove.review_finished needs 0)
    return 0

