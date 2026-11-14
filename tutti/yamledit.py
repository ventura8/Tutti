"""Changes one key of a YAML config file in place, keeping every comment, blank line and the order of the keys around it.

Standard library and PyYAML only (no round-trip YAML library): a careful patcher for the YAML a config file is written in,
block mappings with plain or flow (`{...}`, `[...]`) values, a flow mapping spread over several lines included. Only the
value of the key that changes is rewritten; a key that is not in the file is added at the end of its parent; a reset
removes the key, so its default applies again.

Every edit is checked: the new text is parsed and must equal the old data with exactly that one change. An edit the
patcher cannot make safely raises EditError and the file is left as it was."""
import copy

import yaml

DELETE = object()
NOT_A_KEY = '#{[]},&*!|>%@`\r\n'
WIDTH = 10 ** 6


class EditError(Exception):
    """The patcher could not change the key without touching more than that one value; the message says where to edit."""


def flow(value):
    """`value` as a one-line YAML flow value (a scalar, `[...]` or `{...}`)."""
    text = yaml.safe_dump([value], default_flow_style=True, width=WIDTH, allow_unicode=True, sort_keys=False).strip()
    return text[1:-1].strip()


def block(value, indent):
    """`value` (a non-empty mapping or list) as block YAML lines, each indented by `indent` spaces."""
    text = yaml.safe_dump(value, default_flow_style=False, width=WIDTH, allow_unicode=True, sort_keys=False)
    return ''.join(' ' * indent + line + '\n' for line in text.splitlines())


def is_blank(line):
    s = line.strip()
    return not s or s.startswith('#')


def indent_of(line):
    return len(line) - len(line.lstrip(' '))


def skip_quoted(text, i):
    """The index after the quoted scalar that starts at text[i]."""
    q = text[i]
    i += 1
    while i < len(text):
        if q == '"' and text[i] == '\\':
            i += 2
            continue
        if text[i] == q:
            if q == "'" and text[i + 1:i + 2] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    raise EditError('a quoted value is not closed')


def key_of_line(line):
    """(key, the index after its colon) of a block `key: ...` line, or None (a list item, a comment, a bare value)."""
    i = len(line) - len(line.lstrip(' '))
    if i >= len(line) or line[i] in NOT_A_KEY or line[i:i + 2].rstrip() == '-' or line.startswith('- ', i):
        return None
    if line[i] in '"\'':
        try:
            j = skip_quoted(line, i)
        except EditError:
            return None
        key = line[i + 1:j - 1]
    else:
        j = i
        while j < len(line) and not (line[j] == ':' and line[j + 1:j + 2] in ('', ' ', '\t', '\n', '\r')):
            j += 1
        key = line[i:j].rstrip()
    while j < len(line) and line[j] in ' \t':
        j += 1
    if j >= len(line) or line[j] != ':' or line[j + 1:j + 2] not in ('', ' ', '\t', '\n', '\r') or ' #' in line[i:j]:
        return None
    return key, j + 1


def flow_key_at(text, i):
    """(key, the index after its colon) of the flow mapping entry that starts at text[i]."""
    if text[i] in '"\'':
        j = skip_quoted(text, i)
        key = text[i + 1:j - 1]
    else:
        j = i
        while j < len(text) and text[j] not in ':,{}[]\n':
            j += 1
        key = text[i:j].rstrip()
    while j < len(text) and text[j] in ' \t':
        j += 1
    if j >= len(text) or text[j] != ':' or not key:
        raise EditError('a flow mapping entry without "key: value"')
    return key, j + 1


def is_comment_at(text, i):
    return text[i] == '#' and (i == 0 or text[i - 1] in ' \t\n')


def skip_space(text, i, end):
    """The index of the next character in text[i:end] that is not a space, a newline or part of a comment."""
    while i < end:
        if text[i] in ' \t\r\n':
            i += 1
        elif is_comment_at(text, i):
            i = text.find('\n', i)
            i = end if i < 0 else i
        else:
            return i
    return end


