# The autopilot

When an account is behind its pace, `tutti usage` says "spending too slowly … about 22 bounded task(s) can go to agy now".
The autopilot acts on that by itself: it takes bounded tasks from a backlog and hands them to that account's worker, in
worktrees, with each task's own acceptance check. A pass becomes a branch (and a draft pull request when the repository
asks for one); with `auto_review` (on by default) the cheap reviewer reads it and it lands by the repo's `land` policy. With
`fill_room` (on by default) free room never waits for a person to add tasks: the autopilot queues them from the repos.

## The backlog

One queue in `<state>/backlog.json`, a task per line of work:

| Field | What it is |
| --- | --- |
| `title` | What to change, in one sentence the worker gets as its task. |
| `repo` | A name under `autopilot.repos`. |
| `scope` | The paths it may change (`--scope`, repeatable); empty means the worker decides. |
| `check` | The acceptance check, run from the repository root; default the repo's `check`. |
| `size` | `S`, `M` or `L`: 1, 2 or 3 bounded-task units of an account's room. |
| `source` | `feedback`, `todo`, `gaps`, `orchestrator`, `sentry` (a scout's Sentry triage) or `insight` (self-improvement's local analysis). |
| `status` | `candidate` (suggested; waits for `tutti backlog accept <id>` only while `self_improve.auto_accept` or self-improvement is off), `queued`, `running`, `review`, `needs-person`, `done` or `dropped`. |
| `attempts` | Every run: the step (account or worker), pass or fail, the failure's signature and words, its diff stat and output tail. |
| `notes` | The reviewer's notes; every later attempt gets them. |

```bash
tutti backlog add <repo> "<title>" --scope <path> --check "<command>" --size S
tutti backlog list [--all]
tutti backlog seed <repo>                 # the repo's safe sources, each once
tutti backlog retry <id> --note "<what to change>"   # back to the queue, a needs-person or review task too
tutti backlog done <id>                   # accepted (you merged it)
tutti backlog rm <id>                     # dropped, kept for the record
tutti backlog accept <id>                 # a candidate (suggested by a scout's Sentry triage) joins the queue
```

Tasks from self-improvement (`sentry`, `insight`) get a ready pull request instead of a branch, to the project's
`self_improve.base` unless the project may merge; the rest of their way (reviews, merge, release) is
[TELEMETRY.md](TELEMETRY.md#self-improvement).

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
  a reviewer's `retry` of a passed task stays on its step. A step that has no room now is skipped upwards; when no step
  above has room, the retry falls back to the steps below, strongest first (0.1.23: before, retries waited behind a
  paced cursor and a yielding PC while agy-gemini and agy-claude had room).
  An account outside `autopilot.accounts` takes one task at a time and only while it is not ahead of its pace; a worker
  without an account (a local model) one at a time, never while its machine is paused (yield flag) or offline, and only for a task whose scope names files (it edits only those). The
  ladder names accounts, not a worker that accounts share (`agy-claude`, not `agy`), so each step's pace, panic and model
  apply; the run is logged against that account.
- **It is parked when it makes no progress.** The same failure signature `no_progress` times in a row (default 3) on the
  strongest step, or `max_attempts` attempts (default 12), parks the task as **needs a person** with a summary on the
  dashboard. The signature is a hash of how it failed (worker exit, refused command, no change, or the failing check's last
  lines with numbers masked), so a flaky number does not count as progress.

When nothing is sent, the stopped line names **every** step: why it has no room, or, when it has room, why no queued task
fits it ("agy-claude: room for 1, but the queued tasks need 3", "… no queued task may go there").

## Filling free room (`fill_room`)

On by default; `autopilot.fill_room: false`, `tutti autopilot fill off` or the **fill_room** switch under the dashboard's
Opt-in turns it off. Each round, while the autopilot is on:

1. **The room** is the units the trigger accounts (`autopilot.accounts`) may take now, as `step_room` gives it: the
   planner's allowance for agy's 5 h and weekly windows, never an account that is ahead, in panic or spent. Cursor and the
   other ladder steps take retries only, so filling never pushes them past their pace.
2. **The want** is that room capped at three fresh tasks per parallel run (`max_parallel × 3`), less the fresh tasks
   already queued: the queue never floods, and never runs dry.
3. **The sources** (every repo of `autopilot.repos`, read at most every 10 minutes while nothing new turns up): FEEDBACK.md
   open items, `TODO(owner)` / `FIXME(owner)`, the repo's `gaps` command (lint findings, coverage gaps, Sentry or scout
   findings: one per line, `<path>: <what>`), and the tracked Python, TypeScript and JavaScript files no test names or imports ("Add unit tests for
   `<path>`"; a test imports it when an import, from or require line names it; built folders (`built:`, default dist, build,
   cdn, vendor, third_party, node_modules, generated, coverage), minified and content-hashed bundles skipped; `untested: false` turns it off for a repo whose
   coverage gate already proves every file tested). A TODO in prose, in quotes or in a test is not a task. A title the backlog ever had (dropped and done ones too) is never queued again; repos take turns.

