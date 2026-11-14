# Tutti

*Tutti* (music): everyone plays together. One orchestrating agent, many cheaper workers, a few shared machines, and the
rules that keep them from stepping on each other.

Tutti is a small toolkit for AI-assisted work on a software project when you have more than one model subscription and
more than one computer. It grew out of one project's working day and keeps only what proved useful.

> **Status: v0.1.0, unreleased.** Linux only. The npm package `tutti-agents` is marked `private` until the first
> publish. **Licence:** MIT (see `LICENSE`).

## The ideas

- **The orchestrator.** One capable agent (for example Claude Code) writes specs, briefs workers, reviews diffs and
  ships. It spends its own tokens on judgement, not on bulk.
- **Workers.** Bounded tasks go to cheaper hands: other vendors' CLI agents (Claude `claude`, Cursor `cursor-agent`,
  Google Antigravity `agy`) and local models served by Ollama. Every worker gets its own worktree and a precise brief,
  and never commits. The rule is **review the diff, then gate it**. See [docs/WORKERS.md](docs/WORKERS.md).
- **Remote test machines with slot queues.** Lint, tests and builds run on other machines, behind a queue: at most N
  heavy runs at once and only with enough free memory. A run that waits is normal, not a hang.
- **Fall-over routing.** `tutti test` picks the first fit machine: the quiet build box first, a shared machine only
  when the first is full and the shared one is idle enough. See [docs/MACHINES.md](docs/MACHINES.md).
- **Usage pacing.** Spend every quota fully about 3 hours before its reset. Not earlier (work stalls), not later (quota
  is wasted). `tutti usage` says whether each account is ahead, behind or on pace, and what to do. See
  [docs/USAGE-PACING.md](docs/USAGE-PACING.md).
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

Re-run the same line to update. Uninstall with `install/uninstall.sh` or `install/uninstall.ps1` the same way (config is kept unless `TUTTI_PURGE=1`). Edit the example config the installer wrote under `~/.config/tutti/config.yaml`, then run `tutti config` and `tutti workers`.

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
`~/.config/tutti/config.yaml`. Nothing personal is built in: hosts, paths and accounts live only in that file. It never
holds a secret.

## The CLI

| Command | What it does |
| --- | --- |
| `tutti test <track> '<cmd>'` | Copies the track's worktree to the first fit machine and runs the command there behind the slot queue, in a memory-capped container or plain bash. `--on <machine>` skips routing; `--dir` names the tree. Returns the command's exit code. |
| `tutti ship "<message>"` | In the worktree you stand in: format, commit (with your trailer), run the targeted checks, push, deploy. `--full` runs the full checks; `--dry-run` prints the plan; `--track <t>` refuses any other worktree. |
| `tutti usage` | Every account against its pace. `tutti usage log <account> <percent>` records a reading. |
| `tutti workers` | The workers and whether each is ready. |
| `tutti run <worker> "<task>" [files]` | Briefs a CLI worker in a worktree (`--dir`); `--show` prints the brief. |
| `tutti ask <worker> "<question>" [files]` | A short answer from a local model about files or stdin. |
| `tutti code <worker> <file> "<instruction>"` | A local model rewrites one file; you review `git diff`. |
| `tutti agent <worker> "<task>" "<check>" files...` | A local model edits, runs the check, reads the failure, retries. |
| `tutti yield on\|off\|status --machine <m>` | Raises or lowers a machine's yield flag (`--unload` asks for models to be unloaded). |
| `tutti machines` | Probes every machine. |
| `tutti quiet [--summary <worker>] <cmd>` | Runs a command, keeps the log, prints only the exit code and, on failure, a five-line summary. |
| `tutti guard` | Pauses test containers when memory runs low; resumes them later. |
| `tutti tunnel start\|stop\|status <worker>` | The self-healing ssh tunnel to a worker's model server. |
| `tutti wait pid\|file\|grep <target> [regex]` | Waits that end: always with `--timeout`. |
| `tutti translate <source.json> <locale>` | Translation with mandatory QA; `--check <out.json>` is the import gate. |

## Documents

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the pieces and how a run flows.
- [docs/WORKERS.md](docs/WORKERS.md): briefing Cursor, Antigravity and local models; review the diff, then gate.
- [docs/USAGE-PACING.md](docs/USAGE-PACING.md): the pacing rule and its arithmetic.
- [docs/MACHINES.md](docs/MACHINES.md): slots, routing, heat and memory guards, the yield flag, peers.
- [docs/LESSONS.md](docs/LESSONS.md): what went wrong, and the rule each failure left behind.
- [docs/ROADMAP.md](docs/ROADMAP.md) and [docs/FEEDBACK.md](docs/FEEDBACK.md): what is next; where first users log problems.
- [MIGRATION.md](MIGRATION.md): moving a folder of hand-rolled scripts over to Tutti.
- [AGENTS.md](AGENTS.md), [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md).

## Checks

```bash
bash scripts/check.sh    # shellcheck, ruff, the unit tests and a CLI smoke test: what CI runs
```
