"""docs/API.md, kept true to the code: its generated parts (the CLI, the MCP tools, the config keys) come from the code itself,
and every CLI subcommand, HTTP route and MCP tool the code has must appear in it (tests/test_apidoc.py).

`python3 -m tutti.apidoc --write` rewrites the generated parts between their markers; without `--write` (or with `--check`) it
only checks, and exits 1 naming what is stale or missing. For the HTTP routes only their presence is checked (each must open a
row of a table): their methods, roles and JSON shapes are written by hand. The hand-written parts stay as they are."""
import argparse
import os
import re
import sys

from . import authweb, cli, config, dashboard, mcpserver, settings_schema

DOC = os.path.join(config.ROOT, 'docs', 'API.md')
ROUTE_FILES = ('dashboard.py', 'authweb.py', 'coordinator.py', 'mcpserver.py')
# Ollama's own API, which the fleet calls on other machines: not a route of Tutti's
FOREIGN = {'/api/generate', '/api/ps', '/api/pull', '/api/tags', '/api/'}
ROUTE_RE = re.compile(r"'(/(?:api|hh)/[A-Za-z0-9/_-]+|/mcp)'")


def cell(text):
    """Text safe inside a Markdown table cell: one line, pipes escaped."""
    return ' '.join(str(text).split()).replace('|', '\\|')


def shown(value):
    if value is None or value == '' or value == [] or value == {}:
        return ''
    return f'`{value}`'


# ---- the CLI

def subparsers_of(p):
    return next((a for a in p._actions if isinstance(a, argparse._SubParsersAction)), None)


