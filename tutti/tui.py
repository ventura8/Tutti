"""The terminal UI (t201, docs/TUI.md): the dashboard's main views in a terminal, the same data (the dashboard's snapshot).

Pure: `render(view, state, width, height, ...)` returns the screen as plain lines; `key(view, ui, k)` returns what a key
does. tutti/tuicmd.py draws the lines with curses and reads the keys, so every screen is snapshot-tested at any size here.

Best practice the owner asked for: one layout on every screen (title bar, tabs, body, a footer listing that screen's
keys), discoverable help (`?`), arrow and vim keys, readable at 80x24 and resizing cleanly (lines are cut, never wrapped
into a mess), status as an icon and a word (never colour alone, so colourblind- and NO_COLOR-safe), and a plain mode for
screen readers (no box drawing, no icons)."""

VIEWS = ('overview', 'tasks', 'needs', 'hosts', 'agents')
TITLES = {'overview': 'Overview', 'tasks': 'Tasks', 'needs': 'Needs you', 'hosts': 'Hosts', 'agents': 'Agents'}
TASK_TABS = ('running', 'queued', 'needs-person', 'review', 'done')
TASK_TAB_TITLES = {'running': 'Active', 'queued': 'Queued', 'needs-person': 'Needs a person', 'review': 'To review', 'done': 'Done'}
# status -> (icon, word, colour role); the word always shows, so colour is never the only signal
STATUS = {'free': ('+', 'free', 'ok'), 'busy': ('~', 'busy', 'warn'), 'offline': ('x', 'offline', 'bad'),
          'paused': ('=', 'paused', 'idle'), 'ready': ('+', 'ready', 'ok'), 'not ready': ('x', 'not ready', 'bad'),
          'checking': ('?', 'checking', 'idle'), 'running': ('>', 'running', 'ok'), 'queued': ('.', 'queued', 'idle'),
          'needs-person': ('!', 'needs you', 'bad'), 'review': ('*', 'to review', 'warn'), 'done': ('+', 'done', 'ok')}
KEYS = {'all': [('Tab/1-5', 'view'), ('j/k ↑/↓', 'move'), ('?', 'help'), ('r', 'refresh'), ('q', 'quit')],
        'tasks': [('h/l ←/→', 'status')]}
HELP = ['Keys', '', '  Tab, Shift-Tab, 1-5   next view, previous view, a view by number',
        '  j k, Down Up          move in the list', '  g G, Home End         first, last',
        '  h l, Left Right       (Tasks) previous or next status', '  r                     refresh now',
        '  ?                     this help (any key closes it)', '  q, Esc                quit', '',
        'Views: 1 Overview  2 Tasks  3 Needs you  4 Hosts  5 Agents', '',
        'Plain mode (--plain): no icons or lines, for screen readers. NO_COLOR is honoured.']


def cut(text, width):
    """One line cut to `width` columns ('…' when cut); never wraps."""
    text = ' '.join(str(text).split())
    return text if len(text) <= width else (text[:max(0, width - 1)] + '…' if width > 0 else '')


def status(word, plain=False):
    icon, label, _role = STATUS.get(word, ('?', str(word), 'idle'))
    return label if plain else f'{icon} {label}'


def rows_for(view, state, ui):
    """[(text, status word or None)] of the view's list."""
    s = state or {}
    if view == 'overview':
        sm = s.get('summary') or {}
        nxt = (sm.get('next_worker') or {})
        return [(f'Running {sm.get("running", 0)}, stalled {sm.get("stalled", 0)}, free slots {sm.get("free_slots", 0)}', None),
                (f'Next worker: {nxt.get("account") or nxt.get("worker") or "none"}', None),
                (f'Accounts in panic: {", ".join(sm.get("panic") or []) or "none"}', 'needs-person' if sm.get('panic') else None),
                (f'Behind their pace: {", ".join(sm.get("behind") or []) or "none"}', None),
                (f'Hosts offline: {", ".join(sm.get("machines_offline") or []) or "none"}',
                 'offline' if sm.get('machines_offline') else None),
                (f'Queued: {((s.get("queue") or {}).get("header")) or "nothing waits"}', None)]
    if view == 'tasks':
        tab = TASK_TABS[ui.get('tab', 1) % len(TASK_TABS)]
        items = [t for t in (s.get('tasks') or []) if t.get('status') == tab]
        return [(f'{t.get("id")}  {t.get("title", "")}', tab) for t in items] or [(f'No task is {TASK_TAB_TITLES[tab].lower()}.', None)]
    if view == 'needs':
        parked = (s.get('autopilot') or {}).get('parked') or []
        return [(f'{t.get("id")}  {t.get("title", "")}: {t.get("parked") or "parked"}', 'needs-person') for t in parked] \
            or [('Nothing needs you.', None)]
    if view == 'hosts':
        return [(f'{m.get("name")}  {m.get("reason") or ""}', m.get('status')) for m in (s.get('machines') or {}).get('items') or []] \
            or [('No host yet: tutti machines add --host <address>.', None)]
    if view == 'agents':
        return [(f'{a.get("name")}  {a.get("record") or a.get("kind") or ""}', a.get('state')) for a in s.get('agents') or []] \
            or [('No agent configured yet.', None)]
    return []


