"""The shared event hook (docs/INTEGRATIONS.md, *Events*): what Tutti already surfaces, sent to Slack and Discord channels.

Any feature calls `emit(cfg, kind, title, link)`: it only appends to an outbox in <state>/events.json (no network, safe
inside another lock); the dashboard's round `deliver`s it. Kinds: needs-person, attention (the Needs attention tab),
run-finished, run-failed, release, security. `scan_backlog` derives needs-person, run-finished, run-failed and security
from the backlog's status changes, so no feature has to remember to call it.

A message carries a kind, a title (scrubbed of credentials and home paths, 120 characters) and a link to the dashboard:
never a question's text, a log or a value. The same event (kind + key) is sent once per `dedupe_hours`; a channel gets at
most `max_per_hour`, the rest are counted and summed up in its next message. Off by default per channel; the webhook URLs
are secrets, read at the point of use from ~/.config/tutti/integrations.env."""
import json
import os
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from urllib.parse import urlparse

from . import config, durable, scrub, secretenv

FILE = 'events.json'
KINDS = ('needs-person', 'attention', 'run-finished', 'run-failed', 'release', 'security')
CHANNELS = ('slack', 'discord')
ENV = {'slack': 'TUTTI_SLACK_WEBHOOK_URL', 'discord': 'TUTTI_DISCORD_WEBHOOK_URL'}
HOSTS = {'slack': ('hooks.slack.com',), 'discord': ('discord.com', 'discordapp.com')}
PATHS = {'slack': '/services/', 'discord': '/api/webhooks/'}
DEFAULTS = {'enabled': False, 'events': list(KINDS), 'max_per_hour': 12, 'dedupe_hours': 24}
TAB = {'needs-person': 'needs', 'attention': 'attention', 'security': 'needs'}
LABEL = {'needs-person': 'Needs you', 'attention': 'Needs attention', 'run-finished': 'Finished', 'run-failed': 'Failed',
         'release': 'Released', 'security': 'Security'}
MAX_OUTBOX = 200


def settings(cfg, channel):
    s = ((cfg.get('integrations') or {}).get(channel) or {})
    return {**DEFAULTS, 'webhook_env': ENV[channel], **(s if isinstance(s, dict) else {})}


def dashboard_url(cfg):
    return str((cfg.get('integrations') or {}).get('dashboard_url') or '').rstrip('/')


def check_url(channel, url):
    """None when `url` is a Slack/Discord incoming webhook address, else what is wrong (never the URL itself)."""
    u = urlparse(url.strip())
    if u.scheme != 'https' or (u.hostname or '') not in HOSTS[channel] or not u.path.startswith(PATHS[channel]):
        return (f'it is not a {channel.title()} incoming webhook address (https://{HOSTS[channel][0]}{PATHS[channel]}...): '
                f'copy it again from {channel.title()} (docs/INTEGRATIONS.md)')
    return None


def errors(cfg):
    i = cfg.get('integrations')
    if i is None:
        return []
    if not isinstance(i, dict):
        return [f'integrations is {i!r}: a mapping with linear, slack and discord (docs/INTEGRATIONS.md)']
    errs = []
    url = dashboard_url(cfg)
    if url and not url.startswith(('https://', 'http://')):
        errs.append(f'integrations.dashboard_url is {url!r}: the address you open the dashboard at, starting with https://')
    for ch in CHANNELS:
        raw = i.get(ch)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            errs.append(f'integrations.{ch} is {raw!r}: a mapping such as {{enabled: true}}')
            continue
        s = settings(cfg, ch)
        bad = [k for k in s['events'] or [] if k not in KINDS] if isinstance(s['events'], list) else [s['events']]
        if bad:
            errs.append(f'integrations.{ch}.events has {", ".join(map(str, bad))}: use any of {", ".join(KINDS)}')
        for k in ('max_per_hour', 'dedupe_hours'):
            if not isinstance(s[k], (int, float)) or s[k] < 0:
                errs.append(f'integrations.{ch}.{k} is {s[k]!r}: a number, 0 or more')
    return errs


