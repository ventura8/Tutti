# The terminal UI

`tutti tui` (t201, `tutti/tui.py`, `tutti/tuicmd.py`) shows the dashboard's main views in a terminal: **Overview**,
**Tasks** (Active, Queued, Needs a person, To review, Done), **Needs you**, **Hosts** and **Agents**. The data is the
dashboard's own: the running dashboard's last round (`<state>/dashboard-state.json`, written each round) when it is under
5 minutes old, so the screen fills at once; otherwise one look at every host made here. It refreshes every 15 seconds and on
`r`.

## Keys

The footer of every screen lists its keys; `?` shows them all.

| Key | Does |
| --- | --- |
| Tab, Shift-Tab, 1-5 | Next view, previous view, a view by number |
| j k, Down Up | Move in the list |
| g G, Home End | First, last |
| h l, Left Right | (Tasks) previous or next status |
| r | Refresh now |
| ? | Help (any key closes it) |
| q, Esc | Quit |

## Readable everywhere

- One layout on every screen: a title, the views, the list, a footer with the keys. Lines are cut with `…`, never wrapped,
  so it reads at 80x24 and redraws cleanly when the terminal is resized.
- Status is an icon and a word (`+ free`, `x offline`, `! needs you`), never colour alone, so it reads without colour
  and for colourblind people. Colour follows the terminal (16, 256 or truecolor) and is off with `NO_COLOR` or a dumb
  terminal.
- `tutti tui --plain [--view tasks --status queued] [--width 100]` prints one view as plain text, with no icons or lines,
  for screen readers and scripts; it is also what runs when the output is not a terminal.
- It draws at once ("Loading…") and loads in the background, so a slow host never freezes the keys.

Each view is snapshot-tested at 80x24, 40x12 and 132x50 (`tests/test_tui.py`).
