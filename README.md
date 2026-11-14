# Tutti

*Tutti* (music): everyone plays together. One orchestrating agent, many cheaper workers, a few shared machines, and the
rules that keep them from stepping on each other.

Tutti is a small toolkit for AI-assisted work on a software project when you have more than one model subscription and
more than one computer. It grew out of one project's working day and keeps only what proved useful.

> **Status: v0.1.36.** Linux first; the installer and `tutti setup` also cover macOS, WSL and Windows. The npm package `tutti-agents` is not published yet (until then: the install line, uv or pipx).
> **Licence:** MIT (see `LICENSE`).

## The ideas

- **The orchestrator.** One capable agent (for example Claude Code) writes specs, briefs workers, reviews diffs and
  ships. It spends its own tokens on judgement, not on bulk.
- **Any agent can conduct.** `tutti conduct --main <agent> "<goal>"` makes Cursor, Antigravity, the Copilot CLI or a
  local model the orchestrator, with Tutti's commands as its tools; when its quota is in panic another takes over with a
  written handoff note. See [docs/CONDUCT.md](docs/CONDUCT.md).
- **Workers.** Bounded tasks go to cheaper hands: other vendors' CLI agents (Claude `claude`, Cursor `cursor-agent`,
  Google Antigravity `agy`) and local models served by Ollama. Every worker gets its own worktree and a precise brief,
  and never commits. The rule is **review the diff, then gate it**. See [docs/WORKERS.md](docs/WORKERS.md).
- **Remote test machines with slot queues.** Lint, tests and builds run on other machines, behind a queue: at most N
  heavy runs at once and only with enough free memory. A run that waits is normal, not a hang.
- **Fall-over routing.** `tutti test` picks the first fit machine: the quiet build box first, a shared machine only
  when the first is full and the shared one is idle enough. See [docs/MACHINES.md](docs/MACHINES.md).
- **Usage pacing.** Spend every quota fully about 1 hour before its reset (override per account with `lead_hours`). Not
  earlier (work stalls), not later (quota is wasted). `tutti usage` says whether each account is ahead, behind or on
  pace, and what to do. A planner spends what expires soonest first: agy's 5 h windows share their weekly quota over the
  windows left that can run, so as little as possible expires unused. See [docs/USAGE-PACING.md](docs/USAGE-PACING.md).
- **Peer coordination.** Two orchestrator sessions can share accounts and machines: lock files are the only signal,
  limits live in one shared file, and each session's risky actions need its own owner's approval.
- **The yield flag.** A file on a shared machine that says "this machine is mine for now". While it exists Tutti
  sends that machine nothing new; local-model workers pause, and unload their models when the flag asks for it.
- **Translation with mandatory QA.** Local models translate; no string is written before it passes mechanical
  validators and a back-translation check by a second model. What fails is held for a human, never shipped.

## Install

One line, then sign in. **Linux, WSL, macOS:**

```bash
curl -fsSLo tutti-bootstrap.sh https://github.com/ventura8/Tutti/releases/latest/download/bootstrap.sh && echo "e85e22cb369b46f122c4159301287250c9cd32d97c7a30b20488bd244b202130  tutti-bootstrap.sh" | (sha256sum -c - || shasum -a 256 -c -) && bash tutti-bootstrap.sh
```

**Windows (PowerShell):**

```powershell
irm https://github.com/ventura8/Tutti/releases/latest/download/bootstrap.ps1 -OutFile tutti-bootstrap.ps1; if ((Get-FileHash tutti-bootstrap.ps1).Hash -eq 'D8C7ED5B2E5976B72CB432D91621765F79FF12F84D887A19FADFFDFE68AD2848') { powershell -ExecutionPolicy Bypass -File tutti-bootstrap.ps1 } else { 'tutti: checksum mismatch, not run' }
```

It installs Tutti and what it needs (Python through uv, gh) in your user space, checking every download's SHA-256,
then runs `tutti setup`, which asks nothing: it finds your agents and their sign-ins, GPUs and local models, machines, Android devices and
git projects, writes the config, opens each tool's own sign-in, starts the dashboard at login and turns on automatic
updates. Run `tutti setup` again any time: it only fills what is missing. Details, uv and pipx, and the household's
`--join`: [docs/INSTALL.md](docs/INSTALL.md).

