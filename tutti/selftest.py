"""`tutti selftest live` (docs/TESTING.md, *Live self-test and learning loop*): the dashboard's browser checks run against
this computer's own running dashboard, with its real data, and every new finding becomes a mock fixture, a test and a task.

- Who: a dedicated read-only identity, `tutti-selftest`, a viewer Tutti mints itself in its own users file, with a session
  of one hour that is dropped when the run ends. Never the owner's Google login; the session id goes to the browser through
  the environment, never on a command line or in a log. A viewer cannot change anything (the server refuses it), and the
  runner aborts every request that is not a GET as well: only navigation, the Me/Household view switch and disclosures.
- What: tests/e2e_live/live.py, the same checks as the mock suite (tests/e2e/checks.js: layout, clipped text, headlines
  that fit, plain copy; axe; console errors) on every tab a viewer sees, at phone, tablet, laptop and 4K, light and dark.
- Learning: a finding not seen before is recorded under <state>/selftest/ with a screenshot and the /api/state JSON it was
  drawn from, redacted (e-mail addresses and token-like strings); that JSON becomes tests/e2e/fixtures/live-<id>.json, which
  tests/e2e/test_live_fixtures.py replays in the mock dashboard so the same check fails there until the bug is fixed; and a
  backlog task asks for the fix with that test as its check. A finding seen before is counted, never filed twice.

Pure apart from the files under <state>/selftest/, the users and sessions files, the fixture folder and the backlog."""
import hashlib
import json
import os
import re
import subprocess
import sys
import time

from . import auth, backlog, config

SUB = 'tutti-selftest'
WHO = {'sub': SUB, 'email': 'selftest@tutti.invalid', 'name': 'Tutti self-test'}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMAIL = re.compile(r'[\w.+-]+@[\w-]+(\.[\w-]+)+')
TOKEN = re.compile(r'\b[A-Za-z0-9_\-]{32,}\b')
NUMBER = re.compile(r'\d+(\.\d+)?')


class SelftestError(Exception):
    """The self-test cannot run; the message says what to do."""


def folder(cfg):
    d = os.path.join(config.state_dir(cfg), 'selftest')
    os.makedirs(d, exist_ok=True)
    return d


def mint(cfg, hours=1, clock=time.time):
    """A session id for the read-only self-test viewer. Refused while no admin exists: the first user to sign in becomes
    the admin, and that must be a person."""
    state = config.state_dir(cfg)
    users = auth.Users(state, clock)
    if not any(u.get('role') == 'admin' for u in users.all().values()):
        raise SelftestError('no one has signed in to the dashboard yet: sign in once as the owner, then run the self-test')
    u = users.sign_in(WHO)
    if u['role'] != 'viewer':
        if u['role'] == 'admin':
            raise SelftestError(f'the user {SUB} is an admin: make it a viewer on the Users tab (it must stay read-only)')
        users.set_role(SUB, 'viewer')
    return auth.Sessions(state, hours, clock).create(SUB)[0]


def revoke(cfg, sid, clock=time.time):
    auth.Sessions(config.state_dir(cfg), 1, clock).drop(sid)


