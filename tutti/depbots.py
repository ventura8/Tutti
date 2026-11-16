"""Dependency bots and vulnerability reports as autopilot tasks (docs/SECURITY-INTAKE.md). For every repo under
`autopilot.repos`, Tutti reads through the `gh` login already present (never a token of its own):

- the open pull requests of dependency bots: GitHub Dependabot (`app/dependabot`, branches `dependabot/...`) and Cursor's
  dependency agent (Anybump and Cursor's cloud agents open pull requests as `cursor[bot]` on `cursor/...` branches; only
  the ones that read as a dependency or security change count);
- vulnerability reports: GitHub Dependabot alerts and code-scanning alerts (`gh api`), and the security findings Cursor's
  Security Review / Bugbot (`cursor[bot]`) and Greptile (`greptile-apps[bot]`) post as pull request review comments (neither
  has an alerts API: their comments are the report).

Each becomes a backlog task: a bump's task brings the bot's branch into the worktree, runs the repo's check and fixes what
breaks; trivial (patch and minor) bumps are grouped into one task per repo; reports are deduped (one per package, rule and
file), ranked by severity and queued worst first. A task whose pull request closed or whose alert GitHub marked fixed is
dropped by itself: GitHub closes a Dependabot alert when the fix lands, and Dependabot closes its pull request when the
bump is on the base. The plan is pure; `gather` does the reading and runs outside the backlog's lock."""
import json
import os
import re
import subprocess
import time

from . import backlog, config, durable
from .review import gh

FILE = 'depbots.json'
SEVERITY = {'critical': 4, 'high': 3, 'moderate': 2, 'medium': 2, 'low': 1, 'warning': 1, 'note': 0, 'error': 3}
NAMES = {4: 'critical', 3: 'high', 2: 'medium', 1: 'low', 0: 'info'}
SECURITY_WORDS = re.compile(r'\b(security|vulnerab\w*|cve-\d+|ghsa-|injection|xss|csrf|ssrf|rce|secret|credential|unsafe '
                            r'deserial\w*|path traversal|auth\w* bypass)\b', re.I)
VERSIONS = re.compile(r'from v?(\d+)(?:\.(\d+))?\S* to v?(\d+)(?:\.(\d+))?', re.I)


def settings(cfg):
    return cfg['depbots']


def errors(cfg):
    s = cfg.get('depbots') or {}
    errs = []
    for kind in ('bots', 'scanners'):
        for i, b in enumerate(s.get(kind) or []):
            if not isinstance(b, dict) or not b.get('name') or not isinstance(b.get('authors'), list):
                errs.append(f'depbots.{kind}[{i}] needs name and authors (a list of GitHub logins, e.g. dependabot[bot])')
    if not isinstance(s.get('every_minutes', 360), (int, float)) or s.get('every_minutes', 360) < 15:
        errs.append(f'depbots.every_minutes is {s.get("every_minutes")!r}: a number of minutes, at least 15 (GitHub\'s rate limit)')
    return errs


def norm(login):
    """`app/dependabot`, `dependabot[bot]` and `dependabot` are one bot."""
    login = (login or '').lower()
    return re.sub(r'\[bot\]$', '', login[4:] if login.startswith('app/') else login)


def slug_of(url):
    """owner/name of a GitHub remote URL (https or ssh), else ''."""
    m = re.search(r'github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$', (url or '').strip())
    return f'{m.group(1)}/{m.group(2)}' if m else ''


def repo_slug(repo, run=subprocess.run):
    if repo.get('github'):
        return repo['github']
    r = run(['git', '-C', config.expand(repo['path']), 'remote', 'get-url', 'origin'], capture_output=True, text=True)
    return slug_of(r.stdout) if r.returncode == 0 else ''


def classify_pr(pr, bots):
    """The bot's name when the pull request is a dependency bot's, else None."""
    who = norm((pr.get('author') or {}).get('login'))
    branch = pr.get('headRefName') or ''
    for b in bots:
        if who not in {norm(a) for a in b['authors']} and not any(branch.startswith(p) for p in b.get('branch_prefixes') or []):
            continue
        if b.get('match') and not re.search(b['match'], f'{pr.get("title", "")} {branch}', re.I):
            continue
        return b['name']
    return None


def bump_kind(title):
    """patch, minor or major from 'Bump x from 1.2.3 to 1.3.0'; group or unknown otherwise."""
    m = VERSIONS.search(title or '')
    if not m:
        return 'group' if re.search(r'\bgroup\b', title or '', re.I) else 'unknown'
    if m.group(1) != m.group(3):
        return 'major'
    return 'minor' if (m.group(2) or '0') != (m.group(4) or '0') else 'patch'


