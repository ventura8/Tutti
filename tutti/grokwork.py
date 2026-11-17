"""Grok Bot as a coding worker (`workers.<name>.kind: grok`): a ladder step that posts a bounded task to the scout webhook.

The payload carries repo, branch, title, scope and check. Grok clones on its own computer, does the change, runs the check
and posts a unified diff as a GitHub issue comment; Tutti applies that diff into the worktree. The webhook URL and key are
read through secretenv (names from the scout's url_env/key_env) and never printed."""
import subprocess
import sys
import time

from . import scouts
from .review import gh as default_gh

DIFF_MARK = '```diff'
DIFF_END = '```'
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
        sc = scouts.settings(cfg, scout_name(worker))
    except (GrokWorkError, scouts.ScoutError) as e:
        return False, str(e)
    gone = scouts.missing_env(sc, env)
    if gone:
        return False, f'not set up: {", ".join(gone)} missing (tutti scout setup {sc["name"]})'
    return True, f'scout {sc["name"]} webhook ready'


def git_out(cwd, *args):
    r = subprocess.run(['git', '-C', cwd, *args], capture_output=True, text=True)
    return r.returncode, (r.stdout or '').strip(), (r.stderr or '').strip()


def repo_of(cwd):
    """The clone URL Tutti asks Grok to clone: origin's URL."""
    rc, out, err = git_out(cwd, 'config', '--get', 'remote.origin.url')
    if rc == 0 and out:
        return out
    raise GrokWorkError(f'{cwd} has no origin remote ({err or "empty"}): add one so Grok can clone the repository')


def branch_of(cwd):
    rc, out, _ = git_out(cwd, 'symbolic-ref', '--short', '-q', 'HEAD')
    if rc == 0 and out:
        return out
    rc, sha, err = git_out(cwd, 'rev-parse', '--short', 'HEAD')
    if rc != 0 or not sha:
        raise GrokWorkError(f'cannot read HEAD in {cwd} ({err}): is it a git worktree?')
    return sha


def title_of(task):
    return (task or '').strip().splitlines()[0][:200] if (task or '').strip() else 'coding task'


def code_instructions(repo, branch, title, scope, check):
    """What the routine must do on its own computer; the structured fields are also in the JSON body."""
    paths = ', '.join(scope) if scope else '(the paths the title implies)'
    gate = check or 'true'
    return (f'Coding task from Tutti (kind {CODE_KIND}). Clone {repo} and check out {branch}. '
            f'Title: {title}. Change only: {paths}. Run this check from the repository root and make it pass: {gate}. '
            'Do the work on your own computer. When done, post ONE GitHub issue comment whose body is a unified diff '
            f'(a ```diff fenced block) of your changes against {branch}; if you cannot, post why. '
            'Never open a pull request, never push, never close the issue, never print secrets.')


def full_brief(task, repo, branch, title, scope, check):
    brief = code_instructions(repo, branch, title, scope, check)
    text = (task or '').strip()
    return f'{brief}\n\nFull brief:\n{text}' if text and text not in brief else brief


def payload(repo, branch, title, scope, check, task, reply_to, rid):
    """The webhook body: structured fields plus the full brief. Never carries URL or key."""
    return {'from': 'tutti', 'kind': CODE_KIND, 'id': rid, 'repo': repo, 'branch': branch, 'title': title,
            'scope': list(scope or []), 'check': check or '', 'task': task, 'reply_to': reply_to}


def extract_diff(answer):
    """The first ```diff ... ``` block in the scout's answer, or '' when none."""
    text = answer or ''
    start = text.find(DIFF_MARK)
    if start < 0:
        return ''
    body = text[start + len(DIFF_MARK):]
    if body.startswith('\n'):
        body = body[1:]
    end = body.find(DIFF_END)
    return body[:end] if end >= 0 else ''


def apply_diff(cwd, diff):
    """Applies a unified diff into cwd; raises when git apply fails."""
    if not diff.strip():
        raise GrokWorkError('the scout\'s answer had no ```diff block: ask it again or use another worker')
    r = subprocess.run(['git', '-C', cwd, 'apply', '--index', '--whitespace=nowarn', '-'],
                       input=diff, capture_output=True, text=True)
    if r.returncode != 0:
        raise GrokWorkError(f'git apply failed in {cwd}: {(r.stderr or r.stdout or "").strip()[:300]}: fix the diff or retry')


def _post_body(sc, r, body_task, url, key, post_fn):
    """POSTs the coding payload; returns '' on 200 else the refusal why (never includes the key)."""
    how = 'post the result as one comment on this issue'
    body = payload(body_task['repo'], body_task['branch'], body_task['title'], body_task['scope'], body_task['check'],
                   body_task['task'], {'repo': r['repo'], 'issue': r['issue'], 'url': r['url'], 'how': how}, r['id'])
    try:
        status = post_fn(url, key, body)
    except OSError as e:
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


def send_and_wait(cfg, sc, body_task, post_fn, gh_fn, env, now=None):
    """Opens the issue, POSTs the coding payload, waits for the answer. Returns the request dict."""
    url, key = scouts.secrets(sc, env)
    with scouts.locked(cfg) as data:
        r = scouts.new_request(data, sc, body_task['task'], kind=CODE_KIND, now=now)
        rid = r['id']
    r['issue'], r['url'] = scouts.open_issue(sc, r, gh_fn)
    r['sent'] = now or time.time()
    why = _post_body(sc, r, body_task, url, key, post_fn)
    fail = _record_post(cfg, rid, r, sc, why)
    if fail:
        gh_fn(['issue', 'close', str(r['issue']), '--repo', r['repo'], '--comment', f'Tutti: not sent. {fail}'])
        raise GrokWorkError(fail)
    scouts.wait(sc, r, float(sc.get('timeout_minutes') or 20) * 60, gh_fn)
    return _persist_answer(cfg, rid, r, gh_fn)


def _log(out, lines, msg):
    out.write(msg + '\n')
    out.flush()
    lines.append(msg)


def _require_answer(sc, r):
    if r.get('status') == 'answered':
        return
    why = r.get('why') or f'status {r.get("status")}: look at {r.get("url") or "the scout issue"}'
    raise GrokWorkError(f'no coding answer from scout {sc["name"]}: {why}')


def _execute(cfg, name, task, files, cwd, check, env, post_fn, gh_fn, now, out, lines):
    sc = scouts.settings(cfg, scout_name(cfg['workers'][name]))
    repo, branch = repo_of(cwd), branch_of(cwd)
    title, scope = title_of(task), list(files or [])
    body = payload(repo, branch, title, scope, check, full_brief(task, repo, branch, title, scope, check), {}, 'pending')
    _log(out, lines, f'grok: sending coding task to scout {sc["name"]} (repo={repo}, branch={branch})')
    r = send_and_wait(cfg, sc, body, post_fn or scouts.post, gh_fn or default_gh, env, now)
    _require_answer(sc, r)
    apply_diff(cwd, extract_diff(r.get('answer') or ''))
    return r['id']


def run(cfg, name, task, files=(), cwd=None, out=None, check='', env=None, post_fn=None, gh_fn=None, now=None):
    """Posts the coding task to the scout webhook, waits for a diff comment, applies it. Returns (exit code, log lines)."""
    out, lines = out or sys.stdout, []
    try:
        rid = _execute(cfg, name, task, files, cwd, check, env, post_fn, gh_fn, now, out, lines)
        _log(out, lines, f'grok: applied diff from {rid}')
        return 0, lines
    except (GrokWorkError, scouts.ScoutError) as e:
        _log(out, lines, f'grok: {e}')
        return 1, lines
