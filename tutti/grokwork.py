"""Grok Bot as a patch-author worker (`workers.<name>.kind: grok`): a ladder step that posts a bounded task to the scout webhook.

The payload carries repo, branch, base_sha, title, scope and check. Grok acts as a patch author (no push, PR or build):
it posts a unified diff against base_sha for the scoped files as a GitHub issue comment; Tutti applies that diff in a
worktree and runs the check on its own machines. The webhook URL and key are read through the scout environment provider (names from the
scout's url_env/key_env) and never printed."""
import os
import re
import subprocess
import sys
import time
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from . import scouts
from .review import gh as default_gh

DIFF_BLOCK = re.compile(r'^```diff[ \t]*\r?\n(.*?)^```[ \t]*\r?$', re.MULTILINE | re.DOTALL)
SCP_ORIGIN = re.compile(r'git@[a-zA-Z0-9.-]+:[a-zA-Z0-9_./-]+')
CODE_KIND = 'code'


class GrokWorkError(Exception):
    """A grok coding step failed; the message names the cause and the next action."""


def scout_name(worker):
    name = (worker or {}).get('scout')
    if not isinstance(name, str) or not name.strip():
        raise GrokWorkError('workers.<name>.scout is missing: name the scout whose webhook sends the task (e.g. grok)')
    return name.strip()


def is_coding(cfg, account):
    """True when this account's worker is kind grok (a scout account that also takes bounded coding tasks)."""
    from . import autopilot
    wn = autopilot.worker_of_account(cfg, account)
    return ((cfg.get('workers') or {}).get(wn) or {}).get('kind') == 'grok'


def available(cfg, worker, env=None):
    """(ready?, note): the named scout's webhook variables must be set (names only in the note)."""
    try:
        sc = coding_scout(cfg, scout_name(worker))
    except (GrokWorkError, scouts.ScoutError) as e:
        return False, str(e)
    gone = scouts.missing_env(sc, env)
    if gone:
        return False, f'not set up: {", ".join(gone)} missing (tutti scout setup {sc["name"]})'
    return True, f'scout {sc["name"]} webhook ready'


def coding_scout(cfg, name):
    sc = scouts.settings(cfg, name)
    if not sc.get('github_login'):
        raise GrokWorkError(f'scouts.{name}.github_login is missing: set the GitHub login that posts patches')
    return sc


def git_out(cwd, *args):
    r = subprocess.run(['git', '-C', cwd, *args], capture_output=True, text=True)
    return r.returncode, (r.stdout or '').strip(), (r.stderr or '').strip()


def clone_url_allowed(origin):
    if SCP_ORIGIN.fullmatch(origin):
        return True
    try:
        url = urlsplit(origin)
    except ValueError:
        return False
    if not all((url.scheme in ('https', 'ssh'), url.netloc)):
        return False
    allowed_user = {None, 'git'} if url.scheme == 'ssh' else {None}
    return all((url.password is None, not url.query, not url.fragment, url.username in allowed_user))


def repo_of(cwd):
    """The clone URL Tutti asks Grok to clone: origin's URL."""
    rc, origin, _ = git_out(cwd, 'config', '--get', 'remote.origin.url')
    if rc or not origin:
        raise GrokWorkError('the worktree has no origin: add a clone URL for the repository')
    if not clone_url_allowed(origin):
        raise GrokWorkError('origin must be a remote clone URL without credentials: use HTTPS or Git SSH')
    return origin


def branch_of(cwd):
    rc, out, _ = git_out(cwd, 'symbolic-ref', '--short', '-q', 'HEAD')
    if rc == 0 and out:
        return out
    rc, ref, _ = git_out(cwd, 'name-rev', '--name-only', '--refs=refs/heads/*', '--no-undefined', 'HEAD')
    if rc == 0 and ref and '~' not in ref and '^' not in ref:
        return ref.removeprefix('heads/')
    rc, sha, err = git_out(cwd, 'rev-parse', '--short', 'HEAD')
    if rc != 0 or not sha:
        raise GrokWorkError(f'cannot read HEAD in {cwd} ({err}): is it a git worktree?')
    return sha


def base_sha_of(cwd):
    """The full commit SHA of HEAD that the unified diff must apply against."""
    rc, sha, err = git_out(cwd, 'rev-parse', 'HEAD')
    if rc != 0 or not sha:
        raise GrokWorkError(f'cannot read HEAD SHA in {cwd} ({err}): is it a git worktree?')
    return sha


def title_of(task):
    return (task or '').strip().splitlines()[0][:200] if (task or '').strip() else 'coding task'