def rank(severity):
    return SEVERITY.get((severity or '').lower(), 0)


def from_dependabot(items, slug):
    out = []
    for a in items:
        adv, dep = a.get('security_advisory') or {}, a.get('dependency') or {}
        pkg = (dep.get('package') or {}).get('name') or '?'
        fix = ((a.get('security_vulnerability') or {}).get('first_patched_version') or {}).get('identifier')
        out.append({'key': f'dep:{(dep.get("package") or {}).get("ecosystem", "")}:{pkg}', 'source': 'github-dependabot',
                    'severity': rank(adv.get('severity')), 'title': f'{pkg}: {adv.get("summary") or adv.get("ghsa_id") or "advisory"}',
                    'url': a.get('html_url') or f'https://github.com/{slug}/security/dependabot', 'path': dep.get('manifest_path', ''),
                    'hint': f'upgrade {pkg} to {fix} or later (or pin it there with an override)' if fix else
                            f'no patched {pkg} yet: replace it or remove the vulnerable use'})
    return out


def from_code_scanning(items, slug):
    out = []
    for a in items:
        rule = a.get('rule') or {}
        loc = (a.get('most_recent_instance') or {}).get('location') or {}
        sev = rule.get('security_severity_level') or rule.get('severity')
        out.append({'key': f'rule:{rule.get("id", "?")}:{loc.get("path", "")}', 'source': 'github-code-scanning', 'severity': rank(sev),
                    'title': f'{rule.get("description") or rule.get("id")} in {loc.get("path", "?")}:{loc.get("start_line", "?")}',
                    'url': a.get('html_url') or f'https://github.com/{slug}/security/code-scanning', 'path': loc.get('path', ''),
                    'hint': f'fix the {(a.get("tool") or {}).get("name", "scanner")} finding {rule.get("id", "")}'})
    return out


def from_comments(comments, scanners):
    """Security findings in the review comments of the scanners (Cursor, Greptile): comments without security words are code
    review, not a report, and are left out."""
    by = {norm(a): s['name'] for s in scanners for a in s['authors']}
    out = []
    for c in comments:
        name, body = by.get(norm((c.get('user') or {}).get('login'))), c.get('body') or ''
        if not name or not SECURITY_WORDS.search(body):
            continue
        sev = re.search(r'\b(critical|high|medium|moderate|low)\b', body, re.I)
        first = next((ln.strip(' #*>`') for ln in body.splitlines() if ln.strip(' #*>`')), 'security finding')[:120]
        out.append({'key': f'scan:{c.get("path", "")}:{first.lower()}', 'source': name, 'severity': rank(sev.group(1)) if sev else 2,
                    'title': f'{first} ({c.get("path") or "pull request"})', 'url': c.get('html_url', ''), 'path': c.get('path', ''),
                    'hint': f'{name} reported this on a pull request review'})
    return out


def dedupe(alerts):
    """One entry per key with the worst severity and every source; worst first."""
    seen = {}
    for a in alerts:
        k = seen.get(a['key'])
        if k is None:
            seen[a['key']] = {**a, 'sources': [a['source']], 'urls': [a['url']]}
            continue
        k['sources'] = sorted({*k['sources'], a['source']})
        k['urls'] = [*k['urls'], a['url']]
        if a['severity'] > k['severity']:
            k.update(severity=a['severity'], title=a['title'], hint=a['hint'])
    return sorted(seen.values(), key=lambda a: (-a['severity'], a['key']))


def problem(kind, slug, err):
    """The owner's specific message for a source that cannot be read, with the exact link that fixes it."""
    e = (err or '').lower()
    if kind == 'dependabot' and ('403' in e or 'disabled' in e or 'not authorized' in e):
        return (f'{slug}: Dependabot alerts cannot be read: turn them on at https://github.com/{slug}/settings/security_analysis '
                '(Dependabot alerts), and if they are on, give gh the security_events scope: `gh auth refresh -s security_events`')
    if kind == 'code-scanning' and ('no analysis' in e or '404' in e or 'not enabled' in e):
        return ''   # no code scanning set up is not a fault; the dashboard links the setup page
    if kind == 'code-scanning' and '403' in e:
        return (f'{slug}: code-scanning alerts cannot be read: `gh auth refresh -s security_events`, or turn code scanning on at '
                f'https://github.com/{slug}/security/code-scanning/setup')
    return f'{slug}: reading {kind} failed ({(err or "no output")[:160]}): `gh auth status` shows whether gh is signed in'