## Quick start: use Tutti from your own agent

The one line above already ran `tutti setup`, which also connects every agent it found (`tutti connect --all`: Claude
Code, Claude Desktop, Codex, Antigravity, Cursor, Copilot, VS Code, Gemini). Then ask your agent to hand work to Tutti.
Each agent gets Tutti's MCP server and a short instruction; your other settings are kept and backed up, `tutti connect
--undo <agent>` takes it out, and the dashboard's **Connect your agent** card shows each agent's state with a Test
button. See [docs/CONNECT.md](docs/CONNECT.md). Then:

```bash
tutti config        # checks the file and names every problem with its key
tutti machines      # free slots, load and yield flag of every machine
tutti dashboard     # already running at login: http://127.0.0.1:4777/ (its Set up card lists what is left)
```

From source instead: `git clone https://github.com/ventura8/Tutti`, then `install/install.sh` (or `uv tool install
git+https://github.com/ventura8/Tutti`), then `tutti setup`. Machines you send work to need `rsync` and `ssh` with
key-based login, and Docker if you use the container runner.

Tutti finds its config in `$TUTTI_CONFIG`, else the nearest `tutti.config.yaml` from the current folder upwards, else
`~/.config/tutti/config.yaml` (or `~/.config/tutti/tutti.config.yaml` as a fallback; `$XDG_CONFIG_HOME` is honoured).
Nothing personal is built in: hosts, paths and accounts live only in that file. It never holds a secret.

## The CLI

