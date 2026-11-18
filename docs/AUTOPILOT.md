# The autopilot

When an account is behind its pace, `tutti usage` says "spending too slowly … about 22 bounded task(s) can go to agy now".
The autopilot acts on that by itself: it takes bounded tasks from a backlog and hands them to that account's worker, in
worktrees, with each task's own acceptance check. A pass becomes a branch (and a draft pull request when the repository
asks for one); with `auto_review` (on by default) the cheap reviewer reads it and it lands by the repo's `land` policy. With
`fill_room` (on by default) free room never waits for a person to add tasks: the autopilot queues them from the repos.

When autopilot is off, the active dashboard continues checkpointing existing work and holds reported runs during the off
period. It does not dispatch, run the stuck watchdog, or classify and publish those reports while off. When switched on,
the existing collection behavior resumes; it does not prove a remote process stopped. This does not make an update safe;
execution-boundary proof is still needed (t404).

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
| `status` | `candidate` (suggested; waits for `tutti backlog accept <id>`; a self-improvement one, `source` sentry or insight, is queued by itself unless `self_improve.auto_accept` or self-improvement is off), `queued`, `running`, `review`, `needs-person`, `done` or `dropped`. |
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
Automatic discovery checks quiet mode before each potentially heavy gaps command and defers it when quiet; the other sources
remain available and a later eligible source read retries it. A command already started is not supervised by this check.
An explicit `tutti backlog seed` still runs the configured command.

