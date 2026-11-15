"""Tutti's one GitHub client (docs/GITHUB.md): every GitHub call of Tutti goes through this module.

How a call is authorised, in order:

1. the person's own connection: *Connect GitHub* on the Me view (or `tutti github connect`) runs GitHub's device flow
   (a code and a QR, like the Google sign-in) through Tutti's GitHub App; the token is kept per user in
   <state>/github/<hash of the user>.json, mode 600, refreshed by itself and removed by *Disconnect*;
2. the household's connection (the admin who connected first, or `tutti github connect` on the dashboard's computer) for
   work that runs for nobody in particular: the autopilot, `tutti doctor`, the Projects panel;
3. the GitHub CLI's own sign-in (`gh auth token`), while it is logged in: what Tutti did before the App existed.

All three use GitHub's REST and GraphQL API directly, so Tutti needs no `gh` once a person connected. Reads are polled
with ETags (a 304 costs no rate limit) and a rate limit that ran out stops the calls until GitHub's reset time. No
webhooks: the dashboard stays on the LAN. A token never reaches a log, an error message, argv or a worker's environment
(`worker_env`)."""
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = 'https://api.github.com'
DEVICE_CODE_URL = 'https://github.com/login/device/code'
TOKEN_URL = 'https://github.com/login/oauth/access_token'
DEVICE_PAGE = 'https://github.com/login/device'
GRANT = 'urn:ietf:params:oauth:grant-type:device_code'
FOLDER = 'github'
HOUSEHOLD = 'household.json'
TOKEN_VARS = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN')
# the App's permissions: least privilege for what Tutti does (open, merge and read pull requests; file issues; read CI)
PERMISSIONS = {'contents': 'write', 'pull_requests': 'write', 'issues': 'write', 'checks': 'read', 'actions': 'read',
               'metadata': 'read'}
ROUTE = '/api/github'   # the dashboard's POST (API is GitHub's own address)
APP_SLUG = 'tutti-orchestrator'
MAX_FLOWS = 20
LOW_RATE = 20   # below this many calls left, only the household's own polls wait for the reset


class GitHubError(Exception):
    """A GitHub call did not succeed: the message says why and what to do (never a token)."""


# ---- the App and the token store

def client_id(cfg):
    """The GitHub App's client ID: `github.client_id` in the config, else the release's, else ''."""
    from . import official
    own = str(((cfg or {}).get('github') or {}).get('client_id') or '').strip()
    return own or str(official.release().get('github_client_id') or '').strip()


def client_secret():
    """The App's client secret when the release carries one (GitHub asks for it to refresh a token), else ''."""
    from . import official
    return str(official.release().get('github_client_secret') or '').strip()


def folder(cfg):
    from . import config
    d = os.path.join(config.state_dir(cfg), FOLDER)
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def token_path(cfg, sub):
    return os.path.join(folder(cfg), hashlib.sha256(str(sub).encode()).hexdigest()[:32] + '.json')


def _read(path):
    try:
        with open(path, encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path, data):
    tmp = f'{path}.{secrets.token_hex(4)}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf8') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def save(cfg, sub, answer, login, clock=time.time):
    """Keeps a token answer of GitHub's for one user, mode 600; the first one also becomes the household's."""
    now = clock()
    rec = {'access_token': answer['access_token'], 'refresh_token': answer.get('refresh_token') or '', 'login': login,
           'expires_at': now + int(answer['expires_in']) if answer.get('expires_in') else 0,
           'refresh_expires_at': now + int(answer['refresh_token_expires_in']) if answer.get('refresh_token_expires_in') else 0,
           'connected': now}
    _write(token_path(cfg, sub), rec)
    if not _read(os.path.join(folder(cfg), HOUSEHOLD)).get('sub'):
        _write(os.path.join(folder(cfg), HOUSEHOLD), {'sub': sub})


def forget(cfg, sub):
    """Disconnect: the token is removed here (revoke the App at github.com/settings/apps/authorizations). True when one was kept."""
    path = token_path(cfg, sub)
    if not os.path.exists(path):
        return False
    os.remove(path)
    pointer = os.path.join(folder(cfg), HOUSEHOLD)
    if _read(pointer).get('sub') == sub:
        os.remove(pointer)
    return True


