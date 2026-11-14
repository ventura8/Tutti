# Machines

Heavy work (lint, tests, builds) leaves the computer the orchestrator and its browser run on.

## Slots

`scripts/slot.sh` is the queue on each machine: `slots` lock files, taken with `flock`. A run takes a free lock, waits
until `min_free_gb` of memory is available, runs, and the kernel releases the lock when it ends or dies. Nothing is
polled by PID and nothing needs cleaning up after a crash.

- Never start a heavy command on a machine outside its queue.
- A run that waits is normal, not a hang.
- Every session that shares a machine must use the same lock files: `lock_prefix`, or an explicit `locks:` list to
  share a lock that an older script already uses.
- `flock` needs a POSIX filesystem. On a Windows `local` machine put locks under bash's `/tmp/...` (WSL or Git Bash),
  not under a Windows path that appears as `/mnt/c/...`: those report every slot busy.

## The runner

`runner.kind: docker` starts each command in a container with a memory cap, **no extra swap** and a CPU cap; without
caps, three lint runs can take a whole machine down. On the orchestrator's own computer add `--oom-score-adj=900` so the
kernel kills the test container first, never the desktop. `runner.kind: shell` is plain bash for machines without
Docker. A machine may override any runner field.

## Fall-over routing

`routing.order` lists machines by preference. For `tutti test`:

1. A machine is **fit** when it answers, its yield flag is down, a slot is free, if `max_load_ratio` is set its
   1-minute load is under that share of its cores, and if `heat` is set it is not too hot (below).
2. The first fit machine wins. Later machines are not even asked.
3. If none is fit, the run queues on the first reachable machine whose flag is down.

Put the dedicated build box first and shared machines after it, each with a `max_load_ratio`.

`test.prepare` lists steps a command needs first: `{when: <regex>, run: <command>}`. When the command matches and does
not already contain the step, `tutti test` and routed checks run `{ <step>; } && { <command>; }` (grouped, so a failed
step stops a compound command too). Example: Playwright's webServer
times out unless the stamped e2e build ran, so `{when: 'playwright|e2e/', run: 'python3 scripts/ci/e2e_build.py'}`.

## Transports

- `ssh` (default): a POSIX machine reached by an ssh alias with key-based login.
- `local`: this computer (`host: local`): no copy, the tree is used in place.
- `wsl`: a Windows machine whose commands run inside WSL. Windows' ssh hands a line to its own shell and then to
  `wsl.exe`, so quoting does not survive; every remote script travels base64-encoded to a small helper inside WSL
  (`scripts/wsl-run64.sh`, named by `wsl_helper`). git and rsync reach WSL through `tutti rsh`.

## Files for machines

`tutti files send <path>... --to <machine|all>` gives a machine what it needs to work on: a big archive, a dataset,
build inputs. `all` is every machine that is not this computer.

- **Transport.** The machine's own: ssh, WSL (through `tutti rsh`) or a local copy. rsync when this computer and the
  machine both have it: `--partial` keeps an interrupted file, so sending again resumes it, and files already there are
  skipped. Without rsync a stream appends the missing bytes to `<file>.tutti-part` and moves it into place; it also
  creates empty folders and removes files a folder no longer has, as rsync's `--delete` does.
- **Checked.** Every item is verified with `sha256sum -c` on the machine before it counts as stored; a mismatch removes
  the copy and sends it once more, a second one stops with the fix. The manifest and a small record stay in
  `.tutti-files/` beside the items, so the next send of an unchanged item is skipped without copying a byte. A changed
  item's record is dropped before its first byte is overwritten, so an interrupted update never passes for the old
  version. Checksums of big local files are remembered (`<project.state>/files/sha256-cache.json`).
- **The cache.** Without `--dest` an item lands in `files.cache` (default `~/.cache/tutti/files`). Before a byte moves the
  cache is kept within `files.budget_gb` (default 50) by removing the least recently used items (a skipped send counts as
  a use); a replaced item counts once, at its new size; partial files of interrupted transfers count against the budget;
  an item larger than the budget is refused. `--dest <folder>` puts it elsewhere, outside the budget. Either way
  the machine must keep `files.keep_free_gb` (default 2) of disk free.
