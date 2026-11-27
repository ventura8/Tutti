"""The TUI for Tutti: interactive terminal viewer for tasks and jobs.

Press Enter on a task or job to expand it and display all associated details.
"""
import curses
import textwrap
import time

from . import autopilot, backlog, dispatch, jobprogress, projects, state, taskdetail, waitlist


def task_summary(item):
    """One summary line for an unexpanded task or job."""
    kind = item.get('kind', 'task')
    status = item.get('status', 'unknown')
    title = str(item.get('title', ''))[:60]
    return f"[{item.get('id', '')}] {kind}: {title} ({status})"


def item_detail_lines(item):
    """All associated details for an expanded task or job (tutti/taskdetail.py, the same list the dashboard shows)."""
    facts = item.get('detail_facts') or taskdetail.facts(item)
    return [f'    {label}: {text}' for label, text, *_ in facts]


def row_key(item):
    return item['id'] if item.get('kind') != 'shard' else f'{item["project"]}/{item["id"]}'


class Tui:
    """The terminal UI state machine: navigation and Enter to expand/collapse."""

    def __init__(self, items=None):
        self.items = list(items) if items is not None else []
        self.selected = 0
        self.expanded = set()
        self.offset = 0

    def on_key(self, key):
        self.offset = 0
        if key in ('\n', '\r', 'enter', 'Enter'):
            self._toggle_expanded()
        elif key in ('up', 'k'):
            self.selected = max(0, self.selected - 1)
        elif key in ('down', 'j'):
            self.selected = min(max(0, len(self.items) - 1), self.selected + 1)

    def _toggle_expanded(self):
        if not self.items:
            return
        it_id = row_key(self.items[self.selected])
        if it_id in self.expanded:
            self.expanded.remove(it_id)
        else:
            self.expanded.add(it_id)

    def row(self, idx, it):
        is_open = row_key(it) in self.expanded
        lines = [f'{"> " if idx == self.selected else "  "}{"[-] " if is_open else "[+] "}{task_summary(it)}']
        if is_open:
            lines += item_detail_lines(it) + ['']
        return lines

    def render(self):
        lines = [HEADER, '']
        if not self.items:
            return lines + ['  No tasks or jobs.']
        for idx, it in enumerate(self.items):
            lines += self.row(idx, it)
        return lines

    def viewport(self, height, width):
        rows = []
        start = 0
        for idx, item in enumerate(self.items):
            if idx == self.selected:
                start = len(rows)
            for line in self.row(idx, item):
                rows.extend(part for ln in line.split('\n') for part in (textwrap.wrap(ln, width) or ['']))
        if not rows:
            rows = ['  No tasks or jobs.']
        self.offset = min(max(0, self.offset), len(rows) - start - 1)
        return rows[start + self.offset:start + self.offset + height]

    def draw(self, stdscr):
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        window = self.viewport(max(1, height - 2), max(1, width - 1))
        for idx, line in enumerate([HEADER[:width - 1], *window][:height - 1]):
            try:
                stdscr.addstr(idx, 0, line)
            except curses.error:
                break
        stdscr.refresh()

    def handle_key(self, ch, height):
        if ch in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            self.offset += height if ch == curses.KEY_NPAGE else -height
        elif ch in KEYS:
            self.on_key(KEYS[ch])

    def run(self, stdscr=None):
        if stdscr is None:
            try:
                return curses.wrapper(self.run)
            except curses.error:
                print('\n'.join(self.render()))
                return 0
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.timeout(500)
        while True:
            self.draw(stdscr)
            ch = stdscr.getch()
            if ch in (ord('q'), ord('Q'), 27):
                return 0
            self.handle_key(ch, max(1, stdscr.getmaxyx()[0] - 2))


HEADER = 'Tutti tasks and jobs · Enter expands · PgUp/PgDn scroll · q quits'
KEYS = {curses.KEY_ENTER: 'enter', 10: 'enter', 13: 'enter', curses.KEY_UP: 'up', ord('k'): 'up', curses.KEY_DOWN: 'down',
        ord('j'): 'down'}


def load_items(cfg):
    """Loads all tasks and jobs for the TUI."""
    tasks = backlog.load(cfg).get('tasks', [])
    now = time.time()
    jobs = state.jobs_state(cfg, now)['items']
    projects.tag(cfg, jobs)
    jobprogress.annotate(cfg, jobs, now, lambda worker: state.account_of_worker(cfg, worker) if worker else None)
    shard_state = dispatch.read_state(cfg)
    waiting_shards = [{**item, 'detail_facts': item['facts']} for item in waitlist.shard_items(cfg, shard_state)]
    runs = autopilot.detail_jobs(jobs)
    task_rows = [{**task, 'kind': 'task', 'detail_facts': autopilot.detail_facts(cfg, task, runs.get(task.get('run')))}
                 for task in tasks]
    return [*task_rows, *jobs, *waiting_shards, *dispatch.shard_details(cfg, shard_state)]
