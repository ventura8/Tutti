# Tutti

*Tutti* (music): everyone plays together. One orchestrating agent, many cheaper workers, a few shared machines, and the
rules that keep them from stepping on each other.

Tutti is a small toolkit for AI-assisted work on a software project when you have more than one model subscription and
more than one computer. It grew out of one project's working day and keeps only what proved useful.

> **Status: v0.1.9.** Linux only. The npm package `tutti-agents` is marked `private` until the first
> publish. **Licence:** MIT (see `LICENSE`).

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
  pace, and what to do. See [docs/USAGE-PACING.md](docs/USAGE-PACING.md).
- **Peer coordination.** Two orchestrator sessions can share accounts and machines: lock files are the only signal,
  limits live in one shared file, and each session's risky actions need its own owner's approval.
- **The yield flag.** A file on a shared machine that says "this machine is mine for now". While it exists Tutti
  sends that machine nothing new; local-model workers pause, and unload their models when the flag asks for it.
- **Translation with mandatory QA.** Local models translate; no string is written before it passes mechanical
  validators and a back-translation check by a second model. What fails is held for a human, never shipped.

## Install

Linux, WSL, macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/ventura8/Tutti/main/install/install.sh | bash
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/ventura8/Tutti/main/install/install.ps1 | iex
```

Re-run the same line to update. Uninstall with `install/uninstall.sh` or `install/uninstall.ps1` the same way (config is kept unless `TUTTI_PURGE=1`). Edit the example config the installer wrote under `$XDG_CONFIG_HOME/tutti/config.yaml` (`~/.config/tutti/config.yaml` when `XDG_CONFIG_HOME` is unset; `TUTTI_CONFIG` or a project `tutti.config.yaml` takes precedence), then run `tutti config` and `tutti workers`.

## Quick start

Requirements: Linux, Python 3.10+, `PyYAML`, `git`, `rsync`, `ssh` with key-based login to your machines, and Docker
on the machines if you use the container runner.

```bash
git clone <this repository> ~/Projects/tutti
ln -s ~/Projects/tutti/bin/tutti ~/.local/bin/tutti       # or, once published: npm install -g tutti-agents
python3 -m pip install -r ~/Projects/tutti/requirements.txt
cp ~/Projects/tutti/tutti.config.example.yaml ~/my-project-wt/tutti.config.yaml   # then edit: machines, workers, accounts
tutti config        # checks the file and names every problem with its key
tutti machines      # free slots, load and yield flag of every machine
```

Tutti finds its config in `$TUTTI_CONFIG`, else the nearest `tutti.config.yaml` from the current folder upwards, else
`~/.config/tutti/config.yaml` (or `~/.config/tutti/tutti.config.yaml` as a fallback; `$XDG_CONFIG_HOME` is honoured).
Nothing personal is built in: hosts, paths and accounts live only in that file. It never holds a secret.

## The CLI

| Command | What it does |
| --- | --- |
| `tutti test <track> '<cmd>'` | Copies the track's worktree to the first fit machine and runs the command there behind the slot queue, in a memory-capped container or plain bash. `--on <machine>` skips routing; `--dir` names the tree; `--input <path>` (repeatable) copies a file or folder the job needs to that machine's cache first and hands its folder to the command as `$TUTTI_INPUTS` (read-only in a container). Returns the command's exit code. Steps a command needs first (`test.prepare`, e.g. the e2e build before Playwright) are put in front; a hot machine (`heat`) takes one run, a very hot one none. |
| `tutti files send <path>... --to <machine\|all>` | Gives files and folders (a big archive, a dataset, build inputs) to other machines over their own transport: rsync when both ends have it (resumable, skips what is there), else a resumable stream; each item is checked with `sha256sum` there. Lands in the machine's cache (`files.cache`, size budget `files.budget_gb`, least recently used items leave first) or `--dest <folder>`; `--bwlimit <KB/s>`. Refuses secrets (`.env*`, `*token*`, keys, service-account JSON, `.ssh/`). `tutti files list [--on <m>]` shows what each machine stores; `tutti files clean --on <m> [name...] [--all]` trims or empties a cache. See [docs/MACHINES.md](docs/MACHINES.md#files-for-machines). |
| `tutti ship "<message>"` | In the worktree you stand in: format, commit (with your trailer), run the targeted checks, push, deploy. `--full` runs the full checks; `--dry-run` prints the plan; `--track <t>` refuses any other worktree. Refuses to commit or push mid-rebase or mid-merge, or with conflict markers left in files (it names them). |
| `tutti usage` | Every account against its pace. `tutti usage log <account> <percent> [--resets <time>]` records a reading; `tutti usage read` runs the automatic readers now (Antigravity `agy /usage`, the Cursor plan, limit messages in job logs). See [docs/USAGE-PACING.md](docs/USAGE-PACING.md). |
| `tutti workers` | The workers and whether each is ready. |
| `tutti run <worker> "<task>" [files]` | Briefs a CLI worker in a worktree (`--dir`); `--show` prints the brief; `--input <path> --on <machine>` copies inputs to that machine first and tells the worker where they are. `tutti run auto` picks the worker from the latest readings: never an account in panic or spent, first one whose quota would be lost at the reset (`routing.workers`, `accounts.<name>.worker` and `args`). A limit message in its output logs the account as spent. With `--repo <repo>` the task is handed off end to end: a worktree of the repo (new, or `--track <name>` reused), the worker headless, the diff captured, the targeted checks routed (`delegate.checks`, default `ship.checks`) and a report that says what changed, the diff's path, each check's verdict, whether coverage was checked and any tool call the CLI denied. `--bg` returns at once with the id; `--discard` removes a new worktree after the report. Every run is logged with the account it spent. |
| `tutti conduct "<goal>" [--main <agent>] [--fallback a,b]` | Another agent as the main (conductor) agent: a CLI worker (Cursor, Antigravity, Copilot, Claude Code) or a local model gets the goal and a generated instruction file: Tutti's commands as plain shell commands, the project's `AGENTS.md`/`CLAUDE.md`, the safety limits (no push, tag, merge or deploy; `ship` only with `--dry-run`; no credentials; ask-first actions stop with `NEEDS HUMAN`). When its accounts reach panic or spent (the readings), a limit message appears, or the owner switches on the dashboard, it is stopped and the next in `main.fallback` continues from a handoff note (goal, done, changes, next). `--show` prints the instructions. See [docs/CONDUCT.md](docs/CONDUCT.md). |
| `tutti main [<agent>]` | The main agent now, who has room, the running or last conductor run; `tutti main <agent>` switches it (a running conductor hands over). |
| `tutti handoff done\|next "<text>"` | Inside `tutti conduct`: records a finished step or the next one in the run's progress, the handoff note's source. |
| `tutti runs [show\|discard <id>]` | The delegated runs, a run's report, or removing its worktree. |
| `tutti pr review <n>` | Asks the reviewers (`review.reviewers`: Copilot, CodeRabbit), detects when none will come (a refused request, CodeRabbit's "Review limit reached", or `review.timeout_minutes`) and says so, then has a cheap worker (`review.fallback`) review the diff and posts its findings to the pull request (`--no-post` prints them). |
| `tutti job register\|done` | A script outside Tutti shows on the dashboard with its log, progress and result. |
| `tutti ask <worker> "<question>" [files]` | A short answer from a local model about files or stdin. |
| `tutti code <worker> <file> "<instruction>"` | A local model rewrites one file; you review `git diff`. |
| `tutti agent <worker> "<task>" "<check>" files...` | A local model edits, runs the check, reads the failure, retries. |
| `tutti yield on\|off\|status --machine <m>` | Raises or lowers a machine's yield flag (`--unload` asks for models to be unloaded). |
| `tutti machines` | Probes every machine. |
| `tutti quiet [--summary <worker>] <cmd>` | Runs a command, keeps the log, prints only the exit code and, on failure, a five-line summary. |
| `tutti progress <done>/<total> [--job <log>]` | Reports how far a job is for the dashboard's progress bar (inside `tutti quiet` it finds its job through `TUTTI_PROGRESS_FILE`). |
| `tutti guard` | Pauses test containers when memory runs low; resumes them later. |
| `tutti tunnel start\|stop\|status <worker>` | The self-healing ssh tunnel to a worker's model server. |
| `tutti wait pid\|file\|grep\|run <target> [regex]` | Waits that end: always with `--timeout`, never by matching command lines; `--stall <s>` gives up (exit 125) when the watched log stops changing. Ends with one line: `done: ...`, or for `run <id>` the run's verdict and report. |
| `tutti dashboard [--port 4777]` | A local web dashboard on `127.0.0.1` (installable as an app): the projects Tutti sees on every machine (jobs, agents and runs per project, repositories and branches, a peer's processes, yield flags and GPU), the main agent with a switch, usage and pacing with the panic threshold, machines and their slots, workers and the agents installed here, running, stalled and recent jobs with the end of their logs. `--once` prints the same state as JSON. See [docs/DASHBOARD.md](docs/DASHBOARD.md). |
| `tutti translate <source.json> <locale>` | Translation with mandatory QA; `--check <out.json>` is the import gate. |

## Documents

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the pieces and how a run flows.
- [docs/CONDUCT.md](docs/CONDUCT.md): another agent as the main agent, its tools, limits and the handoff.
- [docs/WORKERS.md](docs/WORKERS.md): briefing Cursor, Antigravity and local models; review the diff, then gate.
- [docs/USAGE-PACING.md](docs/USAGE-PACING.md): the pacing rule and its arithmetic.
- [docs/MACHINES.md](docs/MACHINES.md): slots, routing, heat and memory guards, the yield flag, peers.
- [docs/DASHBOARD.md](docs/DASHBOARD.md): the local dashboard, its data sources and `/api/state`.
- [docs/LESSONS.md](docs/LESSONS.md): what went wrong, and the rule each failure left behind.
- [docs/ROADMAP.md](docs/ROADMAP.md) and [docs/FEEDBACK.md](docs/FEEDBACK.md): what is next; where first users log problems.
- [MIGRATION.md](MIGRATION.md): moving a folder of hand-rolled scripts over to Tutti.
- [AGENTS.md](AGENTS.md), [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md).

## Checks

```bash
bash scripts/check.sh    # shellcheck, ruff, the unit tests and a CLI smoke test: what CI runs
```
