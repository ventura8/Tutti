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