def path_of(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


def load(cfg):
    try:
        with open(path_of(cfg), encoding='utf8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d = d if isinstance(d, dict) else {}
    for k, v in (('outbox', []), ('sent', {}), ('seen', None), ('channels', {})):
        d.setdefault(k, v)
    return d


@contextmanager
def locked(cfg):
    from . import needs   # the same lock pattern (a thread lock and a file lock)
    p = path_of(cfg)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with needs._lock, open(p + '.lock', 'w', encoding='utf8') as lock:
        if needs.fcntl:
            needs.fcntl.flock(lock, needs.fcntl.LOCK_EX)
        d = load(cfg)
        yield d
        durable.dump_json(p, d, indent=1)


def clean_title(title):
    return scrub.text(scrub.secrets_only(title), limit=120, quotes=False)


def emit(cfg, kind, title, link='', key=None, now=None):
    """Queues one event for the channels (the hook other features call). Returns False when the kind is unknown."""
    if kind not in KINDS:
        return False
    ev = {'kind': kind, 'title': clean_title(title), 'link': str(link or ''), 'key': str(key or title)[:200], 'at': now or time.time()}
    with locked(cfg) as d:
        d['outbox'] = (d['outbox'] + [ev])[-MAX_OUTBOX:]
    return True


def backlog_events(prev, tasks):
    """[(kind, title, key)] from the change of each task's status since `prev` ({id: status}); pure."""
    out = []
    for t in tasks:
        was, now = prev.get(t['id']), t.get('status')
        if was == now:
            continue
        title, key = f'{t["id"]} {t.get("title", "")}', f'{t["id"]}:{now}'
        if was is None and str(t.get('source', '')).startswith(('depbots', 'security')):
            out.append(('security', title, key))
        elif now == 'needs-person':
            out.append(('needs-person', title, key))
        elif now in ('review', 'done') and was == 'running':
            out.append(('run-finished', title, key))
        elif now == 'queued' and was == 'running' and any(not a.get('ok', True) for a in (t.get('attempts') or [])[-1:]):
            out.append(('run-failed', title, key))
    return out


def scan_backlog(cfg, now=None):
    """Emits the events of the backlog's changes since the last scan (the first scan only remembers)."""
    from . import backlog
    tasks = backlog.load(cfg)['tasks']
    with locked(cfg) as d:
        prev = d['seen']
        d['seen'] = {t['id']: t.get('status') for t in tasks}
    if prev is None:
        return []
    found = backlog_events(prev, tasks)
    for kind, title, key in found:
        emit(cfg, kind, title, key=key, now=now)
    return found


def text_of(cfg, ev):
    link = ev['link'] or (f'{dashboard_url(cfg)}/#{TAB.get(ev["kind"], "")}' if dashboard_url(cfg) else '')
    return f'Tutti - {LABEL[ev["kind"]]}: {ev["title"]}', link


def payload(channel, message):
    """The webhook body: the text escaped for the channel, the link as a link (no preview), nobody pinged."""
    text, link = message
    if channel == 'slack':
        safe = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        return {'text': safe + (f' <{link}|open>' if link else ''), 'unfurl_links': False, 'unfurl_media': False}
    return {'content': (text[:1800] + (f' <{link}>' if link else '')), 'allowed_mentions': {'parse': []}}


def urlopen_post(url, body, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'tutti'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:   # checked: Slack's or Discord's webhook host
            return res.status
    except urllib.error.HTTPError as e:
        return e.code


def send(channel, url, message, post=None):
    """Posts one (text, link) message; None when it went, else why (never the URL)."""
    bad = check_url(channel, url)
    if bad:
        return f'{ENV[channel]}: {bad}'
    try:
        status = (post or urlopen_post)(url, payload(channel, message))
    except OSError as e:
        return f'{channel.title()} could not be reached ({type(e).__name__}); the next round tries again'
    if status in (200, 204):
        return None
    if status in (401, 403, 404):
        return f'{channel.title()} refused the webhook (HTTP {status}): it was deleted or revoked; run `tutti integrations {channel} setup`'
    return f'{channel.title()} answered HTTP {status}; the next round tries again'


def plan(d, cfg, channel, now):
    """(texts to send, events consumed) for one channel; dedupe and the hourly cap; pure apart from `d`."""
    s = settings(cfg, channel)
    ch = d['channels'].setdefault(channel, {'times': [], 'dropped': 0, 'cursor': 0})
    ch['times'] = [t for t in ch['times'] if now - t < 3600]
    sent = d['sent'].setdefault(channel, {})
    for k in [k for k, t in sent.items() if now - t > float(s['dedupe_hours']) * 3600]:
        del sent[k]
    texts = []
    for ev in d['outbox']:
        if ev['kind'] not in (s['events'] or []):
            continue
        k = f'{ev["kind"]}:{ev["key"]}'
        if k in sent:
            continue
        sent[k] = now
        if len(ch['times']) + len(texts) >= int(s['max_per_hour']):
            ch['dropped'] += 1
            continue
        texts.append(text_of(cfg, ev))
    if texts and ch['dropped']:
        texts[-1] = (texts[-1][0] + f' (+{ch["dropped"]} more not sent this hour: see the dashboard)', texts[-1][1])
        ch['dropped'] = 0
    return texts


def deliver(cfg, now=None, post=None, env=None):
    """Sends the outbox to every enabled channel; returns {channel: [errors]}. The outbox empties once every channel saw it."""
    now = now or time.time()
    out = {}
    with locked(cfg) as d:
        todo = {}
        for ch in CHANNELS:
            if settings(cfg, ch)['enabled']:
                todo[ch] = plan(d, cfg, ch, now)
        d['outbox'] = []
    for ch, texts in todo.items():
        url = secretenv.get(settings(cfg, ch)['webhook_env'], env).strip()
        errs = []
        for text in texts:
            why = send(ch, url, text, post) if url else f'{settings(cfg, ch)["webhook_env"]} is not set: run `tutti integrations {ch} setup`'
            if why:
                errs.append(why)
                break
        with locked(cfg) as d:
            c = d['channels'].setdefault(ch, {'times': [], 'dropped': 0})
            c['times'] = c.get('times', []) + [now] * (len(texts) - len(errs))
            c.update(last=now, error=errs[0] if errs else None)
        out[ch] = errs
    return out


def tick(cfg, now=None, post=None):
    if not any(settings(cfg, ch)['enabled'] for ch in CHANNELS):
        return {}
    scan_backlog(cfg, now)
    return deliver(cfg, now, post)
