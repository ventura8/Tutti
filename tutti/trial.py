"""The review trial of a scout (Grok Bot): for the next `prs` pull requests of the repositories in
`scouts.<name>.review_trial`, `tutti pr review` also asks the scout to review. Every reviewer's findings (Copilot, CodeRabbit,
the scout) are collected per pull request into <state>/review-trial.json, and the orchestrator marks each one real (it was
fixed) or noise. After the last pull request the verdict: the scout keeps `review` among its skills when it found a real
issue no other reviewer raised, else it stays a research scout. A scout's findings are advice: never blocking, never applied
by Tutti."""
import contextlib
import json
import os
import re
import time

from . import scouts
from .review import CODERABBIT_LOGIN, COPILOT_LOGINS, documents, gh

try:
    import fcntl
except ImportError:
    fcntl = None

FILE = 'review-trial.json'
VERDICTS = ('real', 'noise')
NEAR_LINES = 5   # two findings on one file this close are the same issue
PATH_LINE = re.compile(r'`?([\w./-]+\.[A-Za-z0-9]+):(\d+)')
BULLET = re.compile(r'^\s*(?:[-*]|\d+[.)])\s+(.+)')
TASK = ('Review pull request {repo}#{number} ({url}) through your GitHub connection: read its description and its whole diff. '
        'Report only real problems: bugs, wrong behaviour, missing error handling, security issues, tests that do not test what '
        'they claim, docs that contradict the code. Post ONE comment on the pull request. Its first line is exactly "{marker}". '
        'Then one Markdown bullet per finding: `path:line` - what is wrong - the fix; at most 15, most serious first. If there is '
        'nothing worth fixing, write "No findings." under the first line. Never push, approve, request changes or edit anything.')
SECURITY_TASK = (
    'Review pull request {repo}#{number} ({url}) through your GitHub connection with a focus on security: '
    'read its description and its whole diff. Report only real security problems: vulnerabilities, credential/secret leaks, '
    'injection, authentication/authorization flaws, unsafe input handling, permission issues, or unsafe dependencies. '
    'Post ONE comment on the pull request. Its first line is exactly "{marker}". '
    'Then one Markdown bullet per finding: `path:line` - what is wrong - the fix; at most 15, most serious first. '
    'If there is nothing worth fixing, write "No findings." under the first line. Never push, approve, request changes or edit anything.'
)


class TrialError(scouts.ScoutError):
    """A trial command cannot be done; the message says why and what to type instead."""


def path_of(cfg):
    from . import config
    return os.path.join(config.state_dir(cfg), FILE)


def load(cfg):
    try:
        with open(path_of(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    data.setdefault('scouts', {})
    return data


@contextlib.contextmanager
def locked(cfg):
    p = path_of(cfg)
    with open(p + '.lock', 'w', encoding='utf8') as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        data = load(cfg)
        yield data
        tmp = f'{p}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf8') as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, p)


def settings(cfg, name):
    t = scouts.settings(cfg, name).get('review_trial') or {}
    return {'repos': list(t.get('repos') or []), 'prs': int(t.get('prs', 5))} if t else None


def errors(key, t):
    """Config problems of scouts.<name>.review_trial."""
    if t is None:
        return []
    if not isinstance(t, dict) or not isinstance(t.get('repos'), list) or not t['repos']:
        return [f'{key} needs repos: the repositories whose next pull requests the scout also reviews, e.g. [octo/my-app]']
    errs = [f'{key}.repos has {r!r}: write owner/name' for r in t['repos'] if not scouts.is_repo(r)]
    n = t.get('prs', 5)
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        errs.append(f'{key}.prs is {n!r}: how many pull requests the trial lasts, a whole number such as 5')
    return errs


def key_of(repo, number):
    return f'{repo}#{number}'


def split_key(key):
    m = re.fullmatch(r'([\w.-]+/[\w.-]+)#(\d+)', key or '')
    if not m:
        raise TrialError(f'"{key}" is not a pull request: write owner/name#number, e.g. octo/my-app#12')
    return m.group(1), int(m.group(2))


def entry(data, name):
    return data['scouts'].setdefault(name, {'prs': [], 'verdict': None})


def find_pr(e, key):
    for p in e['prs']:
        if p['key'] == key:
            return p
    raise TrialError(f'{key} is not in the trial: `tutti scout trial` lists its pull requests')