def skip_inert(text, i):
    """The index after a quoted scalar or a comment that starts at text[i]; i itself when neither does."""
    if text[i] in '"\'' and (i == 0 or text[i - 1] in ' \t\n{[,:'):
        return skip_quoted(text, i)
    if is_comment_at(text, i):
        end = text.find('\n', i)
        return len(text) if end < 0 else end
    return i


def flow_end(text, i):
    """The index after the flow collection ({...} or [...]) that opens at text[i]."""
    depth = 0
    while i < len(text):
        nxt = skip_inert(text, i)
        if nxt != i:
            i = nxt
            continue
        c = text[i]
        if c in '{[':
            depth += 1
        elif c in '}]':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise EditError('a { or [ is not closed')


def scalar_end(text, i, stops):
    """The end of the plain or quoted scalar at text[i] (trailing spaces left out)."""
    if text[i] in '"\'':
        return skip_quoted(text, i)
    j = i
    while j < len(text) and text[j] not in stops and not is_comment_at(text, j):
        j += 1
    while j > i and text[j - 1] in ' \t':
        j -= 1
    return j


def value_end(text, i, stops):
    if text[i] in '{[':
        return flow_end(text, i)
    if text[i] in '|>':
        raise EditError('a block scalar (| or >) inside a flow value')
    return scalar_end(text, i, stops)


def flow_entries(text, start, end):
    """[(key, entry start, value start, value end)] of the flow mapping text[start:end] ('{' ... '}')."""
    out, i = [], start + 1
    while True:
        i = skip_space(text, i, end - 1)
        if i >= end - 1:
            return out
        key, colon = flow_key_at(text, i)
        v = skip_space(text, colon, end - 1)
        ve = v if text[v] in ',}' else value_end(text, v, ',}\n')
        out.append((key, i, v, ve))
        i = skip_space(text, ve, end - 1)
        if i < end - 1 and text[i] == ',':
            i += 1


class Source:
    """The file's text with its line starts, so a block key's line and a character offset map onto each other."""

    def __init__(self, text):
        self.text = text
        self.lines = text.splitlines(keepends=True)
        self.starts = [0]
        for line in self.lines:
            self.starts.append(self.starts[-1] + len(line))

    def line_end(self, n):
        """The offset of line n's newline (or the end of the text)."""
        line = self.lines[n]
        return self.starts[n] + len(line.rstrip('\r\n'))

    def line_of(self, offset):
        lo, hi = 0, len(self.lines) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo


def block_children(t, lo, hi):
    """(indent, [line numbers]) of the keys directly in the block lines lo..hi, or (None, []) when it has none."""
    meaningful = [n for n in range(lo, hi) if not is_blank(t.lines[n])]
    if not meaningful:
        return None, []
    ind = indent_of(t.lines[meaningful[0]])
    return ind, [n for n in meaningful if indent_of(t.lines[n]) == ind]


def block_extent(t, n, hi):
    """The line after the last line that belongs to the key on line n (its children), trailing comments left out."""
    ind, last = indent_of(t.lines[n]), n
    for k in range(n + 1, hi):
        line = t.lines[k]
        if is_blank(line):
            continue
        if indent_of(line) <= ind and not (indent_of(line) == ind and line.lstrip(' ').startswith('- ')):
            break
        last = k
    return last + 1


class Found:
    """A key that is in the file: where its value starts and ends, and whether it sits in a block or in a flow mapping."""

    def __init__(self, kind, **kw):
        self.kind = kind
        self.__dict__.update(kw)


def block_key(t, lo, hi, key):
    """Found for `key` among the block lines lo..hi, or None."""
    _, rows = block_children(t, lo, hi)
    for n in rows:
        found = key_of_line(t.lines[n])
        if found and found[0] == key:
            return n, found[1]
    return None


