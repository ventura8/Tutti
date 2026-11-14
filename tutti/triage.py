"""Sentry triage by a scout: every project under `projects:` with a `sentry: {org, project}` entry gets a daily watch
`sentry-<project>` on each scout with `sentry_triage: true`. The scout (Grok Bot, connected to Sentry) groups the new or
regressed issues, names the likely cause, file and commit, a fix and a severity, and posts it on a `tutti-scout-watch`
issue in the project's repository (`sentry.reply_repo`, else the project's `repo`, else the scout's `reply.github_repo`).

The answer ends with one line per suggested fix, `TUTTI-TASK: <title> || check: <command> || sentry: <issue id> || severity:
<level>`. Those lines become backlog tasks (source `sentry`, tagged with the project) only for a project that opts in with
`self_improve.tasks: true` and names its `self_improve.autopilot_repo`; any other project (one Tutti may only read) gets the
issue alone. A task is never merged by this path: docs/SCOUTS.md, docs/TELEMETRY.md."""
import re

from . import backlog, scouts

PREFIX = 'sentry-'
MARK = 'TUTTI-TASK:'
SEVERITIES = ('low', 'medium', 'high', 'critical')
SLUG_RX = r'[a-z0-9][a-z0-9_-]{0,63}'


def task_text(name, s):
    org, project = s['org'], s['project']
    return (f'Triage Sentry for project "{name}" (organisation {org}, project {project}): look at the issues that are new or '
            'regressed since your last report (the last 24 hours when there is none). Group them; for each group give the '
            'likely cause, the file and the commit or release that brought it, a suggested fix and a severity (low, medium, '
            'high, critical), with links to the Sentry issues. Do not copy user data, request bodies, tokens or e-mail '
            'addresses from the events. End with one line per fix worth doing, exactly: TUTTI-TASK: <what to change, one '
            'line> || check: <a command that fails before the fix and passes after, e.g. the regression test> || sentry: '
            '<issue id> || severity: <level>. Write no TUTTI-TASK line when nothing new needs a fix.')


def projects_with_sentry(cfg):
    ps = cfg.get('projects') or {}
    pairs = ps.items() if isinstance(ps, dict) else ()
    return {n: p['sentry'] for n, p in pairs if isinstance(p, dict) and isinstance(p.get('sentry'), dict)}


def reply_repo(cfg, name, sc):
    p = (cfg.get('projects') or {}).get(name) or {} if isinstance(cfg.get('projects'), dict) else {}
    return (p.get('sentry') or {}).get('reply_repo') or p.get('repo') or (sc.get('reply') or {}).get('github_repo')


def watches(cfg, sc):
    """The generated watches for a scout with `sentry_triage: true`: {sentry-<project>: {every, task, skill, repo, project}}."""
    if not sc.get('sentry_triage'):
        return {}
    out = {}
    for name, s in projects_with_sentry(cfg).items():
        slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
        out[PREFIX + slug] = {'every': s.get('every', '1d'), 'task': task_text(name, s), 'skill': 'triage',
                              'repo': reply_repo(cfg, name, sc), 'project': name}
    return out


def one_errors(name, s):
    key = f'projects.{name}.sentry'
    errs = [f'{key}.{k} is {s.get(k)!r}: the Sentry {what} slug, as in its URL'
            for k, what in (('org', 'organisation'), ('project', 'project'))
            if not (isinstance(s.get(k), str) and re.fullmatch(SLUG_RX, s[k]))]
    if 'reply_repo' in s and not scouts.is_repo(s['reply_repo']):
        errs.append(f'{key}.reply_repo is {s["reply_repo"]!r}: the GitHub repository as owner/name')
    if 'every' in s and not (isinstance(s['every'], str) and re.fullmatch(scouts.EVERY_RX, s['every'])):
        errs.append(f'{key}.every is {s["every"]!r}: write it as 1d, 12h or 7d')
    return errs


def errors(cfg):
    return [e for name, s in projects_with_sentry(cfg).items() for e in one_errors(name, s)]


def task_lines(answer):
    """The text after `TUTTI-TASK:` on each line that starts with it (after a list marker or a backtick)."""
    for line in (answer or '').splitlines():
        body = line.strip().lstrip('-* ').lstrip('`')
        if body.startswith(MARK):
            rest = body[len(MARK):].strip().rstrip('`').strip()
            if rest:
                yield rest


def task_item(text):
    """One line's fields: {title, check, sentry, severity}; unknown or malformed fields are left out."""
    parts = [p.strip() for p in text.split('||')]
    item = {'title': re.sub(r'\s+', ' ', parts[0])[:200], 'check': '', 'sentry': '', 'severity': 'medium'}
    for p in parts[1:]:
        k, _, v = p.partition(':')
        k, v = k.strip().lower(), v.strip().strip('`')
        if k == 'check':
            item['check'] = v[:300]
        elif k == 'sentry' and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', v):
            item['sentry'] = v
        elif k == 'severity' and v.lower() in SEVERITIES:
            item['severity'] = v.lower()
    return item


def parse(answer):
    """The suggested fixes in a scout's answer: [{title, check, sentry, severity}]; lines without a title are skipped."""
    return [it for it in (task_item(t) for t in task_lines(answer)) if it['title']]


def policy(cfg, project):
    """The project's self-improvement policy (selfimprove.policy), read lazily to keep this module free of a cycle."""
    from . import selfimprove
    return selfimprove.policy(cfg, project)


def feed(cfg, r, enabled):
    """Turns an answered Sentry watch into backlog tasks when its project opts in. Returns (added, why)."""
    project = r.get('project')
    items = parse(r.get('answer'))
    pol = policy(cfg, project) if project else None
    if not items:
        return 0, 'no TUTTI-TASK line'
    if not pol or not pol['tasks'] or pol['autopilot_repo'] not in (cfg.get('autopilot') or {}).get('repos', {}):
        return 0, f'{project or "this project"} takes issues only (self_improve.tasks is off or no autopilot repo)'
    added = 0
    with backlog.locked(cfg) as data:
        for it in items:
            if it['sentry'] and any(t.get('sentry') == it['sentry'] and t['status'] not in ('done', 'dropped') for t in data['tasks']):
                continue   # one task per Sentry issue
            notes = [f'From the Sentry triage of {project} ({r.get("url") or r["id"]}), severity {it["severity"]}.',
                     'First add a regression test that reproduces the issue and fails; then fix it so the test passes.']
            t = backlog.new_task(pol['autopilot_repo'], it['title'], check=it['check'], source='sentry', notes=notes)
            t.update(project=project, sentry=it['sentry'], severity=it['severity'], status='queued' if enabled else 'candidate',
                     pr_mode='ready')
            _t, was_new = backlog.add(data, t)
            added += int(was_new)
    return added, 'ok'


def feed_answered(cfg, data, enabled):
    """Every answered Sentry watch not fed yet. Returns event lines."""
    events = []
    for r in data['requests']:
        if r.get('kind') != 'watch' or not str(r.get('watch') or '').startswith(PREFIX) or r.get('status') != 'answered' or r.get('fed'):
            continue
        added, why = feed(cfg, r, enabled)
        r['fed'] = {'added': added, 'why': why}
        events.append(f'{r["id"]} Sentry triage of {r.get("project")}: {added} task(s) for the backlog ({why})')
    return events