def eligible(cfg, data, name, repo):
    """The scout's trial still takes pull requests of `repo` (read from `data`, the trial file)."""
    t = settings(cfg, name)
    e = data['scouts'].get(name) or {'prs': [], 'verdict': None}
    return bool(t) and repo in t['repos'] and not e['verdict'] and len(e['prs']) < t['prs']


def trial_scout(cfg, repo):
    """The scout whose trial still takes pull requests of `repo`, or None."""
    data = load(cfg)
    return next((n for n in cfg.get('scouts') or {} if eligible(cfg, data, n, repo)), None)


def may_review(cfg, name):
    """'' when the scout may review, else why not: `review` in its skills, a running trial, or a trial it passed."""
    if 'review' in scouts.settings(cfg, name)['skills']:
        return ''
    t, e = settings(cfg, name), load(cfg)['scouts'].get(name) or {}
    if (e.get('verdict') or {}).get('promote'):
        return ''
    if t and not e.get('verdict'):
        return ''
    return (f'scout {name} does not review pull requests: its review trial ended with "keep it to research"'
            if e.get('verdict') else f'scout {name} has no review skill: add review to scouts.{name}.skills, or a review_trial')


def reserve(cfg, name, repo, number):
    """Under the trial lock, before anything is sent: (the pull request's trial record or None, whether this call added it).
    A slot is taken here, so two reviews at once never both take the last one."""
    k = key_of(repo, number)
    with locked(cfg) as data:
        e = entry(data, name)
        p = next((x for x in e['prs'] if x['key'] == k), None)
        if p is None and eligible(cfg, data, name, repo):
            p = {'key': k, 'repo': repo, 'number': number, 'request': None, 'added': time.time(), 'findings': [], 'closed': False}
            e['prs'].append(p)
            return p, True
        return p, False


def settle(cfg, name, key, r, new):
    """After the send: a review that went out is linked to its trial record; a failed one frees the slot it took."""
    with locked(cfg) as data:
        e = entry(data, name)
        p = next((x for x in e['prs'] if x['key'] == key), None)
        if p is None:
            return
        if r['status'] == 'waiting':
            p['request'] = r['id']
        elif new and not p.get('request'):
            e['prs'].remove(p)


def active(cfg, rid):
    """The trial's request when it is still waiting or answered (sent once, never again), else None."""
    if not rid:
        return None
    with contextlib.suppress(scouts.ScoutError):
        r = scouts.find(scouts.load(cfg), rid)
        return r if r['status'] in ('waiting', 'answered') else None
    return None


def _settle_trial_review(cfg, name, repo, number, payload, new, security):
    """Records a trial review outcome unless this was a security-focus ask (those skip the trial ledger)."""
    if not security:
        settle(cfg, name, key_of(repo, number), payload, new)


def ask_review(cfg, name, repo, number, url, gh_fn=gh, post_fn=scouts.post, env=None, accounts=None, listed=False, security=False):
    """Sends the scout a review of one pull request (it answers on the pull request) and returns the request. A pull request
    of the trial whose review already went out is not sent again. `listed`: the scout is in review.reviewers, which is the
    owner's consent to its reviews whatever its trial says. `security`: a security-focus ask that does not share the trial
    ledger with a general review."""
    why = '' if listed else may_review(cfg, name)
    if why:
        raise TrialError(why)
    sc = scouts.settings(cfg, name)
    scouts.guard(cfg, sc, accounts)
    target, key = scouts.secrets(sc, env)
    p, new = (None, False) if security else reserve(cfg, name, repo, number)
    sent = active(cfg, (p or {}).get('request'))
    if sent:
        return sent
    tmpl = SECURITY_TASK if security else TASK
    task = tmpl.format(repo=repo, number=number, url=url, marker=scouts.REVIEW_MARKER)
    try:
        with scouts.locked(cfg) as data:
            r = scouts.new_request(data, sc, task, 'review', repo=repo, skill='review')
            r.update(issue=number, url=url)
            scouts.send(sc, r, target, key, gh_fn, post_fn)
    except BaseException:
        _settle_trial_review(cfg, name, repo, number, {'status': 'failed'}, new, security)
        raise
    _settle_trial_review(cfg, name, repo, number, r, new, security)
    return r


