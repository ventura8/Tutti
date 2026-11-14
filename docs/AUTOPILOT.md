# The autopilot

When an account is behind its pace, `tutti usage` says "spending too slowly … about 22 bounded task(s) can go to agy now".
The autopilot acts on that by itself: it takes bounded tasks from a backlog and hands them to that account's worker, in
worktrees, with each task's own acceptance check. Nothing it makes is ever merged: a pass becomes a branch (and a draft pull
request when the repository asks for one) that a person or the orchestrator reviews.

## The backlog

One queue in `<state>/backlog.json`, a task per line of work:

| Field | What it is |
| --- | --- |
| `title` | What to change, in one sentence the worker gets as its task. |
| `repo` | A name under `autopilot.repos`. |
| `scope` | The paths it may change (`--scope`, repeatable); empty means the worker decides. |
| `check` | The acceptance check, run from the repository root; default the repo's `check`. |
| `size` | `S`, `M` or `L`: 1, 2 or 3 bounded-task units of an account's room. |
| `source` | `feedback`, `todo`, `gaps` or `orchestrator`. |
| `status` | `queued`, `running`, `review`, `needs-person`, `done` or `dropped`. |
| `attempts` | Every run: the step (account or worker), pass or fail, the failure's signature and words, its diff stat and output tail. |
| `notes` | The reviewer's notes; every later attempt gets them. |

```bash
tutti backlog add <repo> "<title>" --scope <path> --check "<command>" --size S
tutti backlog list [--all]
tutti backlog seed <repo>                 # the repo's safe sources, each once
tutti backlog retry <id> --note "<what to change>"   # back to the queue, a needs-person or review task too
tutti backlog done <id>                   # accepted (you merged it)
tutti backlog rm <id>                     # dropped, kept for the record
```

`seed` reads only sources that are safe to automate: the open items of the repo's `docs/FEEDBACK.md` (table rows whose status
starts with "Open", and the dated notes under the table), `TODO(owner):` and `FIXME(owner):` comments (an owner tag means a
person meant it), and the lines the repo's `gaps` command prints (`<path>: <what is missing>`, e.g. a coverage or lint report).

## A round

The dashboard runs one round after every probe while the autopilot is on (`tutti autopilot tick` runs one by hand):

1. **Collect.** Every running task whose run ended gets an attempt. A pass is published: Tutti (never the worker) commits the
   worktree on `autopilot/<id>-<slug>` with `autopilot.repos.<repo>.commit` (default `chore(autopilot): {title}`) and the
   repo's `trailers` (a failed checkout or staging stops there and says so), and with `pr: draft` pushes it and opens a draft pull request. The task waits in **Results to review**.
2. **Plan.** Capacity is `max_parallel` minus the runs going, and at most the free slots of machines that are fit and not hot
   (no machines configured: the checks run here). Fresh tasks go only to an account of `autopilot.accounts` that is
   **behind** its line (or late), as many units as its room (`room_tasks`) minus what runs on it and what attempts started
   after its newest reading already spent (that cost stays counted until a new reading shows it). Never an account that
   costs money, is in panic or is spent. A task without an acceptance check (its own or the repo's) is never sent.
3. **Dispatch.** `tutti run <worker> "<brief>" --repo <path> --track ap-<id> --check "<check>"` in the background, with the
   account's `args` (its model) and its name. Each start is saved at once, so a start that fails leaves the task queued and a
   crash never starts it twice. A local-model worker works through `tutti agent`'s loop on the task's scope files (it may
   create them). A running task cannot be retried, marked done or removed until its run ends.

It stops dispatching, and the panel says why, when the backlog is empty ("backlog empty — add tasks"), when every account is
at or over its even-pace line, in panic or spent, when no machine has a cool free slot, or when it is turned off. Runs already
going finish.

## Failures fix themselves

A failed attempt goes back to the queue; it is never dropped and never retried blindly:

- **It continues from the same worktree** (`ap-<id>`): the next worker sees the earlier changes and is told to continue.
- **Its brief carries what went wrong**: the last attempt's diff stat, the end of the failing check's output, the earlier
  failures one line each, and the reviewer's notes.
- **It climbs the ladder.** A retry starts one step above the step its last attempt failed on in `autopilot.ladder`
  (default: `autopilot.accounts`), e.g. `agy-gemini → agy-claude → cursor → pc` (a local model); the last step repeats, and
  a reviewer's `retry` of a passed task stays on its step. A step that has no room now is skipped upwards, never downwards.
  An account outside `autopilot.accounts` takes one task at a time and only while it is not ahead of its pace; a worker
  without an account (a local model) one at a time, and only for a task whose scope names files (it edits only those). The
  ladder names accounts, not a worker that accounts share (`agy-claude`, not `agy`), so each step's pace, panic and model
  apply; the run is logged against that account.
- **It is parked when it makes no progress.** The same failure signature `no_progress` times in a row (default 3) on the
  strongest step, or `max_attempts` attempts (default 12), parks the task as **needs a person** with a summary on the
  dashboard. The signature is a hash of how it failed (worker exit, refused command, no change, or the failing check's last
  lines with numbers masked), so a flaky number does not count as progress.

## Config

```yaml
autopilot:
  enabled: false                       # the dashboard's toggle (or `tutti autopilot on|off`) overrides it
  accounts: [agy-claude, agy-gemini]   # their "behind" pace triggers fresh tasks
  ladder: [agy-gemini, agy-claude, cursor, pc]   # retries climb it; accounts or workers; never one that costs money
  max_parallel: 2
  no_progress: 3
  max_attempts: 12
  repos:
    tutti:
      path: ~/Projects/tutti
      check: bash scripts/check.sh     # the default acceptance check of its tasks
      where: local                     # or route: the check runs on a routed machine (the runner image)
      pr: false                        # or draft: push the branch and open a draft pull request
      commit: 'chore(autopilot): {title}'
      trailers: []                     # lines added to the commit message
      gaps: ''                         # a command printing one gap per line, read by `tutti backlog seed`
```

`tutti config` names every wrong key: an unknown account or worker, one that costs money, a repo without `path`.

## The panel

**Autopilot** on the dashboard: the toggle, the ladder and `max_parallel`, the last decision and its reason, then the runs
going (step, elapsed time, credits when the CLI prints them, the last log line), the results to review (branch, pull request,
or why the commit or push was refused), what needs a person, and the queue. Every task shows its attempt count and its
failure history (step, why, run, signature).
