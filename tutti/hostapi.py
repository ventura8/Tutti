"""The hosts API (t406, docs/API.md, *Hosts*): "machine" becomes "host". New names, used by the dashboard:

- GET  /api/hosts            every host with its live state (the snapshot's machines, under their new name)
- POST /api/host-limit       {"host": ..., "window": ..., "pct": ...}   was /api/machine-limit {"machine": ...}
- POST /api/user/main-host   {"host": ...}                             was /api/user/main-machine {"machine": ...}

The old routes keep working, deprecated: they answer as before plus `Deprecation: true` and a `Link` to the new route
(RFC 8594 / 9745). A new route is translated to its old handler before the sign-in checks, so who may call it is exactly
who could call the old one. Pure: everything here takes and returns plain values."""
import json

API_HOSTS = '/api/hosts'
ALIASES = {'/api/host-limit': '/api/machine-limit', '/api/user/main-host': '/api/user/main-machine'}
DEPRECATED = {old: new for new, old in ALIASES.items()}


def route(path):
    """(the path the handlers know, whether the body names a host): a new route maps to its old handler."""
    return (ALIASES[path], True) if path in ALIASES else (path, False)


def body(raw):
    """A new route's JSON body with "host" also sent as "machine" (what the handler reads); anything else unchanged."""
    try:
        data = json.loads(raw or b'null')
    except ValueError:
        return raw
    if isinstance(data, dict) and 'host' in data and 'machine' not in data:
        data['machine'] = data['host']
        return json.dumps(data).encode()
    return raw


def deprecation(path):
    """The headers a deprecated route adds to its answer ({} for every other path)."""
    new = DEPRECATED.get(path)
    return {'Deprecation': 'true', 'Link': f'<{new}>; rel="successor-version"'} if new else {}


def hosts(snapshot):
    """GET /api/hosts: {"hosts": [...]} from the state snapshot's machines; each item as the machines list has it."""
    items = ((snapshot or {}).get('machines') or {}).get('items') or []
    return {'hosts': [dict(m) for m in items if isinstance(m, dict)]}