The panel and each trigger account's card say what it did: "Autopilot: filling 46 slots — 1 running here, 6 queued · queued 4
new task(s) from the repos' sources". With the autopilot or `fill_room` off, the card shows "46 bounded tasks fit here now"
and **Let autopilot fill it**, which turns both on.

## Results land by themselves (`auto_review`)

On by default. One result at a time, `tutti autopilot land <id>` runs in the background:

1. **The repo's gates are required.** Each dispatch records the repo's `check` (its lint and type gates) that the
   attempt's acceptance runs with the task's own check; a result lands only when its passing attempt ran today's gates.
   Otherwise it goes back to the queue to run them; a repo without a `check` never lands by itself.
2. **Rebased first.** The branch is rebased onto `base` (default: the branch the repo's checkout is on) fetched from
   `origin`. A failed fetch waits 10 minutes and tries again (never lands on a stale base); a conflict is aborted and
   the task needs a person.
3. **The reviewer** is the first `review.fallback` worker with room (the path `tutti pr review` falls back to); it reads the
   whole branch (`git diff <base>...HEAD`) in an empty folder with `review.PROMPT`. "No findings" is clean; bullets are
   findings. A reviewer that cannot run is tried again in 10 minutes, not every round.
4. **Findings** send the task back to the queue with them as notes (same step, same worktree); the third time it needs a person.
5. **Clean** lands by `autopilot.repos.<repo>.land`: pushed, a pull request opened (a draft is marked ready; a failure says
   so and keeps the task). `merge` then squash-merges it and deletes the remote branch (a non-zero exit is checked against
   the PR's state first); `pr` (the default) leaves it for a person; `branch` never auto-reviews. Auto-merge is for the
   orchestration toolkit's own repo; product repos take `pr`.

The repos' sources are read before the backlog's lock is taken, and every source command has a 120 s timeout, so a slow
`gaps` command never blocks the backlog.

## Config

```yaml
autopilot:
  enabled: false                       # the dashboard's toggle (or `tutti autopilot on|off`) overrides it
  accounts: [agy-claude, agy-gemini]   # their "behind" pace triggers fresh tasks
  ladder: [agy-gemini, agy-claude, cursor, pc]   # retries climb it; accounts or workers; never one that costs money
  max_parallel: 2
  no_progress: 3
  max_attempts: 12
  quiet_hours: []                      # UTC spans it must not run, e.g. ['22:00-06:00']; the planner moves their share
  fill_room: true                      # free room on autopilot.accounts is filled from the repos' sources
  auto_review: true                    # results are reviewed by review.fallback and landed by their repo's `land`
  repos:
    tutti:
      path: ~/Projects/tutti
      check: bash scripts/check.sh     # the repo's gates: run with every task's own check, both must pass
      where: local                     # or route: the check runs on a routed machine (the runner image)
      pr: false                        # or draft: push the branch and open a draft pull request
      commit: 'chore(autopilot): {title}'
      trailers: []                     # lines added to the commit message
      gaps: ''                         # a command printing one gap per line, read by `tutti backlog seed` and fill_room
      land: pr                         # after a clean review: pr (a person merges; the default), merge (this toolkit's own repo only) or branch
      base: main                       # the branch it lands on (default: the branch the repo's checkout is on)
```

`tutti config` names every wrong key: an unknown account or worker, one that costs money, a repo without `path`.

**Acceptance is the task's check and the repo's gates.** A result passes only when the task's own check (its new tests)
and the repo's `check` (its lint and type gates of the changed files) both pass: `( <task check> ) && ( <repo check> )`.
Before 0.1.16 a task's own check replaced the repo's, and results whose new tests passed were marked passing while they
broke ruff, mypy and the scenario registry (LESSONS). A failure of either feeds the retry loop like any other.

**Room comes from the planner.** A trigger account whose windows sit under a weekly limit gets as many tasks as the
planner's allowance holds (docs/USAGE-PACING.md, *The planner*), whatever its own verdict: a 5 h window's quota is spent
before it expires, as far as the weekly allows. `autopilot.quiet_hours` lists UTC spans it must not run.

## The panel

**Autopilot** on the dashboard: the toggle, the ladder, `max_parallel`, whether it fills free room and reviews results, the last decision and its reason, then the runs
going (step, elapsed time, credits when the CLI prints them, the last log line), the queue and the latest decisions (four,
then **Show N more**, in a box that scrolls). Results to review and tasks that need a person are in **Needs you**. Every task
shows its attempt count and its failure history (step, why, run, signature).
