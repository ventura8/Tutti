"""The live self-test (t241, docs/DASHBOARD.md, *Self-test*): the running dashboard asks itself, with the household's real
config and state, for every route its pages on disk read, and records each answer. It runs a minute after the dashboard
starts and then daily. A route the pages call but the running code does not serve (the 2026-11-22 empty tabs: new pages,
old code), a crash on real data or a slow answer is a failure: shown on the Self-test card and filed once as a backlog task
for the next worker, so the mock browser tests learn what the live instance found.

Pure: `page_routes` (the pages' GET calls), `judge` (one answer) and `tasks_for` (new backlog tasks); `run` and `record` do
the calls and the file."""
import json
import os
import re
import time

from . import authweb

FILE = 'selftest.json'
SLOW_S = 5.0

# answered by the sign-in layer (authweb) before the dashboard's own routes: its own constants, so this list cannot drift
UPSTREAM = ('/api/auth/', authweb.SCHEDULE, authweb.USERS)
FETCH_RX = re.compile(r"fetch\(\s*['`](/api/[A-Za-z0-9/_-]+)['`]\s*(,\s*\{(?P<opts>[^}]*))?")


def page_routes(web):
    """{path: [files]} of every literal /api route the pages fetch with GET (no `method:` in its options)."""
    out = {}
    for name in sorted(os.listdir(web)):
        if not name.endswith('.js'):
            continue
        with open(os.path.join(web, name), encoding='utf8') as f:
            text = f.read()
        for m in FETCH_RX.finditer(text):
            opts = m.group('opts') or ''
            if 'method' in opts and not re.search(r"method:\s*'GET'", opts):
                continue
            path = m.group(1)
            if not path.startswith(UPSTREAM):
                out.setdefault(path, []).append(name)
    return out


def judge(path, code, seconds, error=''):
    """{path, code, ms, ok, why}: a crash, a 404 (the pages call it, the code does not serve it), a 5xx or a slow answer fails;
    a sign-in answer (401, 403) means it is served."""
    why = ''
    if error:
        why = f'it crashed on the real data ({error})'
    elif code == 404:
        why = 'the pages call it but the running code does not serve it: the dashboard runs older code than its pages (restart it)'
    elif code >= 500:
        why = f'it answered {code}'
    elif seconds > SLOW_S:
        why = f'it took {seconds:.1f} s (over {SLOW_S:.0f} s): the page feels broken'
    return {'path': path, 'code': code, 'ms': round(seconds * 1000), 'ok': not why, 'why': why}


def run(collector, web, get, clock=time.monotonic):
    """[judged answers] for every page route, in path order; `get(collector, path)` is the dashboard's own GET."""
    out = []
    for path, _files in sorted(page_routes(web).items()):
        start = clock()
        try:
            code, error = get(collector, path), ''
        except Exception as e:   # the point: a crash on real data is a finding, not the end of the run
            code, error = 500, f'{type(e).__name__}: {str(e)[:160]}'
        out.append(judge(path, code, clock() - start, error))
    return out


def tasks_for(checks, tasks):
    """[(title, note)] of backlog tasks to add: one per failing route, unless an open task already has that title."""
    open_titles = {t.get('title') for t in tasks if t.get('status') not in ('done', 'dropped')}
    out = []
    for c in checks:
        title = f'Live self-test: {c["path"]} fails'
        if not c['ok'] and title not in open_titles:
            out.append((title, f'{c["why"]} (code {c["code"]}, {c["ms"]} ms). Add a mock browser test for it '
                               '(tests/e2e) so the mock suite catches it next time (t241).'))
    return out


def record(cfg, checks, version, now=None, file_tasks=True):
    """Writes <state>/selftest.json and files a backlog task per new failure; returns the record."""
    from . import backlog, config
    rec = {'at': now or time.time(), 'version': version, 'checks': checks,
           'failed': sum(1 for c in checks if not c['ok']), 'total': len(checks)}
    path = os.path.join(config.state_dir(cfg), FILE)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + '.tmp', 'w', encoding='utf8') as f:
            json.dump(rec, f)
        os.replace(path + '.tmp', path)
    except OSError:
        pass   # a read-only state folder: the card says so (no record)
    if file_tasks and rec['failed'] and (cfg.get('autopilot') or {}).get('repos', {}).get('tutti'):
        with backlog.locked(cfg) as data:
            for title, note in tasks_for(checks, data['tasks']):
                backlog.add(data, backlog.new_task('tutti', title, notes=[note], source='selftest'))
    return rec


def load(cfg):
    """The last record, or None (never run, or unreadable)."""
    from . import config
    try:
        with open(os.path.join(config.state_dir(cfg), FILE), encoding='utf8') as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None
