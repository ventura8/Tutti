"""Sending telemetry: Sentry's envelope API over plain HTTPS (no SDK), or a JSON POST to `telemetry.endpoint`.

A DSN `https://<public key>@<host>[/<path>]/<project id>` gives the envelope URL `https://<host>[/<path>]/api/<project id>/envelope/`
and the `X-Sentry-Auth` header. Crash and failure events go one per envelope; usage events go in batches of BATCH, as the
breadcrumbs of one `info` event (Sentry keeps at most 100 breadcrumbs an event). Every item was scrubbed when it was
recorded (telemetry.py, scrub.py); nothing here adds a host name, a path or an IP address."""
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import __version__

BATCH = 100
DROP = (400, 413, 422)   # the item itself was refused: dropped. Any other failure keeps the queue
TIMEOUT = 5
LOOPBACK = ('127.0.0.1', 'localhost', '::1')


class DsnError(ValueError):
    """A DSN Tutti cannot use; the message says which part is wrong and where to copy a good one."""


def parse_dsn(dsn):
    """(envelope URL, public key) of a DSN; raises DsnError."""
    where = 'copy it from Sentry: Settings, Projects, <your project>, Client Keys (DSN)'
    u = urlsplit((dsn or '').strip())
    if u.scheme not in ('https', 'http') or not u.hostname:
        raise DsnError(f'the DSN must start with https:// and name a host: {where}')
    if u.scheme == 'http' and u.hostname not in LOOPBACK:
        raise DsnError(f'the DSN must use https:// (plain http only on 127.0.0.1, for tests): {where}')
    if not u.username:
        raise DsnError(f'the DSN has no public key before the @: {where}')
    path, _, project = u.path.rstrip('/').rpartition('/')
    if not project.isdigit():
        raise DsnError(f'the DSN does not end with the project id (a number): {where}')
    port = f':{u.port}' if u.port else ''
    return f'{u.scheme}://{u.hostname}{port}{path}/api/{project}/envelope/', u.username


def host_of(dsn):
    """The Sentry host a DSN sends to (shown on the dashboard); never the key."""
    try:
        return urlsplit(dsn).hostname or '' if dsn else ''
    except ValueError:
        return ''


def target(cfg=None, env=None):
    """{'kind': 'sentry', 'url', 'key', 'dsn'} or {'kind': 'endpoint', 'url'}, or None when neither is set or valid."""
    from . import telemetry
    value, _ = telemetry.dsn(cfg, env)
    if value:
        try:
            url, key = parse_dsn(value)
            return {'kind': 'sentry', 'url': url, 'key': key, 'dsn': value}
        except DsnError:
            return None
    endpoint = ((cfg or {}).get('telemetry') or {}).get('endpoint') or ''
    return {'kind': 'endpoint', 'url': endpoint} if endpoint else None


def iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).isoformat(timespec='milliseconds')


def envelope(event, dsn):
    """One event as an envelope: the header line, the item header, the payload."""
    payload = json.dumps(event, separators=(',', ':')).encode()
    head = {'event_id': event['event_id'], 'sent_at': iso(), 'dsn': dsn, 'sdk': {'name': 'tutti', 'version': __version__}}
    item = {'type': 'event', 'length': len(payload), 'content_type': 'application/json'}
    return b'\n'.join([json.dumps(head).encode(), json.dumps(item).encode(), payload]) + b'\n'


def usage_event(events, install_id):
    """A batch of usage events as one `info` event: each a breadcrumb (category = kind, message = name, data = fields)."""
    from . import telemetry
    ev = telemetry.base_event('info', {'where': 'usage', 'batch': len(events)})
    ev['message'] = {'formatted': f'tutti usage: {len(events)} event(s)'}
    ev['fingerprint'] = ['tutti-usage-batch']
    ev['user'] = {'id': install_id}
    crumbs = []
    for e in events:
        data = {k: v for k, v in e.items() if k not in ('v', 'ts', 'kind', 'name')}
        try:
            ts = datetime.fromisoformat(e['ts']).timestamp()
        except (KeyError, TypeError, ValueError):
            ts = time.time()
        crumbs.append({'timestamp': ts, 'category': e.get('kind'), 'message': e.get('name'), 'data': data, 'level': 'info',
                       'type': 'default'})
    ev['breadcrumbs'] = {'values': crumbs}
    return ev