def render(view, state, width, height, ui=None, plain=False):
    """The screen as `height` lines of at most `width` columns: title, tabs, body (scrolled to the cursor), footer."""
    ui = ui or {}
    width, height = max(width, 20), max(height, 6)
    title = cut(f'Tutti {(state or {}).get("version", "")} · {TITLES[view]}', width)
    tabs = cut('  '.join((f'[{i + 1} {TITLES[v]}]' if v == view else f' {i + 1} {TITLES[v]} ') for i, v in enumerate(VIEWS)), width)
    if ui.get('help'):
        body = [cut(line, width) for line in HELP]
    elif state is None:
        body = [cut('Loading… (the first look at every host takes a few seconds; q quits)', width)]
    else:
        rows = rows_for(view, state, ui)
        cur = min(max(ui.get('cursor', 0), 0), max(len(rows) - 1, 0))
        room = height - 5 if view != 'tasks' else height - 6
        top = min(max(0, cur - room + 1), max(0, len(rows) - room))
        body = []
        if view == 'tasks':
            tab = TASK_TABS[ui.get('tab', 1) % len(TASK_TABS)]
            body.append(cut('  '.join(f'<{TASK_TAB_TITLES[t]}>' if t == tab else TASK_TAB_TITLES[t] for t in TASK_TABS), width))
        for i, (text, word) in enumerate(rows[top:top + room], start=top):
            mark = '>' if i == cur and not plain else ' '
            tag = f'{status(word, plain)}  ' if word else ''
            body.append(cut(f'{mark} {tag}{text}' if not plain else f'{tag}{text}', width))
    keys = [*KEYS['all'], *KEYS.get(view, [])]
    footer = cut('  '.join(f'{k} {what}' for k, what in keys), width)
    rule = '' if plain else '─' * width
    lines = [title, tabs, rule, *body]
    lines = lines[:height - 2] + [''] * max(0, height - 2 - len(lines))
    return [*lines, rule, footer][:height]


MOVES = {'j': 1, 'down': 1, 'k': -1, 'up': -1}
JUMPS = {'g': 0, 'home': 0, 'G': 10 ** 6, 'end': 10 ** 6}


def key(view, ui, k):
    """(view, ui, quit) after key `k` (a name: 'tab', 'btab', 'up', 'down', 'left', 'right', 'home', 'end', or a character)."""
    ui = dict(ui or {})
    if ui.get('help'):
        ui['help'] = False
        return view, ui, False
    if k in ('q', 'esc'):
        return view, ui, True
    if k == '?':
        ui['help'] = True
    elif k in ('tab', 'btab'):
        view, ui['cursor'] = VIEWS[(VIEWS.index(view) + (1 if k == 'tab' else -1)) % len(VIEWS)], 0
    elif len(k) == 1 and k in '12345':
        view, ui['cursor'] = VIEWS[int(k) - 1], 0
    elif k in MOVES:
        ui['cursor'] = max(0, ui.get('cursor', 0) + MOVES[k])
    elif k in JUMPS:
        ui['cursor'] = JUMPS[k]
    elif view == 'tasks' and k in ('l', 'right', 'h', 'left'):
        ui['tab'], ui['cursor'] = (ui.get('tab', 1) + (1 if k in ('l', 'right') else -1)) % len(TASK_TABS), 0
    return view, ui, False


def role_of(line):
    """The colour role of a drawn line: the role of the status icon and word it shows, or None."""
    for icon, label, role in STATUS.values():
        if f'{icon} {label}' in line:
            return role
    return None