def _api(gh_fn, path):
    rc, out, err = gh_fn(['api', '--paginate', path])
    if rc != 0:
        return None, err or out
    try:
        return json.loads(re.sub(r'\]\s*\[', ',', out.strip()) or '[]'), ''
    except ValueError as exc:
        return None, f'unreadable answer ({exc})'


def read_repo(slug, s, gh_fn=gh):
    """{prs, alerts, problems, code_scanning} of one repository."""
    out = {'prs': [], 'alerts': [], 'problems': [], 'code_scanning': True}
    rc, raw, err = gh_fn(['pr', 'list', '-R', slug, '--state', 'open', '--limit', '100', '--json',
                          'number,title,headRefName,author,url,labels,statusCheckRollup'])
    if rc != 0:
        out['problems'].append(problem('pull requests', slug, err))
        return out
    for pr in json.loads(raw or '[]'):
        bot = classify_pr(pr, s['bots'])
        if bot:
            checks = pr.get('statusCheckRollup') or []
            out['prs'].append({'number': pr['number'], 'title': pr['title'], 'branch': pr['headRefName'], 'url': pr['url'], 'bot': bot,
                               'kind': bump_kind(pr['title']),
                               'failing': any((c.get('conclusion') or '').upper() == 'FAILURE' for c in checks)})
    items, err = _api(gh_fn, f'repos/{slug}/dependabot/alerts?state=open&per_page=100')
    out['alerts'] += from_dependabot(items, slug) if items is not None else []
    out['problems'] += [p for p in [problem('dependabot', slug, err)] if items is None and p]
    items, err = _api(gh_fn, f'repos/{slug}/code-scanning/alerts?state=open&per_page=100')
    out['alerts'] += from_code_scanning(items, slug) if items is not None else []
    out['code_scanning'] = items is not None
    out['problems'] += [p for p in [problem('code-scanning', slug, err)] if items is None and p]
    items, err = _api(gh_fn, f'repos/{slug}/pulls/comments?sort=created&direction=desc&per_page=100')
    out['alerts'] += from_comments(items, s['scanners']) if items is not None else []
    out['alerts'] = dedupe(out['alerts'])
    return out


def due(cfg, now):
    s = settings(cfg)
    last = (load(cfg).get('read_at') or 0)
    return s.get('enabled', True) and bool(cfg['autopilot']['repos']) and now - last >= s.get('every_minutes', 360) * 60


def gather(cfg, now, gh_fn=gh, run=subprocess.run, force=False):
    """{repo name: read_repo + slug} when due (or forced), else None. Network: never under the backlog's lock."""
    if not force and not due(cfg, now):
        return None
    found = {}
    for name, repo in cfg['autopilot']['repos'].items():
        slug = repo_slug(repo, run)
        if not slug:
            found[name] = {'slug': '', 'prs': [], 'alerts': [], 'code_scanning': False,
                           'problems': [f'{name}: no GitHub repository found (no github.com origin in {repo["path"]}): set '
                                        f'autopilot.repos.{name}.github to owner/name']}
            continue
        found[name] = {'slug': slug, **read_repo(slug, settings(cfg), gh_fn)}
    return found


def pr_note(pr, check):
    return (f'{pr["bot"]} opened {pr["url"]} on branch {pr["branch"]}. Bring its change in first: '
            f'`git fetch origin {pr["branch"]} && git diff HEAD...FETCH_HEAD | git apply --3way --index`, then run the check '
            f'({check or "the repo check"}) and fix what breaks: code, the lockfile, an override or a pin. Keep the bump. '
            'When the result lands on the base, the bot closes its pull request by itself.')


def tasks_for(name, scan, check, group_trivial=True):
    """[(title, size, source, notes, ref)] for one repo's scan: ref is the pull request url or the alert key."""
    out, trivial = [], []
    for pr in scan['prs']:
        if group_trivial and pr['kind'] in ('patch', 'minor') and not pr['failing']:
            trivial.append(pr)
            continue
        size = 'M' if pr['kind'] in ('major', 'group') or pr['failing'] else 'S'
        out.append((f'Dependency update: {pr["title"]} (#{pr["number"]})', size, f'depbot:{pr["bot"]}', [pr_note(pr, check)], pr['url']))
    if len(trivial) == 1:
        pr = trivial[0]
        out.append((f'Dependency update: {pr["title"]} (#{pr["number"]})', 'S', f'depbot:{pr["bot"]}', [pr_note(pr, check)], pr['url']))
    elif trivial:
        out.append(('Land the open patch and minor dependency bumps', 'S', 'depbot:group', [pr_note(p, check) for p in trivial],
                    ' '.join(p['url'] for p in trivial)))
    for a in scan['alerts']:
        out.append((f'Fix {NAMES[a["severity"]]} vulnerability: {a["title"]}', 'M' if a['severity'] >= 3 else 'S',
                    f'security:{"+".join(a["sources"])}', [f'{a["hint"]}. Reports: {" ".join(a["urls"])}', 'Run the check before '
                    'finishing; GitHub closes the alert by itself when the fix is on the default branch.'], a['key']))
    return out