def form_post(url, fields, http=None):
    """(status, JSON) of a form POST to one of GitHub's fixed OAuth endpoints."""
    status, _h, raw = (http or urlopen_http)('POST', url, {'Accept': 'application/json',
                                                           'Content-Type': 'application/x-www-form-urlencoded'},
                                             urllib.parse.urlencode(fields).encode())
    try:
        return status, json.loads(raw or b'{}')
    except ValueError:
        return status, {}


REVOKED = ('bad_refresh_token', 'invalid_grant', 'unauthorized_client', 'incorrect_client_credentials')
_refresh_lock = threading.Lock()   # refresh tokens are single use: one refresh at a time, the file re-read inside


def refresh(cfg, sub, rec, http=None, clock=time.time):
    """A fresh record for an expired token, or {}. The file is removed only when GitHub says the refresh token is dead (or it
    expired); a network failure or a 5xx keeps it for the next call."""
    if not rec.get('refresh_token') or (rec.get('refresh_expires_at') and rec['refresh_expires_at'] <= clock()):
        forget(cfg, sub)
        return {}
    fields = {'client_id': client_id(cfg), 'grant_type': 'refresh_token', 'refresh_token': rec['refresh_token']}
    if client_secret():
        fields['client_secret'] = client_secret()
    try:
        status, answer = form_post(TOKEN_URL, fields, http)
    except GitHubError:
        return {}
    if status == 200 and answer.get('access_token'):
        save(cfg, sub, answer, rec.get('login') or '', clock)
        return _read(token_path(cfg, sub))
    if answer.get('error') in REVOKED:
        forget(cfg, sub)
    return {}


def _expired(rec, clock):
    return bool(rec and rec.get('expires_at') and rec['expires_at'] - 60 <= clock())


def user_token(cfg, sub, http=None, clock=time.time):
    """(token, login) of one user's connection, refreshed when it expires within a minute; ('', '') when none."""
    if not sub:
        return '', ''
    rec = _read(token_path(cfg, sub))
    if _expired(rec, clock):
        with _refresh_lock:
            rec = _read(token_path(cfg, sub))   # another thread may have refreshed it meanwhile
            if _expired(rec, clock):
                rec = refresh(cfg, sub, rec, http, clock)
    return (rec.get('access_token') or '', rec.get('login') or '') if rec else ('', '')


_gh_cache = {'at': -1e9, 'token': ''}
GH_CACHE_SECONDS = 300


def gh_token(run=subprocess.run, clock=time.monotonic):
    """The GitHub CLI's token while it is logged in, else '' (held in memory for a few minutes only, never logged)."""
    if clock() - _gh_cache['at'] < GH_CACHE_SECONDS:
        return _gh_cache['token']
    _gh_cache.update(at=clock(), token=_ask_gh(run))
    return _gh_cache['token']