def code_instructions(repo, branch, title, scope, check, base_sha=''):
    """Patch-author brief for the routine; the structured fields are also in the JSON body."""
    paths = ', '.join(scope)
    gate = check or 'true'
    base = f'{branch} (base SHA {base_sha})' if base_sha else branch
    return (f'Patch-author task from Tutti (kind {CODE_KIND}). Repo: {repo}, ref: {base}. '
            f'Title: {title}. Clone the repository and inspect the specified base SHA. Change only: {paths}. '
            f'Acceptance check (Tutti runs this on its own machines): {gate}. '
            'Work as a patch author only: do NOT run builds/tests, do NOT push, do NOT open a pull request, '
            'do NOT close the issue, never print secrets. Post ONE GitHub issue comment whose body is a unified diff '
            f'(a ```diff fenced block) against {base} for the scoped files; if you cannot, post why.')


def full_brief(task, repo, branch, title, scope, check, base_sha=''):
    brief = code_instructions(repo, branch, title, scope, check, base_sha)
    text = (task or '').strip()
    return f'{brief}\n\nFull brief:\n{text}' if text and text not in brief else brief


def payload(repo, branch, title, scope, check, task, reply_to, rid, base_sha=''):
    """The webhook body: structured fields plus the full brief. Never carries URL or key."""
    return {'from': 'tutti', 'kind': CODE_KIND, 'id': rid, 'repo': repo, 'branch': branch, 'base_sha': base_sha,
            'title': title, 'scope': list(scope or []), 'check': check or '', 'task': task, 'reply_to': reply_to}


def extract_diff(answer):
    """The first ```diff ... ``` block in the scout's answer, or '' when none."""
    match = DIFF_BLOCK.search(answer or '')
    return match[1] if match else ''


def patch_paths(cwd, diff):
    result = subprocess.run(['git', '-C', cwd, 'apply', '--numstat', '-z', '-'],
                            input=diff.encode(), capture_output=True)
    if result.returncode:
        raise GrokWorkError('git could not read the patch: ask the scout for a valid unified diff')
    return {os.fsdecode(entry.split(b'\t', 2)[2]) for entry in result.stdout.split(b'\0') if entry}


def apply_diff(cwd, diff, scope):
    """Applies an in-scope unified diff into the worktree."""
    if not diff.strip():
        raise GrokWorkError('the scout\'s answer had no ```diff block: ask it again or use another worker')
    headers = ('rename from ', 'rename to ', 'copy from ', 'copy to ')
    if any(line.startswith(headers) for line in diff.splitlines()):
        raise GrokWorkError('the scout sent a rename or copy: ask for separate deletion and addition hunks')
    if not patch_paths(cwd, diff) <= set(scope):
        raise GrokWorkError('the scout diff changes files outside scope: narrow the patch or explicitly name those files')
    r = subprocess.run(['git', '-C', cwd, 'apply', '--whitespace=nowarn', '-'],
                       input=diff, capture_output=True, text=True)
    if r.returncode != 0:
        raise GrokWorkError('git apply failed: check that the scout used the requested base and retry')


def _post_body(sc, r, body_task, url, key, post_fn):
    """POSTs the coding payload; returns '' on 200 else the refusal why (never includes the key)."""
    how = 'post the result as one comment on this issue'
    body = payload(body_task['repo'], body_task['branch'], body_task['title'], body_task['scope'], body_task['check'],
                   body_task['task'], {'repo': r['repo'], 'issue': r['issue'], 'url': r['url'], 'how': how}, r['id'],
                   body_task.get('base_sha', ''))
    try:
        status = post_fn(url, key, body)
    except (OSError, ValueError) as e:
        return f'the scout\'s webhook could not be reached ({type(e).__name__}): check the network and {sc["url_env"]}'
    return '' if status == 200 else scouts.refused(status, sc)


def _record_post(cfg, rid, r, sc, why):
    """Writes the post outcome into the scout request; returns the failure why or ''."""
    with scouts.locked(cfg) as data:
        cur = scouts.find(data, rid)
        cur.update(issue=r['issue'], url=r['url'], sent=r['sent'])
        if why:
            cur.update(status='failed', why=why)
            return why
        cur['status'] = 'waiting'
        scouts.spend_estimate(sc, cur, cur['sent'])
        r.update(cur)
    return ''


def _persist_answer(cfg, rid, r, gh_fn):
    with scouts.locked(cfg) as data:
        scouts.find(data, rid).update(r)
    if r.get('status') == 'answered':
        gh_fn(['issue', 'close', str(r['issue']), '--repo', r['repo'], '--reason', 'completed'])
    return dict(r)


