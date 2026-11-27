"""Keyboard navigation for the terminal dashboard."""

import sys
import textwrap

from . import agentcards


def terminal_rows(snapshot, agents):
    if agents:
        return agentcards.lines(snapshot['agents'])[1:]
    return [
        f'Main agent: {agentcards.describe(snapshot["main"]["agent"])}',
        f'Running jobs: {snapshot["summary"]["running"]}',
        f'Free slots: {snapshot["summary"]["free_slots"]}',
    ]


def draw(screen, snapshot, agents, offset):
    height, width = screen.getmaxyx()
    width = max(1, width - 1)
    page = max(0, height - 2)
    rows = [part for line in terminal_rows(snapshot, agents) for part in (textwrap.wrap(line, width) or [''])]
    offset = max(0, min(offset, len(rows) - page))
    tabs = 'Overview  [Agents]' if agents else '[Overview]  Agents'
    visible = [tabs, 'Tab: switch  ↑↓: scroll  R: refresh  Q: quit', *rows[offset:offset + page]]
    screen.erase()
    for index, row in enumerate(visible[:height]):
        if width > 1:
            screen.addnstr(index, 0, row, width)
    screen.refresh()
    return offset


def navigate(screen, snapshot, refresh):
    agents, offset = True, 0
    while True:
        offset = draw(screen, snapshot, agents, offset)
        key = screen.getkey()
        match key.lower():
            case 'q' | '\x1b':
                return
            case '\t' | 'key_btab':
                agents, offset = not agents, 0
            case 'r':
                snapshot = refresh()
            case _:
                offset += {'KEY_DOWN': 1, 'KEY_UP': -1}.get(key, 0)


def show(snapshot, refresh):
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            import curses
        except ImportError:
            pass
        else:
            try:
                curses.wrapper(navigate, snapshot, refresh)
                return
            except curses.error:
                pass
    print('\n'.join(agentcards.lines(snapshot['agents'])))
