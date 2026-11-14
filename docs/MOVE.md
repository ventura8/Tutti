# Moving Tutti to another machine

You may start Tutti on a laptop and later want it on an always-on mini PC or desktop, so the dashboard, the autopilot
and the readings keep going when the laptop sleeps. One command moves it, and one command brings it back:

```bash
tutti move --to nuc --dry-run      # check the target and print the whole plan; nothing changes
tutti move --to nuc                # move: preflight, copy, cut-over, this computer becomes a client
tutti move --to nuc --jobs handover   # running autopilot jobs are stopped and queued again instead of waited for
tutti move --to nuc --devices      # the Android devices follow (tutti devices move: key copied, nothing asked; DEVICES.md)
tutti move --status                # where Tutti runs, the clients connected, the last or current move
tutti move --rollback              # Tutti back on this computer (also automatic when the new host fails its health check)
```

The dashboard has the same thing on its **Where Tutti runs** card: the host, its uptime, the clients connected, the last
move, and **Move Tutti to…** (pick the machine and what happens to running jobs, **Show the plan**, then **Move**). The
card shows each step while it runs. Moving is an admin action (the dashboard's `/api/move` is an admin path).

The target is a machine already under `machines:` that this computer reaches by ssh with a key (BatchMode, no password).

## What a move does

A move is one resumable operation. Each step is written to `<state>/move/journal.json` as it finishes; if anything
stops it (a dropped connection, a closed laptop lid), `tutti move --to <machine>` again continues where it stopped, and
`--restart` starts over.

| Step | What happens |
| --- | --- |
| preflight | ssh works; the target's OS and architecture, free disk in its home (twice the state, plus the repositories, plus 1 GB), Tutti there and its version, Python's venv, rsync, systemd --user, lingering, its LAN and VPN addresses, which of the other machines it can ssh to, the version of each agent CLI there. A blocker stops the move with the fix. Sleep is left alone: presence handles a host that sleeps. |
| install | Tutti installed there when it is missing or older, with the one-line install (`tutti/install.py`, `install_line`, `--no-setup`: the config comes from this move). |
| tools | The agent CLIs (claude, cursor-agent, agy, copilot, gemini, codex) there at the same versions, below. |
| logins | Their logins copied and checked, below. |
| copy | The state folder, the usage log, `~/.local/state/tutti` and the repositories with their worktrees copied with rsync while Tutti still runs here (usage logs, the backlog, the telemetry queue, users and roles, sessions, controls, the learned pacing profile, the household coordinator's state, the scouts' state). |
| freeze | The dashboard here is stopped (its user service, or the process by its pid) and `<state>/move/frozen` refuses every command that would change the state (reads still run). |
| jobs | Running autopilot runs and reviews or merges in flight: `--jobs wait` (the default) waits up to `--wait-minutes` (60) for them, then hands over what is left; `--jobs handover` stops each one at once (its process tree) and queues its task again, so it continues from its worktree. |
| sync | The final copy: the state (with deletions), each repository's git folder and the autopilot's own worktrees; everything else was copied while Tutti still ran, so the cut-over stays short. |
| configure | The target's config written to `~/.config/tutti/config.yaml` there (below), and the secrets in `~/.config/tutti/*.env` and `household.json` copied over ssh's standard input with mode 600. Their content never appears in argv, the journal or a log. |
| start | Tutti started there as the login service `tutti setup` installs (`install.service_plan`: `tutti-dashboard.service`, systemd --user), and `tutti-name.service` publishing `tutti.local` on the LAN (mDNS, when `avahi-publish` is there). |
| health | The dashboard must answer on the target's `127.0.0.1` within 30 s. If it does not, the move rolls back by itself. |
| client | This computer's config (and the per-login copy when it is the same Tutti) gets a `host:` block pointing at the new host, with a `.bak-pre-move` backup; `tutti-client.service` starts here; this login's household membership points at the stable name (its old file kept for a rollback). |
| verify | `http://127.0.0.1:<port>/` here answers as the new host's dashboard (a failure rolls back), and `tutti doctor` and `tutti workers` run there and are reported. |
| done | The move recorded (`<state>/move/last.json`, here and there) for the card's "Last move". |

### The config from the target's point of view

- the target becomes `host: local`; this computer becomes an ssh machine (`host:` its LAN address, `user:`, `via:` itself);
- a machine the target cannot reach by ssh gets the same ssh access as here (below); only when that is not possible (no
  entry here, or ssh still fails from the target) it gets `via: <this computer>` and is probed through this computer;
- every path under this computer's home (the state, the worktrees, the usage log, the autopilot repos, scripts, reading
  files) becomes the same path under the target's home: the jobs, the readers, the reviews, git and gh run on the target,
  which has the CLIs, the logins and the repositories after the move, so it keeps working while this computer is away;
