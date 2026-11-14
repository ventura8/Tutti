# Connect your agent

Tutti works from inside the agent you already use. `tutti connect` wires Tutti into every main agent installed on this
computer: each gets Tutti's MCP server (`tutti mcp`) and a short instruction telling it when to hand work off. Then you
just talk to your agent ("hand the flaky test fix to Tutti", "what should I offload now?").

```bash
tutti connect --all          # every installed agent (the same as plain `tutti connect`)
tutti connect cursor         # one agent
tutti connect --status       # what is connected; --test also starts each agent's server and lists its tools
tutti connect --undo codex   # take Tutti out of one agent (or --undo alone: all of them)
```

The dashboard has the same as a card, **Connect your agent**: one row per agent with its state (connected, needs restart,
not installed, can't connect locally and why), Connect, Disconnect and **Test**.

Claude has two rows, kept apart: **Claude Code (Code tab in Claude Desktop, and the CLI)** and **Claude Desktop (chat)**.
Claude Code's state comes from `claude mcp list` (asked at most once a minute): registered and healthy is *connected*;
registered but failing is *error*, with what `claude mcp list` says. A Claude Code session started before Tutti was added
does not see it, but new sessions do, so that is a soft note under *connected*, never the status. Claude Desktop rewrites
`claude_desktop_config.json` itself and can drop Tutti's entry: when an agent `tutti connect` connected (and nobody
disconnected) has lost its entry, the status check puts it back (at most once in 10 minutes, so an app mid-write is
not fought over), backs the file up first, and says that only the chat
part of Claude Desktop needs a full quit (its tray or menu bar icon, Quit) and reopen. Test is a real round trip: it starts the
server exactly as that agent's config says and lists Tutti's tools through MCP.

## What each agent gets

Checked against each tool on 2026-11-14 (the CLIs' own `mcp add` commands were run in a scratch home to see the file and
shape they write).