def redact(value):
    """The /api/state JSON with no e-mail address and no token-like string left in it."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return TOKEN.sub('REDACTED', EMAIL.sub('someone@example.com', value))
    return value


def signature(f):
    """A finding's identity across runs: the view, the check and its text without numbers (a count or a pixel size that
    moves is the same finding)."""
    text = NUMBER.sub('#', f['detail'])
    return hashlib.sha256(f'{f["view"]}|{f["check"]}|{text}'.encode()).hexdigest()[:12]


def repo_of(cfg):
    """The autopilot repo that is this Tutti checkout, else None (the task is then filed under 'tutti')."""
    for name, r in ((cfg.get('autopilot') or {}).get('repos') or {}).items():
        if r.get('path') and os.path.realpath(config.expand(r['path'])) == os.path.realpath(ROOT):
            return name
    return None


def learn(cfg, result, fixtures=None, clock=time.time):
    """Records the run, writes a fixture and files a task for each finding not seen before. Returns the run's summary."""
    d = folder(cfg)
    seen_path = os.path.join(d, 'seen.json')
    try:
        with open(seen_path, encoding='utf8') as f:
            seen = json.load(f)
    except (OSError, ValueError):
        seen = {}
    fixtures = fixtures or os.path.join(ROOT, 'tests', 'e2e', 'fixtures')
    state = redact(result.get('state') or {})
    new, repeat = [], []
    for f in result.get('findings') or []:
        sig = signature(f)
        if sig in seen:
            seen[sig]['count'] += 1
            seen[sig]['last'] = clock()
            repeat.append(sig)
            continue
        os.makedirs(fixtures, exist_ok=True)
        fixture = os.path.join(fixtures, f'live-{sig}.json')
        with open(fixture, 'w', encoding='utf8') as out:
            json.dump({'finding': redact(f), 'state': state}, out, indent=1, sort_keys=True)
        task_id = file_task(cfg, f, sig)
        seen[sig] = {'finding': redact(f), 'first': clock(), 'last': clock(), 'count': 1, 'task': task_id,
                     'fixture': os.path.relpath(fixture, ROOT)}
        new.append(sig)
    with open(seen_path, 'w', encoding='utf8') as out:
        json.dump(seen, out, indent=1)
    summary = {'at': clock(), 'ok': not result.get('findings') and not result.get('error'), 'error': result.get('error'),
               'checked': result.get('checked', 0), 'findings': len(result.get('findings') or []), 'new': new, 'repeat': repeat,
               'tasks': [seen[s]['task'] for s in new if seen[s].get('task')]}
    with open(os.path.join(d, 'last.json'), 'w', encoding='utf8') as out:
        json.dump(summary, out, indent=1)
    return summary


def file_task(cfg, f, sig):
    title = f'Dashboard self-test {sig}: {f["check"]} on {f["view"]} ({f["width"]}, {f["theme"]})'
    check = f'PYTHONPATH=tests python3 -m unittest discover -s tests/e2e -p test_live_fixtures.py -k {sig}'
    notes = [f'Live finding: {redact(f["detail"])[:400]}',
             f'Fixture: tests/e2e/fixtures/live-{sig}.json (the redacted /api/state it was drawn from)']
    with backlog.locked(cfg) as data:   # the autopilot's round never interleaves with this write
        task, _ = backlog.add(data, backlog.new_task(repo_of(cfg) or 'tutti', title, ('tutti/web',), check, 'S', 'selftest', notes))
    return task['id']


def last(cfg):
    """The last run's summary for the dashboard and What did I miss?, else None."""
    try:
        with open(os.path.join(folder(cfg), 'last.json'), encoding='utf8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run(cfg, python=None, widths=None, out=None):
    """Mints the viewer, runs tests/e2e_live/live.py against the running dashboard, revokes the session, learns."""
    runner = os.path.join(ROOT, 'tests', 'e2e_live', 'live.py')
    if not os.path.isfile(runner):
        raise SelftestError(f'{runner} is missing: the self-test runs from a Tutti checkout (git clone), not an installed package')
    port = (cfg.get('dashboard') or {}).get('port') or 4777
    out = out or os.path.join(folder(cfg), time.strftime('%Y%m%d-%H%M%S'))
    os.makedirs(out, exist_ok=True)
    sid = mint(cfg)
    try:
        env = {**os.environ, 'TUTTI_LIVE_SESSION': sid, 'TUTTI_LIVE_COOKIE': f'tutti_session_{port}'}
        args = [python or sys.executable, runner, f'http://127.0.0.1:{port}', out, *(['--widths', ','.join(widths)] if widths else [])]
        done = subprocess.run(args, env=env, capture_output=True, text=True, timeout=1800, check=False)
    finally:
        revoke(cfg, sid)
    try:
        with open(os.path.join(out, 'result.json'), encoding='utf8') as f:
            result = json.load(f)
    except (OSError, ValueError):
        result = {'error': f'the live runner stopped ({done.returncode}): {(done.stderr or done.stdout).strip()[-400:]}'}
    return learn(cfg, result), out