- `host: {machine, me, address, port}`;
- the same text is written to every config path the old computer had for this Tutti (e.g. a project's `tutti.config.yaml`
  inside the copied worktrees folder), so a command run anywhere on the target finds the host's config;
- a `household.bind` or `dashboard.bind` that names an address becomes `auto` (`netpolicy.binds`: the target's own LAN and VPN addresses).

Every comment and the key order are kept (`tutti/yamledit.py`), and the result passes the config check before anything
is written.

## Everything Tutti relies on

### CLIs at the same versions

Each agent CLI installed under the home is carried by its own install folder, so the target runs the very same version
with no download: `~/.local/share/<tool>/versions/<v>` (claude, cursor-agent), an npm package under
`~/.local/lib/node_modules` (copilot, gemini), or the single file in `~/.local/bin` (agy); its link in `~/.local/bin` is
made again. A tool the target already has at the same version is skipped. When an npm CLI moves and the target has no
node, the same node version comes from nodejs.org, checked against its SHA256SUMS. gh comes with Tutti's own install.
A CLI installed by a system package manager cannot be carried: the plan names it, to be installed on the target the same way.

### Logins

The owner decided (2026-11-14, relayed by the orchestrating agent) that a move copies the agent tools' logins, so the
target needs no sign-in. This is the one documented exception to "Tutti never copies another tool's credential files",
and it applies only to `tutti move`, run by an admin:

- each tool's login and settings files (`~/.claude/.credentials.json`, `~/.claude.json`, `~/.claude/settings.json`;
  `~/.config/cursor/auth.json`, `~/.cursor/cli-config.json`; `~/.gemini/oauth_creds.json`, `google_accounts.json`,
  `settings.json`, `installation_id` and `antigravity-cli/` without its conversations and caches; `~/.copilot/config.json`;
  `~/.codex/auth.json`, `config.toml`) go over ssh with rsync, files mode 600 and folders 700. Tutti finds them by name
  only; it never reads, prints or logs their content;
- gh keeps its token in the keyring, so it goes the way gh documents: `gh auth token` piped straight into
  `gh auth login --with-token` there (held in memory, never shown), then `gh auth setup-git`;
- each tool's own status command checks the login on the target (`claude auth status`, `cursor-agent status`,
  agy's `/usage`, `gh auth status`, `codex login status`; gemini and copilot have none, so their login file there must be
  non-empty); its output is matched, never shown;
- a tool that refuses a copied login gets its own device-code sign-in (`ssh -t <target> <tool> login`), listed once at the
  end: the only prompt a move can leave.

### Repositories and worktrees

The autopilot repos, every git worktree of them under the home and the worktrees folder go in one rsync with hard links
kept (the worktrees share node_modules with the main checkout), the state folder excluded. Then `git worktree repair`
runs there with the new paths, so each worktree and its repository find each other and WIP, branches and refs come along.
The final sync copies what changed during the move. Projects on other machines stay where they are.

### Machines the target cannot reach yet

