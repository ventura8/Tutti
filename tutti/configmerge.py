"""Adds entries to a tutti.config.yaml as text, so every comment and the owner's layout stay: an entry goes at the end
of its top-level section, a missing section at the end of the file. Only what is absent is added (idempotent)."""
import yaml

HEADER = ('# tutti.config.yaml, written by `tutti setup` (docs/INSTALL.md). Edit freely: `tutti setup` only adds what is\n'
          '# missing and keeps your comments. Everything personal lives here; never put a token, password or key in it.\n')


def flow(value):
    """One line of YAML for a value (dumped inside a list: a bare scalar would get a `...` document end)."""
    return yaml.safe_dump([value], default_flow_style=True, width=10 ** 6, sort_keys=False).strip()[1:-1]


def key_text(key):
    return flow(key) if not str(key).replace('-', '').replace('_', '').isalnum() else str(key)


def section_span(lines, section):
    """(start, end) line indexes of a top-level `section:` block, end exclusive; None when absent."""
    start = next((i for i, line in enumerate(lines) if line.split('#')[0].rstrip() in (f'{section}:', f'{section}: {{}}')), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in ' \t' or lines[end].startswith('#')):
        end += 1
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith('#')):
        end -= 1   # trailing blanks and the next section's comments stay with the next section
    return start, end


def add(text, section, entries, why=None):
    """`text` with each entry of {name: value} added under `section` (created when absent); the caller (plan) passes only
    names that are not there yet. None when the section is a non-empty flow mapping (`machines: {a: ...}`): text cannot be
    added inside it safely, so the caller reports it."""
    why = why or {}
    if not entries:
        return text
    lines = text.splitlines()
    new = [f'  {key_text(k)}: {flow(v)}' + (f'   # {why[k]}' if why.get(k) else '') for k, v in entries.items()]
    span = section_span(lines, section)
    if span is None:
        if any(line.startswith(f'{section}:') for line in lines):
            return None   # `section: {...}` inline: the caller reports it, the owner moves it to a block once
        tail = [''] if lines and lines[-1].strip() else []
        return '\n'.join(lines + tail + [f'{section}:'] + new) + '\n'
    start, end = span
    _, hash_, comment = lines[start].partition('#')
    lines[start] = f'{section}:' + (f'  #{comment}' if hash_ else '')   # `section: {}` becomes a block, its comment kept
    return '\n'.join(lines[:end] + new + lines[end:]) + '\n'


def set_top(text, key, value, why=''):
    """`text` with a top-level `key: value` appended when the key is absent."""
    if any(line.startswith(f'{key}:') for line in text.splitlines()):
        return text
    if not text or text.endswith('\n\n'):
        sep = ''
    elif text.endswith('\n'):
        sep = '\n'
    else:
        sep = '\n\n'
    return text + sep + f'{key}: {flow(value)}' + (f'   # {why}' if why else '') + '\n'