def option_rows(p):
    rows = []
    for a in p._actions:
        if isinstance(a, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        name = ', '.join(a.option_strings) if a.option_strings else a.dest
        if a.choices and not a.option_strings:
            name += ' {' + ','.join(str(c) for c in a.choices) + '}'
        switch = isinstance(a, (argparse._StoreTrueAction, argparse._StoreFalseAction))
        default = '' if switch or a.default is argparse.SUPPRESS else shown(a.default)
        rows.append(f'| `{cell(name)}` | {cell(a.help or "")} | {default} |')
    return rows


def cli_command(name, p, depth=3):
    out = [f'{"#" * depth} `tutti {name}`', '', cell(p.description or ''), '', '```text', p.format_usage().strip(), '```', '']
    rows = option_rows(p)
    if rows:
        out += ['| Argument | What it does | Default |', '| --- | --- | --- |', *rows, '']
    sp = subparsers_of(p)
    if sp:
        for sub, sp_p in sp.choices.items():
            out += cli_command(f'{name} {sub}', sp_p, min(depth + 1, 6))
    return out


def cli_section():
    old = os.environ.get('COLUMNS')
    os.environ['COLUMNS'] = '120'   # argparse wraps its usage line to the terminal: the same width everywhere
    try:
        return [line for name, sp in subparsers_of(cli.parser()).choices.items() for line in cli_command(name, sp)]
    finally:
        if old is None:
            del os.environ['COLUMNS']
        else:
            os.environ['COLUMNS'] = old


def cli_names():
    return list(subparsers_of(cli.parser()).choices)


# ---- the MCP tools

def param_row(k, v, req):
    kind = v.get('type', '') + (' of ' + v['items']['type'] if v.get('type') == 'array' else '')
    enum = f'; one of: {", ".join(v["enum"])}.' if v.get('enum') else ''
    return f'| `{k}` | {kind} | {"yes" if k in req else "no"} | {cell(v.get("description", ""))}{enum} |'


def need_of(t):
    if t['name'] == 'away_mode':
        return 'admin role (also for the status)'
    return 'any role (read-only)' if t['annotations']['readOnlyHint'] else 'admin role (changes something)'


def mcp_tool(t):
    out = [f'### `{t["name"]}`', '', cell(t['description']), '', f'Needs: {need_of(t)}.', '']
    props, req = t['inputSchema']['properties'], t['inputSchema'].get('required', [])
    if not props:
        return out + ['No parameters.', '']
    rows = [param_row(k, v, req) for k, v in props.items()]
    return out + ['| Parameter | Type | Required | Meaning |', '| --- | --- | --- | --- |', *rows, '']


def mcp_section():
    return [line for t in mcpserver.TOOLS for line in mcp_tool(t)]


# ---- the config keys

def default_of(path, f):
    if 'default' in f:
        return f['default']
    parts = path.split('.')
    if '*' in parts:
        head = parts[0]
        base = settings_schema.ENTRY_DEFAULTS.get(head)
        if base is None or len(parts) != 3:
            return None
        return base.get(parts[2])
    node = config.DEFAULTS
    for k in parts:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def kind_of(f):
    unit = f' ({f["unit"]})' if f.get('unit') else ''
    choices = f' {", ".join(map(str, f["choices"]))}' if f.get('choices') else ''
    return cell(f'{f.get("control") or "text"}{unit}{choices}')


def config_section():
    out = ['| Key | Type | Default | Meaning | Who changes it |', '| --- | --- | --- | --- | --- |']
    for path, f in settings_schema.FIELDS.items():
        restart = ' Applies after a restart.' if path in settings_schema.RESTART else ''
        out.append(f'| `{path}` | {kind_of(f)} | {cell(shown(default_of(path, f)))} | '
                   f'{cell(f["label"] + ": " + f["help"])}{restart} | {who_of(path)} |')
    for path, value in default_leaves(config.DEFAULTS):
        if path not in settings_schema.FIELDS:
            out.append(f'| `{path}` | {cell(type(value).__name__)} | {cell(shown(value))} | (no Settings line: see '
                       f'tutti.config.example.yaml) | {who_of(path)} |')
    return out


def who_of(path):
    return 'each user (own value) or an admin' if path.split('.')[0] in settings_schema.MANAGED or path == 'main.agent' else 'admin'


def default_leaves(node, prefix=''):
    """[(dotted path, default)] of every key in config.DEFAULTS whose value is not a mapping of further keys; a mapping
    of named entries (machines, workers: empty by default) is one row."""
    out = []
    for k, v in node.items():
        path = f'{prefix}{k}'
        if isinstance(v, dict) and v and path not in settings_schema.ENTRIES:
            out += default_leaves(v, path + '.')
        else:
            out.append((path, v))
    return out


# ---- the HTTP routes

def http_routes():
    """Every path Tutti's own HTTP servers answer (the dashboard, its sign-in, the household coordinator, MCP over HTTP)."""
    found = set(dashboard.POSTS) | set(dashboard.SELF_POSTS) | set(dashboard.ADMIN_PATHS) | set(authweb.AUTH_POSTS)
    found |= set(authweb.PUBLIC_GET) | set(authweb.LOGIN_PAGES) | set(authweb.ADMIN_GET) | set(authweb.VIEWER_POST)
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ROUTE_FILES:
        with open(os.path.join(here, name), encoding='utf8') as f:
            found |= set(ROUTE_RE.findall(f.read()))
    return sorted(found - FOREIGN)


# ---- the document

SECTIONS = {'cli': cli_section, 'mcp': mcp_section, 'config': config_section}


def markers(name):
    return f'<!-- generated:{name}:start (python3 -m tutti.apidoc --write) -->', f'<!-- generated:{name}:end -->'


def render(text):
    """`text` with every generated part made anew."""
    for name, fn in SECTIONS.items():
        start, end = markers(name)
        if start not in text or end not in text:
            raise ValueError(f'docs/API.md lacks the {name} markers: put {start} and {end} back')
        head, rest = text.split(start, 1)
        _, tail = rest.split(end, 1)
        text = head + start + '\n\n' + '\n'.join(fn()).rstrip() + '\n\n' + end + tail
    return text


def missing(text):
    """[what the code has that docs/API.md does not name]."""
    out = [f'CLI subcommand `tutti {n}`' for n in cli_names() if f'`tutti {n}`' not in text]
    out += [f'HTTP route `{r}`' for r in http_routes() if not re.search(r'^\|[^|\n]*`' + re.escape(r) + '`', text, re.M)]
    out += [f'MCP tool `{t["name"]}`' for t in mcpserver.TOOLS if f'`{t["name"]}`' not in text]
    return out


def problems(path=DOC):
    with open(path, encoding='utf8') as f:
        text = f.read()
    out = missing(text)
    if render(text) != text:
        out.append('a generated part is stale: run `python3 -m tutti.apidoc --write`')
    return out


def main(argv=None, path=DOC):
    a = argparse.ArgumentParser(prog='python3 -m tutti.apidoc', description=__doc__.splitlines()[0])
    a.add_argument('--write', action='store_true', help='rewrite the generated parts of docs/API.md')
    a.add_argument('--check', action='store_true', help='only check (the default): exit 1 naming what is stale or missing')
    args = a.parse_args(argv)
    if args.write:
        with open(path, encoding='utf8') as f:
            text = render(f.read())
        with open(path, 'w', encoding='utf8') as f:
            f.write(text)
    found = problems(path)
    for p in found:
        sys.stderr.write(f'docs/API.md: {p}\n')
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
