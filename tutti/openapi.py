"""The dashboard's OpenAPI 3.1 document (Swagger), made from docs/API.md's route tables, so the two cannot drift
(docs/API.md, *OpenAPI*). Served at GET /api/openapi.json for any Swagger UI or client generator, shown by the dashboard's
own /api-docs.html, and kept as docs/openapi.json (`python3 -m tutti.apidoc --write`).

Each table row under "## The dashboard HTTP API" becomes an operation: its route (a `` `/api/x/` + id `` route gets
`{id}`), its method (the Method column, else POST in a Changing table), who may call it, its answer text and, when the Body
cell starts with a JSON example, that example. A deprecated route (tutti/hostapi.py) is marked deprecated with its successor.
Pure: text in, a dict out."""
import json
import re

from . import __version__, hostapi

SECTION = '## The dashboard HTTP API'
CODE = re.compile(r'`([^`]+)`')


def _cells(line):
    """A table row's cells; an escaped \\| inside a cell (`"18-01"\\|"off"`) stays in it, as one |."""
    inner = line.strip()[1:-1] if line.strip().endswith('|') else line.strip()[1:]
    return [c.strip().replace('\\|', '|') for c in re.split(r'(?<!\\)\|', inner)]


def _plain(text):
    return re.sub(r'\s+', ' ', CODE.sub(r'\1', text)).strip()


def _path(cell):
    m = CODE.search(cell)
    if not m or not m.group(1).startswith('/'):
        return None
    path = m.group(1)
    if '+' in cell[m.end():]:   # `/api/task/` + task id
        path = path.rstrip('/') + '/{id}'
    return path


def _example(cell):
    m = CODE.search(cell)
    if not m:
        return None
    try:
        value = json.loads(m.group(1))
    except ValueError:
        return None
    return value if isinstance(value, (dict, list)) else None


def operations(text):
    """[(path, method, op)] from the dashboard section's tables of docs/API.md."""
    if SECTION not in text:
        return []
    body = text.split(SECTION, 1)[1].split('\n## ', 1)[0]
    out, tag, header = [], 'Dashboard', None
    for line in body.splitlines():
        if line.startswith('### '):
            tag, header = line[4:].strip(), None
            continue
        if not line.startswith('|'):
            header = None
            continue
        cells = _cells(line)
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if set(line.replace('|', '').strip()) <= {'-', ' '}:
            continue
        row = dict(zip(header, cells, strict=False))
        made = _operation(row, tag)
        if made:
            out.append(made)
    return out


def _operation(row, tag):
    """(path, method, op) of one table row, or None for a row that names no route."""
    path = _path(row.get('route', ''))
    if not path:
        return None
    method = (row.get('method') or ('POST' if 'body' in row else 'GET')).split()[0].upper()
    who = _plain(row.get('who') or ('admin' if 'admin' in tag.lower() else ''))
    answer = _plain(row.get('answer') or row.get('answer and errors') or '')
    op = {'tags': [tag], 'summary': answer[:120], 'description': answer, 'x-who': who,
          'responses': {'200': {'description': 'OK (JSON)'}, '401': {'description': 'not signed in'},
                        '403': {'description': 'not allowed for this role'}}}
    if '{id}' in path:
        op['parameters'] = [{'name': 'id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}]
    example = _example(row.get('body') or '')
    if method == 'POST':
        op['requestBody'] = {'required': True, 'content': {'application/json': {
            'schema': {'type': 'object'}, **({'example': example} if example is not None else {})}}}
    successor = hostapi.DEPRECATED.get(path)
    if successor:
        op['deprecated'] = True
        op['description'] += f' Deprecated: use {successor}.'
    return path, method.lower(), op


def document(text, version='see GET /api/health'):
    """The OpenAPI 3.1 document (the committed copy carries no version, so a release does not make it stale)."""
    paths = {}
    for path, method, op in operations(text):
        paths.setdefault(path, {})[method] = op
    return {'openapi': '3.1.0',
            'info': {'title': 'Tutti dashboard API', 'version': version,
                     'description': 'Made from docs/API.md. Sign in at the dashboard first; a POST also sends the page\'s '
                                    'X-Tutti-Token (docs/API.md, Requests and answers).'},
            'servers': [{'url': '/'}], 'paths': dict(sorted(paths.items()))}


def from_doc(path, version=None):
    with open(path, encoding='utf8') as f:
        return document(f.read(), version or __version__)
