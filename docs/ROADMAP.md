# Roadmap

v0.1.0 is the extraction of one project's working toolkit. The backlog is fed by [FEEDBACK.md](FEEDBACK.md).

## Next (0.2)

- `tutti test`: a full run on a `wsl` machine verified end to end (the probe is verified; the copy and run are ported
  but not yet exercised by a second user).
- `tutti usage`: readers for accounts that expose usage (a command or an API), so fewer readings are typed.
- `tutti translate`: the rest of the original pipeline: several streams sharing one queue (`flock` claims), a second
  judge on a larger model for strings the first judge doubted, a per-locale glossary and register, locale punctuation
  fixes, catalogue formats beyond flat JSON.
- `tutti worktree add|remove <track>`: create and clean worktrees, so a brief can name one.
- `tutti lock <name> -- <cmd>`: named locks for devices and other things peers share.
- A `limits` section in the config that renders the shared limits file for peers.

## Later

- Heat guard (CPU and GPU temperature) beside the memory guard.
- A machine-readable `--json` output for `machines`, `usage` and `workers`.
- macOS support for the orchestrator's side (the machines stay Linux).
- Publishing: choose the licence, flip `private` in `package.json`, release on npm as `tutti-agents`.

## Not planned

- A daemon, a web UI or a hosted service. State stays in files.
- Storing or brokering credentials of any kind.

## Worker selection by task (owner, 2026-11-14)
`tutti brief` should classify a task (bounded / multi-file / cross-platform design / security / judgement) and pick the worker: local model or agy/cursor for bounded work, Claude (default model; Fable only on explicit owner opt-in) for design and judgement, splitting design-first tasks into cheap sub-tasks. It also sets the effort: low by default, medium for multi-file work, high for design, security or hard debugging (owner, 2026-11-14). A UI such as Orca or Emdash only hosts the sessions; it does not decide who works.

