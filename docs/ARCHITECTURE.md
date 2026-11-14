# Architecture

Tutti is a thin layer: a Python package (standard library plus PyYAML) behind one command, and a few bash scripts that
run on the machines. There is no daemon; state is files. The one server besides the local dashboard is a household's
coordinator (docs/HOUSEHOLD.md), and only when several people share machines and projects.

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
| `tutti/config.py` | Finding (`$TUTTI_CONFIG`, then `$XDG_CONFIG_HOME/tutti/config.yaml`, then the nearest file), loading, defaulting and validating; `write_text` saves through a symlink into the real file, atomically. Every error names its key and the fix. |
| `tutti/configone.py` | One config per user: other copies that differ (doctor, Settings) and their merge into the one in use, each copy then a symlink to it. |
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
| `tutti/review.py` | `tutti pr review`: asking Copilot and CodeRabbit through `gh`, reading their answers and limits, skipping a reviewer whose account reads 100 % until its reset, the local fallback review posted as a comment. `gh` is passed in for tests. |
| `tutti/helper.py` | The reading helper: the per-install key, the bookmarklet page (`/helper`) and the page script for an orchestrator's browser tool; the parser both use is `tutti/web/helper-parse.js`, the bookmarklet's wrapper `tutti/web/helper-bookmarklet.js`. |
| `tutti/releases.py` | A project's open pull requests (`projects.<n>.repo`, through gh): a release in progress first, CI and review state, cached per repository. |
| `tutti/scouts.py`, `tutti/scoutcmd.py` | Scouts (helpers that are not workers, docs/SCOUTS.md): config checks, the webhook POST (the key only in its header, from an environment variable or stdin), the issue and its answer through `gh`, the pure plan paced by the scout's account, the round and the view; `scoutcmd.py` is `tutti scout`. |
| `tutti/trial.py` | A scout's review trial: asked beside `tutti pr review`, findings per reviewer, real or noise, the verdict. |
| `tutti/scrub.py` | What telemetry may keep, pure: categories only as field values; free text with home folders, hosts, IPs, e-mails, URLs, tokens, keys and quoted fragments replaced. |
| `tutti/telemetry.py`, `tutti/sentry.py`, `tutti/telemetrycmd.py` | Telemetry (docs/TELEMETRY.md): local events and their rotation, consent, the DSN, crash and failure events, the outbox and its flush; Sentry's envelope API and the custom endpoint; the CLI's entry wrapper (`cli.entry`, used by `bin/tutti`; `cli.main` records nothing) and `tutti telemetry`. |
| `tutti/insights.py` | The local events summarised, pure: most used, slowest, most failing, wasted quota, stall and park causes, crashes. |
| `tutti/triage.py`, `tutti/selfimprove.py` | Sentry triage watches per project and their `TUTTI-TASK` lines; self-improvement's pure gates (tasks, pull request base, merge, release, daily room), the local analysis, the review/merge/release round and the dashboard lane. |
| `tutti/users.py`, `tutti/householdcmd.py` | Who this Tutti works for (docs/HOUSEHOLD.md): the name, the random id in `<state>/user.json` and its hash (the only way telemetry names a user), the accounts whose spare room is shared, the membership in `~/.config/tutti/household.json` (600); `householdcmd.py` is `tutti user` and `tutti household`. |
| `tutti/household.py`, `tutti/coordinator.py`, `tutti/sharing.py` | The household (docs/HOUSEHOLD.md): the pure core (the join code, the derived token, signed requests, members, leases, shared backlogs and their dedupe); the coordinator's HTTP service and state file and the members' signed client (no proxy, a 3 s timeout, `Unreachable` means work alone); a member's round on its shared repos (one `/hh/sync` per autopilot round outside the backlog's lock, the shared tasks mirrored in, the ones it may not start held: leased by another member, or all of them while the coordinator is unreachable). |
| `tutti/mainmachine.py`, `tutti/manage.py`, `tutti/web/userstab.js` | A user's main machine: `tutti move` of their own Tutti, the server's "only yourself" check, presence while a client is connected. Acting for a member: the admin's Users tab queues an action at the coordinator (`/hh/act`, audited), the member's Tutti runs it on its round (schedule, main agent, main machine, connect, pairing, machine use, pause, update). |
| `tutti/hhjobs.py` | Compute on another member's machine with no login there: the member pushes a WIP ref and sends a signed job, the owner's Tutti claims it, runs it in a sandboxed worktree with its own `tutti test --on <m>` (a container unless `household.member_shell`) and returns the exit code and output through the coordinator. |
| `tutti/netpolicy.py` | Never on the internet: the addresses a household's coordinator, the dashboard and `tutti mcp --http` may listen on and join links may name (localhost, RFC 1918, 100.64/10, fc00::/7, WireGuard interfaces); `auto` binds; a public address or a wildcard raises `PublicAddress` with the fix. |
| `tutti/fairshare.py` | Fair shares of shared machines, pure: weighted water-filling, interactive before background, the starvation rule, hour weights from a member's 7 x 24 profile, the household plan, Jain's index; the member's grant cache and `limit` (what a shared machine leaves free for it). `sharing.limit_items` and `sharing.limited_probe` apply it to the autopilot's machines and to `tutti test`'s routing. |
| `tutti/chat.py`, `tutti/chatrun.py`, `tutti/web/chat.js` | The dashboard's Chat (docs/CHAT.md): conversations per user in `<state>/chat/` (600), requests parsed into the MCP tools' calls, the role checks, the model order, the facts and the built-in answers (pure); the answer threads, Ollama and CLI streaming, Undo, the audit lines and `POST /api/chat` (I/O); the page's safe Markdown and composer. |
| `tutti/dashboard.py`, `tutti/dashtelemetry.py`, `tutti/web/` | `tutti dashboard`: the 127.0.0.1 HTTP server, background probes, `/api/state` and the PWA shell. |
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
- `<project.state>/scouts.json`: every scout request (question, watch, review), its issue and answer; never a key or URL.
- `<project.state>/review-trial.json`: the review trial's pull requests, findings and verdict.
- `<project.state>/telemetry/`: `events.jsonl` (rotated), `consent.json` (sending yes or no, the random install id),
  `outbox.jsonl` (what waits to be sent, only with consent), `send.json` (the last send).
- `<project.state>/user.json`: this user's random id, the name and the `share_spare` switches set by `tutti user`.
- `~/.config/tutti/household.json` (600): the household membership and the user's token; absent for a household of one.
- `<project.state>/self-improve.json`: the switch, the last analysis, reviews asked, merges and releases.
- On each machine: `<root>/<track>/` (the copy), `<root>/repo.git`, `<root>/tutti-slot.sh`, the slot lock files and the
  yield flag file.

## Design rules

- **Config, not code.** No host, path, account or model name is built in.
- **No secrets.** Tutti never reads, stores or sends a credential; workers sign in by themselves, once, by a person.
- **Refuse early, say why.** Every refusal names what happened, why, and the next step.
- **Pure core.** Routing, pacing and validation are pure functions; I/O sits at the edge and is thin.
