"""A scout's review becomes work (owner, 2026-11-22: "figure out and solve problems as they come"; docs/SCOUTS.md,
*Reviews*). An answered review request whose comment has findings (lines like "- `file:line` - ...") files one backlog
task per pull request, "Address the <scout> review of #N", with the findings in its notes, as a suggestion (`candidate`):
the PR's author fixes them on the PR's own branch, so the autopilot does not start a competing fix. A clean review files
nothing. The merge step holds a PR while its review task is open. Pure: `findings`, `answered`; `feed` writes the backlog."""
import re

FINDING = re.compile(r'(?m)^\s*-\s+`[^`]+`')
FED = 'review_fed'


def findings(answer):
    """The number of findings in a review comment (its "- `where` - what" lines)."""
    return len(FINDING.findall(str(answer or '')))


FRESH_S = 2 * 86400   # a review answered longer ago is marked fed without a task (its PR is likely long merged)


def answered(sdata, now=None):
    """Answered review requests not turned into work yet (each marked fed); older ones are marked without one."""
    import time
    now = time.time() if now is None else now
    out = []
    for r in sdata.get('requests') or []:
        if r.get('kind') == 'review' and r.get('status') == 'answered' and not r.get(FED):
            r[FED] = True
            if now - float(r.get('answered') or 0) <= FRESH_S:
                out.append(dict(r))
    return out


def repo_for(cfg, slug, remote_of):
    """The autopilot repo whose origin is `slug` (owner/name), or None."""
    for name, repo in ((cfg.get('autopilot') or {}).get('repos') or {}).items():
        url = remote_of(repo.get('path') or '')
        if url and re.search(rf'[:/]{re.escape(slug)}(\.git)?/?$', url):
            return name
    return None


def feed(cfg, reviews, remote_of=None):
    """Event lines: one task filed per review with findings (never twice for the same PR while one is open)."""
    from . import backlog
    remote_of = remote_of or _remote
    events = []
    if not reviews:
        return events
    with backlog.locked(cfg) as data:
        for r in reviews:
            n = findings(r.get('answer'))
            if not n:
                events.append(f'{r["id"]}: the review of {r.get("repo")}#{r.get("issue")} found nothing to fix')
                continue
            repo = repo_for(cfg, r.get('repo') or '', remote_of)
            title = f'Address the {r.get("scout", "scout")} review of #{r.get("issue")} ({n} finding{"s" if n != 1 else ""})'
            if not repo or any(t.get('pr_review') == f'{r.get("repo")}#{r.get("issue")}' and t.get('status') not in ('done', 'dropped')
                               for t in data['tasks']):
                continue
            task = backlog.new_task(repo, title, source='scout-review',
                                    notes=[f'{r.get("url")}: ' + ' '.join(str(r.get('answer') or '').split())[:3000]])
            task.update(status='candidate', pr_review=f'{r.get("repo")}#{r.get("issue")}', pr=r.get('url'))
            task, _added = backlog.add(data, task)
            events.append(f'{r["id"]}: {n} finding(s) on {r.get("repo")}#{r.get("issue")}: filed as {task["id"]}')
    return events


def open_for(tasks, slug, pr):
    """The open review task of `slug`#`pr`, or None: the merge step holds the PR while there is one."""
    key = f'{slug}#{pr}'
    return next((t for t in tasks if t.get('pr_review') == key and t.get('status') not in ('done', 'dropped')), None)


def _remote(path):
    import subprocess
    try:
        return subprocess.run(['git', '-C', path, 'remote', 'get-url', 'origin'], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''