def in_trial(cfg, repo, number, say, gh_fn=gh, post_fn=scouts.post, env=None):
    """`tutti pr review`'s hook: during a trial the scout is asked too. Never raises: a scout's trouble never blocks a review."""
    name = trial_scout(cfg, repo)
    if not name:
        return None
    try:
        rc, url, _err = gh_fn(['pr', 'view', str(number), '--repo', repo, '--json', 'url', '--jq', '.url'])
        r = ask_review(cfg, name, repo, number, url.strip() if rc == 0 else f'https://github.com/{repo}/pull/{number}',
                       gh_fn, post_fn, env)
    except scouts.ScoutError as e:
        say(f'review: the {name} scout\'s trial review was not sent ({e}); the review goes on without it')
        return None
    except (ValueError, OSError) as e:   # a broken state file or address: named by its kind only, never its text
        say(f'review: the {name} scout\'s trial review was not sent ({type(e).__name__} while preparing it: check '
            f'`tutti scout list` and the state folder); the review goes on without it')
        return None
    say(f'review: also asked the {name} scout (trial, advisory only): {r["status"]}'
        + (f' ({r.get("why")})' if r['status'] == 'failed' else ''))
    return r


# ---- findings

def scout_findings(text):
    """The bullets of a scout's review comment, each with its path and line when it names one."""
    out = []
    for line in (text or '').splitlines():
        m = BULLET.match(line)
        if m:
            pl = PATH_LINE.search(m.group(1))
            out.append({'path': pl.group(1) if pl else None, 'line': int(pl.group(2)) if pl else None, 'text': m.group(1)[:240]})
    return out


def who_of(login):
    if login in COPILOT_LOGINS:
        return 'copilot'
    return 'coderabbit' if login == CODERABBIT_LOGIN else None


def bot_findings(repo, number, gh_fn=gh):
    """Copilot's and CodeRabbit's inline review comments on the pull request: one finding each (replies left out)."""
    rc, text, err = gh_fn(['api', '--paginate', f'repos/{repo}/pulls/{number}/comments?per_page=100'])
    if rc != 0:
        raise TrialError(f'reading the review comments of {repo}#{number} failed ({err}): check `gh auth status`')
    out = []
    for c in (x for page in documents(text) for x in (page if isinstance(page, list) else [page])):
        who = who_of((c.get('user') or {}).get('login', ''))
        if who and not c.get('in_reply_to_id'):
            out.append({'reviewer': who, 'path': c.get('path'), 'line': c.get('line') or c.get('original_line'),
                        'text': re.sub(r'\s+', ' ', c.get('body') or '')[:240], 'ref': c.get('id')})
    return out


def merge_findings(old, new):
    """New findings join with ids; one already there (same reviewer, path, line and start of text) keeps its verdict."""
    seen = {(f['reviewer'], f.get('path'), f.get('line'), f['text'][:80]) for f in old}
    out = list(old)
    for f in new:
        if (f['reviewer'], f.get('path'), f.get('line'), f['text'][:80]) not in seen:
            out.append({**f, 'id': f'f{len(out) + 1}', 'verdict': None})
    return out


def collect(cfg, name, key, gh_fn=gh):
    """Reads every reviewer's findings of one trial pull request into the trial file; returns the pull request's record."""
    repo, number = split_key(key)
    req = None
    with scouts.locked(cfg) as sdata:
        with contextlib.suppress(scouts.ScoutError):
            req = scouts.find(sdata, find_pr(entry(load(cfg), name), key)['request'])
            if req['status'] == 'waiting':
                scouts.collect(scouts.settings(cfg, name), req, gh_fn)
    found = bot_findings(repo, number, gh_fn)
    if req and req.get('answer'):
        found += [{**f, 'reviewer': name} for f in scout_findings(req['answer'])]
    with locked(cfg) as data:
        p = find_pr(entry(data, name), key)
        p['findings'] = merge_findings(p['findings'], found)
        p['scout_status'] = req['status'] if req else 'unknown'
        return p


def mark(cfg, name, key, fid, verdict):
    if verdict not in VERDICTS:
        raise TrialError(f'"{verdict}": use real (it was fixed) or noise (nothing to fix)')
    with locked(cfg) as data:
        p = find_pr(entry(data, name), key)
        f = next((f for f in p['findings'] if f['id'] == fid), None)
        if not f:
            raise TrialError(f'{key} has no finding {fid}: `tutti scout trial show {key}` lists them')
        f['verdict'] = verdict
        return f


