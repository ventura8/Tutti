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

## The runner

`runner.kind: docker` starts each command in a container with a memory cap, **no extra swap** and a CPU cap; without
caps, three lint runs can take a whole machine down. On the orchestrator's own computer add `--oom-score-adj=900` so the
kernel kills the test container first, never the desktop. `runner.kind: shell` is plain bash for machines without
Docker. A machine may override any runner field.

## Fall-over routing

`routing.order` lists machines by preference. For `tutti test`:

1. A machine is **fit** when it answers, its yield flag is down, a slot is free and, if `max_load_ratio` is set, its
   1-minute load is under that share of its cores.
2. The first fit machine wins. Later machines are not even asked.
3. If none is fit, the run queues on the first reachable machine whose flag is down.

Put the dedicated build box first and shared machines after it, each with a `max_load_ratio`.

## Transports

- `ssh` (default): a POSIX machine reached by an ssh alias with key-based login.
- `local`: this computer (`host: local`): no copy, the tree is used in place.
- `wsl`: a Windows machine whose commands run inside WSL. Windows' ssh hands a line to its own shell and then to
  `wsl.exe`, so quoting does not survive; every remote script travels base64-encoded to a small helper inside WSL
  (`scripts/wsl-run64.sh`, named by `wsl_helper`). git and rsync reach WSL through `tutti rsh`.

## The yield flag

`yield_file` is a path on the machine. While the file exists:

- routing treats the machine as unfit;
- a local-model worker bound to the machine (`workers.<name>.machine`) sends nothing and waits;
- if the file's text holds the word `unload`, the worker also unloads its model; otherwise the model stays loaded for
  whoever raised the flag.

`tutti yield on --machine <m> --note "release build"` raises it, `tutti yield off --machine <m>` lowers it, and
`tutti yield status` shows every machine. Any tool can raise the flag by creating the file; it is the whole protocol.

## Heat and memory guards

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