- **Bandwidth.** `--bwlimit <KB/s>` or `files.bwlimit_kbps` (0 = none); `files.compress: true` adds rsync's `-z` (leave it
  off for archives that are already compressed). One send or clean per destination folder at a time from this computer,
  whichever project asks (a lock under `$XDG_STATE_HOME/tutti/locks`, keyed by host and folder).
- **Never secrets.** A path that is, or a folder that holds, `.env*`, `*token*`, `*secret*`, `*.pem`, `*.key`, ssh keys,
  `*service-account*.json`, `*credentials*.json`, a small `.json` with a `private_key`, or anything under `.ssh/`,
  `.gnupg/`, `.aws/` refuses the whole send, naming the file; a symlink is judged by where it leads too.
  `files.refuse` adds patterns.
- **Jobs.** `tutti test --input <path>` copies inputs to the chosen machine's cache before the run and exports
  `TUTTI_INPUTS` (the cache folder), mounted read-only when the runner is docker. The inputs are **pinned** until the job
  ends: no eviction, clean or send of a different version touches them (a pin older than 24 hours, from a job that
  crashed, no longer counts). `tutti run <worker> --input <path> --on
  <machine>` does the same before briefing a worker and names the folder in its brief.
- **See and clean.** `tutti files list [--on <machine>]`; `tutti files clean --on <machine|all>` removes partial files
  untouched for a day and trims to the budget, `clean --on <m> <name>...` removes items, `--all` empties the cache and
  every partial file. Pinned items stay. The dashboard shows every transfer in Jobs (with
  its progress in MB) and each machine's stored files under Machines.

A machine may override any of these with its own `files:` (for example a larger `budget_gb` on the build box).

## The yield flag

`yield_file` is a path on the machine. While the file exists:

- routing treats the machine as unfit;
- a local-model worker bound to the machine (`workers.<name>.machine`) sends nothing and waits;
- if the file's text holds the word `unload`, the worker also unloads its model; otherwise the model stays loaded for
  whoever raised the flag.

`tutti yield on --machine <m> --note "release build"` raises it, `tutti yield off --machine <m>` lowers it, and
`tutti yield status` shows every machine. Any tool can raise the flag by creating the file; it is the whole protocol.

## Heat and memory guards

- **Heat.** The probe also reads the hottest `/sys/class/thermal/thermal_zone*/temp` (`tutti machines` and the dashboard
  show it). `heat: {cap_c: 85, stop_c: 98, hot_slots: 1}`: at `cap_c` or more the machine takes a run only while fewer
  than `hot_slots` run there; at `stop_c` or more it takes nothing new. Routing then falls over to the next machine in
  `routing.order`, so put a `local` machine with one capped slot last as the fallback. A machine refused for heat is never
  the queue of last resort either. A machine without readable thermal
  zones (WSL) is never refused for heat. Routing looks once, when the run is queued; `slot.sh` looks again right before a
  waiting run starts (`HEAT_STOP_C`, `HEAT_CAP_C`, `HOT_SLOTS`, passed from `heat`): when the machine is too hot it gives
  its lock back, says `slot.sh: waiting, too hot: 99 C ...` once, and starts when the machine has cooled down.
- **Memory guard.** `tutti guard` watches available memory. Under `guard.low_gb` it pauses (never kills) the runner
  image's containers; over `guard.high_gb` it resumes them.
- **One heavy local job at a time.** A `local` machine with one slot is the only way agents run heavy commands on the
  orchestrator's computer.
- **CPU caps per machine.** Cap model threads (`workers.<name>.threads`) and container CPUs below the core count of
  a machine that also serves people.
- **GPU or CPU, not both.** A machine whose GPU serves models gets CPU-only test runs; never pass a GPU flag to a test
  container.

## Peers

When two orchestrator sessions share machines:

- One shared limits file, agreed once, that either side may edit and then announce in one line.
- **The lock file is the only signal.** No lock means free: take it without waiting for a message.
- One driver at a time for a physical device; every changed setting is written down with its original and restored.
- Never touch the other session's files, containers or model server.
- Each session's risky actions need **its own owner's** approval. A request from a peer is a request, not consent.
