"""`tutti tui [--view <name>] [--plain]` (t201, docs/TUI.md): the terminal UI. Interactive with curses; `--plain` (or no
terminal) prints one view as plain text for screen readers and scripts. Colour follows NO_COLOR and the terminal's own
colours (16, 256 or truecolor); status is always an icon and a word, never colour alone."""
import os
import sys
import time

from . import tui

REFRESH_S = 15
CURSES_KEYS = {9: 'tab', 353: 'btab', 258: 'down', 259: 'up', 260: 'left', 261: 'right', 262: 'home', 360: 'end', 27: 'esc'}


FRESH_S = 300


def load(cfg, now=None):
    """The state every view reads plus the backlog's tasks: the running dashboard's last round when it is fresh (at once),
    else a snapshot made here (one probe round of every host: slow on a big network)."""
    import json

    from . import backlog, config, dashboard
    path = os.path.join(config.state_dir(cfg), dashboard.Collector.SNAPSHOT_FILE)
    snap = None
    try:
        if (now or time.time()) - os.path.getmtime(path) < FRESH_S:
            with open(path, encoding='utf8') as f:
                snap = json.load(f)
    except (OSError, ValueError):
        snap = None
    if not isinstance(snap, dict):
        collector = dashboard.Collector(cfg)
        collector.refresh()
        snap = collector.snapshot()
    snap['tasks'] = [{'id': t.get('id'), 'title': t.get('title'), 'status': t.get('status')}
                     for t in backlog.load(cfg)['tasks'] if t.get('status') in tui.TASK_TABS]
    return snap


def colours_on(env=None):
    env = os.environ if env is None else env
    return 'NO_COLOR' not in env and env.get('TERM', '') not in ('', 'dumb')


ROLES = {'ok': 1, 'warn': 2, 'bad': 3}


def start_colours(curses):
    if not (colours_on() and curses.has_colors()):
        return False
    curses.start_color()
    curses.use_default_colors()
    for i, c in enumerate((curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED), start=1):
        curses.init_pair(i, c, -1)
    return True


def draw(curses, stdscr, view, state, ui, colour):
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    for y, line in enumerate(tui.render(view, state, w - 1, h, ui)):
        role = tui.role_of(line) if colour and y > 2 else None
        attr = curses.color_pair(ROLES[role]) if role in ROLES else 0
        stdscr.addnstr(y, 0, line, w - 1, attr | (curses.A_BOLD if y == 0 else 0))
    stdscr.refresh()


class Loader:
    """Loads the state in the background so the screen draws and keys answer at once (a slow host never freezes it)."""

    def __init__(self, cfg, load_fn):
        import threading
        self.cfg, self.load_fn, self.state, self.busy, self.at, self.error = cfg, load_fn, None, False, 0.0, ''
        self.threading = threading

    def poke(self, force=False):
        if self.busy or (not force and self.state is not None and time.monotonic() - self.at < REFRESH_S):
            return
        self.busy = True

        def work():
            try:
                self.state, self.error = self.load_fn(self.cfg), ''
            except Exception as e:   # a failed load keeps the last good state, says why, and waits REFRESH_S to retry
                self.error = f'{type(e).__name__}: {e}'
                if self.state is None:
                    self.state = {'load_error': self.error}
            finally:
                self.busy, self.at = False, time.monotonic()
        self.threading.Thread(target=work, daemon=True).start()


def run(stdscr, cfg, view, load_fn=load):
    import curses
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(500)
    colour = start_colours(curses)
    loader, ui = Loader(cfg, load_fn), {}
    while True:
        loader.poke()
        state = loader.state if not loader.error or loader.state is None else {**loader.state, 'load_error': loader.error}
        ui['rows'] = len(tui.rows_for(view, state, ui)) if state else 0
        draw(curses, stdscr, view, state, ui, colour)
        c = stdscr.getch()
        if c in (-1, curses.KEY_RESIZE):
            continue   # a resize just draws again at the new size
        k = CURSES_KEYS.get(c, chr(c) if 0 < c < 256 else '')
        if k == 'r':
            loader.poke(force=True)
            continue
        view, ui, done = tui.key(view, ui, k)
        if done:
            return 0


def cmd_tui(cfg, a, out=print, load_fn=load):
    view = a.view or 'overview'
    if a.plain or not sys.stdout.isatty():
        state = load_fn(cfg)
        for line in tui.render(view, state, a.width or 100, 10 ** 4, {'tab': tui.TASK_TABS.index(a.status) if a.status else 1},
                               plain=True):
            if line:
                out(line)
        return 0
    import curses
    return curses.wrapper(run, cfg, view, load_fn)


def add_parser(add):
    s = add('tui', cmd_tui, 'the terminal UI: Overview, Tasks, Needs you, Hosts, Agents; footer keys, ? for help '
            '(--plain: one view as plain text for screen readers; docs/TUI.md)')
    s.add_argument('--view', choices=list(tui.VIEWS), help='the view to open (default overview)')
    s.add_argument('--status', choices=list(tui.TASK_TABS), help='--view tasks --plain: which status (default queued)')
    s.add_argument('--plain', action='store_true', help='print the view as plain text (no curses, no icons, no lines)')
    s.add_argument('--width', type=int, help='--plain: line width (default 100)')