def block_found(t, n, after_colon, hi):
    """The value of the block key on line n: inline (scalar, flow, block scalar) or its child lines."""
    start = t.starts[n] + after_colon
    v = skip_space(t.text, start, t.line_end(n))
    end = block_extent(t, n, hi)
    if v >= t.line_end(n):   # nothing inline: the value is the child lines (or null)
        return Found('block', line=n, indent=indent_of(t.lines[n]), inline=False, start=start, end=t.starts[end] if end > n + 1 else
                     t.line_end(n), child_lo=n + 1, child_hi=end, key_end=start)
    if t.text[v] in '|>':
        return Found('block', line=n, indent=indent_of(t.lines[n]), inline=False, start=start,
                     end=t.starts[end] if end > n + 1 else t.line_end(n), child_lo=n + 1, child_hi=end, key_end=start, scalar=True)
    ve = value_end(t.text, v, '\n')
    return Found('block', line=n, indent=indent_of(t.lines[n]), inline=True, start=v, end=ve, flow=t.text[v] if t.text[v] in '{[' else '',
                 key_end=start, last_line=end)


def find_in(t, container, key):
    """Found for `key` directly in a block or flow container, or None."""
    if container[0] == 'block':
        row = block_key(t, container[1], container[2], key)
        return block_found(t, row[0], row[1], container[2]) if row else None
    for k, es, vs, ve in flow_entries(t.text, container[1], container[2]):
        if k == key:
            return Found('flow', entry=es, start=vs, end=ve, map_start=container[1], map_end=container[2])
    return None


def locate(t, path):
    """(Found, parent) for the key at `path`, or (None, (parent container, the path left)) when it is not in the file."""
    container = ('block', 0, len(t.lines), -1)
    for depth, key in enumerate(path):
        hit = find_in(t, container, key)
        if hit is None:
            return None, (container, list(path[depth:]))
        if depth == len(path) - 1:
            return hit, container
        container = descend(t, hit, path[:depth + 1])
    raise EditError('an empty path')


def descend(t, hit, where):
    if hit.kind == 'block' and not hit.inline and not getattr(hit, 'scalar', False):
        return ('block', hit.child_lo, hit.child_hi, hit.indent)
    if t.text[hit.start] == '{':
        return ('flow', hit.start, hit.end, None)
    raise EditError(f'{".".join(where)} is not a mapping in the file, so a key cannot go under it')


def nest(keys, value):
    for k in reversed(keys):
        value = {k: value}
    return value


def entry_lines(key, value, indent):
    if isinstance(value, (dict, list)) and value:
        return f'{" " * indent}{key}:\n' + block(value, indent + 2)
    return f'{" " * indent}{key}: {flow(value)}\n'


def replace_found(t, hit, value):
    text = t.text
    if hit.kind == 'flow' or hit.inline:
        return text[:hit.start] + flow(value) + text[hit.end:]
    # the key's own line keeps its comment; the child lines (or a block scalar) are replaced
    rest = text[hit.key_end:t.line_end(hit.line)].strip()
    note = '  ' + rest if rest.startswith('#') else ''
    has_children = hit.child_hi > hit.line + 1
    tail = text[hit.end:] if has_children else text[hit.end + 1:]
    head = text[:hit.key_end]
    if isinstance(value, (dict, list)) and value:   # block children replaced by block children
        return head + note + '\n' + block(value, hit.indent + 2) + tail
    return head + ' ' + flow(value) + note + '\n' + tail


def insert_flow(text, container, key, val):
    """`key: val` added at the end of a flow mapping."""
    close = container[2] - 1
    inner = text[container[1] + 1:close]
    before = text[:close].rstrip(' \t')
    sep = ', ' if inner.strip() and not before.endswith(',') else ''
    return before + sep + f'{key}: {flow(val)}' + text[close:]