| Command | What it does |
| --- | --- |
| `tutti test <track> '<cmd>'` | Copies the track's worktree to the first fit machine and runs the command there behind the slot queue, in a memory-capped container or plain bash. `--on <machine>` skips routing; `--dir` names the tree; `--input <path>` (repeatable) copies a file or folder the job needs to that machine's cache first and hands its folder to the command as `$TUTTI_INPUTS` (read-only in a container). Returns the command's exit code. Steps a command needs first (`test.prepare`, e.g. the e2e build before Playwright) are put in front; a hot machine (`heat`) takes one run, a very hot one none. |
| `tutti test --device <name\|any> -- <cmd>` | Runs a command against an Android device on the machine its adb sees it on: `ANDROID_SERIAL` set (adb, Gradle's `connectedAndroidTest` and Playwright pick the device from it), the device's lock held so one run at a time uses it (a second waits in the same queue as machine slots), its battery, charging and heat checked first (it waits up to `--wait-minutes` for a phone to charge or cool down), the screen woken. Without a track the current folder is the tree. `--fetch <path>` copies screenshots, videos and reports back. See [docs/MACHINES.md](docs/MACHINES.md#android-devices). |
| `tutti devices [add\|pair\|move]` | Every Android device: connected, authorised, battery, charging, battery temperature, screen, Wi-Fi, whether a run holds it, and why it is not ready; a wireless device whose port changed is reconnected through mDNS. `pair` pairs one over wireless debugging (a QR code to scan; a TV types the code it shows; USB fallback) and adds it to the config. `move --to <machine> [devices]` moves devices to another machine with no re-pairing (the adb key is copied at mode 600), a preflight and a rollback. `add` prints the manual setup. See [docs/DEVICES.md](docs/DEVICES.md). |
| `tutti pause\|resume <kind> <name> [--stop-now]`, `tutti optin <kind> <name> on\|off` | The dashboard's switches from a terminal: pause or resume a machine, device, worker or account (it takes nothing new; `--stop-now` hands running jobs over first), turn an opt-in item on or off. Kept in `<state>/controls.json` (the dashboard's opt-in switches write the config file, as its Settings tab does). See [docs/DASHBOARD.md](docs/DASHBOARD.md#controls). |
| `tutti files send <path>... --to <machine\|all>` | Gives files and folders (a big archive, a dataset, build inputs) to other machines over their own transport: rsync when both ends have it (resumable, skips what is there), else a resumable stream; each item is checked with `sha256sum` there. Lands in the machine's cache (`files.cache`, size budget `files.budget_gb`, least recently used items leave first) or `--dest <folder>`; `--bwlimit <KB/s>`. Refuses secrets (`.env*`, `*token*`, keys, service-account JSON, `.ssh/`). `tutti files list [--on <m>]` shows what each machine stores; `tutti files clean --on <m> [name...] [--all]` trims or empties a cache. See [docs/MACHINES.md](docs/MACHINES.md#files-for-machines). |
| `tutti ship "<message>"` | In the worktree you stand in: format, commit (with your trailer), run the targeted checks, push, deploy. `--full` runs the full checks; `--dry-run` prints the plan; `--track <t>` refuses any other worktree. Refuses to commit or push mid-rebase or mid-merge, or with conflict markers left in files (it names them). |
| `tutti usage` | Every account against its pace. `tutti usage log <account> <percent> [--resets <time>]` records a reading; `tutti usage read` runs the automatic readers now (Antigravity `agy /usage`, the Cursor plan, limit messages in job logs). `tutti usage page-script [cursor]` prints the reading helper's parser for an orchestrator's browser tool to run in the owner's open usage page; `tutti usage page-reading '<json>'` records what it returned. See [docs/USAGE-PACING.md](docs/USAGE-PACING.md). |
| `tutti owner [mode <m>]` | The owner's own prompting: the mode (`auto`, `active`, `away`, `vacation-until-YYYY-MM-DD`) and the learned weekday x hour profile. On working days the planner keeps the owner's expected burn on the main agent's accounts; on weekends, away or on vacation the workers spend it all, so every account ends near 100 % an hour before its reset. The dashboard has **I'm away until…** and a 7 x 24 heatmap. See [docs/USAGE-PACING.md](docs/USAGE-PACING.md). |
| `tutti user [name <n>\|main-agent <kind>\|share <account> on\|off]` | Who this Tutti works for: the name (default: the login's), a hashed id, the main agent (`main-agent claude|codex|agy|cursor|copilot|gemini|local` switches it: the owner reserve moves to its accounts and `tutti connect` runs for it), the config and state folders, the household role, and which accounts share the room they would lose at their reset with the household (`accounts.<n>.share_spare`, off by default). A single user is a household of one and needs nothing new. See [docs/HOUSEHOLD.md](docs/HOUSEHOLD.md). |
| `tutti household init\|join <code>\|status\|leave\|code\|serve\|project share\|unshare <repo>\|machine share\|unshare\|use\|pause\|resume <m>` | Several people in one home, one set of machines: each member keeps their own main agent, accounts and pacing; shared machines, local models and shared projects' backlogs are scheduled fairly by one coordinator. With the dashboard's Google sign-in, a new member runs one command, `tutti household join "<invite-url>"` (quoted) (from **Users**, **Invite someone**): it detects their main agent and subscriptions, opens the invite to sign in, and pairs in under a minute; without sign-in, `init` on an always-on computer prints a join code. LAN or VPN only: a public address is refused. A shared repo's tasks are published to one backlog, deduplicated across members, and leased with a heartbeat so two members' autopilots never do the same task; unreachable, a member works alone and shared tasks wait. `machine share <m>` lends a machine (and its local models) to the household, `machine use <m>` adds another member's; the coordinator grants each member a weighted fair share of its slots (prompting before background autopilots, weighted by who is expected awake, no starvation, never idle while someone has work). Accounts with `share_spare` give the room they would lose at their reset to other members' shared projects. See [docs/HOUSEHOLD.md](docs/HOUSEHOLD.md). |
| `tutti workers` | The workers and whether each is ready. |
| `tutti run <worker> "<task>" [files]` | Briefs a CLI worker in a worktree (`--dir`); `--show` prints the brief; `--input <path> --on <machine>` copies inputs to that machine first and tells the worker where they are. `tutti run auto` picks the worker from the latest readings: never an account in panic or spent, first one whose quota would be lost at the reset (`routing.workers`, `accounts.<name>.worker` and `args`). A limit message in its output logs the account as spent. With `--repo <repo>` the task is handed off end to end: a worktree of the repo (new, or `--track <name>` reused), the worker headless, the diff captured, the targeted checks routed (the target repository's own: `projects.<name>.checks`, a `tutti.config.*` inside it, else `delegate.checks` / `ship.checks` when `delegate.project` is that repository or unset) and a report that says what changed, the diff's path, each check's verdict, whether coverage was checked and any tool call the CLI denied. `--check "<cmd>"` (repeatable, `--check-where local|route`) runs the given check instead of those; a local-model worker takes a delegated task through `tutti agent`'s loop. `--bg` returns at once with the id; `--discard` removes a new worktree after the report. Every run is logged with the account it spent. |
| `tutti autopilot [on\|off\|tick\|fill on\|off\|land <id>]` | When an account is behind its pace, hands bounded tasks from the backlog to it by itself (worktrees, each task's own check, at most `max_parallel`, never on an account that costs money, in panic, at its line, or with no cool free slot). A pass becomes a branch, or a draft pull request (self-improvement's tasks may be merged, only where a project allows it: docs/TELEMETRY.md); with `auto_review` the cheap reviewer reads it and it lands by the repo's `land` (merge, pr or branch). With `fill_room` (on by default) free room is filled from the repos' sources (TODOs, gaps, untested files) by itself. A failure retries from the same worktree with the failure in its brief, one step up `autopilot.ladder` (down when nothing above has room), until it passes or is parked as "needs a person" (the same failure 3 times on the strongest step). The dashboard has the toggle and the panel. See [docs/AUTOPILOT.md](docs/AUTOPILOT.md). |
| `tutti dispatch [status\|on\|off\|now]` | Keep busy (on by default): idle Android devices and machines pull the pending test shards that projects declare (`projects.<p>.tests`), on every new commit, highest priority first by a weighted fair share. Pause, yield, heat, battery, phone calls (paused, then resumed 30 s after the call) and opt-ins are respected, and a crashed run's lease is recovered. A target idle more than 5 min while work waits gets a warning with the cause and a fix button on the dashboard. See [docs/DISPATCH.md](docs/DISPATCH.md). |
| `tutti backlog add\|list\|rm\|seed\|retry\|done\|accept` | The autopilot's queue: `add <repo> "<title>" --scope <path> --check "<cmd>"`; `seed <repo>` reads FEEDBACK open items, `TODO(owner)` and the repo's `gaps` command; `retry <id> --note` sends a task back with the reviewer's notes; `accept <id>` queues a suggested candidate (a scout's Sentry triage). |
| `tutti conduct "<goal>" [--main <agent>] [--fallback a,b]` | Another agent as the main (conductor) agent: a CLI worker (Cursor, Antigravity, Copilot, Claude Code) or a local model gets the goal and a generated instruction file: Tutti's commands as plain shell commands, the project's `AGENTS.md`/`CLAUDE.md`, the safety limits (no push, tag, merge or deploy; `ship` only with `--dry-run`; no credentials; ask-first actions stop with `NEEDS HUMAN`). When its accounts reach panic or spent (the readings), a limit message appears, or the owner switches on the dashboard, it is stopped and the next in `main.fallback` continues from a handoff note (goal, done, changes, next). `--show` prints the instructions. See [docs/CONDUCT.md](docs/CONDUCT.md). |
| `tutti connect [<agent>\|--all] [--undo\|--status [--test]]` | Wires Tutti into every main agent installed here (Claude Code, Claude Desktop, Codex, Antigravity, Cursor, Copilot CLI, VS Code, Gemini CLI): its MCP server in the agent's own config plus a skill or a marked instructions block, merged, backed up, idempotent; `--undo` removes it, `--status --test` lists each agent's state and makes a real MCP round trip. ChatGPT's app cannot reach a local server; it says why and what to do. See [docs/CONNECT.md](docs/CONNECT.md). |
| `tutti mcp [--http [PORT] [--bind <LAN or VPN address>]]` | Tutti as an MCP server: delegate a task, job status and logs, the backlog, usage and hand-off advice, machines and devices, test on a machine or device, ask a scout, away mode. Changing tools need the `admin` role. HTTP listens on loopback, LAN or VPN addresses only, never a public one. See [docs/CONNECT.md](docs/CONNECT.md#the-mcp-server). |
| `tutti main [<agent>]` | The main agent now, who has room, the running or last conductor run; `tutti main <agent>` switches it (a running conductor hands over). |
| `tutti handoff done\|next "<text>"` | Inside `tutti conduct`: records a finished step or the next one in the run's progress, the handoff note's source. |
| `tutti runs [show\|discard <id>]` | The delegated runs, a run's report, or removing its worktree. |
| `tutti pr review <n>` | During a scout's review trial it also asks the scout (advisory, docs/SCOUTS.md). Asks the reviewers (`review.reviewers`: Copilot, CodeRabbit), detects when none will come (a refused request, CodeRabbit's "Review limit reached", or `review.timeout_minutes`) and says so, skips a reviewer whose account (named like it: `copilot`, `coderabbit`) reads 100 % until its reset (a rate-limit comment writes that reading, until the time it names, else an hour), saying until when, then has a cheap worker (`review.fallback`) review the diff and posts its findings to the pull request (`--no-post` prints them). |
| `tutti scout ask <scout> "<question>"` | Scouts are helpers that are not workers (Grok Bot): a question goes to its webhook routine and the answer comes back as a comment on a GitHub issue (label `tutti-scout`), which Tutti reads and prints. `scout watch` sends recurring watches (dependency release notes, the live site), `scout tick` sends what is due within the scout's weekly allowance (never on-demand), `scout review <pr>` asks for an advisory review, `scout trial` runs the review trial, `scout setup` prints the owner's steps. See [docs/SCOUTS.md](docs/SCOUTS.md). |
| `tutti telemetry [status\|on\|off\|setup\|report\|send]` | Crash reports and usage statistics: recorded on this computer always (`<state>/telemetry/events.jsonl`), sent to your Sentry project (or `telemetry.endpoint`) only after you say yes, scrubbed of everything personal. `setup` keeps the DSN in `~/.config/tutti/telemetry.env` (600); `report` summarises the local events (most used, slowest, most failing, wasted quota, stall causes); `send --test` checks the DSN. See [docs/TELEMETRY.md](docs/TELEMETRY.md). |
| `tutti selfimprove [status\|on\|off\|tick]` | Self-improvement (on by default): Sentry issues triaged by a scout and the local insights become backlog tasks with a regression test, the autopilot fixes them, a pull request gets the AI reviews, and where a project allows it (`projects.<name>.self_improve`) a green one is merged and released in batches; other projects stop at a ready pull request on their work branch. See [docs/TELEMETRY.md](docs/TELEMETRY.md#self-improvement). |
| `tutti job register\|done` | A script outside Tutti shows on the dashboard with its log, progress and result. |
| `tutti ask <worker> "<question>" [files]` | A short answer from a local model about files or stdin. |
| `tutti code <worker> <file> "<instruction>"` | A local model rewrites one file; you review `git diff`. |
| `tutti agent <worker> "<task>" "<check>" files...` | A local model edits, runs the check, reads the failure, retries. |
| `tutti yield on\|off\|status --machine <m>` | Raises or lowers a machine's yield flag (`--unload` asks for models to be unloaded). |
| `tutti update` | Updates every worker CLI on every machine without sudo (`cursor-agent update`, `agy update`, `copilot update`, `claude update`, npm for user installs, ollama pulls), never while a Tutti run uses the program, and prints the one exact sudo command for apt packages (`update.packages`, e.g. the Cursor editor) or root-owned npm installs. `--check` only reads versions. The dashboard runs it daily (`update.auto: false` turns that off). |
| `tutti machines [add-android]` | Probes every machine (a Termux phone also shows battery, charging, heat and Wi-Fi). `add-android` prints the one-time Termux setup and the config lines; lint and unit runs on a phone are opt-in. |
| `tutti setup [--ask] [--join <invite>]` | The first run, no questions (docs/INSTALL.md): finds agents and their sign-ins, GPUs, local models, machines (ssh config, Tailscale, LAN), Android devices, git projects and gh; writes the config with recommended defaults, keeping your comments (the Set up card shows each default with Change); at the end lists once what only you can do and opens each tool's own sign-in; starts the dashboard at login (systemd, launchd, a Windows task); turns on self-update; fixes a git that cannot do HTTPS. Again: only fills what is missing. |
| `tutti self-update [--check]` | Installs the newest checksummed release (release installs; on by default at login and daily, `self_update.enabled: false` turns it off). |
| `tutti doctor` | Checks this machine; today: a git whose HTTPS helper cannot load its libraries (a broken git first in PATH), with the fix. |
| `tutti quiet [--summary <worker>] <cmd>` | Runs a command, keeps the log, prints only the exit code and, on failure, a five-line summary. |
| `tutti progress <done>/<total> [--job <log>]` | Reports how far a job is for the dashboard's progress bar (inside `tutti quiet` it finds its job through `TUTTI_PROGRESS_FILE`): the exact layer, above known phases, estimates from history and agent estimates (docs/DASHBOARD.md, *Progress*). |
| `tutti guard` | Pauses test containers when memory runs low; resumes them later. |
| `tutti tunnel start\|stop\|status <worker>` | The self-healing ssh tunnel to a worker's model server. |
| `tutti wait pid\|file\|grep\|run <target> [regex]` | Waits that end: always with `--timeout`, never by matching command lines; `--stall <s>` gives up (exit 125) when the watched log stops changing. Ends with one line: `done: ...`, or for `run <id>` the run's verdict and report. |
| `tutti dashboard [--port 4777]` | A local web dashboard on `127.0.0.1` (installable as an app): the projects Tutti sees on every machine (jobs, agents and runs per project, repositories and branches, a peer's processes, yield flags and GPU), the main agent with a switch, usage and pacing first (agy's 5 h and weekly windows side by side, the planner's decision, and local models with their estimated energy cost in your currency, docs/ENERGY.md), what needs you, machines and their slots, workers and the agents installed here, running, stalled and recent jobs with the end of their logs and their context use, and the autocompact threshold. Its **Settings** tab holds every config key (label, help, the right control, value, default, reset; search; project priority as an ordered list), saved into your `tutti.config.yaml` with its comments kept and a backup first; secrets show only as set or not set. `--once` prints the same state as JSON. See [docs/DASHBOARD.md](docs/DASHBOARD.md#settings). |
| `tutti translate <source.json> <locale>` | Translation with mandatory QA; `--check <out.json>` is the import gate. |

## Documents

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the pieces and how a run flows.
- [docs/CONNECT.md](docs/CONNECT.md): `tutti connect` and the MCP server: Tutti from inside Claude, Codex, Antigravity, Cursor, Copilot and Gemini.
- [docs/AUTOPILOT.md](docs/AUTOPILOT.md): the backlog, rounds, retries up the ladder and parking.
- [docs/CONDUCT.md](docs/CONDUCT.md): another agent as the main agent, its tools, limits and the handoff.
- [docs/WORKERS.md](docs/WORKERS.md): briefing Cursor, Antigravity and local models; review the diff, then gate.
- [docs/SCOUTS.md](docs/SCOUTS.md): scouts (Grok Bot): questions answered on GitHub issues, watches, the review trial, the owner's setup.
- [docs/INSTALL.md](docs/INSTALL.md): the one-line install per OS, `tutti setup`, updates, uv and pipx, and the interface `household join` uses.
- [docs/HOUSEHOLD.md](docs/HOUSEHOLD.md): several users in one household: users, the coordinator, leases, fair shares, the threat model and the migration.
- [docs/USAGE-PACING.md](docs/USAGE-PACING.md): the pacing rule and its arithmetic.
- [docs/MACHINES.md](docs/MACHINES.md): slots, routing, heat and memory guards, the yield flag, peers.
- [docs/DASHBOARD.md](docs/DASHBOARD.md): the local dashboard, its data sources and `/api/state`.
- [docs/ENERGY.md](docs/ENERGY.md): local models' power, energy and estimated cost: the method, the price tables and their sources.
- [docs/DISPATCH.md](docs/DISPATCH.md): Keep busy: pending test shards, the dispatcher, leases, the idle watchdog, priorities.
- [docs/LESSONS.md](docs/LESSONS.md): what went wrong, and the rule each failure left behind.
- [docs/ROADMAP.md](docs/ROADMAP.md) and [docs/FEEDBACK.md](docs/FEEDBACK.md): what is next; where first users log problems.
- [MIGRATION.md](MIGRATION.md): moving a folder of hand-rolled scripts over to Tutti.
- [AGENTS.md](AGENTS.md), [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md).

## Checks

```bash
bash scripts/check.sh    # shellcheck, ruff, the unit tests and a CLI smoke test: what CI runs
```