For a machine this computer reaches by ssh and the target does not (a desktop only this computer has a key for), the move
gives the target the same access (the owner's decision, 2026-11-14): its `Host` entry from `~/.ssh/config` (added there only
when that alias has none), the key file it names (mode 600, never read or shown) and its host key from `known_hosts`
(`ssh-keygen -F`). Then `ssh <alias> true` is tried from the target, and the machine is used directly from there (tests
routed to it, a model's tunnel). Only a machine whose entry cannot be found here is probed through the client.

### Local models on the old computer

A local model this computer serves on its own loopback (an ollama worker at `127.0.0.1:<port>`, no `machine`) is reached by
the host at its own `127.0.0.1:11435` (then 11436, ...) through the client's ssh (`-R`, `host.reverse`): never a LAN
bind. While the old computer is away, that worker does not answer and routing skips it.

### Readings from the old computer's session

An account's `reading_file` (Claude's readings, written by the main session's get_usage on the old computer) is sent by
`tutti client` to the same path under the host's home whenever it changes (checked every minute), so the host paces
with it while the old computer is on.

## After the move

**The host** runs the dashboard, the state, the readings, the autopilot and its worker runs, reviews and landings,
scouts, self-improvement, the daily update round and the household coordinator, with its own CLIs, logins and
repositories: household work goes on while the old computer is away or asleep. The dashboard still listens on its own
`127.0.0.1` only.

**The old computer is a client.** `tutti-client.service` keeps one ssh connection to the host open. It carries:

- the dashboard's port: `http://127.0.0.1:4777/` here is the host's dashboard (same address, same installed app, same
  OAuth origins);
- the bridge (below): this computer's own jobs on the host's jobs card, and probes of machines the host cannot reach.

Commands that read or change the state (`backlog`, `autopilot`, `pause`, `resume`, `optin`, `usage`, `main`, `owner`,
`dispatch`, `selfimprove`, `scout`, `runs`, `wait run`, `household`, `user`, `run`) run on the host over ssh, so the
terminal, scripts and every connected agent (`tutti mcp` runs these same commands, so `tutti connect`'s MCP entries need
no change) see the one state the dashboard shows. Commands about this computer (`test`, `ship`, `connect`, `mcp`,
`update`, `doctor`, `conduct`) stay here.

`tutti run auto "<task>" --repo map` from the client runs on the host, in its copy of the repository. The bookmarklet and
the reading helper keep posting to `http://127.0.0.1:4777/`, which is the host's dashboard through the client.

### When the host is down

The client keeps a read-only copy of the host's state (`<state>/host-mirror`, refreshed every 5 minutes by
`tutti client`). With the host unreachable:

- reads (`tutti usage`, `backlog list`, `autopilot`, `runs`, ...) run on that copy and say "read-only copy, copied N min ago";
- `tutti run` works here alone and says that its record stays on this computer;
- a change (`pause`, `backlog add`, ...) is refused with the reason and the fix;
- `tutti dashboard` serves a read-only dashboard from the copy, which says so on the Where Tutti runs card.

## The bridge

The host needs things only the client has: its repositories, its agents' sign-ins, the reading file its own session
writes (`accounts.claude.reading_file`). The client's ssh to the host runs `tutti bridge serve --machine <client>` there,
which listens on a unix socket `<state>/bridge/<client>.sock` (a folder only that login can open). The host's Tutti asks
through it; the client answers. No port is opened, and the host needs no ssh key into the client.

The client never runs a command the host names. It answers only Tutti's own requests, from its own config:

| Request | What the client does |
| --- | --- |
| `ping` | its version and host name |
| `probe` | `remote.probe` of one of its own machines (one the host cannot reach) |
| `reader` | the account's reader and plan from its own config, with its own sign-in |
| `reading-file` | the account's `reading_file` from its own config |
| `run` | `tutti run ...` with its own config (no `--config` accepted); the output streams back, the report files follow |
| `exec` | git, gh or one of its worker programs, by bare name from its own PATH, only in its repositories or worktrees folder (a folder is required; the autopilot's publish and land) |
| `review` | one of its worker programs reviewing the host's diff in an empty folder |
| `stop` | ends the process tree of a run it runs for the host (a move's `--jobs handover`) |
| `jobs` | its own running jobs (scripts, quiet runs, ship checks), shown on the host's jobs card tagged with the client |

`tutti bridge status` on the host lists the connected clients.

## OAuth origins

The move prints the change to the dashboard's Google OAuth client (`auth.required_origins` on the new host against the
old one). With the dashboard reached through the client's tunnel, the origins stay `http://127.0.0.1:4777` and
`http://localhost:4777`, so usually nothing changes; a LAN or VPN origin (`dashboard.origins`) is listed as add and
remove lines for console.cloud.google.com/apis/credentials. Every origin and address is checked by `tutti/netpolicy.py`:
localhost, LAN or VPN only; a public one is a blocker.

## Never on the internet

The dashboard stays on `127.0.0.1` at both ends; members and clients reach the host over the LAN or a VPN (`tutti.local`
or the VPN's name). A move never writes a public bind, a wildcard bind or a public origin, and offers no tunnel to the
internet.

## What needs a person

The plan lists these once, at the end, each with its exact command:

- `sudo loginctl enable-linger <user>` on the target, only when lingering is off there (shown in the plan, before the move);
- a tool's own device-code sign-in, only when it refuses its copied login.

`host.jobs: <client>` (an option, not set by a move) still runs the worker jobs on a client through its bridge, for a
host that cannot hold the CLIs.

## Known limits

- A host is Linux with systemd for now.
- A client's own jobs show on the host's jobs card (status and log tail) while the client is connected; their full log stays
  on the client.
- Members of a household on other computers point their `household.json` at `tutti.local:<port>` once (later moves
  need no edit); this computer's membership is repointed by the move.
