# The setup wizard

`tutti setup` installs and detects with no questions (docs/INSTALL.md). The few things only you can decide are one
wizard, with two faces over one engine (`tutti/wizard.py`):

- the dashboard's **Setup wizard** (`#setup`): opens by itself the first time an admin opens the dashboard, and from
  **Settings → Setup wizard** at any time;
- the terminal's: `tutti setup --tui` (full screen, curses), or `tutti setup --tui --plain` (one numbered question at a
  time). `tutti setup` run in a terminal with no browser (an ssh session, a server without a desktop) offers it.

Both show the same steps, the same rows and fields, and apply through the same code; what one does, the other shows.

## The steps

| Step | What it shows | What Apply does |
| --- | --- | --- |
| Welcome | what Tutti will do, what it found, what it never does | records that you started |
| Main and backup machines | every machine of the config and every one discovered (`~/.ssh/config`, Tailscale, the LAN), each with its role (main, backup, machine), slots, transport and share; a machine found only on Tailscale or the LAN with its `ssh-copy-id` line | writes `failover.main` and `failover.backup` (`auto`: Tutti elects; `none`: no failover; an ssh host not in the config is added first). The old backup is told to stand down first (`tutti main backup`). *Install the failover service* runs `tutti failover setup`: Tutti, its config and state copied to the backup, its round started there and here. A main other than this computer says `tutti move <m>` (docs/MOVE.md) |
| Machines | each machine's share (`use_pct`) and quiet hours, and ssh hosts to add | writes `machines.<m>.use_pct` and `machines.<m>.quiet_hours` (empty removes them), adds the hosts picked |
| Accounts and agents | Claude, Cursor, Antigravity (Gemini and Claude models), Copilot and the local models: each sign-in's state and the tool's own sign-in command | adds each agent's worker and account, writes the plan you pick (`accounts.<n>.subscription`; Cursor reads its own), adds Copilot only when you opt in (it costs money per request), and the local model |
| GitHub and repositories | `gh`'s sign-in, Tutti's GitHub App (`tutti github connect`), new repositories under your project folders and your GitHub repositories (`gh repo list`) | adds the repositories you pick as projects; clones the GitHub ones you pick into `~/Projects` with `gh repo clone` |
| Android devices | devices adb sees, the ones in the config, and how to pair one over Wi-Fi (`tutti devices pair`, the Devices card's QR) | records the step (pairing happens on the device) |
| Integrations | Linear, Slack and Discord: *coming soon* until an integration plugs in | turns on the integrations you pick |
| Household | the Users tab's invite link and `tutti household code` | records the step |
| Review and finish | every step's state | marks the wizard finished |

Every step can be skipped and revisited; the wizard opens on the first step left.

## The rules it keeps

- **Prefilled.** Each field starts with what Tutti found or what the config says; change only what is wrong.
- **One click or one copy-paste.** A step applies with one button. What only you can do (a tool's sign-in, `ssh-copy-id`,
  allowing USB debugging) is its exact command with a Copy button; the terminal wizard offers to run a sign-in for you.
- **Done or needs you.** Every step and every row says *done*, *needs you*, *optional* or *skipped*.
- **Resumable and idempotent.** Progress is `<state>/wizard.json` (step ids and times only). Applying a step twice
  changes nothing the second time; only values that differ are written.
- **Never weaker.** Sign-ins open in each tool's own program, so Tutti never sees a password or token; nothing secret is
  written to the config, `wizard.json` or the page. Config edits keep your comments, are checked by `tutti config`'s
  rules before they are written, and keep the old file as `.bak`. A bad value is refused with the key, the reason and the
  fix, and nothing is written.
- **Admins only for the household.** With sign-in on (docs/AUTH.md), a viewer sees the wizard with every household-wide
  step locked and without its details; `POST /api/wizard` answers 403 to anyone but an admin.

## The terminal wizard

Keyboard only. Full screen: Up and Down choose, Enter opens a step, Left and Right change a choice, Space ticks a box,
Enter edits a number or text (Enter keeps, Esc cancels), Tab moves on, q leaves. The cursor always sits on the line in
focus, so a screen reader reads it. `--plain` (and wherever curses cannot start, e.g. Windows without `windows-curses`)
asks one numbered question at a time and prints every answer: the friendliest for a screen reader. Standard library only.

## For other changes: plugging in an integration

```python
from tutti import wizard

wizard.register('linear', 'Linear', state_fn, apply_fn, what='issues become backlog tasks')
```

`state_fn(cfg)` returns `(state, detail, command)`, state `ok`, `todo` or `info`; `apply_fn(cfg, True)` turns it on and
returns a line for the message. Credentials stay in the integration's own sign-in.

## API

- `GET /api/wizard`: `{first_run, admin, current, finished, steps: [{id, title, admin, locked, status, rows, fields, links}]}`.
  Detection is reused for five minutes.
- `POST /api/wizard` (admin): `{"step": "<id>", "data": {<field key>: value}}`, `{"step": "<id>", "data": {"skip": true}}`,
  or `{"action": "detect"}` to detect again. 409 with the reason when a value is refused.
