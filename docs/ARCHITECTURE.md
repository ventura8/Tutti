# Architecture

Tutti is a thin layer: a Python package (standard library plus PyYAML) behind one command, and a few bash scripts that
run on the machines. There is no daemon and no server; state is files.

```text
                         tutti.config.yaml  (machines, workers, accounts, ship steps)
                                  |
  orchestrator  ---->  bin/tutti (tutti/cli.py)
   (an agent                |
    or a person)            +-- test     routing.py -> remote.py -> ssh/WSL/local -> scripts/slot.sh -> runner
                            +-- ship     ship.py: format, commit, checks (local or routed), push, deploy
                            +-- usage    pacing.py: windows, targets, verdicts; a JSON-lines log
                            +-- run      workers.py: the brief, then cursor-agent / agy / claude
                            |            delegate.py (--repo): worktree, headless worker, diff, routed checks, report
                            +-- pr       review.py: request reviewers, detect none coming, local fallback review via gh
                            +-- ask, code, agent, translate    local.py, translate.py: Ollama's HTTP API
                            +-- yield, machines, guard, tunnel, quiet, wait
```

## Modules

| File | Owns |
| --- | --- |
| `tutti/config.py` | Finding (`$TUTTI_CONFIG`, nearest file, then `$XDG_CONFIG_HOME/tutti/config.yaml`), loading, defaulting and validating. Every error names its key and the fix. |
| `tutti/routing.py` | Pure fall-over logic over `Probe`s. No I/O, so it is unit-tested exhaustively. |
| `tutti/remote.py` | Transports (`local`, `ssh`, `wsl`), the probe, the tree copy, the runner line, the yield flag. |
| `tutti/pacing.py` | Quota windows, the target time, the verdict and the report. Pure apart from the log file. |
| `tutti/workers.py` | The brief (task, files, project rules, standing rules) and CLI workers. |
| `tutti/local.py` | Local models: ask, rewrite one file, the edit-check-retry loop, yield waits, busy markers. |
| `tutti/translate.py` | Translation with mandatory QA and the import gate. |
| `tutti/ship.py` | The ship sequence and its refusals (wrong worktree, protected branch). |
| `tutti/waiting.py` | Waits on a pid, a file or a log line, always under a timeout. |
| `tutti/files.py` | Files for other machines, the pure part: the secret check, item manifests (SHA-256), the cache's least-recently-used eviction, progress lines. |
| `tutti/transfer.py` | `tutti files` and `--input`: copies items over the machine's transport (rsync, else a resumable stream), verifies them with `sha256sum` there, records them in the machine's cache and keeps it within its budget. |
| `tutti/autopilot.py` | `tutti autopilot`: the pure plan (pace, room, heat, `max_parallel`, the ladder), failure signatures, parking, the retry brief; dispatch, collect and publish (branch, draft pull request). |
| `tutti/backlog.py` | `tutti backlog`: the task queue in `<state>/backlog.json` under a lock, and its safe sources (FEEDBACK, `TODO(owner)`, `gaps`). |
| `tutti/conduct.py` | `tutti conduct`, `tutti main`, `tutti handoff`: the main agent's choice and order, the instruction file, the `tutti` shim on its PATH, the watch (panic, limit, switch), the handoff note, the local model's command loop. |
| `tutti/projects.py` | The dashboard's projects: a folder's repository from its `.git` files, jobs tagged by project, a read-only look at every machine's processes in git repositories and its GPU, a WSL machine's Windows side over plain ssh. |
| `tutti/state.py` | The dashboard's state as plain data: accounts and pacing, machines, workers, jobs, the summary. Probes are passed in. |
| `tutti/delegate.py` | `tutti run --repo` and `tutti runs`: the worktree, the headless worker, the diff, the checks (never stopping at the first failure), DENIED tool calls, the coverage note and the report. |
| `tutti/review.py` | `tutti pr review`: asking Copilot and CodeRabbit through `gh`, reading their answers and limits, the local fallback review posted as a comment. `gh` is passed in for tests. |
| `tutti/helper.py` | The reading helper: the per-install key, the bookmarklet page (`/helper`) and the page script for an orchestrator's browser tool; the parser both use is `tutti/web/helper-parse.js`, the bookmarklet's wrapper `tutti/web/helper-bookmarklet.js`. |
| `tutti/releases.py` | A project's open pull requests (`projects.<n>.repo`, through gh): a release in progress first, CI and review state, cached per repository. |
| `tutti/dashboard.py`, `tutti/web/` | `tutti dashboard`: the 127.0.0.1 HTTP server, background probes, `/api/state` and the PWA shell. |
| `scripts/slot.sh` | The slot queue, copied to each machine on every run. |
| `scripts/memguard.sh`, `quiet.sh`, `tunnel.sh`, `wsl-run64.sh` | The memory guard, quiet runs, the tunnel loop, the WSL shim. |

## How `tutti test <track> '<cmd>'` flows

1. The config gives the routing order. Machines are probed lazily, in order: free slots, load, cores, yield flag.
2. The first fit machine wins. If none is fit, the run queues on the first reachable machine whose flag is down.
3. For a remote machine: the tree's `HEAD` is pushed to a bare repository there, the tree is copied with rsync
   (incremental, the config's excludes: no secrets, no build output), and the copy gets a shallow git repository at
   that commit, so tests that ask git still work.
4. The command runs behind `slot.sh`: it takes a free slot lock, waits for free memory, then starts the runner
   (a container capped in memory and CPUs, or plain bash).
5. The command's exit code comes back unchanged.

## State

- `<project.state>/usage.jsonl` (or `usage.log`): one line per usage reading.
- `<project.state>/llm-busy-<pid>`: a live local-model job; low-priority jobs wait for these to go.
- `<project.state>/tunnel-<worker>.pid|.log`, `quiet/*.log`, `ship-*.log`, each run log with its result `<log>.rc` and
  command `<log>.cmd`; `waits/<pid>.json` while a `tutti wait` runs. `tutti dashboard` reads all of these.
- On each machine: `<root>/<track>/` (the copy), `<root>/repo.git`, `<root>/tutti-slot.sh`, the slot lock files and the
  yield flag file.

## Design rules

- **Config, not code.** No host, path, account or model name is built in.
- **No secrets.** Tutti never reads, stores or sends a credential; workers sign in by themselves, once, by a person.
- **Refuse early, say why.** Every refusal names what happened, why, and the next step.
- **Pure core.** Routing, pacing and validation are pure functions; I/O sits at the edge and is thin.