def _open_reply_issue(cfg, sc, r, gh_fn):
    try:
        return scouts.open_issue(sc, r, gh_fn)
    except scouts.ScoutError:
        with scouts.locked(cfg) as data:
            scouts.find(data, r['id']).update(status='failed', why='could not open a reply issue')
        raise


def _wait_for_reply(cfg, sc, r, gh_fn):
    try:
        scouts.wait(sc, r, float(sc['timeout_minutes']) * 60, gh_fn)
    except scouts.ScoutError as e:
        r.update(status='failed', why=str(e))
    if r['status'] == 'waiting':
        r.update(status='timeout', why='no reply in time: check the scout routine and the configured GitHub login')
    return _persist_answer(cfg, r['id'], r, gh_fn)


def send_and_wait(cfg, sc, body_task, post_fn, gh_fn, env, now=None):
    """Opens the issue, POSTs the coding payload, waits for the answer. Returns the request dict."""
    url, key = scouts.secrets(sc, env)
    with scouts.locked(cfg) as data:
        r = scouts.new_request(data, sc, 'Reply with the requested patch or say why it could not be prepared.',
                               kind=CODE_KIND, now=now)
        rid = r['id']
    r['issue'], r['url'] = _open_reply_issue(cfg, sc, r, gh_fn)
    r['sent'] = now or time.time()
    why = _post_body(sc, r, body_task, url, key, post_fn)
    fail = _record_post(cfg, rid, r, sc, why)
    if fail:
        gh_fn(['issue', 'close', str(r['issue']), '--repo', r['repo'], '--comment', f'Tutti: not sent. {fail}'])
        raise GrokWorkError(fail)
    return _wait_for_reply(cfg, sc, r, gh_fn)


def _log(out, lines, msg):
    out.write(msg + '\n')
    out.flush()
    lines.append(msg)


def _require_answer(sc, r):
    if r.get('status') == 'answered':
        return
    why = r.get('why') or f'status {r.get("status")}: look at {r.get("url") or "the scout issue"}'
    raise GrokWorkError(f'no coding answer from scout {sc["name"]}: {why}')


def task_scope(files, cwd):
    scope = [path.removeprefix('./') for path in files or ()]
    if not scope or any(not scoped_file(path, cwd) for path in scope):
        raise GrokWorkError('task scope is missing or invalid: list exact repository-relative files Grok may change')
    return scope


def scoped_file(path, cwd):
    relative = PurePosixPath(path)
    return (bool(relative.parts) and relative.as_posix() == path and not relative.is_absolute()
            and not {'.git', '..'}.intersection(relative.parts) and not os.path.isdir(os.path.join(cwd, path)))


def _execute(cfg, name, task, files, cwd, check, env, post_fn, gh_fn, now, out, lines):
    if not check.strip():
        raise GrokWorkError('acceptance check is missing: configure a check for the repository or pass --check')
    sc = coding_scout(cfg, scout_name(cfg['workers'][name]))
    repo, branch, base_sha = repo_of(cwd), branch_of(cwd), base_sha_of(cwd)
    title, scope = title_of(task), task_scope(files, cwd)
    brief = full_brief(task, repo, branch, title, scope, check, base_sha)
    body = payload(repo, branch, title, scope, check, brief, {}, 'pending', base_sha)
    _log(out, lines, f'grok: sending patch task to scout {sc["name"]} (base={base_sha[:12]})')
    r = send_and_wait(cfg, sc, body, post_fn or scouts.post, gh_fn or default_gh, env, now)
    _require_answer(sc, r)
    if base_sha_of(cwd) != base_sha:
        raise GrokWorkError('the worktree HEAD changed while waiting: retry against the desired base SHA')
    apply_diff(cwd, extract_diff(r.get('answer') or ''), scope)
    return r['id']


def run(cfg, name, task, files=(), cwd=None, out=None, check='', env=None, post_fn=None, gh_fn=None, now=None):
    """Posts the coding task to the scout webhook, waits for a diff comment, applies it. Returns (exit code, log lines)."""
    out, lines = out or sys.stdout, []
    cwd = os.path.abspath(cwd or '.')
    try:
        rid = _execute(cfg, name, task, files, cwd, check, env, post_fn, gh_fn, now, out, lines)
        _log(out, lines, f'grok: applied diff from {rid}')
        return 0, lines
    except (GrokWorkError, scouts.ScoutError) as e:
        _log(out, lines, f'grok: {e}')
        return 1, lines
    except OSError as e:
        _log(out, lines, f'grok: could not access Git or scout state ({type(e).__name__}): check Git and state-folder permissions')
        return 1, lines
