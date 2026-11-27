"""The TUI for Tutti: interactive terminal viewer for machines, needs, dispatch, chat, and setup.

Keyboard-first navigation with arrow and vim keys, 80x24 layout, NO_COLOR and truecolor fallbacks,
colorblind-safe status badges, and screen-reader friendly plain mode.
"""

import os
import re
import sys
import unicodedata

try:
    import curses
except ImportError:
    curses = None


SCREENS = ('machines', 'needs', 'dispatch', 'chat', 'setup')
SCREEN_TITLES = {
    'machines': 'Machines & Devices',
    'needs': 'Needs Attention',
    'dispatch': 'Dispatch & Queue',
    'chat': 'Chat & Agents',
    'setup': 'Setup Wizard',
}

ANSI_16 = {
    'green': '\033[32m',
    'yellow': '\033[33m',
    'red': '\033[31m',
    'cyan': '\033[36m',
    'blue': '\033[34m',
    'dim': '\033[90m',
    'bold': '\033[1m',
    'reverse': '\033[7m',
    'reset': '\033[0m',
}

ANSI_256 = {
    'green': '\033[38;5;35m',
    'yellow': '\033[38;5;220m',
    'red': '\033[38;5;196m',
    'cyan': '\033[38;5;44m',
    'blue': '\033[38;5;39m',
    'dim': '\033[38;5;244m',
    'bold': '\033[1m',
    'reverse': '\033[7m',
    'reset': '\033[0m',
}

ANSI_TRUECOLOR = {
    'green': '\033[38;2;46;204;113m',
    'yellow': '\033[38;2;241;196;15m',
    'red': '\033[38;2;231;76;60m',
    'cyan': '\033[38;2;52;152;219m',
    'blue': '\033[38;2;41;128;185m',
    'dim': '\033[38;2;127;140;141m',
    'bold': '\033[1m',
    'reverse': '\033[7m',
    'reset': '\033[0m',
}

ANSI_PALETTES = {
    '16': ANSI_16,
    '256': ANSI_256,
    'truecolor': ANSI_TRUECOLOR,
}

STATUS_BADGES = {
    'ok': ('✓', 'Ready', 'green'),
    'ready': ('✓', 'Ready', 'green'),
    'done': ('✓', 'Done', 'green'),
    'clear': ('✓', 'Clear', 'green'),
    'passed': ('✓', 'Passed', 'green'),
    'needs': ('!', 'Needs You', 'yellow'),
    'warn': ('!', 'Warning', 'yellow'),
    'yield': ('⏸', 'Yield', 'yellow'),
    'locked': ('🔒', 'Locked', 'yellow'),
    'optional': ('○', 'Optional', 'cyan'),
    'running': ('⟳', 'Running', 'cyan'),
    'queued': ('⏳', 'Queued', 'blue'),
    'error': ('✗', 'Error', 'red'),
    'fail': ('✗', 'Failed', 'red'),
    'idle': ('-', 'Idle', 'dim'),
    'info': ('i', 'Info', 'cyan'),
    'configured': ('i', 'Configured', 'cyan'),
    'dropped': ('-', 'Dropped', 'dim'),
    'skipped': ('-', 'Skipped', 'dim'),
}

ANSI_ESCAPE_RE = re.compile(r'\033\[[0-9;]*[a-zA-Z]')

HELP_LINES = [
    'Tutti TUI — Help & Navigation',
    '',
    'Screens:',
    '  1. Machines & Devices — Configured machines, slots and devices',
    '  2. Needs Attention    — Questions waiting for owner answers',
    '  3. Dispatch & Queue   — Backlog tasks and their status',
    '  4. Chat & Agents      — Local conversation previews',
    '  5. Setup Wizard       — Results of the guided setup',
    '',
    'Keybindings:',
    '  ↑ / k, ↓ / j     Navigate items up / down',
    '  ← / h, → / l     Previous / next screen',
    '  Tab / Shift-Tab  Cycle through screens',
    '  1 - 5            Directly switch to screen 1-5',
    '  Enter / Space    Open or close item details',
    '  ?                Close this help screen',
    '  Up / Down, j/k   Scroll help and details',
    '  Esc              Go back or quit from the list',
    '  q                Quit from any view',
    '',
    'Accessibility & Options:',
    '  --plain          Screen-reader friendly line-by-line output',
    '  NO_COLOR=1       Disable palette colors (text labels stay visible)',
    '  Status badges    Colorblind-safe [icon text] format across all screens',
]


