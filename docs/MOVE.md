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
| preflight | ssh works; the target's OS and architecture, free disk in its home (twice the state plus 1 GB), Tutti there and its version, Python's venv, rsync, systemd --user, lingering, whether it can suspend, its LAN and VPN addresses, which of the other machines it can ssh to, which worker and reader programs it has. A blocker stops the move with the fix. |
| install | Tutti installed there when it is missing or older, with the one-line install (`tutti/install.py`, `install_line`, `--no-setup`: the config comes from this move). |
| copy | The state folder, the usage log and `~/.local/state/tutti` copied with rsync while Tutti still runs here (usage logs, the backlog, the telemetry queue, users and roles, sessions, controls, the learned pacing profile, the household coordinator's state). |
| freeze | The dashboard here is stopped (its user service, or the process by its pid) and `<state>/move/frozen` refuses every command that would change the state (reads still run). |
| jobs | Running autopilot runs: `--jobs wait` (the default) waits up to `--wait-minutes` (60) for them, then hands over what is left; `--jobs handover` stops each one at once (its process tree) and queues its task again, so it continues from its worktree. |
| sync | The final copy, with deletions. |
| configure | The target's config written to `~/.config/tutti/config.yaml` there (below), and the secrets in `~/.config/tutti/*.env` and `household.json` copied over ssh's standard input with mode 600. Their content never appears in argv, the journal or a log. |
| start | Tutti started there as the login service `tutti setup` installs (`install.service_plan`: `tutti-dashboard.service`, systemd --user), and `tutti-name.service` publishing `tutti.local` on the LAN (mDNS, when `avahi-publish` is there). |
| health | The dashboard must answer on the target's `127.0.0.1` within 30 s. If it does not, the move rolls back by itself. |
| client | This computer's config (and the per-login copy when it is the same Tutti) gets a `host:` block pointing at the new host, with a `.bak-pre-move` backup; `tutti-client.service` starts here; this login's household membership points at the stable name (its old file kept for a rollback). |
| verify | `http://127.0.0.1:<port>/` here answers as the new host's dashboard, and the host sees this computer's bridge. |
| done | The move recorded (`<state>/move/last.json`, here and there) for the card's "Last move". |

### The config from the target's point of view

- the target becomes `host: local`; this computer becomes an ssh machine (`host:` its LAN address, `user:`, `via:` itself);
- a machine the target cannot reach by ssh gets `via: <this computer>`: the host probes it through this computer;
- the state, the worktrees folder and the usage log move under the target's home;
- an account whose reader needs a program or a sign-in the target lacks gets `read_on: <this computer>`, and one with a
  `reading_file` gets `reading_from: <this computer>`: they are read here, through the bridge, and logged there;
- `host: {machine, me, address, port, jobs}`: `jobs: <this computer>` when the repositories or the workers' sign-ins are
  here (the usual case): worker runs, the reviewer, and the autopilot's git and gh then run here, started by the host;
- a `household.bind` or `dashboard.bind` that names an address becomes `auto` (`netpolicy.binds`: the target's own LAN and VPN addresses).

Every comment and the key order are kept (`tutti/yamledit.py`), and the result passes the config check before anything
is written.

## After the move

**The host** runs the dashboard, the state, the readings, the autopilot's planning, scouts, self-improvement and the
household coordinator. The dashboard still listens on its own `127.0.0.1` only.

**The old computer is a client.** `tutti-client.service` keeps one ssh connection to the host open. It carries:

- the dashboard's port: `http://127.0.0.1:4777/` here is the host's dashboard (same address, same installed app, same
  OAuth origins);
- the bridge (below).

Commands that read or change the state (`backlog`, `autopilot`, `pause`, `resume`, `optin`, `usage`, `main`, `owner`,
`dispatch`, `selfimprove`, `scout`, `runs`, `wait run`, `household`, `user`, `run`) run on the host over ssh, so the
terminal, scripts and every connected agent (`tutti mcp` runs these same commands, so `tutti connect`'s MCP entries need
no change) see the one state the dashboard shows. Commands about this computer (`test`, `ship`, `connect`, `mcp`,
`update`, `doctor`, `conduct`) stay here.

`tutti run auto "<task>" --repo map` from the client: the host picks the worker from its readings, runs the job here
through the bridge (this computer's repositories and sign-ins), and keeps the record and the report.

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

- `sudo loginctl enable-linger <user>` on the target, when lingering is off (Tutti then runs with nobody signed in);
- `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`, when the target can suspend;
- a worker's sign-in on the target (`ssh -t <target> <tool> login`), only when its jobs run there (no `host.jobs`).
  Tutti never copies another tool's credential files.

To run the jobs on the host instead: clone the repositories there, sign the workers in there, point
`autopilot.repos.<r>.path` at the clones and remove `host.jobs` from the host's config.

## Known limits

- A host is Linux with systemd for now.
- A client's own jobs show on the host's jobs card (status and log tail) while the client is connected; their full log stays
  on the client.
- Members of a household on other computers point their `household.json` at `tutti.local:<port>` once (later moves
  need no edit); this computer's membership is repointed by the move.