def apply(cfg, data, found):
    """Queues new tasks and drops queued ones whose pull request or alert is gone. Returns event lines."""
    if found is None:
        return []
    events, live = [], set()
    for name, scan in found.items():
        if scan['problems'] and not scan['prs'] and not scan['alerts']:
            live |= {t.get('depbot_ref') for t in data['tasks'] if t['repo'] == name}   # unread: keep its tasks
            continue
        check = cfg['autopilot']['repos'][name].get('check') or ''
        for title, size, source, notes, ref in tasks_for(name, scan, check, settings(cfg).get('group_trivial', True)):
            live.add(ref)
            task, added = backlog.add(data, {**backlog.new_task(name, title, (), check, size, source, notes), 'depbot_ref': ref})
            if added:
                events.append(f'depbots: queued {task["id"]} {title}')
            elif task['status'] == 'queued' and task.get('notes') != notes:
                task['notes'] = notes   # the group's member list follows the open pull requests
    for t in data['tasks']:
        if t.get('depbot_ref') and t['status'] == 'queued' and t['repo'] in found and t['depbot_ref'] not in live:
            t['status'] = 'dropped'
            t.setdefault('notes', []).append('dropped by itself: the pull request closed or GitHub marked the alert fixed')
            events.append(f'depbots: dropped {t["id"]} (closed or fixed upstream)')
    return events


def summary(found, now):
    repos = []
    for name, scan in (found or {}).items():
        worst = max((a['severity'] for a in scan['alerts']), default=None)
        slug = scan['slug']
        repos.append({'name': name, 'slug': slug, 'prs': [{k: p[k] for k in ('number', 'title', 'url', 'bot', 'kind', 'failing')}
                                                           for p in scan['prs']],
                      'alerts': len(scan['alerts']), 'worst': NAMES[worst] if worst is not None else None,
                      'top': [{'title': a['title'], 'url': a['url'], 'sources': a['sources'], 'severity': NAMES[a['severity']]}
                              for a in scan['alerts'][:5]],
                      'links': {'prs': f'https://github.com/{slug}/pulls?q=is%3Apr+is%3Aopen+author%3Aapp%2Fdependabot',
                                'security': f'https://github.com/{slug}/security'} if slug else {},
                      'code_scanning': scan['code_scanning'], 'problems': scan['problems']})
    return {'read_at': now, 'repos': repos}


def path_of(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


def load(cfg):
    try:
        with open(path_of(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(cfg, found, now):
    durable.dump_json(path_of(cfg), summary(found, now), indent=1)


def view(cfg):
    """The dashboard's Dependencies and security card: counts, the worst severity, links and what needs the owner."""
    d = load(cfg)
    repos = d.get('repos') or []
    worst = max((rank(r['worst']) for r in repos if r.get('worst')), default=None)
    return {'enabled': settings(cfg).get('enabled', True), 'read_at': d.get('read_at'), 'repos': repos,
            'prs': sum(len(r['prs']) for r in repos), 'alerts': sum(r['alerts'] for r in repos),
            'worst': NAMES[worst] if worst is not None else None, 'problems': [p for r in repos for p in r['problems']]}


def round_read(cfg, now, on, record=None):
    """The autopilot round's read: None when off, not due or failed (the failure is recorded), else the saved scan."""
    try:
        found = gather(cfg, now) if on else None
    except (OSError, ValueError) as exc:
        if record:
            record('error', 'depbots.gather', reason=type(exc).__name__)
        return None
    if found is not None:
        save(cfg, found, now)
    return found


def scan_now(cfg, gh_fn=gh, run=subprocess.run, now=None):
    """`tutti security scan`: read every repo now and queue the tasks. Returns (found, events)."""
    now = now or time.time()
    found = gather(cfg, now, gh_fn, run, force=True)
    with backlog.locked(cfg) as data:
        events = apply(cfg, data, found)
        backlog.save(cfg, data)
    save(cfg, found, now)
    return found, events