def _term_color_mode(term):
    if '256color' in term:
        return '256'
    return 'none' if term in ('', 'unknown') else '16'


def detect_color_mode(env=None):
    """Detects color capability: none (NO_COLOR/dumb), 16, 256, or truecolor."""
    env = os.environ if env is None else env
    term = env.get('TERM', '').lower()
    if env.get('NO_COLOR') or term == 'dumb':
        return 'none'
    if env.get('COLORTERM', '').lower() in ('truecolor', '24bit'):
        return 'truecolor'
    return _term_color_mode(term)


def colorize(text, color, mode='none'):
    """Applies ANSI styling according to color mode (none, 16, 256, truecolor)."""
    if mode == 'none' or not color:
        return text
    pal = ANSI_PALETTES.get(mode)
    if not pal or color not in pal:
        return text
    return f'{pal[color]}{text}{pal["reset"]}'


def format_status(status_key, label=None, color_mode='none'):
    """Colorblind-safe badge: always [icon text], styled with ANSI if supported."""
    icon, default_lbl, color = STATUS_BADGES.get(status_key, ('?', display_text(status_key or 'unknown'), 'dim'))
    text_label = label or default_lbl
    badge = f'[{icon} {text_label}]'
    return colorize(badge, color, color_mode)


def strip_ansi(text):
    return ANSI_ESCAPE_RE.sub('', text)


def cell_width(char):
    if unicodedata.category(char) in ('Mn', 'Me', 'Cf'):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1


def visible_len(text):
    return sum(map(cell_width, strip_ansi(text)))


def split_cells(text, width):
    for idx, char in enumerate(text):
        width -= cell_width(char)
        if width < 0:
            return text[:idx], text[idx:]
    return text, ''


def clamp_line(text, width):
    vlen = visible_len(text)
    if vlen <= width:
        return text + (' ' * (width - vlen))
    ending = '...' if width >= 3 else ''
    prefix, _ = split_cells(strip_ansi(text), max(0, width - len(ending)))
    return prefix + ending + ' ' * (width - visible_len(prefix) - len(ending))


def display_text(value):
    return ''.join(char if char.isprintable() else ' ' for char in str(value))


def _format_tab(idx, sid, active_screen, compact, color_mode):
    lbl = str(idx) if compact else f'{idx}:{sid.capitalize()}'
    if sid != active_screen:
        return lbl
    return colorize(f'[{lbl}]', 'bold', color_mode)


def render_header(active_screen, width, color_mode='none'):
    compact = width < 70
    tabs = [_format_tab(i, sid, active_screen, compact, color_mode) for i, sid in enumerate(SCREENS, 1)]
    tab_strip = ' '.join(tabs)
    if compact:
        return clamp_line(f' {active_screen.capitalize()} │ {tab_strip}', width)
    line = f' Tutti TUI │ Screen: {tab_strip} │ [?] Help'
    return clamp_line(line, width)


def render_divider(width):
    return '─' * width


def render_footer(width, color_mode='none', overlay=False):
    if overlay:
        keys = '[j/k] Scroll  [Esc] Back'
    elif width < 70:
        keys = '[?] Help [1-5] Tabs j/k Enter q'
    else:
        keys = '[Tab/1-5] Screens  [↑/↓ j/k] Navigate  [Enter] View  [?] Help  [q] Quit'
    return clamp_line(f' {keys}', width)


