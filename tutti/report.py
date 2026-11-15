"""Report a problem: `tutti report` and the dashboard's **Report a problem** button (docs/REPORT.md).

The person says what happened and what they expected. Tutti adds a diagnostic bundle that is scrubbed by the same rules as
telemetry (tutti/scrub.py, docs/TELEMETRY.md): the version, the OS, the `tutti doctor` summary, the recent errors of the
local telemetry (types, places and counts only) and the shape of the config with every value replaced by its type. Never a
secret, a path, a host name, an e-mail address, a prompt or code. The person sees the whole text before anything is sent.

Delivery: a GitHub issue on Tutti's repository with the label `user-report` when gh is signed in; otherwise a prefilled
"new issue" link that the person opens in the browser and sends themselves. When sending telemetry is on, the report also
goes to Sentry as user feedback, linked to the last crash. Pure building here; the I/O (gh, the browser, Sentry) at the edge."""
import functools
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.parse

from . import __version__, config, github, scrub, telemetry

REPO = 'ventura8/Tutti'
LABEL = 'user-report'
NEW_ISSUE = f'https://github.com/{REPO}/issues/new'
MAX_TEXT = 2000          # characters of each of the person's two answers
MAX_URL = 7000           # a prefilled link longer than this is cut (browsers and GitHub refuse very long URLs)
ERROR_KINDS = ('crash', 'failure', 'error')
SENDS_PER_HOUR = 5       # reports the dashboard sends in an hour at most (a page cannot flood the repository)
_sends, _sends_lock = [], threading.Lock()
KNOWN_KEYS = set(config.DEFAULTS)


class ReportError(Exception):
    """The report cannot be built or sent; the message says why and what to do."""


# ---- the bundle (pure)

def scalar_shape(value):
    if isinstance(value, (list, tuple)):
        return f'list[{len(value)}]'
    if isinstance(value, bool):
        return 'bool'
    return 'null' if value is None else type(value).__name__


def known_keys(keys, k):
    """The defaults below a key Tutti defines ({} when it has none), or None when `k` is a name a person chose."""
    if not isinstance(keys, dict) or k not in keys or scrub.category(k) != k:
        return None
    return keys[k] if isinstance(keys[k], dict) else {}


def shape(value, keys=None, depth=0):
    """The config's shape: the keys Tutti defines kept, names a person chose (machines, workers, projects) numbered, every
    value replaced by its type, so no host, path, name or secret is in it."""
    if not isinstance(value, dict):
        return scalar_shape(value)
    if depth > 3:
        return 'mapping'
    out, n = {}, 0
    for k, v in value.items():
        if str(k).startswith('_'):
            continue
        sub = known_keys(keys, k)
        if sub is None:
            n += 1
            out[f'<name{n}>'] = shape(v, None, depth + 1)
        else:
            out[k] = shape(v, sub, depth + 1)
    return out


def config_shape(cfg):
    return shape({k: v for k, v in (cfg or {}).items() if k in KNOWN_KEYS}, config.DEFAULTS)


def recent_errors(events, limit=15):
    """The newest crashes and failures of the local telemetry: kind, name and their category fields only."""
    out = []
    for ev in reversed(events):
        if ev.get('kind') in ERROR_KINDS:
            fields = scrub.fields({k: v for k, v in ev.items() if k not in ('v', 'ts', 'kind', 'name')})
            out.append({'when': str(ev.get('ts') or '')[:16], 'kind': ev['kind'], 'name': scrub.category(ev.get('name')),
                        **fields})
            if len(out) >= limit:
                break
    return out


def clean(text, hosts=()):
    """The person's own words, scrubbed like a crash message (e-mails, paths, hosts, URLs, keys), quotes kept."""
    return scrub.text(text, hosts=hosts, limit=MAX_TEXT, quotes=False)


def bundle(cfg, what, expected, doctor_lines, events, sending, hosts=()):
    """Everything the report holds, already scrubbed: {title, what, expected, diagnostics}."""
    what, expected = clean(what, hosts), clean(expected, hosts)
    if not what:
        raise ReportError('say what happened first: one or two sentences are enough')
    title = what.split('. ')[0][:80]
    diagnostics = {
        'version': __version__,
        'os': f'{platform.system() or sys.platform} {scrub.category(platform.release())}',
        'python': platform.python_version(),
        'doctor': [scrub.text(x, hosts=hosts, limit=200) for x in doctor_lines] or ['ok'],
        'telemetry_sending': bool(sending),
        'recent_errors': recent_errors(events),
        'config_shape': config_shape(cfg),
    }
    return {'title': f'User report: {title}', 'what': what, 'expected': expected, 'diagnostics': diagnostics}