def _ask_gh(run):
    if os.environ.get('TUTTI_OFFLINE'):   # tests and scripts/check.sh: never the owner's real sign-in
        return ''
    try:
        r = run(['gh', 'auth', 'token'], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return ''
    return (r.stdout or '').strip() if r.returncode == 0 else ''


def token_for(cfg, sub=None, http=None, clock=time.time, gh=gh_token):
    """(token, where from: 'user' | 'household' | 'gh' | '') for a call on behalf of `sub` (None: the household's work)."""
    if not cfg:
        tok = gh()
        return (tok, 'gh') if tok else ('', '')
    tok, _login = user_token(cfg, sub, http, clock)
    if tok:
        return tok, 'user'
    tok, _login = user_token(cfg, _read(os.path.join(folder(cfg), HOUSEHOLD)).get('sub'), http, clock)
    if tok:
        return tok, 'household'
    tok = gh()
    return (tok, 'gh') if tok else ('', '')


def worker_env(env=None):
    """A copy of the environment for a worker process, without any GitHub token: workers never act on GitHub as a person."""
    return {k: v for k, v in (os.environ if env is None else env).items() if k not in TOKEN_VARS}


# ---- the HTTP edge

def urlopen_http(method, url, headers, body=None, timeout=20):
    """(status, lower-cased headers, body) of one request to a fixed https endpoint of GitHub's."""
    if not url.startswith(('https://api.github.com/', 'https://github.com/')):
        raise GitHubError(f'refused a request outside GitHub ({urllib.parse.urlsplit(url).hostname})')
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:   # a fixed https address of GitHub's
            return res.status, {k.lower(): v for k, v in res.headers.items()}, res.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read(256 * 1024)
    except (OSError, ValueError) as e:
        raise GitHubError(f'GitHub did not answer ({type(e).__name__}): check the network, then try again') from e


_etags, _limits, _lock = {}, {}, threading.Lock()


class Client:
    """REST and GraphQL with one token: ETag-cached reads, and no call while the token's rate limit is spent."""

    def __init__(self, token, http=None, clock=time.time):
        if not token:
            raise GitHubError('GitHub is not connected: choose Connect GitHub on the Me view (or run `tutti github connect`), '
                              'or sign in the GitHub CLI with `gh auth login`')
        self.token, self.http, self.clock = token, http or urlopen_http, clock
        self.key = hashlib.sha256(token.encode()).hexdigest()[:16]   # the caches never hold the token itself

    def _blocked(self):
        reset = _limits.get(self.key, 0)
        if reset > self.clock():
            return f'GitHub\'s rate limit for this connection is used up until {time.strftime("%H:%M", time.localtime(reset))}: ' \
                   'Tutti waits and asks again then'
        return ''

    def _note_limit(self, status, headers):
        left, reset = headers.get('x-ratelimit-remaining'), headers.get('x-ratelimit-reset')
        retry = headers.get('retry-after')
        with _lock:
            if retry and status in (403, 429):
                _limits[self.key] = self.clock() + int(retry)
            elif left is not None and reset and int(left) <= 0:
                _limits[self.key] = float(reset)

    def request(self, method, path, body=None, ok=(200, 201, 202, 204)):
        """The parsed JSON answer (None for 204); GitHubError with GitHub's own message otherwise."""
        why = self._blocked()
        if why:
            raise GitHubError(why)
        url = path if path.startswith('https://') else API + path
        headers = {'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'tutti'}
        cache_key = (self.key, url)
        if method == 'GET' and cache_key in _etags:
            headers['If-None-Match'] = _etags[cache_key][0]
        data = json.dumps(body).encode() if body is not None else None
        if data:
            headers['Content-Type'] = 'application/json'
        status, rh, raw = self.http(method, url, headers, data)
        self._note_limit(status, rh)
        if status == 304 and cache_key in _etags:
            return _etags[cache_key][1]
        answer = _json(raw)
        if status not in ok:
            raise GitHubError(describe(status, answer, method, path))
        if method == 'GET' and rh.get('etag'):
            with _lock:
                _etags[cache_key] = (rh['etag'], answer)
        return answer

    def graphql(self, query, variables):
        answer = self.request('POST', '/graphql', {'query': query, 'variables': variables})
        if (answer or {}).get('errors'):
            raise GitHubError(f'GitHub refused the query: {answer["errors"][0].get("message", "")[:200]}')
        return (answer or {}).get('data') or {}


def _json(raw):
    try:
        return json.loads(raw or b'null')
    except ValueError:
        return None


def describe(status, answer, method, path):
    """A plain message for a refused call: what, why, what to do."""
    msg = (answer or {}).get('message', '') if isinstance(answer, dict) else ''
    hint = {401: 'the connection expired or was revoked: reconnect GitHub on the Me view',
            403: 'the connection may not do this: install Tutti\'s GitHub App on the repository, or ask its owner',
            404: 'the repository or item is not visible to this connection: install Tutti\'s GitHub App on it',
            409: 'GitHub refused it in this state (the branch moved on, or it is already done)',
            422: 'GitHub refused the request\'s contents'}.get(status, 'try again later')
    return f'GitHub answered {status} to {method} {path.split("?")[0]}{": " + msg[:160] if msg else ""}: {hint}'


def client(cfg, sub=None, http=None, clock=time.time, gh=gh_token):
    return Client(token_for(cfg, sub, http, clock, gh)[0], http, clock)


# ---- what Tutti asks of GitHub

PR_QUERY = ('query($o:String!,$r:String!){repository(owner:$o,name:$r){pullRequests(states:OPEN,first:10,'
            'orderBy:{field:CREATED_AT,direction:DESC}){nodes{number title url headRefName isDraft reviewDecision '
            'latestReviews(first:10){nodes{author{login} state}} commits(last:1){nodes{commit{statusCheckRollup{contexts(first:50)'
            '{nodes{__typename ... on CheckRun{name status conclusion} ... on StatusContext{context state}}}}}}}}}}}')


def split_repo(repo):
    owner, _, name = (repo or '').partition('/')
    if not owner or not name or '/' in name:
        raise GitHubError(f'"{repo}" is not owner/name: write the repository as owner/name')
    return owner, name


def open_prs(c, repo):
    """The repository's open pull requests in the shape releases.pr_items reads."""
    owner, name = split_repo(repo)
    nodes = ((c.graphql(PR_QUERY, {'o': owner, 'r': name}).get('repository') or {}).get('pullRequests') or {}).get('nodes') or []
    out = []
    for p in nodes:
        commits = (p.get('commits') or {}).get('nodes') or [{}]
        rollup = ((commits[-1].get('commit') or {}).get('statusCheckRollup') or {}).get('contexts') or {}
        out.append({'number': p.get('number'), 'title': p.get('title'), 'url': p.get('url'), 'branch': p.get('headRefName'),
                    'draft': p.get('isDraft'), 'decision': p.get('reviewDecision'),
                    'checks': [{'name': x.get('name') or x.get('context'), 'status': x.get('status'),
                                'conclusion': x.get('conclusion'), 'state': x.get('state')} for x in rollup.get('nodes') or []],
                    'reviews': [{'author': (r.get('author') or {}).get('login'), 'state': r.get('state')}
                                for r in (p.get('latestReviews') or {}).get('nodes') or []]})
    return out


def latest_releases(c, repo, n=3):
    """[{tag, name, published, url, assets}] of the newest releases."""
    rows = c.request('GET', f'/repos/{repo}/releases?per_page={int(n)}') or []
    return [{'tag': r.get('tag_name'), 'name': r.get('name') or r.get('tag_name'), 'published': r.get('published_at'),
             'url': r.get('html_url') if str(r.get('html_url') or '').startswith('https://github.com/') else None,
             'draft': bool(r.get('draft')), 'assets': len(r.get('assets') or [])} for r in rows]


def pr_create(c, repo, head, base, title, body, draft=False):
    """The new pull request's URL."""
    base = base or (c.request('GET', f'/repos/{repo}') or {}).get('default_branch') or 'main'
    pr = c.request('POST', f'/repos/{repo}/pulls', {'head': head, 'base': base, 'title': title, 'body': body, 'draft': bool(draft)})
    return pr['html_url']


def pr_ready(c, repo, number):
    node = (c.request('GET', f'/repos/{repo}/pulls/{int(number)}') or {}).get('node_id')
    c.graphql('mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){clientMutationId}}', {'id': node})


def pr_merge(c, repo, number, sha, method='squash'):
    """Merges only the commit that was reviewed (`sha`): GitHub refuses it when the branch moved on."""
    c.request('PUT', f'/repos/{repo}/pulls/{int(number)}/merge', {'sha': sha, 'merge_method': method})


def pr_state(c, repo, number):
    """'MERGED', 'OPEN' or 'CLOSED'."""
    pr = c.request('GET', f'/repos/{repo}/pulls/{int(number)}') or {}
    return 'MERGED' if pr.get('merged') else str(pr.get('state') or '').upper()


def delete_branch(c, repo, branch):
    c.request('DELETE', f'/repos/{repo}/git/refs/heads/{urllib.parse.quote(branch)}', ok=(204, 422))


def issue_create(c, repo, title, body, labels=()):
    """The new issue's URL; a label the repository lacks is dropped rather than failing the issue."""
    try:
        return c.request('POST', f'/repos/{repo}/issues', {'title': title, 'body': body, 'labels': list(labels)})['html_url']
    except GitHubError:
        if not labels:
            raise
        return c.request('POST', f'/repos/{repo}/issues', {'title': title, 'body': body})['html_url']


PR_URL = re.compile(r'^https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)')
REMOTE = re.compile(r'github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$')


def parse_pr_url(url):
    """(owner/name, number) of a pull request's URL; GitHubError for anything else."""
    m = PR_URL.match((url or '').strip())
    if not m:
        raise GitHubError(f'"{url}" is not a GitHub pull request URL')
    return m.group(1), int(m.group(2))


def repo_of_remote(url):
    """owner/name of a GitHub remote URL (https or ssh), or ''."""
    m = REMOTE.search((url or '').strip())
    return m.group(1) if m else ''


def repo_of_worktree(wt, run=subprocess.run):
    from . import worktree
    if not wt:
        raise GitHubError('no repository was named: pass --repo owner/name')
    r = run([worktree.git_bin(), '-C', wt, 'remote', 'get-url', 'origin'], capture_output=True, text=True, check=False)
    repo = repo_of_remote(r.stdout if r.returncode == 0 else '')
    if not repo:
        raise GitHubError(f'the checkout {wt} has no GitHub remote named origin: add one with git remote add origin ...')
    return repo


# ---- `gh`-shaped calls: the callers keep their argv, the API answers

_current = {'cfg': None}


def use(cfg):
    """The configuration whose connections `run` uses (set once by bin/tutti and the dashboard after loading it)."""
    _current['cfg'] = cfg


class NotCovered(Exception):
    """A gh call the API path does not answer: the real gh runs it."""


class Done:
    """What subprocess.run returns, for the callers that read returncode, stdout and stderr."""

    def __init__(self, returncode, stdout='', stderr=''):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


VALUE_FLAGS = {'--repo', '--title', '--body', '--base', '--head', '--match-head-commit', '--json', '--jq', '--label',
               '--body-file'}
SHORT = {'-R': '--repo', '-t': '--title', '-b': '--body', '-B': '--base', '-H': '--head', '-q': '--jq', '-l': '--label',
         '-F': '--body-file'}


def flags(args):
    """({flag: value or True}, positional) of a gh argv tail: the flags in VALUE_FLAGS take the next word, any other flag is a
    switch; repeated flags keep the last value."""
    out, pos, i = {}, [], 0
    while i < len(args):
        a = SHORT.get(args[i], args[i])
        if a in VALUE_FLAGS and i + 1 < len(args):
            out[a] = args[i + 1]
            i += 2
            continue
        if a.startswith('-'):
            out[a.split('=', 1)[0]] = a.split('=', 1)[1] if '=' in a else True
        else:
            pos.append(a)
        i += 1
    return out, pos


def _target(f, pos, cwd):
    """(repo, number) of a `gh pr <verb> <url|number>` call."""
    ref = pos[0] if pos else ''
    if ref.startswith('https://'):
        return parse_pr_url(ref)
    return f.get('--repo') or repo_of_worktree(cwd), int(ref)


def _pr(c, verb, f, pos, cwd):
    if verb == 'create':
        repo = f.get('--repo') or repo_of_worktree(cwd)
        return pr_create(c, repo, f.get('--head'), f.get('--base'), f.get('--title') or '', f.get('--body') or '',
                         bool(f.get('--draft'))) + '\n'
    repo, number = _target(f, pos, cwd)
    if verb == 'ready':
        pr_ready(c, repo, number)
        return ''
    if verb == 'merge':
        if not f.get('--match-head-commit') or f.get('--auto') or f.get('--admin'):
            raise NotCovered('merge')   # only a merge pinned to the reviewed commit goes through the API
        method = 'squash'
        if f.get('--rebase'):
            method = 'rebase'
        elif f.get('--merge'):
            method = 'merge'
        pr_merge(c, repo, number, f['--match-head-commit'], method)
        if f.get('--delete-branch'):
            delete_branch(c, repo, (c.request('GET', f'/repos/{repo}/pulls/{int(number)}') or {}).get('head', {}).get('ref') or '')
        return ''
    if verb == 'view' and f.get('--json') == 'state':
        return pr_state(c, repo, number) + '\n'
    raise NotCovered(verb)


def _api_call(c, args, cwd, stdin):
    """The stdout of a gh call the API can answer; NotCovered when it cannot (the real gh runs it)."""
    group, verb, rest = (args + ['', ''])[0], (args + ['', ''])[1], args[2:]
    f, pos = flags(rest)
    if group == 'pr':
        return _pr(c, verb, f, pos, cwd)
    if group == 'issue' and verb == 'create':
        body = stdin if f.get('--body-file') == '-' else f.get('--body') or ''
        return issue_create(c, f['--repo'], f.get('--title') or '', body or '', [f['--label']] if f.get('--label') else ()) + '\n'
    if group == 'auth' and verb == 'status':
        return 'Logged in to github.com through Tutti\n'
    raise NotCovered(group)


def run(argv, cwd=None, input=None, sub=None, cfg=None, http=None, fallback=subprocess.run, clock=time.time, **kw):
    """subprocess.run for a `gh ...` argv: the API with the person's, the household's or gh's token when one exists; the
    real GitHub CLI for anything the API path does not cover. Never puts a token in argv or the environment."""
    if not argv or argv[0] != 'gh':
        return fallback(argv, cwd=cwd, input=input, **kw)
    cfg = cfg if cfg is not None else _current['cfg']
    tok = token_for(cfg, sub, http, clock)[0]
    if tok:
        try:
            return Done(0, _api_call(Client(tok, http, clock), list(argv[1:]), cwd, input))
        except NotCovered:
            pass
        except (GitHubError, KeyError, ValueError, IndexError) as e:
            return Done(1, '', str(e))
    if argv[1:3] == ['auth', 'status'] and not tok:
        return Done(1, '', 'GitHub is not connected: Connect GitHub on the Me view, or gh auth login')
    try:
        return fallback(argv, cwd=cwd, input=input, **kw)
    except FileNotFoundError:
        return Done(127, '', 'the GitHub CLI is not installed and GitHub is not connected: choose Connect GitHub on the Me view')


# ---- connecting: GitHub's device flow, per user

def safe_page(url):
    parts = urllib.parse.urlsplit(url or '')
    return url if parts.scheme == 'https' and parts.hostname == 'github.com' else DEVICE_PAGE


class Flows:
    """The connections waiting for the person to enter their code at github.com/login/device, one per user."""

    def __init__(self, cfg, http=None, clock=time.time):
        self.cfg, self.http, self.clock = cfg, http, clock
        self.waiting, self.lock = {}, threading.Lock()

    def start(self, sub):
        """What the page shows: user_code, verification_url, qr_url, expires_in, interval."""
        cid = client_id(self.cfg)
        if not cid:
            raise GitHubError('Tutti\'s GitHub App is not set up in this copy yet: update Tutti, or set github.client_id in '
                              'the config; until then Tutti uses the GitHub CLI when `gh auth login` was done')
        status, answer = form_post(DEVICE_CODE_URL, {'client_id': cid}, self.http)
        if status != 200 or not answer.get('device_code'):
            raise GitHubError(f'GitHub did not start the connection ({answer.get("error") or status}): try again in a minute')
        now = self.clock()
        interval = max(5, int(answer.get('interval') or 5))
        with self.lock:
            if len(self.waiting) >= MAX_FLOWS and sub not in self.waiting:
                raise GitHubError('too many connections are waiting: try again in a few minutes')
            self.waiting[sub] = {'device_code': answer['device_code'], 'interval': interval, 'next': now + interval,
                                 'expires': now + int(answer.get('expires_in') or 900)}
        url = safe_page(answer.get('verification_uri'))
        return {'user_code': answer.get('user_code'), 'verification_url': url, 'qr_url': url,
                'expires_in': int(answer.get('expires_in') or 900), 'interval': interval}

    def poll(self, sub):
        """None while waiting, else the GitHub login now connected. GitHub is asked at most once per its interval."""
        now = self.clock()
        with self.lock:
            flow = self.waiting.get(sub)
            if not flow or flow['expires'] <= now:
                self.waiting.pop(sub, None)
                raise GitHubError('this connection expired or was never started: choose Connect GitHub again')
            if now < flow['next']:
                return None
            flow['next'] = now + flow['interval']
        status, answer = form_post(TOKEN_URL, {'client_id': client_id(self.cfg), 'device_code': flow['device_code'],
                                               'grant_type': GRANT}, self.http)
        error = answer.get('error')
        if error == 'authorization_pending':
            return None
        if error == 'slow_down':
            with self.lock:
                flow['interval'] += 5
                flow['next'] = now + flow['interval']
            return None
        with self.lock:
            self.waiting.pop(sub, None)
        if status != 200 or not answer.get('access_token'):
            why = {'access_denied': 'you chose Cancel on GitHub\'s page', 'expired_token': 'the code expired'}.get(error, error)
            raise GitHubError(f'GitHub did not connect ({why or status}): choose Connect GitHub again')
        login = (Client(answer['access_token'], self.http, self.clock).request('GET', '/user') or {}).get('login') or ''
        save(self.cfg, sub, answer, login, self.clock)
        return login


def status(cfg, sub=None, gh=gh_token):
    """What the Me view and `tutti github status` show: {connected, login, source, app, install_url}."""
    rec = _read(token_path(cfg, sub)) if sub else {}
    source = 'user' if rec.get('access_token') else token_for(cfg, sub, gh=gh)[1]
    return {'connected': bool(rec.get('access_token')), 'login': rec.get('login') or '', 'source': source,
            'app': bool(client_id(cfg)), 'install_url': f'https://github.com/apps/{APP_SLUG}/installations/new'}


def doctor_line(cfg, gh=gh_token):
    """A problem line for `tutti doctor`, or '' when Tutti can reach GitHub."""
    if token_for(cfg, None, gh=gh)[0]:
        return ''
    return ('GitHub is not connected, so the autopilot cannot open pull requests and the Projects panel shows none: choose '
            'Connect GitHub on the dashboard\'s Me view, run `tutti github connect`, or sign in the GitHub CLI (gh auth login)')


# ---- the dashboard's POST /api/github

_flows = {}


def flows_of(cfg):
    key = id(cfg)
    if key not in _flows:
        _flows[key] = Flows(cfg)
    return _flows[key]


def api(collector, headers, raw, flows=None):
    """(status, answer) for POST /api/github {"action": "status"|"start"|"poll"|"disconnect"}: the signed-in person's own
    connection (with sign-in off, the dashboard's owner)."""
    try:
        data = json.loads(raw or b'{}')
    except ValueError:
        data = None
    action = (data or {}).get('action')
    who = getattr(collector, 'who', lambda _h: None)(headers)
    sub = (who or {}).get('sub') or 'owner'
    flows = flows or flows_of(collector.cfg)
    try:
        if action == 'status':
            return 200, status(collector.cfg, sub)
        if action == 'start':
            return 200, {'ok': True, **flows.start(sub)}
        if action == 'poll':
            login = flows.poll(sub)
            return 200, ({'pending': True} if login is None else {'ok': True, 'login': login})
        if action == 'disconnect':
            return 200, {'ok': True, 'removed': forget(collector.cfg, sub)}
    except GitHubError as e:
        return 409, {'error': str(e)}
    return 400, {'error': 'send {"action": "status"|"start"|"poll"|"disconnect"}'}


# ---- the App's manifest (the owner creates the App once; docs/GITHUB.md)

def manifest():
    """The GitHub App manifest for Tutti's official App: public, no webhook, device flow, least privilege."""
    return {'name': 'Tutti orchestrator', 'url': 'https://github.com/ventura8/Tutti', 'public': True,
            'description': 'Connects Tutti (open-source AI orchestrator) to GitHub: pull requests, CI and issues.',
            'hook_attributes': {'url': 'https://github.com/ventura8/Tutti', 'active': False},
            'redirect_url': 'https://github.com/ventura8/Tutti/blob/main/docs/GITHUB.md',
            'request_oauth_on_install': False, 'setup_on_update': False, 'default_permissions': dict(PERMISSIONS),
            'default_events': []}