def wrapped_rows(lines, width):
    rows = []
    for line in lines:
        rest = display_text(line)
        while visible_len(rest) > width:
            part, rest = split_cells(rest, width)
            rows.append(part or '?')
            if not part:
                rest = rest[1:]
        rows.append(rest)
    return rows


def render_overlay(state, lines, width, height):
    rows = wrapped_rows(lines, width)
    state.offset = max(0, min(state.offset, len(rows) - height))
    visible = rows[state.offset:state.offset + height]
    return [clamp_line(line, width) for line in visible] + [' ' * width] * (height - len(visible))


def format_item_summary(it, is_sel, is_exp, width, color_mode='none'):
    prefix = '> ' if is_sel else '  '
    mark = '[-] ' if is_exp else '[+] '
    badge = format_status(it.get('status', 'ok'), color_mode=color_mode)
    title = display_text(it.get('title', ''))
    detail = display_text(it.get('detail', ''))
    suffix = f' — {detail}' if (detail and width > 60) else ''
    content = f'{prefix}{mark}{badge} {title}{suffix}'
    return clamp_line(content, width)


def item_details(it):
    lines = [display_text(it.get('title', '')), f'ID: {it.get("id", "none")}']
    if it.get('options'):
        lines.append(f'Options: {", ".join(map(str, it["options"]))}')
    if it.get('detail'):
        lines.append(f'Detail: {it["detail"]}')
    return [display_text(line) for line in lines]


