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
                            +-- ask, code, agent, translate    local.py, translate.py: Ollama's HTTP API
                            +-- yield, machines, guard, tunnel, quiet, wait
```

## Modules

| File | Owns |
| --- | --- |
| `tutti/config.py` | Finding, loading, defaulting and validating the config. Every error names its key and the fix. |
| `tutti/routing.py` | Pure fall-over logic over `Probe`s. No I/O, so it is unit-tested exhaustively. |
| `tutti/remote.py` | Transports (`local`, `ssh`, `wsl`), the probe, the tree copy, the runner line, the yield flag. |
| `tutti/pacing.py` | Quota windows, the target time, the verdict and the report. Pure apart from the log file. |
| `tutti/workers.py` | The brief (task, files, project rules, standing rules) and CLI workers. |
| `tutti/local.py` | Local models: ask, rewrite one file, the edit-check-retry loop, yield waits, busy markers. |
| `tutti/translate.py` | Translation with mandatory QA and the import gate. |
| `tutti/ship.py` | The ship sequence and its refusals (wrong worktree, protected branch). |
| `tutti/waiting.py` | Waits on a pid, a file or a log line, always under a timeout. |
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
- `<project.state>/tunnel-<worker>.pid|.log`, `quiet/*.log`, `ship-*.log`.
- On each machine: `<root>/<track>/` (the copy), `<root>/repo.git`, `<root>/tutti-slot.sh`, the slot lock files and the
  yield flag file.

## Design rules

- **Config, not code.** No host, path, account or model name is built in.
- **No secrets.** Tutti never reads, stores or sends a credential; workers sign in by themselves, once, by a person.
- **Refuse early, say why.** Every refusal names what happened, why, and the next step.
- **Pure core.** Routing, pacing and validation are pure functions; I/O sits at the edge and is thin.