def insert_missing(t, container, rest, value):
    key, val = rest[0], nest(rest[1:], value)
    text = t.text
    if container[0] == 'flow':
        return insert_flow(text, container, key, val)
    _, lo, hi, parent_indent = container
    ind, rows = block_children(t, lo, hi)
    if parent_indent < 0:   # a new top-level key goes at the end of the file
        return text + ('' if not text or text.endswith('\n') else '\n') + entry_lines(key, val, 0)
    if rows:   # after the parent's last key (its trailing comments stay below)
        at_line, ind = block_extent(t, rows[-1], hi), ind
    else:      # the parent's first key
        at_line, ind = lo, parent_indent + 2
    at = t.starts[at_line] if at_line < len(t.lines) else len(text)
    prefix = '' if not text or text[at - 1:at] == '\n' else '\n'
    return text[:at] + prefix + entry_lines(key, val, ind) + text[at:]


def delete_found(t, hit):
    text = t.text
    if hit.kind == 'block':
        last = hit.last_line if hit.inline else max(hit.child_hi, hit.line + 1)
        end = t.starts[last] if last < len(t.lines) else len(text)
        return text[:t.starts[hit.line]] + text[end:]
    entries = flow_entries(text, hit.map_start, hit.map_end)
    idx = [e[1] for e in entries].index(hit.entry)
    if idx + 1 < len(entries):   # "key: value, " up to the next entry
        return text[:hit.entry] + text[entries[idx + 1][1]:]
    if idx > 0:                  # the last entry: from the end of the one before it
        return text[:entries[idx - 1][3]] + text[hit.end:].lstrip(' \t')
    return text[:hit.map_start + 1] + text[hit.map_end - 1:]


def changed(data, path, value):
    """The parsed data with the one change: the value set at `path` (its parents created), or the key removed."""
    out = copy.deepcopy(data) if isinstance(data, dict) else {}
    node = out
    for k in path[:-1]:
        nxt = node.get(k)
        if not isinstance(nxt, dict) and value is DELETE:
            node = {}   # nothing to remove: a throwaway parent
            continue
        if not isinstance(nxt, dict):
            node[k] = {}
        node = node[k]
    if value is DELETE:
        node.pop(path[-1], None)
    else:
        node[path[-1]] = copy.deepcopy(value)
    return out


def emptied(parent, t):
    return parent is not None and parent.kind == 'block' and not parent.inline and not getattr(parent, 'scalar', False) and \
        not block_children(t, parent.child_lo, parent.child_hi)[1]


def delete(t, hit, path):
    new = delete_found(t, hit)
    if len(path) < 2:
        return new
    nt = Source(new)   # a block parent left without keys becomes {} (an empty mapping, not null)
    parent, _ = locate(nt, path[:-1])
    return replace_found(nt, parent, {}) if emptied(parent, nt) else new


def apply(text, path, value):
    t = Source(text)
    hit, where = locate(t, path)
    if value is DELETE:
        return text if hit is None else delete(t, hit, path)
    if hit is None:
        return insert_missing(t, where[0], where[1], value)
    return replace_found(t, hit, value)


def edit(text, path, value):
    """The text with the key at `path` set to `value`, or removed when value is DELETE; EditError when that cannot be done
    without changing anything else (the parsed result is compared with the expected data)."""
    path = list(path)
    if not path or not all(isinstance(k, str) and k for k in path):
        raise EditError('the key path must be a list of names')
    before = yaml.safe_load(text) if text.strip() else {}
    if before is not None and not isinstance(before, dict):
        raise EditError('the file is not a mapping at the top level')
    try:
        new = apply(text, path, value)
        after = yaml.safe_load(new) if new.strip() else {}
    except (EditError, yaml.YAMLError, ValueError, IndexError) as e:
        raise EditError(f'{".".join(path)} could not be changed in place ({e}): edit it in the file by hand') from e
    if (after or {}) != changed(before or {}, path, value):
        raise EditError(f'{".".join(path)} could not be changed in place without touching other keys: edit it in the file by hand')
    return new
