"""The TUI for Tutti: interactive terminal viewer for tasks and jobs.

Press Enter on a task or job to expand it and display all associated details.
"""
import curses
import sys
import time

from . import backlog, state


def task_summary(item):
    """One summary line for an unexpanded task or job."""
    kind = item.get('kind', 'task')
    status = item.get('status', 'unknown')
    title = str(item.get('title', ''))[:60]
    return f"[{item.get('id', '')}] {kind}: {title} ({status})"


def _val(val):
    if val is None or val == '' or val == []:
        return 'none'
    if isinstance(val, list):
        return ', '.join(str(x) for x in val)
    return str(val)


def item_detail_lines(item):
    """All associated details for an expanded task or job."""
    worker_info = f"{_val(item.get('worker'))} · {_val(item.get('model'))} · {_val(item.get('account') or item.get('step'))}"
    mach_info = f"{_val(item.get('machine'))} · {_val(item.get('device'))}"
    links = f"{_val(item.get('branch'))} · {_val(item.get('pr'))} · {_val(item.get('ci'))}"
    se = f"{_val(item.get('started'))} · {_val(item.get('closed_at') or item.get('ended'))}"
    twc = f"{_val(item.get('tokens') or item.get('credits'))} · {_val(item.get('watts'))} · {_val(item.get('cost'))}"
    rel = f"{_val(item.get('related'))} · {_val(item.get('merged_into'))} · {_val(item.get('together_with'))}"
    acts = item.get('actions') or ['retry', 'cancel', 'reprioritise', 'claim']
    out = [
        f"    ID: {_val(item.get('id'))}",
        f"    Title: {_val(item.get('title'))}",
        f"    Repo / Project: {_val(item.get('project') or item.get('repo'))}",
        f"    Priority: {_val(item.get('priority'))}",
        f"    Size: {_val(item.get('size'))}",
        f"    Source: {_val(item.get('source'))}",
        f"    Scope paths: {_val(item.get('scope'))}",
        f"    Acceptance check: {_val(item.get('check'))}",
        f"    Claimed by: {_val(item.get('claimed_by'))}",
        f'    Worker / Model / Account: {worker_info}',
        f'    Machine / Device: {mach_info}',
        f'    Branch / PR / CI: {links}',
        f'    Start / End: {se}',
        f"    Duration: {_val(item.get('duration') or item.get('elapsed'))}",
        f"    ETA: {_val(item.get('eta'))}",
        f'    Tokens / Watts / Cost: {twc}',
        f'    Related / Merged: {rel}',
        f'    Actions (admin): {_val(acts)}',
    ]
    notes = item.get('notes') or []
    out.append(f'    Notes history ({len(notes)}):' if notes else '    Notes history: none')
    for n in notes:
        out.append(f'      - {n}')
    attempts = item.get('attempts') or item.get('history') or []
    out.append(f'    Attempts and logs ({len(attempts)}):' if attempts else '    Attempts and logs: none')
    for a in attempts:
        res = 'passed' if a.get('ok') else f"failed: {a.get('why', '')}"
        tail = f" | tail: {a['tail'][:60]}" if a.get('tail') else ''
        log = f" (log: {a['log']})" if a.get('log') else ''
        out.append(f"      - run {a.get('run', '?')}: {res}{log}{tail}")
    return out


class Tui:
    """The terminal UI state machine: navigation and Enter to expand/collapse."""

    def __init__(self, items=None, cfg=None):
        self.cfg = cfg
        self.items = list(items) if items is not None else []
        self.selected = 0
        self.expanded = set()

    def on_key(self, key):
        if key in ('\n', '\r', 'enter', 'Enter'):
            self._toggle_expanded()
        elif key in ('up', 'k'):
            self.selected = max(0, self.selected - 1)
        elif key in ('down', 'j'):
            self.selected = min(max(0, len(self.items) - 1), self.selected + 1)

    def _toggle_expanded(self):
        if not self.items:
            return
        it_id = str(self.items[self.selected].get('id', ''))
        if it_id in self.expanded:
            self.expanded.remove(it_id)
        else:
            self.expanded.add(it_id)

    def render(self):
        lines = ['Tutti TUI — Tasks and Jobs (Enter to expand/collapse, q to quit)', '']
        if not self.items:
            lines.append('  No tasks or jobs.')
            return lines
        for idx, it in enumerate(self.items):
            prefix = '> ' if idx == self.selected else '  '
            it_id = str(it.get('id', ''))
            is_open = it_id in self.expanded
            mark = '[-] ' if is_open else '[+] '
            lines.append(f'{prefix}{mark}{task_summary(it)}')
            if is_open:
                lines.extend(item_detail_lines(it))
                lines.append('')
        return lines

    def run(self, stdscr=None):
        if stdscr is None:
            try:
                return curses.wrapper(self.run)
            except Exception:
                print('\n'.join(self.render()))
                return 0
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.timeout(500)
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            for idx, ln in enumerate(self.render()[:h - 1]):
                try:
                    stdscr.addstr(idx, 0, ln[:w - 1])
                except curses.error:
                    pass
            stdscr.refresh()
            try:
                ch = stdscr.getch()
            except curses.error:
                continue
            if ch in (ord('q'), ord('Q'), 27):
                break
            if ch in (curses.KEY_ENTER, 10, 13):
                self.on_key('enter')
            elif ch in (curses.KEY_UP, ord('k')):
                self.on_key('up')
            elif ch in (curses.KEY_DOWN, ord('j')):
                self.on_key('down')
        return 0


def load_items(cfg):
    """Loads all tasks and jobs for the TUI."""
    tasks = backlog.load(cfg).get('tasks', [])
    now = time.time()
    jobs = state.jobs_state(cfg, now).get('items', [])
    items = []
    for t in tasks:
        items.append({**t, 'kind': 'task'})
    for j in jobs:
        items.append({**j, 'kind': 'job'})
    return items