def render_item_list(items, selected, expanded, width, height, color_mode='none'):
    if not items:
        msg = clamp_line('  No items on this screen. Press [?] for help.', width)
        return [msg] + [' ' * width] * (height - 1)
    top = max(0, min(selected - height // 2, len(items) - height))
    rows = [format_item_summary(it, idx == selected, str(it.get('id', '')) in expanded, width, color_mode)
            for idx, it in enumerate(items[top:top + height], top)]
    return rows + [' ' * width] * (height - len(rows))


def overlay_lines(state):
    if state.help_open:
        return HELP_LINES
    if state.expanded:
        return item_details(state.get_items()[state.selected])
    return None


def render_body(state, width, height, color_mode='none'):
    overlay = overlay_lines(state)
    if overlay is not None:
        return render_overlay(state, overlay, width, height)
    items = state.get_items() if not state.loading and not state.error else []
    if state.error:
        err = clamp_line(f'  {format_status("error", "Error", color_mode)} {display_text(state.error)}', width)
        return [err] + [' ' * width] * (height - 1)
    if state.loading:
        load = clamp_line(f'  {format_status("running", "Loading...", color_mode)} Please wait', width)
        return [load] + [' ' * width] * (height - 1)
    return render_item_list(items, state.selected, state.expanded, width, height, color_mode)


def render_screen(state, width=80, height=24, color_mode='none'):
    """Render to the current terminal dimensions."""
    footer = render_footer(width, color_mode, overlay=state.help_open or bool(state.expanded))
    if height < 5 or width < 1:
        return [footer]
    body = render_body(state, width, height - 4, color_mode)
    divider = render_divider(width)
    return [render_header(state.screen, width, color_mode), divider, *body, divider, footer]


def _plain_items(state, items):
    if not items:
        return ['No items available.']
    out = []
    for idx, it in enumerate(items, 1):
        sel = ' [FOCUSED]' if (idx - 1) == state.selected else ''
        status = it.get('status', 'ok')
        _, label, _ = STATUS_BADGES.get(status, ('?', display_text(status), 'dim'))
        title = display_text(it.get('title', ''))
        out.append(f'Item {idx} of {len(items)}: {label}. {title}{sel}')
        out.append(f'    Detail: {display_text(it.get("detail", "none"))}')
    return out


def render_plain(state):
    """Screen-reader friendly, accessible representation."""
    title = SCREEN_TITLES.get(state.screen, state.screen)
    out = [
        f'=== Tutti TUI — {title} ===',
        f'Active screen: {state.screen} (1: machines, 2: needs, 3: dispatch, 4: chat, 5: setup)',
        'Keys: 1-5=Screens, up/down or j/k=Navigate, enter=Inspect, ?=Help, escape=Back, q=Quit',
        '',
    ]
    overlay = overlay_lines(state)
    if overlay is not None:
        return out + list(overlay)
    if state.loading:
        return out + ['LOADING: Loading data...']
    items = state.get_items() if not state.error else []
    if state.error:
        return out + [f'ERROR: {display_text(state.error)}']
    return out + _plain_items(state, items)


def _machine_item(name, m):
    return {
        'id': name,
        'title': f'Host: {name} ({m.get("host", "local")})',
        'status': 'configured',
        'detail': f'slots: {m.get("slots", 1)}, host: {m.get("host", "local")}',
    }


def _device_item(name, d):
    return {
        'id': name,
        'title': f'Device: {name} ({d.get("serial", "usb")})',
        'status': 'configured',
        'detail': f'model: {d.get("model", "android")}',
    }


def load_machines(cfg=None):
    cfg = cfg or {}
    machs = [_machine_item(name, m) for name, m in (cfg.get('machines') or {}).items()]
    devs = [_device_item(name, d) for name, d in (cfg.get('devices') or {}).items()]
    return machs + devs


def load_needs(cfg=None):
    from . import mcpserver, needs

    questions = needs.open_for(cfg, mcpserver.role_of(cfg), needs.OWNER) if cfg else []
    items = []
    for q in questions:
        items.append(
            {
                'id': q.get('id', 'q'),
                'title': q.get('question', 'Input needed'),
                'status': 'needs',
                'detail': q.get('why', 'waiting for answer'),
                'options': q.get('options', []),
            }
        )
    if not items:
        items = [{'id': 'n0', 'title': 'No pending questions', 'status': 'clear', 'detail': 'No open questions for this session'}]
    return items


def _dispatch_item(t):
    status = t.get('status') or 'queued'
    return {
        'id': t.get('id', 't'),
        'title': f'[{t.get("id")}] {t.get("title", "Task")}',
        'status': {'needs-person': 'needs', 'failed': 'fail'}.get(status, status),
        'detail': f'repo: {t.get("repo", "tutti")} · worker: {t.get("worker", "auto")}',
    }


def load_dispatch(cfg=None):
    from . import backlog

    tasks = backlog.load(cfg).get('tasks', []) if cfg else []
    items = [_dispatch_item(t) for t in tasks]
    if not items:
        return [{'id': 'd0', 'title': 'Dispatch queue is idle', 'status': 'idle', 'detail': 'No backlog tasks'}]
    return items


def load_chat(cfg=None):
    from . import chat, config

    rows = chat.Store(config.state_dir(cfg)).listing('local') if cfg else []
    return [{**row, 'status': 'info', 'detail': row['preview']} for row in rows]


def load_setup(cfg=None):
    from . import setupcmd

    report = setupcmd.read_report(cfg) if cfg else None
    if not report:
        return [{'id': 'setup', 'title': 'Setup report unavailable', 'status': 'info',
                 'detail': 'Run tutti setup to configure Tutti and create a report.'}]
    statuses = {'ok': 'done', 'todo': 'needs', 'info': 'info', 'error': 'error', 'skip': 'skipped'}
    return [{**row, 'status': statuses.get(row['state'], row['state'])} for row in report.get('items', [])]


LOADERS = {
    'machines': load_machines,
    'needs': load_needs,
    'dispatch': load_dispatch,
    'chat': load_chat,
    'setup': load_setup,
}


class TuiState:
    """TUI state machine: current screen, selected item, expanded set, and help."""

    def __init__(self, cfg=None, screen='machines', color_mode='none'):
        self.cfg = cfg
        self.screen = screen if screen in SCREENS else 'machines'
        self.selected = 0
        self.expanded = set()
        self.help_open = False
        self.offset = 0
        self.loading = False
        self.error = None
        self.color_mode = color_mode
        self._cache = {}

    def get_items(self):
        if self.screen not in self._cache:
            try:
                self._cache[self.screen] = LOADERS[self.screen](self.cfg)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.error = f'Could not load this screen ({type(exc).__name__}). Check the config and reopen Tutti.'
                return []
        return self._cache[self.screen]

    def set_screen(self, screen):
        if screen in SCREENS:
            self.screen = screen
            self.selected = 0
            self.expanded.clear()
            self.help_open = False
            self.error = None
            self.offset = 0

    def next_screen(self):
        idx = (SCREENS.index(self.screen) + 1) % len(SCREENS)
        self.set_screen(SCREENS[idx])

    def prev_screen(self):
        idx = (SCREENS.index(self.screen) - 1) % len(SCREENS)
        self.set_screen(SCREENS[idx])

    def toggle_expanded(self):
        items = self.get_items()
        if not items or self.selected >= len(items):
            return
        it_id = str(items[self.selected].get('id', ''))
        if it_id in self.expanded:
            self.expanded.remove(it_id)
        else:
            self.expanded.add(it_id)

    def _move(self, delta):
        items = self.get_items()
        self.selected = max(0, min(max(0, len(items) - 1), self.selected + delta))

    def _overlay_key(self, key):
        if key in ('up', 'k', 'down', 'j'):
            self.offset = max(0, self.offset + (-1 if key in ('up', 'k') else 1))
        if key in ('escape', 'enter', 'space'):
            if self.help_open:
                self.help_open = False
            else:
                self.expanded.clear()
            self.offset = 0

    def on_key(self, key):
        """Dispatch a normalized key."""
        if key in ('q', 'Q'):
            return 'quit'
        if key == '?':
            self.help_open = not self.help_open
            self.offset = 0
        elif self.help_open or self.expanded:
            self._overlay_key(key)
        elif key == 'escape':
            return 'quit'
        else:
            self._handle_nav(key)
        return None

    def _handle_nav(self, key):
        if key in ('1', '2', '3', '4', '5'):
            self.set_screen(SCREENS[int(key) - 1])
            return True
        nav = {
            'up': lambda: self._move(-1),
            'k': lambda: self._move(-1),
            'down': lambda: self._move(1),
            'j': lambda: self._move(1),
            'left': self.prev_screen,
            'h': self.prev_screen,
            'btab': self.prev_screen,
            'right': self.next_screen,
            'l': self.next_screen,
            'tab': self.next_screen,
            'enter': self.toggle_expanded,
            'space': self.toggle_expanded,
            '\n': self.toggle_expanded,
            '\r': self.toggle_expanded,
        }
        fn = nav.get(key)
        if fn is None:
            return False
        fn()
        return True


PLAIN_KEYS = {'\033[A': 'up', '\033[B': 'down', '\033[C': 'right', '\033[D': 'left'}


def _read_plain_line(read):
    try:
        return read('tutti> ').strip()
    except (EOFError, KeyboardInterrupt):
        return 'quit'


def run_plain(cfg=None, screen='machines', color_mode='none', read=input, out=None):
    """Accessible plain-text REPL for screen readers and dumb terminals."""
    state = TuiState(cfg=cfg, screen=screen, color_mode=color_mode)
    out = sys.stdout if out is None else out
    print('\n'.join(render_plain(state)), file=out)
    while True:
        line = _read_plain_line(read)
        key = {'': 'enter', 'quit': 'q', 'exit': 'q'}.get(line.lower(), line.lower())
        key = PLAIN_KEYS.get(line, key)
        if state.on_key(key) == 'quit':
            break
        print('\n'.join(render_plain(state)), file=out)
    return 0


CURSES_KEY_MAP = {
    curses.KEY_UP: 'up',
    ord('k'): 'k',
    curses.KEY_DOWN: 'down',
    ord('j'): 'j',
    curses.KEY_LEFT: 'left',
    ord('h'): 'h',
    curses.KEY_RIGHT: 'right',
    ord('l'): 'l',
    9: 'tab',
    curses.KEY_BTAB: 'btab',
    10: 'enter',
    13: 'enter',
    curses.KEY_ENTER: 'enter',
    ord(' '): 'space',
    ord('?'): '?',
    27: 'escape',
    ord('q'): 'q',
    ord('Q'): 'q',
} if curses is not None else {}


CURSES_COLORS = {
    'green': (2, 35, 0x2ecc71),
    'yellow': (3, 220, 0xf1c40f),
    'red': (1, 196, 0xe74c3c),
    'cyan': (6, 44, 0x3498db),
    'blue': (4, 39, 0x2980b9),
    'dim': (7, 244, 0x7f8c8d),
}
COLOR_TIERS = ((1 << 24, 2), (256, 1), (8, 0))


def _curses_palette(mode, tier):
    minimum, index = tier
    if curses.COLORS < minimum:
        return None
    attrs = {'bold': curses.A_BOLD, 'reset': 0}
    for pair, (key, colors) in enumerate(CURSES_COLORS.items(), 1):
        curses.init_pair(pair, colors[index], -1)
        attrs[key] = curses.color_pair(pair)
    return {ANSI_PALETTES[mode][key]: attr for key, attr in attrs.items()}


def curses_styles(mode):
    if mode == 'none' or not curses.has_colors():
        return {}
    try:
        curses.start_color()
        curses.use_default_colors()
    except curses.error:
        return {}
    return _best_curses_palette(mode)


def _best_curses_palette(mode):
    fallback_start = {'truecolor': 0, '256': 1, '16': 2}
    for tier in COLOR_TIERS[fallback_start[mode]:]:
        try:
            styles = _curses_palette(mode, tier)
            if styles:
                return styles
        except curses.error:
            continue
    return {}


def _draw_line(stdscr, y, line, styles):
    attr = 0
    x = 0
    for chunk in re.split(f'({ANSI_ESCAPE_RE.pattern})', line.rstrip()):
        if ANSI_ESCAPE_RE.fullmatch(chunk):
            attr = styles.get(chunk, 0)
        elif chunk:
            stdscr.addstr(y, x, chunk, attr)
            x += visible_len(chunk)


def draw_curses_screen(stdscr, lines, height, styles):
    stdscr.erase()
    for y, line in enumerate(lines[:height]):
        try:
            _draw_line(stdscr, y, line, styles)
        except curses.error:
            pass  # ncurses reports writing the bottom-right cell even when it succeeds.
    stdscr.refresh()


def _curses_key(ch):
    if ch in CURSES_KEY_MAP:
        return CURSES_KEY_MAP[ch]
    if 0 <= ch <= 255 and chr(ch).isprintable():
        return chr(ch)
    return None


def _curses_getch(stdscr):
    try:
        return stdscr.getch()
    except curses.error:
        return -1


def curses_loop(stdscr, state):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)
    styles = curses_styles(state.color_mode)
    while True:
        height, width = stdscr.getmaxyx()
        mode = state.color_mode if styles else 'none'
        lines = render_screen(state, width, height, color_mode=mode)
        draw_curses_screen(stdscr, lines, height, styles)
        key = _curses_key(_curses_getch(stdscr))
        if key and state.on_key(key) == 'quit':
            return 0


def run_curses(cfg=None, screen='machines', color_mode='none'):
    """Interactive full-screen curses loop, with a plain fallback."""
    if curses is not None and os.environ.get('TERM') != 'dumb':
        state = TuiState(cfg=cfg, screen=screen, color_mode=color_mode)
        try:
            return curses.wrapper(curses_loop, state)
        except curses.error:
            pass
    return run_plain(cfg=cfg, screen=screen, color_mode=color_mode)