def post(url, body, headers, timeout=TIMEOUT):
    """POSTs and returns the HTTP status (0 when the network failed). Redirects are not followed."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        e.close()
        return e.code
    except (OSError, ValueError):   # URLError is an OSError
        return 0


def auth_header(key):
    return f'Sentry sentry_version=7, sentry_key={key}, sentry_client=tutti/{__version__}'


def send_sentry(t, payloads, post_fn):
    """Sends each payload (an event); returns how many Sentry accepted (2xx) and the ones to keep (a 429, a 5xx, offline)."""
    sent, left = 0, []
    headers = {'Content-Type': 'application/x-sentry-envelope', 'X-Sentry-Auth': auth_header(t['key']),
               'User-Agent': f'tutti/{__version__}'}
    for i, (_item, ev) in enumerate(payloads):
        code = post_fn(t['url'], envelope(ev, t['dsn']), headers)
        if 200 <= code < 300:
            sent += 1
        elif code not in DROP:
            # offline, throttled, a server error, or a wrong or revoked DSN (401, 403, 404): keep the rest for later
            raise_keep = [it for it, _ in payloads[i:]]
            if code in (401, 403, 404):
                raise DsnError(f'Sentry answered {code}: the DSN is wrong or its key was revoked; run `tutti telemetry setup` '
                               f'with the DSN from the project\'s Client Keys ({len(raise_keep)} item(s) kept)')
            return sent, left + raise_keep
        # 400, 413, 422: Sentry refused this item itself; sending it again would be refused again, so it is dropped
    return sent, left


def send_all(t, items, install_id, post_fn=None):
    """Sends queued items to the target. Returns (sent count, items to keep)."""
    post_fn = post_fn or post
    events = [it for it in items if it.get('type') == 'event' and isinstance(it.get('event'), dict)]
    usage = [it for it in items if it.get('type') == 'usage' and isinstance(it.get('event'), dict)]
    if t['kind'] == 'endpoint':
        return send_endpoint(t, events + usage, install_id, post_fn)
    payloads = [(it, it['event']) for it in events]
    for i in range(0, len(usage), BATCH):
        chunk = usage[i:i + BATCH]
        payloads.append((chunk, usage_event([c['event'] for c in chunk], install_id)))
    sent, left = send_sentry(t, payloads, post_fn)
    flat = []
    for it in left:
        flat += it if isinstance(it, list) else [it]
    return sent, flat


def send_endpoint(t, items, install_id, post_fn):
    """The configurable endpoint: POST {install_id, release, items: [...]} in batches of BATCH."""
    sent = 0
    for i in range(0, len(items), BATCH):
        chunk = items[i:i + BATCH]
        body = json.dumps({'install_id': install_id, 'release': f'tutti@{__version__}', 'id': secrets.token_hex(8),
                           'items': chunk}).encode()
        code = post_fn(t['url'], body, {'Content-Type': 'application/json', 'User-Agent': f'tutti/{__version__}'})
        if not 200 <= code < 300:
            return sent, items[i:]
        sent += len(chunk)
    return sent, []


def endpoint_errors(value):
    """Config check for telemetry.endpoint: https (http only on 127.0.0.1)."""
    if not value:
        return []
    u = urlsplit(str(value))
    if u.scheme == 'https' and u.hostname:
        return []
    if u.scheme == 'http' and u.hostname in LOOPBACK:
        return []
    return [f'telemetry.endpoint is {os.path.basename(str(value))!r}...: an https:// URL (plain http only on 127.0.0.1)']