## `tutti setup`: a wizard that does everything (owner, 2026-11-14, top priority)
Minimum input from the user. `tutti setup` detects and installs what is missing and asks only for the steps a person must do (sign-ins), one at a time, with the exact command or click:
- Workers: install/verify Cursor CLI (`cursor-agent`), Google Antigravity (`agy`), Ollama + the best local coding/translation models that fit the GPU; run each one's login and verify with a 1-line test prompt.
- Permissions: write the allow-rules each tool needs, merged safely (never replacing a file): Claude Code `permissions.allow` for `cursor-agent`/`agy`; Antigravity `permissions.allow` for the read-only commands it needs headless (and detect the exact command it was denied from its `cli.log`, then offer that rule). Show the diff and ask once.
- Machines: add test boxes over SSH (copy the key with the user's OK), detect cores/memory/GPU/NPU, set slot counts, install Docker/WSL where missing.
- Accounts and pacing: read plan limits and reset times where an API exists (Claude `get_usage`), ask once for the others (Cursor, Antigravity) and store them.
- Peers: discover other sessions (Claude Code cross-session messaging) and the shared-machine yield flag.
- Re-runnable and idempotent; `tutti doctor` checks the same things and prints one fix line per problem.
Lessons that drove it: LESSONS.md (Gemini CLI free tier closed → agy; agy headless needs per-command allow rules; Claude Code needs allow rules to start other CLIs).

## An excellent TUI now, a GUI next (owner, 2026-11-14)
- **TUI (`tutti` with no arguments, or `tutti ui`)**: one live screen — workers (cursor/agy/local/Claude sessions) with their task and state, machines with slots/load/heat/GPU, the test queue, accounts with % used and the pace line to "1 h before reset", peers and yield flags, recent ship/test verdicts; keys to start/stop a task, open its log, review its diff, yield a machine. Keyboard-first, mouse optional, works over SSH, colour + no-colour, narrow terminals. Candidate library: Textual (Python, MIT) — fits the Python CLI.
- **GUI (after the TUI; planned right after the 2026-11-14 reset)**: the same model served locally (web UI on localhost, phone-friendly), sharing the TUI's data layer; evaluate embedding or integrating Orca/Emdash for the worktree/session view instead of rebuilding it (docs/COMPETITORS.md).

## Continuous pacing after every reset (owner, 2026-11-14)
`tutti pace` runs on its own (a timer or the TUI loop) from each account's reset: target = a steady, even burn so every quota hits ~100 % exactly 1 h before its next reset (Claude: Saturday 03:00 local; Cursor/agy: their monthly days). Every check compares actual % with the straight line and acts: behind → start queued work, more parallel workers, raise worker effort where useful; ahead → pause low-priority agents, move work to cheaper workers or local models, lower effort. It shares the plan with peer sessions (50/50 Claude split with the second project) and logs each decision. It never stalls work before the target and never leaves quota unused at the reset.
- Antigravity ($20 plan) has rolling 5-hour limits, two separate ones: Claude models and Gemini models (owner, 2026-11-14). `tutti pace` models each as its own window (5 h, rolling), routes agy work to whichever model family has room (`agy --model`), and paces both so neither sits unused for long; accounts can have several windows (weekly, monthly, 5 h) and pacing respects all of them.

## Sharing with another agent session, the easy way (owner, 2026-11-14)
`tutti peers`: find other Claude Code sessions (cross-session messaging / Remote Control), pair once, then share plans, quota splits, machine yields and QA findings as structured messages instead of hand-written ones: `tutti share plan|usage|finding|yield`, an inbox in the TUI, and GitHub issues/PRs as the durable channel. Modern defaults throughout (2026): zero-config discovery, one-line installs, sane defaults, no copy-paste setups, JSON output for every command, shell completions, `--help` with examples.
- Effort coordination (owner, 2026-11-14): every task Tutti starts gets an effort level chosen by task type (low default; medium multi-file/verification; high design/security/hard debugging; higher only on request), adjusted by pacing (behind the line → higher effort where it improves results; ahead → lower), and shared with peers so both sessions follow the same policy. Logged per task.

## Backlog gathered from the first project's history (before the name "Tutti", 2026-11-12..14)
- Machine guards: memory guard (pause containers under a free-memory floor), heat guard (NUC hit 104 °C: lower slots when hot), per-machine slot counts changed safely (queued waiters keep the old count), never an uncapped container.
- Test routing: remote slots with fall-over to the next machine, a priority lane for urgent work (the "third slot" case) that an agent may use without tripping safety checks, idle-machine discovery.
- Waiting: never `pgrep -f`/`pkill -f` from a shell holding the pattern; wait on PIDs, done-files or log lines, always with a timeout (three agents stalled for hours).
- Stall detection: flag agents idle while "waiting" on a run that no longer exists; nudge or restart them.
- Ship: commit/push from the caller's worktree, fast-forward only, never deploy an older commit; conflict-marker guard before any push.
- Translation: the full ltrans pipeline (two translation streams, laptop + GPU QA, judge2, glossary, CJK sentence rules, owner-approved exceptions, a yield file for another project's GPU use) instead of the lean port.
- Shared GPU etiquette: the other project's flag pauses our model use and workers; unload models on request.
- Remote machines run by another agent: ask that agent (peer message) for admin actions instead of the user.
- Browser agents: check sign-in state themselves, never sign in, open tabs from a signed-in tab, close what they opened.
- Logins helper: one-liners to give the user for each CLI login and permission rule, verified afterwards.
- Best local models for coding and translation that fit each GPU, not the cheapest; GPU/NPU for all vendors.
- Adopt from the competitor review: Claude Code cross-session messaging for peers, `ccusage` as a usage reader, an Orca/Emdash trial for the session UI, Jules as a worker if the plan includes it, Cline/Kilo/Aider + Ollama to replace the hand-written local agent loop.
- Living plan: keep every request in a plan file and update it continuously; memory notes for standing rules.
