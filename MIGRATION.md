# Migrating a folder of hand-rolled scripts to Tutti

Tutti was extracted from such a folder. Its first user switches like this; the same steps fit any project.

## 1. Install and configure

```bash
ln -s ~/Projects/tutti/bin/tutti ~/.local/bin/tutti
```

Write `tutti.config.yaml` in the folder that holds your worktrees (Tutti finds it from any worktree below), and link
it as `~/.config/tutti/config.yaml` so the main checkout finds it too. Keep it **out of the Tutti repository**: it
names your hosts and paths. Then `tutti config` and `tutti machines`.

To share queues with the old scripts while both are in use, give each machine the old lock files: `lock_prefix`
(for `<prefix><n>.lock`) or an explicit `locks:` list. Old and new callers then wait in one queue.

## 2. Replace the commands

| Old script | Tutti | Notes |
| --- | --- | --- |
| `<machine>-test.sh <track> '<cmd>'`, the "any machine" router | `tutti test <track> '<cmd>'` | Same copy (rsync + shallow git), same caps, routing from `routing.order`. |
| the one-local-job script | `tutti test --on <local machine> <track> '<cmd>'` | A `host: local` machine with one slot. |
| `ship.sh "<msg>"`, `FULL=1 ship.sh` | `tutti ship "<msg>"`, `tutti ship --full "<msg>"` | Keep project-specific steps (format, targeted check, deploy) as small scripts named in `ship:`. `NOADD=1` is `--no-add`. |
| a usage table kept by hand | `tutti usage`, `tutti usage log <account> <percent>` | Accounts and reset times in the config; readings in a JSON-lines log. |
| ask / rewrite / agent-loop scripts for local models | `tutti ask`, `tutti code`, `tutti agent` | Models and hosts from `workers:`. |
| quiet runner, memory guard, tunnel loop | `tutti quiet`, `tutti guard`, `tutti tunnel` | |
| a "machine is busy" marker file | `yield_file` of the machine; `tutti yield` | Point `yield_file` at the marker the other side already writes. |

## 3. Switch, then retire

1. Run one real `tutti test` per machine and one `tutti ship --dry-run` per kind of worktree.
2. Change the agent instructions (AGENTS.md, briefs, memory notes) to name the `tutti` commands.
3. Turn each old script into a one-line shim that calls `tutti` and says so on stderr, so stale briefs keep working.
4. After a week without a shim being hit, delete the shims.

Log every problem in [docs/FEEDBACK.md](docs/FEEDBACK.md). Improvements go into Tutti first, then into the project.
