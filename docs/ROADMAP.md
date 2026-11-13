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
`tutti brief` should classify a task (bounded / multi-file / cross-platform design / security / judgement) and pick the worker: local model or agy/cursor for bounded work, Claude (default model; Fable only on explicit owner opt-in) for design and judgement, splitting design-first tasks into cheap sub-tasks. A UI such as Orca or Emdash only hosts the sessions; it does not decide who works.