def close(cfg, name, key):
    """Ends one pull request of the trial (every finding marked); after the last one the verdict is written."""
    with locked(cfg) as data:
        e = entry(data, name)
        p = find_pr(e, key)
        if p.get('scout_status') in (None, 'waiting', 'unknown'):
            raise TrialError(f'{key}: the scout\'s review is not in yet ({p.get("scout_status") or "not collected"}): run '
                             f'`tutti scout trial collect {key}` once its comment is there (or it timed out)')
        open_ids = [f['id'] for f in p['findings'] if not f.get('verdict')]
        if open_ids:
            raise TrialError(f'{key}: mark {", ".join(open_ids)} first (tutti scout trial mark {key} <id> real|noise)')
        p['closed'] = True
        t = settings(cfg, name)
        if t and sum(1 for x in e['prs'] if x['closed']) >= t['prs'] and not e['verdict']:
            e['verdict'] = verdict(name, e['prs'])
        return e


def unique(f, others):
    """A finding no other reviewer raised: none of theirs on the same file within NEAR_LINES lines."""
    if not f.get('path'):
        return True
    return not any(o.get('path') == f['path'] and (o.get('line') is None or f.get('line') is None
                                                   or abs(o['line'] - f['line']) <= NEAR_LINES) for o in others)


def tally(name, prs):
    """{reviewer: {raised, real, noise, open}} over the pull requests, and the scout's real findings nobody else raised."""
    rows, only = {}, 0
    for p in prs:
        for f in p['findings']:
            row = rows.setdefault(f['reviewer'], {'raised': 0, 'real': 0, 'noise': 0, 'open': 0})
            row['raised'] += 1
            row[f.get('verdict') or 'open'] += 1
        others = [f for f in p['findings'] if f['reviewer'] != name]
        only += sum(1 for f in p['findings'] if f['reviewer'] == name and f.get('verdict') == 'real' and unique(f, others))
    return rows, only


def verdict(name, prs):
    rows, only = tally(name, prs)
    mine = rows.get(name) or {'raised': 0, 'real': 0, 'noise': 0}
    promote = only > 0
    why = (f'{name} found {only} real issue(s) no other reviewer raised in {len(prs)} pull requests: review joins its skills '
           '(advisory, never blocking)' if promote else
           f'{name} found no real issue the others missed in {len(prs)} pull requests ({mine["real"]} real, {mine["noise"]} noise): '
           'it stays a research scout')
    return {'promote': promote, 'unique_real': only, 'why': why, 'at': time.time()}


def view(cfg):
    """The dashboard's trial summary per scout with a trial: progress, per-reviewer tallies, the verdict."""
    data = load(cfg)
    out = []
    for name in cfg.get('scouts') or {}:
        t, e = settings(cfg, name), data['scouts'].get(name)
        if not t and not e:
            continue
        e = e or {'prs': [], 'verdict': None}
        rows, only = tally(name, e['prs'])
        out.append({'scout': name, 'repos': (t or {}).get('repos', []), 'limit': (t or {}).get('prs', 0),
                    'prs': [{'key': p['key'], 'closed': p['closed'], 'findings': len(p['findings']),
                             'open': sum(1 for f in p['findings'] if not f.get('verdict'))} for p in e['prs']],
                    'closed': sum(1 for p in e['prs'] if p['closed']), 'reviewers': rows, 'unique_real': only, 'verdict': e['verdict']})
    return out


def describe(items):
    if not items:
        return 'no review trial: add scouts.<name>.review_trial: {repos: [owner/name], prs: 5} to start one'
    lines = []
    for v in items:
        lines.append(f'{v["scout"]}: {v["closed"]}/{v["limit"]} pull requests done; {v["unique_real"]} real finding(s) only it raised')
        lines += [f'  {who}: {r["raised"]} raised, {r["real"]} real, {r["noise"]} noise, {r["open"]} to mark'
                  for who, r in v['reviewers'].items()]
        lines += [f'  {p["key"]}: {"done" if p["closed"] else "open"}, {p["findings"]} finding(s), {p["open"]} to mark' for p in v['prs']]
        lines.append(f'  verdict: {v["verdict"]["why"]}' if v['verdict'] else '  verdict: after the last pull request')
    return '\n'.join(lines)


def show(cfg, name, key):
    p = find_pr(entry(load(cfg), name), key)
    rows = [f'{f["id"]} [{f["reviewer"]}] {f.get("verdict") or "?"} {f.get("path") or ""}{":" + str(f["line"]) if f.get("line") else ""} '
            f'{f["text"][:140]}' for f in p['findings']]
    return '\n'.join([f'{key} ({"done" if p["closed"] else "open"}):'] + (rows or ['  no findings collected: tutti scout trial collect']))