def body(rep, details=6000):
    """The issue's text (Markdown), the same the person previews. `details`: characters of the JSON block at most."""
    d = rep['diagnostics']
    return '\n'.join([
        '### What happened', '', rep['what'], '',
        '### What I expected', '', rep['expected'] or '(not given)', '',
        '### Diagnostics (scrubbed: no secrets, paths, hosts, e-mails, prompts or code)', '',
        f'- Tutti {d["version"]}, {d["os"]}, Python {d["python"]}',
        f'- doctor: {"; ".join(d["doctor"])}',
        f'- telemetry sending: {"on" if d["telemetry_sending"] else "off"}', '',
        '<details><summary>Recent errors and the config shape</summary>', '',
        '```json', json.dumps({'recent_errors': d['recent_errors'], 'config_shape': d['config_shape']}, indent=1)[:details], '```',
        '', '</details>', '', '_Sent with `tutti report`._'])


def issue_url(rep):
    """A prefilled "new issue" link, cut to a length GitHub accepts: the diagnostics' JSON block first (its fence and
    <details> stay closed), then the person's own text."""
    def link(r, details):
        q = urllib.parse.urlencode({'labels': LABEL, 'title': r['title'], 'body': body(r, details)})
        return f'{NEW_ISSUE}?{q}'
    for details in (6000, 3000, 1500, 600, 0):
        url = link(rep, details)
        if len(url) <= MAX_URL:
            return url
    cut = dict(rep)
    while len(url) > MAX_URL and len(cut['what']) + len(cut['expected']) > 100:
        cut['what'], cut['expected'] = cut['what'][:len(cut['what']) * 3 // 4], cut['expected'][:len(cut['expected']) * 3 // 4]
        url = link(cut, 0)
    return url


def feedback_event(rep, crash_id=''):
    """The report as Sentry user feedback, linked to the last crash when there is one."""
    ev = telemetry.base_event('info', {'where': 'report'})
    ev['type'] = 'feedback'
    fb = {'message': f'{rep["what"]}\n\nExpected: {rep["expected"] or "-"}'[:4000], 'source': 'tutti-report',
          'name': 'Tutti user'}
    if crash_id:
        fb['associated_event_id'] = crash_id
    ev['contexts'] = {**ev.get('contexts', {}), 'feedback': fb}
    return ev


# ---- the edge: gathering, gh, the browser, Sentry

def gather(cfg, what, expected, doctor_fn=None):
    """The scrubbed report for this computer."""
    from . import doctor
    folder = telemetry.folder_of(config.state_dir(cfg))
    lines = (doctor_fn or doctor.check_git)()
    return bundle(cfg, what, expected, lines, telemetry.read_events(folder), telemetry.sending(folder),
                  telemetry.personal_hosts(cfg))


def gh_signed_in(run=None):
    run = run or github.run
    try:
        return run(['gh', 'auth', 'status'], capture_output=True, text=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def create_issue(rep, run=None):
    """The new issue's URL, or '' when GitHub could not create it. The label is tried first, then the issue without it."""
    run = run or github.run
    base = ['gh', 'issue', 'create', '--repo', REPO, '--title', rep['title'], '--body-file', '-']
    for args in (base + ['--label', LABEL], base):
        try:
            p = run(args, input=body(rep), capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return ''
        if p.returncode == 0:
            return (p.stdout or '').strip().splitlines()[-1] if p.stdout.strip() else NEW_ISSUE
    return ''


def to_sentry(cfg, rep, post_fn=None):
    """Queues the report as Sentry feedback and sends it now, only when sending is on. True when Sentry took it."""
    folder = telemetry.folder_of(config.state_dir(cfg))
    if not telemetry.sending(folder):
        return False
    crash = telemetry.read_json(os.path.join(folder, telemetry.LAST_CRASH), {}).get('event_id') or ''
    telemetry.queue(folder, {'type': 'event', 'event': feedback_event(rep, crash)})
    return telemetry.flush(folder, cfg, post_fn=post_fn, force=True).get('sent', 0) > 0


def send(cfg, rep, run=None, post_fn=None, signed_in=None):
    """Delivers a previewed report: {'how': 'issue'|'link', 'url', 'sentry': bool}. 'link' means the person still has to
    open the URL and choose Submit new issue on GitHub."""
    sentry_ok = to_sentry(cfg, rep, post_fn)
    has_gh = gh_signed_in(run) if signed_in is None else signed_in
    url = create_issue(rep, run) if has_gh else ''
    telemetry.record('feature', 'report.send', how='issue' if url else 'link', sentry=sentry_ok)
    if url:
        return {'how': 'issue', 'url': url, 'sentry': sentry_ok}
    return {'how': 'link', 'url': issue_url(rep), 'sentry': sentry_ok}


def ask(prompt, io):
    try:
        return io.get('input', input)(prompt).strip()
    except EOFError:
        return ''


def cmd(cfg, a, out=sys.stdout, io=None):
    """`tutti report [--what ...] [--expected ...] [--preview] [--yes]`."""
    io = io or {}
    what = a.what or ask('What happened? ', io)
    expected = a.expected if a.expected is not None else ask('What did you expect instead? ', io)
    try:
        rep = gather(cfg, what, expected, io.get('doctor'))
    except ReportError as e:
        print(f'report: {e}', file=sys.stderr)
        return 2
    print(f'{rep["title"]}\n\n{body(rep)}\n', file=out)
    if a.preview:
        print('report: preview only, nothing sent', file=out)
        return 0
    if not a.yes and ask('Send this report to Tutti\'s maintainers? [y/N] ', io).lower() not in ('y', 'yes'):
        print('report: not sent', file=out)
        return 0
    done = send(cfg, rep, run=io.get('run', github.run), post_fn=io.get('post'))
    if done['how'] == 'issue':
        print(f'report: sent as {done["url"]}. Thank you.', file=out)
    else:
        print('report: gh is not signed in here, so the report opens on GitHub for you to send: choose "Submit new issue" '
              f'there. If it does not open, copy this link into a browser:\n{done["url"]}', file=out)
        io.get('open', _open)(done['url'])
    if done['sentry']:
        print('report: also sent to Sentry with the last crash', file=out)
    return 0


def _open(url):
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:   # no browser here: the link is printed above
        pass


def preview_id(what, expected):
    """Binds a send to the text the person previewed: the dashboard sends only what it showed."""
    return hashlib.sha256(f'{what}\0{expected}'.encode()).hexdigest()[:24]


def take_send(clock=time.time):
    """True when another report may go now (at most SENDS_PER_HOUR an hour), and counts it."""
    now = clock()
    with _sends_lock:
        _sends[:] = [t for t in _sends if now - t < 3600]
        if len(_sends) >= SENDS_PER_HOUR:
            return False
        _sends.append(now)
        return True


def api(collector, raw, run=None, post_fn=None, clock=time.time, sub=None):
    """(status, answer) for POST /api/report: {"what", "expected", "send": false} previews and answers its "preview" id;
    "send": true with that id delivers, at most SENDS_PER_HOUR an hour."""
    try:
        data = json.loads(raw or b'{}')
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get('what'), str):
        return 400, {'error': 'send {"what": "...", "expected": "...", "send": false} to preview, "send": true to send'}
    try:
        rep = gather(collector.cfg, data['what'], str(data.get('expected') or ''))
    except ReportError as e:
        return 400, {'error': str(e)}
    pid = preview_id(data['what'], str(data.get('expected') or ''))
    if not data.get('send'):
        return 200, {'title': rep['title'], 'body': body(rep), 'preview': pid}
    if data.get('preview') != pid:
        return 409, {'error': 'the text changed since the preview: choose Preview, read it, then Send'}
    if not take_send(clock):
        return 429, {'error': f'{SENDS_PER_HOUR} reports were sent from this dashboard in the last hour: try again in an hour, '
                              'or run tutti report in a terminal'}
    # the issue is filed under the person's own GitHub connection when they made one (docs/GITHUB.md)
    return 200, send(collector.cfg, rep, run=run or functools.partial(github.run, sub=sub), post_fn=post_fn)
