"""Open pull requests of a project's GitHub repository, for the dashboard's Projects panel: a release in progress (an open pull
request whose title names a version, `... (0.1.10)`) and any other open one, each with its CI state and its review state.

`projects.<name>.repo: owner/name` turns it on. The probe thread asks `gh pr list` (the owner's own gh sign-in, read-only) at
most every CACHE_SECONDS per repository; a failure is shown on the project, never raised."""
import json
import re
import subprocess
import threading
import time

CACHE_SECONDS = 120
VERSION = re.compile(r'\b(\d+\.\d+\.\d+(?:-[\w.]+)?)\b')
JQ = ('[.[] | {number, title, url, branch: .headRefName, draft: .isDraft, decision: .reviewDecision, '
      'checks: [.statusCheckRollup[]? | {name: (.name // .context), status, conclusion, state}], '
      'reviews: [.latestReviews[]? | {author: .author.login, state}]}]')
FAILED = {'FAILURE', 'ERROR', 'CANCELLED', 'TIMED_OUT', 'ACTION_REQUIRED', 'STARTUP_FAILURE'}
PASSED = {'SUCCESS', 'NEUTRAL', 'SKIPPED'}
_cache = {}
_lock = threading.Lock()


def check_result(c):
    """'passed', 'failed' or 'pending' of one check run (status/conclusion) or commit status (state)."""
    result = (c.get('conclusion') or c.get('state') or '').upper()
    if result in FAILED:
        return 'failed'
    if result in PASSED:
        return 'passed'
    return 'pending'


def ci_state(checks):
    """(failing | pending | passing | none, a one-line detail naming the failing or pending checks)."""
    if not checks:
        return 'none', 'no checks reported'
    by = {'failed': [], 'pending': [], 'passed': []}
    for c in checks:
        by[check_result(c)].append(c.get('name') or c.get('context') or '?')
    counts = ', '.join(f'{len(v)} {k}' for k, v in by.items() if v)
    if by['failed']:
        return 'failing', f'{counts}: {", ".join(by["failed"][:4])} failed'
    if by['pending']:
        return 'pending', f'{counts}: {", ".join(by["pending"][:4])} still running'
    return 'passing', counts


def review_state(decision, reviews):
    """A short line: the review decision when the repository requires reviews, else who reviewed and how."""
    words = {'APPROVED': 'approved', 'CHANGES_REQUESTED': 'changes requested', 'REVIEW_REQUIRED': 'review required'}
    who = [f'{r.get("author") or "?"} {(r.get("state") or "").lower().replace("_", " ")}' for r in reviews or []]
    head = words.get(decision or '', '')
    if not who:
        return head or 'no review yet'
    return '; '.join(x for x in (head, ', '.join(who)) if x)


def pr_items(data):
    """The dashboard's view of `gh pr list --json ... --jq JQ`: releases first, then by number."""
    out = []
    for p in data or []:
        ci, detail = ci_state(p.get('checks'))
        found = VERSION.findall(p.get('title') or '')   # the last one: "fix the items of 0.1.8 (0.1.10)" releases 0.1.10
        url = p.get('url') if str(p.get('url') or '').startswith('https://') else None   # the page links only https addresses
        out.append({'number': p.get('number'), 'title': p.get('title') or '', 'url': url, 'branch': p.get('branch'),
                    'draft': bool(p.get('draft')), 'release': found[-1] if found else None, 'ci': ci, 'ci_detail': detail,
                    'review': review_state(p.get('decision'), p.get('reviews'))})
    out.sort(key=lambda x: (x['release'] is None, -(x['number'] or 0)))
    return out


def gh_list(repo, timeout=30):
    """The repository's open pull requests through gh, or an OSError that says why."""
    argv = ['gh', 'pr', 'list', '-R', repo, '--state', 'open', '--limit', '10', '--json',
            'number,title,url,headRefName,isDraft,reviewDecision,statusCheckRollup,latestReviews', '--jq', JQ]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as e:
        raise OSError('gh is not on PATH: install the GitHub CLI and run gh auth login once') from e
    except subprocess.TimeoutExpired as e:
        raise OSError(f'gh did not answer within {timeout} s; asked again at the next probe') from e
    if r.returncode != 0:
        raise OSError(f'gh pr list -R {repo} exited {r.returncode}: {(r.stderr or "").strip()[:160]} (is gh signed in?)')
    try:
        return json.loads(r.stdout or '[]')
    except ValueError as e:
        raise OSError(f'gh pr list -R {repo} answered something that is not JSON ({e})') from e


def repo_prs(repo, list_fn=gh_list, clock=time.time):
    """{items, error} for one repository, cached for CACHE_SECONDS; a failure keeps the last items and names the problem."""
    now = clock()
    with _lock:
        hit = _cache.get(repo)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        value = {'items': pr_items(list_fn(repo)), 'error': None}
    except OSError as e:
        value = {'items': (hit[1]['items'] if hit else []), 'error': str(e)}
    with _lock:
        _cache[repo] = (now, value)
    return value


def all_prs(cfg, list_fn=gh_list, clock=time.time):
    """{project name: {repo, items, error}} for every configured project with `repo:`."""
    out = {}
    for name, p in (cfg.get('projects') or {}).items():
        repo = (p or {}).get('repo')
        if repo:
            out[name] = {'repo': repo, **repo_prs(repo, list_fn, clock)}
    return out


def attach(projects_state, prs):
    """Puts each project's pull requests on its dashboard item (a project with open pull requests shows even when idle)."""
    items = projects_state['items']
    by_name = {p['name']: p for p in items}
    for name, value in (prs or {}).items():
        p = by_name.get(name)
        if p is None:
            if not value['items'] and not value['error']:
                continue
            p = {'name': name, 'repos': [], 'jobs': 0, 'running': 0, 'agents': 0, 'runs': 0, 'last_activity': None, 'machines': []}
            items.append(p)
        p['prs'] = value
    return projects_state