| Agent | MCP server | Instructions | After connecting |
| --- | --- | --- | --- |
| Claude Code (CLI and the desktop app's Code tab) | `claude mcp add-json --scope user tutti` (user scope in `~/.claude.json`) | skill `~/.claude/skills/tutti/SKILL.md` | a new session |
| Claude Desktop (chat) | `~/.config/Claude/claude_desktop_config.json` (`mcpServers`; macOS: `~/Library/Application Support/Claude/`) | none (the server's own instructions) | quit and reopen the app |
| OpenAI Codex CLI | `~/.codex/config.toml`, a marked `[mcp_servers.tutti]` block | marked block in `~/.codex/AGENTS.md` | a new session |
| ChatGPT app (desktop, web) | not possible: its connectors only reach servers on the internet | | use Codex |
| Antigravity (IDE and `agy`) | `~/.gemini/config/mcp_config.json` (`mcpServers`, what `agy mcp add` writes; it no longer reads `.agents/settings.json`) | marked block in `~/.gemini/GEMINI.md` | a new `agy` session; the IDE: MCP, Refresh |
| Cursor (IDE and `cursor-agent`) | `~/.cursor/mcp.json` | skill `~/.cursor/skills/tutti/SKILL.md` | restart, switch `tutti` on once in Settings, MCP |
| GitHub Copilot CLI | `~/.copilot/mcp-config.json` (`type: local`) | marked block in `~/.copilot/copilot-instructions.md` | a new session |
| VS Code (Copilot agent mode) | `~/.config/Code/User/mcp.json` (`servers`, `type: stdio`) | none | "MCP: List Servers", tutti, Start (one trust prompt) |
| Gemini CLI | `~/.gemini/settings.json` (`mcpServers`) | the same `~/.gemini/GEMINI.md` block | a new session |

The entry starts the absolute `tutti` with `--config <your config> mcp`, so a desktop app with a short `PATH` finds both.

**Safe edits.** Only the `tutti` entry and the block between `<!-- tutti:begin -->` and `<!-- tutti:end -->` (TOML:
`# tutti:begin` / `# tutti:end`) are ever written or removed; every other server, setting and line stays. Each file is
copied to `~/.config/tutti/connect-backups/<agent>/` before its first change (`~/.claude.json` too, before `claude mcp` edits it). A second run changes nothing. A file that is
not valid JSON or TOML is refused and left untouched. A `[mcp_servers.tutti]` you wrote yourself in Codex is kept as it
is. Antigravity and Gemini CLI share `GEMINI.md`: undoing one keeps the block while the other is connected.

**ChatGPT.** ChatGPT's app cannot connect: its connectors call the server from OpenAI's side, so they only reach servers
on the internet, and Tutti is never exposed to the internet. Use Codex instead (the same OpenAI account):
`tutti connect codex`.

## The MCP server

`tutti mcp` speaks MCP over stdio (what every agent above launches). `tutti mcp --http [PORT]` serves the same on
`http://127.0.0.1:PORT/mcp` (default `mcp.http_port`, 4779) for a client that needs HTTP; it answers only with
`Authorization: Bearer <the token in <state>/mcp-token>` (mode 600, made on first use). `--bind <address>` (or
`mcp.bind`) listens on a LAN or VPN address instead, for another computer of the household over the VPN: only loopback
and private addresses are accepted (10/8, 172.16/12, 192.168/16, 100.64/10 as Tailscale uses, fc00::/7, a WireGuard
address, whatever its range; checked by `tutti/netpolicy.py`, the same rule as the household's servers). A public
address, a host name that resolves to one, and `0.0.0.0` / `::` are refused: Tutti is never exposed to the internet; people
reach it only over a VPN. `auto` means 127.0.0.1 here. Off loopback the HTTP is plain, so use it over the VPN (which
encrypts) rather than an untrusted LAN. Every tool runs the `tutti`
command itself, so an answer is what the terminal would print.

| Tool | Changes something | Runs |
| --- | --- | --- |
| `handoff_advice` | no | `tutti usage` and `tutti main`, plus the hand-off rule |
| `delegate_task` (task, project, size, track) | yes | `tutti run auto "[size S] <task>" --repo <project> --bg` |
| `job_status` (id) | no | `tutti runs show <id>` (or `runs list`); a `test-...` id reads the test's log and exit code |
| `backlog_list`, `backlog_add` (repo, title, size, scope, check) | add: yes | `tutti backlog list\|add` |
| `usage`, `machines`, `devices` | no | the same commands |
| `test_on` (track, command, machine or device) | yes | `tutti test` detached; poll `job_status` |
| `scout_ask` (scout, question, skill) | yes | `tutti scout ask --no-wait` |
| `away_mode` (mode) | yes (admin, also for the status) | `tutti owner status\|mode <m>` |

**Roles.** A tool that changes something needs the `admin` role; read-only tools are open to every role. The roles are
the dashboard sign-in's (`pending`, `viewer`, `admin`). The server takes `$TUTTI_ROLE`, else `mcp.role` in the config,
else `admin` (the person who owns this computer and its config) over stdio and on loopback. `--http` on a LAN or VPN
address defaults to `viewer`: whoever holds the token there is not necessarily the owner, so set `mcp.role: admin` to allow
changes. A request with another site's `Origin` is refused (a web page cannot call it), a tool argument that starts with
`-` is refused, and a silent connection is closed after 30 s. A refused call says which role it needs and where to get
it; nothing runs.

## For `tutti setup`

`tutti setup` calls the same code, no subprocess needed:

```python
from tutti import connect
results = connect.connect_all(cfg_path)   # [{agent, status, changed: [{file, backup}], note}] for every agent
rows = connect.status_all()               # [{agent, label, status, note}] for the summary screen
steps = connect.human_steps(results)      # guided.step('other', 'Restart <agent>', None, why): the restarts, for guided.render
```

`tutti setup` adds those steps to its one list at the end (docs/INSTALL.md, *The rule for every setup*); `tutti connect`
prints the same list itself.

`status` is one of `connected` (with `soft`: a note that is not a problem, such as older Claude Code sessions), `needs-restart`, `not-connected`, `not-installed`, `cannot` (ChatGPT) or `error` (a
broken config file; `note` names it and the fix). Agents that are not installed are skipped, never created.