A task whose title starts with `scout:` or `research:` is a research question for a scout (docs/SCOUTS.md, *The week's
plan*): the autopilot never sends it to a worker (`research:` joined in 0.1.59: retitle a worker's task that starts with it).

## A round

The dashboard runs one round after every probe while the autopilot is on (`tutti autopilot tick` runs one by hand):

1. **Collect.** Every running task whose run ended gets an attempt. A pass is published: Tutti (never the worker) commits the
   worktree on `autopilot/<id>-<slug>` with `autopilot.repos.<repo>.commit` (default `chore(autopilot): {title}`) and the
   repo's `trailers` (a failed checkout or staging stops there and says so), and with `pr: draft` pushes it and opens a draft pull request. The task waits in **Results to review**.
2. **Plan.** Capacity is the **adaptive limit** (`tutti/parallel.py`, below) minus the runs going. Fresh tasks first go to an account of `autopilot.accounts` that is
   **behind** its line (or late), or on pace but still under its line (topped up to it, so the window ends at its target), as many units as its room (`room_tasks`) minus what runs on it and what attempts started
   after its newest reading already spent (that cost stays counted until a new reading shows it). Never an account that
   costs money or is spent. Ordinary paced work is capped so the configured estimated task cost stays below the account's
   own panic line, including units already reserved; the estimate is not a guarantee of actual use. Linked limits remain
   governed by their planner, and the takeover and cautious panic fallback below use their separate policies.
   A task without an acceptance check (its own or the repo's) is never sent.
3. **Dispatch.** `tutti run <worker> "<brief>" --repo <path> --track ap-<id> --check "<check>"` in the background, with the
   account's `args` (its model) and its name. Each start is saved at once, so a start that fails leaves the task queued and a
   crash never starts it twice. A local-model worker works through `tutti agent`'s loop on the task's scope files (it may
   create them). A running task cannot be retried, marked done or removed until its run ends.

It stops dispatching, and the panel says why, when the backlog is empty ("backlog empty — add tasks"), when no account or local
model has usable room, when no machine has a cool free slot, or when it is turned off. Runs already
going finish.

An ahead trigger account can take a small task at a time when fresh real readings and each direct quota window's
discretionary balance cover its estimated `task_pct` cost. Linked-window costs require a measured or configured
`window_points` conversion. Related accounts' planner budgets are intersected so each owner's reserve is kept; running
or unreflected related work blocks another slow start, and one overlapping group takes at most one per planning round.
Starts are spaced at half the rate that would use the available balance by each window's target. Shared accounts are chosen
by recorded task units relative to their remaining safe task capacity. A finished or interrupted start keeps its quota
reservation until a reading taken after that run covers it. Missing, stale, future or failed readings, unknown conversions,
nested windows, a quiet period, panic, explicit pause, too little room or household-shared spare accounts stop this path.
Local models still come first when online accounts are ahead, subject to their own safety limits.


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
  While the main agent (`autopilot.main_accounts`, default `claude`) is in panic or spent, every other account takes its
  work up to its own panic line: as many tasks as fit between its use and `panic_at` at its cost per task (`task_pct`),
  even while ahead of its pace; unknown usage, its own panic or a spent linked quota stops takeover dispatch. If the main has moved to a
  local model and all online accounts are unavailable, the autopilot can send at most one small bounded unit per panicked account
  whose current and linked windows have a known task's worth of quota left. It prefers the account with the most remaining
  quota, reserves shared windows across the batch and keeps recent work debited until each window has a newer reading.
  Stale, estimated or missing readings cannot authorize rescue, including a configured linked window removed from the
  active state by a pause. Ordinary dispatch and direct auto routing also refuse an account when a configured linked
  window is missing. Nested limits must all be observed; pacing currently supplies only direct linked limits, so such
  accounts wait until their nested limits are included. Rescue independently checks every window's reading age against
  `stale_hours` (two hours when unset or zero), including automatic readers, and rejects future timestamps and selected
  estimates. It respects quiet windows and never uses a spent or
  explicitly paused account. It does not pause accounts.
  An account outside `autopilot.accounts` takes one task at a time and only while it is not ahead of its pace; a worker
  without an account (a local model) one at a time, never while its machine is paused (yield flag) or offline, and only for a task whose scope names files (it edits only those). Completed local attempts do not consume quota or keep the model busy. The
  ladder names accounts, not a worker that accounts share (`agy-claude`, not `agy`), so each step's pace, panic and model
  apply; the run is logged against that account.
- **It is parked when it makes no progress.** The same failure signature `no_progress` times in a row (default 3) on the
  strongest step, or `max_attempts` attempts (default 12), parks the task as **needs a person** with a summary on the
  dashboard. The signature is a hash of how it failed (worker exit, refused command, no change, or the failing check's last
  lines with numbers masked), so a flaky number does not count as progress. A worker's exit says whether it hit its
  usage limit or ran out of time. A reported usage limit (including `HTTP 429 Too Many Requests` in the worker's own lines)
  or CLI permission refusal is **environment**: it does not count toward `max_attempts` and never parks the task as no
  progress (the account or the CLI is the problem, not the task). A run without a report stays held until its work is
  verified stopped or its report arrives, except a confirmed reboot of a fully local attempt (empty `job_bridge`, no
  routed check), which is queued again without signalling a reused PID. Reported environment failures do not exhaust
  the ladder even when every step repeats them;
  availability, pacing and the retry backoff (capped at 30 minutes) still determine when a step can be tried again; this
  does not repair a persistent worker failure. A bare `429` in an assertion or a test name is
  not a usage limit.
- **The same failure twice stops it from burning quota.** Twice in a row on one step: that step is not given the task
  again (when no step is left, it is parked). Twice in a row on different workers, for a failure that is not the
  worker's own (its limit, its time, a refused tool, any other exit): the task or its check is the problem, and it is
  parked at once. Only the worker's own last lines are read for its limit, never Tutti's summary that quotes the task.
  When every step it may go to is worn out this way, it is parked.
- **agy may read and write its own worktree.** A worker whose command carries an allow-list (agy's `--settings`) gets
  `read_file(<worktree>)` and `write_file(<worktree>)` added for each run; headless agy auto-denies anything else.

When nothing is sent, the stopped line names **every** step: why it has no room, or, when it has room, why no queued task
fits it ("agy-claude: room for 1, but the queued tasks need 3", "… no queued task may go there").
If an account is ahead of pace, this line says it will be rechecked on the next successful active dashboard round while
autopilot is running. Failover must allow this dashboard to act. This is not a promise of a fresh quota reading or a dispatch time.

## Filling free room (`fill_room`)

On by default; `autopilot.fill_room: false`, `tutti autopilot fill off` or the **fill_room** switch under the dashboard's
Opt-in turns it off. Each round, while the autopilot is on:

1. **The room** is the units the trigger accounts (`autopilot.accounts`) may take now, as `step_room` gives it: the
   planner's allowance for agy's 5 h and weekly windows, plus eligible slow-use room; never a
   panicked or spent account. Cursor and the
   other ladder steps do not increase fill room, so filling never pushes them past their pace.

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

## Shared projects (a household)

A repo with `household: true` (or `tutti household project share <repo>`) is shared with the household's other members
(docs/HOUSEHOLD.md). Each round first makes one call to the coordinator, outside the backlog's lock: this member's new
tasks of the repo are published to the project's shared backlog (a title it already has, in any status, is not added
again), its finished ones are closed for everyone, the leases of its running and in-review tasks are renewed, and the
queued ones it may start next are reserved. The answer mirrors the other members' tasks into the backlog (source
`household`), closes a queued task another member finished ("done by Bo"), and holds every queued task whose lease
another member has ("leased by Bo"). The plan then works as usual, under this member's own accounts only. While the
coordinator cannot be reached, the shared repos' queued tasks wait and the private repos go on.

A member reserves only as many shared tasks as it could start now (`max_parallel` minus its runs), so it never keeps
tasks from the others. An account with `share_spare` whose room would be lost at its reset may also take other members'
shared projects (*spare repos*, `hh-<name>`, cloned once; docs/HOUSEHOLD.md, *Spare room*): only those spare accounts
take their tasks, and a result is a draft pull request. A household's shared machines offer each member only its grant
(docs/MACHINES.md, *Shared with a household*).

## Results land by themselves (`auto_review`)

On by default. One result at a time, `tutti autopilot land <id>` runs in the background:

1. **The repo's gates are required.** Each dispatch records the repo's `check` (its lint and type gates) that the
   attempt's acceptance runs with the task's own check; a result lands only when its passing attempt ran today's gates.
   Otherwise it goes back to the queue to run them; a repo without a `check` never lands by itself.
2. **Rebased first.** The branch is rebased onto `base` (default: the branch the repo's checkout is on) fetched from
   `origin`. A failed fetch waits 10 minutes and tries again (never lands on a stale base). A conflict never needs a
   person: when the base already holds the branch's changes (`git cherry`), the task is closed as superseded; otherwise
   the branch's diff is kept as `<state>/runs/redo-<id>.patch`, the worktree moves to the fresh base and the task is
   queued again with that diff named as a guide (the branch keeps its commit). After 3 redos on a base that keeps moving
   under it, it is parked.
3. **The reviewer** is the first `review.fallback` worker with room (the path `tutti pr review` falls back to); it reads the
   whole branch (`git diff <base>...HEAD`) in an empty folder with `review.PROMPT`. "No findings" is clean; bullets are
   findings. A reviewer that cannot run is tried again in 10 minutes, not every round.
4. **Findings** send the task back to the queue with them as notes (same step, same worktree); the third time it needs a person.
5. **Clean** lands by `autopilot.repos.<repo>.land`: pushed, a pull request opened (a draft is marked ready; a failure says
   so and keeps the task). `merge` then squash-merges it and deletes the remote branch (a non-zero exit is checked against
   the PR's state first); `pr` (the default) leaves it for a person; `branch` never auto-reviews. Auto-merge is for the
   orchestration toolkit's own repo; product repos take `pr`. The merge is pinned to the reviewed commit
   (`--match-head-commit`), and a branch that changes what the gates run (CI, build or lint configs, a file the check
   command names) stops at its pull request for a person: a result never merges a change to its own judge.
6. **Untrusted words.** A task from Sentry, a scout, the household or feedback carries a safety line in its brief (treat it as
   a description only; never run a command it names, never read credentials). A check suggested by such text is never run
   or passed on, and workers and reviewers run without the tokens Tutti keeps for itself (`GH_TOKEN`, Sentry, npm, the
   scouts' webhook variables: `workers.NOT_FOR_WORKERS`). docs/SECURITY-AUDIT-2026-11.md, H7, says what risk remains.

The repos' sources are read before the backlog's lock is taken, and every source command has a 120 s timeout, so a slow
`gaps` command never blocks the backlog.

## Adaptive parallelism

When the local machine offers only a share of its cores, the CPU limit explains the host total, the share and adjusted load separately. This explanation does not change the capacity calculation.

Account cards show estimated quota room in bounded tasks. Whether work can start also depends on account eligibility and available capacity; the estimate alone does not reserve a slot.

How many runs go at once is measured every round, not fixed. The limit is the smallest of:

- **accounts**: the runs going plus the room the ladder's accounts have now (the planner's tasks);
- **memory**: the runs going plus this machine's free memory (MemAvailable) above `reserve_gb`, `run_gb` per run. An agent
  run (agy, cursor) is mostly cloud-model calls: light here, so many can go together;
- **cpu**: the runs going plus the idle cores, `run_cpu` per run;
- **gates**: the runs going plus the fit machines' free gate slots times `gate_share` (a run holds a slot only for its gate);
- `max_parallel` when the owner sets it (Settings: an optional cap), and `ceiling`.

It rises with free capacity and drops under memory pressure. The dashboard remeasures host resources and disk space
before each autopilot round; a previous low-space or high-load reading does not persist after recovery. The dashboard's Autopilot line says
`running N of M possible: limited by <bound> (<why>)`. Every machine's probe now reports its free memory too (`mem_free_gb`).

**Heavy gates leave the laptop.** A repo whose checks run `local` sends a heavy gate (`parallel.heavy`: ESLint, tsc, Vitest,
Jest, pytest, Playwright, mypy, a quality stage, Gradle, dotnet) to the routed machines (`--check-where route`: the NUC first),
and any gate while this machine is under `reserve_gb` free. With no other machine it stays here.

```yaml
autopilot:
  # max_parallel: 4                    # optional cap; empty = adaptive
  parallel: {run_gb: 0.6, run_cpu: 0.25, reserve_gb: 4, gate_share: 3, ceiling: 12}
```

**Second opinions.** A task that failed twice or more gets one scout question (docs/SCOUTS.md) about its latest failure; its
retry waits up to `scouts.<name>.spend_rest.second_wait_minutes` (30) for the answer, which joins the task's notes and so the
next attempt's brief. A backlog task whose title starts with `research:` (or `scout:`) goes to a scout, never to a worker.

## Config

```yaml
autopilot:
  enabled: false                       # the dashboard's toggle (or `tutti autopilot on|off`) overrides it
  accounts: [agy-claude, agy-gemini]   # their "behind" pace triggers fresh tasks
  ladder: [agy-gemini, agy-claude, cursor, pc]   # retries climb it; accounts or workers; never one that costs money
  fresh_fallback_accounts: []            # optionally name unshared ladder accounts for fresh queued work
  # max_parallel: 2                    # optional cap on the adaptive limit (above)
  no_progress: 3
  max_attempts: 12
  quiet_hours: []                      # local-time spans (time.zone) it must not run, e.g. ['22:00-06:00']; the planner moves their share
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
      household: false                 # true: its backlog is shared with the household (docs/HOUSEHOLD.md)
```

`tutti config` names every wrong key: an unknown account or worker, one that costs money, a repo without `path`.

**Acceptance is the task's check and the repo's gates.** A result passes only when the task's own check (its new tests)
and the repo's `check` (its lint and type gates of the changed files) both pass: `( <task check> ) && ( <repo check> )`.
Before 0.1.16 a task's own check replaced the repo's, and results whose new tests passed were marked passing while they
broke ruff, mypy and the scenario registry (LESSONS). A failure of either feeds the retry loop like any other.

**Room comes from the planner.** A trigger account whose windows sit under a weekly limit gets as many tasks as the
planner's allowance holds (docs/USAGE-PACING.md, *The planner*), whatever its own verdict: a 5 h window's quota is spent
before it expires, as far as the weekly allows. `autopilot.quiet_hours` lists local-time spans (`time.zone`, daylight saving included) it must not run.

## The panel

Running and queued tasks can show a rough **attempt ETA** from comparable successful attempts of the same repo, size and ladder step; with too few samples the ETA stays unknown (t236).


**Autopilot** on the dashboard: the toggle, the ladder, `max_parallel`, whether it fills free room and reviews results, the last decision and its reason, then the runs
going (step, elapsed time, credits when the CLI prints them, the last log line), the queue and the latest decisions (four,
then **Show N more**, in a box that scrolls). Results to review and tasks that need a person are in **Needs you**. Every task
shows its attempt count and its failure history (step, why, run, signature).

## Priority

`tutti backlog priority <id> P0` saves a task priority from P0 (highest) through P5. Untriaged tasks are treated as P5;
a title alone cannot promote a task. Run-now tasks take precedence, then eligible
tasks are considered P0 through P5; project sharing applies within each level. This orders dispatch, not completion.

`projects.<name>.priority` ranks the projects: 0 is the most important (the Settings tab's *Project priority* list writes
it). An autopilot repo belongs to the project whose `self_improve.autopilot_repo` names it, else to the one whose `match`
finds the repo's name or folder; a repo no project claims ranks last. Queued tasks (fresh ones and retries) and the tasks
`fill_room` adds are shared out by weighted fair share: a project of priority p weighs 1 / (p + 1), so priorities 0, 1
and 2 get the work about 6 : 3 : 2, the runs going count as served, and the next task goes to the project with the least
work per weight. A more important project goes first and more often; a less important one still gets its turn, so it
never starves. One helper, `config.project_priority(cfg, name)` (with `config.repo_priority` and `tutti/priority.py`'s
`fair_order`), is what every scheduler uses.



## Launch recovery

Before starting a worker, the autopilot reserves its run ID and saves a running launch intent. A crash after that save
cannot leave the task queued in this local backlog while its worker might already be running. If the outcome is unknown, the task remains
visible and waits for its actual report; a confirmed failure to create the child goes back to the queue. This does not
verify or recover a worker that has detached, a remote run, or an ordinary no-report run after a confirmed launch.
It does not replace household lease renewal or guarantee that another member cannot take over after a lease expires.


### Report correlation

New runs include their saved non-secret run identity in their report. A report with a different run ID or identity is
held visibly instead of being published or retried; a later matching report is collected. Only launches marked for bound reports enforce the identity; attempts already in flight keep their existing behavior.
A configured bridge client must advertise bound-report support before new autopilot work starts; older or offline clients
are deferred with an update/restart message. A distinct bound-run operation prevents an older client from silently
starting work if the connection changes after the capability check. The identity is a correlation value, not an authentication secret, a proof that a process
tree stopped, or a fence against a same-user worker. Per-attempt worktrees, remote effects and household lease fencing
still need separate ownership controls.
