# Changelog

The format follows Keep a Changelog; versions follow Semantic Versioning.

## 0.1.28 (2026-11-14)

### Changed

- The dashboard's Grok Bot card (a scout's account) shows its scout facts, the allowance used and left, the next watch
  and the last answer, instead of "N bounded tasks fit here now": a scout takes no tasks.
- Sentry and scout triage tasks join the backlog queue by themselves when self-improvement is on
  (`self_improve.auto_accept`, default `true`), so `fill_room` and the autopilot pick them up; a waiting candidate is
  queued at the next self-improvement round. The 3-a-day cap and the dedupe hold; a read-only project (RoRadio) still
  gets the issue only.

- `tutti pr review` skips a reviewer whose allowance is used and goes straight to the fallback: its account (named like
  the reviewer, `copilot` or `coderabbit`) reads 100 % with a reset still ahead (`tutti usage log copilot 100 --resets
  2026-12-01T00:00Z`), or its last PR comment said it is rate-limited (the reading is then written by itself, until the
  time the comment names, else an hour). It says which reviewer was skipped and until when; after the reset it is asked
  again by itself.

### Fixed

- Self-improvement insight tasks also wait as `candidate` when `self_improve.auto_accept` is off (cursor's review).
- The telemetry *Sending* tests are hermetic: a real DSN in `~/.config/tutti/telemetry.env` or a `TUTTI_*` variable on
  the machine can no longer make a test send to Sentry or fail.

## 0.1.27 (2026-11-14)

Token and runtime savings (measured on the owner's setup, before → after).

### Added

- Automatic compaction of a worker agent's context at `autocompact.threshold_pct` (60 % by default) of its window,
  changeable on the dashboard (*Devices and controls* → *Settings*, `POST /api/autocompact`) without editing the config.
  Claude Code compacts natively (`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, also as the main agent of `tutti conduct`); a local
  model as the main agent is compacted by Tutti (older exchanges become one summary line each); cursor-agent, agy and
  copilot have no headless compaction, so a run whose output shows its context use is stopped at the threshold and
  started again in the same worktree with a handoff note (at most three times). `workers.<w>.context_tokens` sets a
  window. Running jobs show `context N %` where it is known.
- `/api/state` carries an ETag: the page asks with `If-None-Match`, an unchanged answer is a `304` (0 bytes) and is not
  parsed or rendered again; gzip when the browser takes it (96 KB → 17 KB a poll); one answer serves every tab for a second.
- One shared ssh connection per machine (a background `ControlMaster`, `ControlPersist` 10 min) for probes, `tutti test`,
  rsync and git: 0.18 s → 0.02 s a call to the build machine. `machines.<m>.ssh_reuse: false` turns it off; never on Windows.

### Changed

- A reader whose answer did not change is asked half as often next time, down to every eight intervals; a change or
  **Refresh readings** resets it. agy `/usage` runs: 120 → 40 an hour on an idle dashboard.
- The projects' activity scan reads every process's folder and program with one `find` and one `ps` instead of three
  forks per process: a dashboard probe 12.1 s → 1.7 s (median), the scan's CPU 2.9 s → 0.3 s every 30 s.
- A retry's brief carries the failing output compacted (colour codes, progress redraws, Tutti's echo and repeated lines
  out, error lines kept) and each earlier reason as one line: the longest real retry brief 847 → 557 tokens. The same
  compaction shortens a command's answer to a local main agent and the last output in a handoff note.

### Fixed

- `tutti pr review`: GitHub answers 201 to a Copilot review request it silently drops, and 422 "not a collaborator"
  for the bare bot login, so Copilot looked requested and never came. The request now asks as `Copilot` first, checks
  that the answer lists Copilot, falls back to GraphQL `requestReviewsByLogin` with the bot login, and otherwise says
  where to look (Copilot code review in the repository's settings, or a head commit already reviewed).

## 0.1.26 (2026-11-14)

### Fixed

- `fill_room` queued "Add unit tests" for shell scripts (`install.sh`, `scripts/check.sh`): shell is checked by shellcheck
  and its callers' tests, so the untested source now takes Python, TypeScript and JavaScript files only.

## 0.1.25 (2026-11-14)

### Added

- The owner's own prompting in the planner (`tutti/owner.py`, `pacing.schedule`, `pacing.mode`, `tutti owner`): a weekday
  x hour profile, seeded Mon-Fri 8 h within 01-14 local and learned from the main agent's readings (the workers' baseline
  taken off). On working days the main agent's accounts keep the owner's expected burn until the window's target; on
  weekends, away (`auto` infers it) or on vacation nothing is kept and the workers spend the rest evenly, so every account
  ends near 100 % about an hour before its reset. Week-long simulations prove 95 % or more at the end and no owner hour
  projected at the limit (reset on Monday, Saturday, Wednesday; a day away; two weeks of vacation; an hour a day).
- The dashboard: the mode, **I'm away until…**, **Away today**, **I'm back (auto)**, the working-day switches, a 7 x 24
  heatmap of the learned week, and **On track to finish at X % by its reset** on every account card.

### Fixed

- `fill_room` queued built bundles (`cdn/…`, content-hashed or minified files) and quoted `TODO(owner)` in docs, help text and
  test fixtures as tasks. Built folders and bundles are skipped, a TODO in prose, a quote or a test is not a task, a file a
  test imports counts as tested, and `autopilot.repos.<repo>.untested: false` turns the untested source off for a repo whose
  coverage gate already proves every file tested.

## 0.1.23 (2026-11-14)

### Added

- `autopilot.fill_room` (on by default; `tutti autopilot fill on|off`, and a **fill_room** switch under the dashboard's
  Opt-in): free room on the trigger accounts, as the planner gives it, is filled by itself. Fresh tasks are queued from
  the repos' FEEDBACK.md, owner-tagged TODOs, the `gaps` command and the tracked source files no test names, up to three
  per parallel run; a title the backlog ever had is never queued again.
- `autopilot.auto_review` (on by default) and `autopilot.repos.<repo>.land` (`merge`, `pr`, `branch`) with `base`: a result
  to review is read by the cheap reviewer (`review.fallback`) in the background (`tutti autopilot land <id>`); clean, it is
  rebased, pushed and opened as a pull request, merged at once with `land: merge`; findings send it back with them as notes,
  the third time to a person. A result lands only when its passing attempt ran the repo's current `check` (its lint and
  type gates, recorded at dispatch); the reviewer reads the whole rebased branch; a failed fetch, reviewer or merge is
  tried again in 10 minutes; the repos' sources are read outside the backlog's lock, each command with a 120 s timeout.

### Changed

- The accounts' cards no longer show "Use it: N bounded task(s) can go here now (tutti run auto …)". With the autopilot and
  `fill_room` on: "Autopilot: filling N slots — R running here, Q queued" and what the last fill did; otherwise a short hint
  and **Let autopilot fill it**.

### Fixed

- A retry whose steps above all had no room waited while the steps below had room: t13 to t15 had failed on agy-gemini and
  agy-claude, so only cursor (ahead of its pace) and the PC (yielding) could take them and both agy accounts sat idle. A
  retry now falls back to the steps below, strongest first, and the stopped line names every step, the ones with room too.
## 0.1.21 (2026-11-14)

### Added

- Telemetry (docs/TELEMETRY.md): every command (name, fixed action, option names only, duration, exit code, outcome) and
  feature use (routing decisions, test and device runs, delegated runs, autopilot plans, attempts, escalations and parks,
  waits and stalls, scout commands, dashboard panels and controls) recorded in `<state>/telemetry/events.jsonl`, rotated.
  Field values are categories or numbers only; free text (crash messages) is scrubbed of home paths, hosts, IPs, e-mails,
  URLs, tokens, keys and quoted fragments (`tutti/scrub.py`).
- Crash reporting to Sentry through its envelope API (no SDK): uncaught exceptions in the CLI and the dashboard server,
  failed commands with their exit code, and the dashboard page's script errors, with release, environment, OS and Python.
  Sending is opt-in (asked once on the first interactive run; `tutti telemetry on|off`; the dashboard's switch), batched
  and offline-tolerant; also to a custom `telemetry.endpoint`.
- `tutti telemetry status|on|off|setup|report|send`: `setup` keeps the DSN in `~/.config/tutti/telemetry.env` (600),
  `report` and the dashboard's Insights card summarise the local events (most used, slowest, most failing, wasted quota,
  stall causes, crashes), `send --test` checks the DSN.
- Sentry triage by a scout: `projects.<name>.sentry: {org, project}` and `sentry_triage: true` on a scout give a daily
  watch per project; its `TUTTI-TASK` lines become backlog tasks (`source: sentry`, one per Sentry issue) only for a
  project that opts in, otherwise the issue alone. `tutti backlog accept <id>` queues a `candidate`.
- Self-improvement (on by default; `tutti selfimprove`, the dashboard's Self-improvement card): Sentry issues and local
  insights become tasks with a regression test, the autopilot fixes them, a ready pull request gets `tutti pr review`, and
  where a project allows (`projects.<name>.self_improve: merge, release`) a green pull request with no open thread is
  merged (never `--admin`) and released in batches (`scripts/release-patch.sh`); other projects stop at a ready pull
  request to their work branch, never `main` or `master`.

## 0.1.20 (2026-11-14)

### Added

- `tutti scout setup grok`: the guided Grok Bot scout setup in a real terminal: the in-app steps with the routine's text
  ready to copy, the webhook URL (checked) and key (hidden input, never echoed or logged), saved to
  `~/.config/tutti/grok.env` with mode 600 and sourced once from `~/.profile`, the scout added to the config when
  missing, a running dashboard restarted by pid, and a test question. Without a TTY it prints the command to run.
- Tutti loads `~/.config/tutti/grok.env` when the scout's variables are missing, so restarts never depend on the shell.
- The dashboard's Scouts card shows `tutti scout setup grok` as the one thing to run while a scout is not set up, and
  **Add a reading** (opens the account's usage page) while its meter has no reading or a stale one.
- Each question a scout sends logs a spend estimate on its account (the newest reading plus `task_pct`, source
  `estimate`), so the meter moves between readings; an estimate never counts as a fresh reading.

- The dashboard's pause and resume section is grouped with headings: Work machines, Android devices, Local agents
  (Ollama, llama.cpp), Online agents (CLI workers, accounts and scouts), then Opt-in; an empty group says so.

### Fixed

- The reading helper reports a usage page still on its spinner (no "N% used" shown) and saves nothing, never a 0: the
  bookmarklet says so, `tutti usage page-reading` exits 3. Grok Bot's weekly meter ("Grok Bot", "Resets Nov 15") is read
  in the same click as Cursor's (test on the real page's text).
- After a helper click, the dashboard names the accounts on that page the click did not carry (a button dragged before
  the account was configured) and says to drag the button again.

## 0.1.19 (2026-11-14)

### Added

- Android devices as e2e targets: `devices:` in the config (serial, the machine its adb sees it on, a safe-use policy),
  `tutti devices [add]` with battery, charging, battery temperature, screen, Wi-Fi and lock readings over adb
  (`scripts/device-readings.sh`, read-only), and `tutti test --device <name|any> -- <cmd>`: `ANDROID_SERIAL` set, one run
  at a time per device behind its lock, the policy checked first, the screen woken, `--fetch` for results. Recipes for
  Gradle instrumentation and Playwright's Android Chrome.
- Termux machines (`transport: android`, `ssh -p 8022`): a native runner, Termux:API power readings in the probe, the same
  policy (charging, 50 % or more, under 40 C, Wi-Fi), always last in routing; opt-in, off by default.
  `tutti machines add-android` prints the owner's setup.
- Controls on the dashboard and `tutti pause|resume|optin`: pause or resume any machine, device, worker or account and
  switch opt-in items (`costs_money`, `opt_in`, Termux lint runs), kept in `<state>/controls.json`. "Pause, stop now"
  hands running jobs over first: an agent run's work is committed and continued by the next worker from a handoff, a test
  run starts again elsewhere, a transfer keeps its partial file.
- `tutti test` runs are dashboard jobs with their output; `tunnel.machine` reaches a model server on a machine of the
  config (its user and port).

### Fixed

- The planner's line on an agy card said "window ends HH:MM" for the spend-by time (one hour before the reset); it now says
  "spend by HH:MM …, resets HH:MM …", the reset as the meter shows it.

## 0.1.18 (2026-11-14)

### Added

- Scouts: helpers that are not workers. Grok Bot (Cursor Pro's chat bot, its own weekly allowance) through its webhook
  routine: `tutti scout ask` opens a GitHub issue (label `tutti-scout`), POSTs `{task, reply_to}` and prints the answer the
  routine posts there; `scout answer`, `scout watch` (recurring watches whose results are issues), `scout tick` (the
  dashboard's round), `scout list`, `scout setup` (the owner's steps). The URL and key come from environment variables
  (or stdin) and are never stored or shown. Paced by the `grok-bot` account: nothing is sent once the allowance is used
  (never on-demand); `early` watches use an allowance that is behind its pace. docs/SCOUTS.md.
- `tutti scout review <pr>`: an advisory review posted on the pull request, tagged `grok-scout`; and the review trial
  (`scouts.<name>.review_trial`): for the next pull requests `tutti pr review` also asks the scout, findings of every
  reviewer are collected and marked real or noise in `<state>/review-trial.json`, and the verdict says whether review
  joins the scout's skills. The dashboard's Scouts card shows all of it.

## 0.1.16 (2026-11-14)

### Added

- The quota planner (`tutti/planner.py`, docs/USAGE-PACING.md): what expires soonest is spent first, across accounts. An
  account's `limits` names a longer quota that bounds it (agy's 5 h windows under `agy-claude-weekly`, `agy-gemini-weekly`,
  read from the same `/usage` answer): the weekly room is shared over the 5 h windows left that can run, at least what the
  later windows cannot hold, so nothing expires unused; a window in `autopilot.quiet_hours` gets nothing and its share moves.
  `reserve` (default 0) keeps points for work outside Tutti; `background: true` (Cursor's own reviews) estimates the spend
  outside Tutti from readings and plans around it. The autopilot sends as many tasks as the plan holds; `tutti run auto`
  picks the soonest-expiring room. A simulated week shows it wastes less than spreading the weekly evenly.
- Dashboard: an agy card shows its 5 h window and its weekly limit side by side, and the planner's line ("this window: X of
  Y weekly points … → N task(s) now").
- Dashboard: **Needs you**, near the top: tasks that need a person, panic, stalled jobs (with **Open its log**), machines
  offline, workers down, readings due, results to review.

### Changed

- Dashboard layout by importance (docs/DASHBOARD.md, *Layout*): tiles and Usage & pacing first, full width, the accounts in
  one row of compact cards (a swipeable row on a phone); then Needs you; Running jobs beside Machines; the Autopilot beside
  the Main agent and Workers; Projects; Finished jobs at the bottom. A 12-column grid whose rows fill their width, cards of a
  row the same height, long lists folded with "Show N more". Checked at 375, 1440 and 2560 px, light and dark.
- Autopilot acceptance is the task's own check AND the repo's `check` (its gates): results whose new tests passed but broke
  lint or type checks were marked passing (LESSONS).

## 0.1.15 (2026-11-14)

### Added

- `tutti doctor`: finds a git first in PATH whose HTTPS helper cannot run (another tool's bundled git with a mismatched
  libcurl) and says to fix PATH, naming a working git when there is one; it runs without a project config.

## 0.1.14 (2026-11-14)

### Added

- The dashboard updates itself: a restart on a new release is noticed through `/api/state`'s version; the page updates
  its service worker (`registration.update()`, `skipWaiting`, `clients.claim`) and reloads once, never in a loop.

### Fixed

- The autopilot never sends a retry to a local model whose machine is paused (its yield flag is up) or offline.

### Changed

- Docs: an agy account whose `--settings` allow only some commands soft-denies the rest headless, so autopilot tasks that
  need a command lost one attempt on it; give every agy account the `command(*)` allow the autopilot needs (LESSONS).

## 0.1.13 (2026-11-14)

### Added

- `tutti autopilot`: when an account is behind its pace, Tutti hands bounded backlog tasks to it by itself instead of only
  advising: worktrees, each task's own check, at most `max_parallel`, only while a machine has a cool free slot, never on an
  account that costs money, is in panic, spent or at its line. A pass becomes a branch (or a draft pull request), never merged.
  A failure retries from the same worktree with the diff, the failing output and the reviewer's notes in its brief, one step
  up `autopilot.ladder`; the same failure 3 times on the strongest step (or `max_attempts`) parks it as "needs a person".
- `tutti backlog add|list|rm|seed|retry|done`: the queue in `<state>/backlog.json`, seeded from FEEDBACK open items,
  `TODO(owner)` comments and a repo's `gaps` command.
- The dashboard's Autopilot card: the toggle, the last decision, runs, results to review, parked tasks, the queue, attempt
  counts and failure histories; `POST /api/autopilot`.
- `tutti run --repo ... --check "<cmd>"` runs the target repo's own check; a local-model worker takes a delegated task.

## 0.1.12 (2026-11-14)

### Added

- Every running job on the dashboard has trackable progress, in layers, each labelled with its source (docs/DASHBOARD.md,
  *Progress*): exact counters (now also a pipeline's `static 3/51 eslint`, rsync's `45%` and `slot.sh`'s queue), known
  phases (`dashboard.shapes`: phases or counted lines with `total_command`; built in for `tutti run --repo`), estimates
  from the history of finished durations per kind and machine (`<state>/durations.json`, capped at 95 %, "taking longer
  than usual"), and agents (a conductor's handoff notes, else history, else `dashboard.typical_minutes`). The rest shows
  its elapsed time: no active job shows "—" any more. ETA when it can be estimated.
- Job facts, live and after the end: tokens and tool calls (an agent's transcript, or the token lines an agent CLI
  prints), AI credits, the account spent with its % used, tests passed and failed, files changed (+/−), bytes copied,
  the current step. A finished job shows no bar: its result, exit code, duration and the report.
- A running `tutti conduct` is a job; `slot.sh` says when a run waits for a slot and when it gets one.
- Usage cards: **Add a reading** shows only where it is needed (no automatic reading, or one older than `stale_hours`);
  an account's `note` is a warning callout with an icon, right above it.

### Fixed

- `tutti run --repo <another repo>` ran the config's default check in the other repository. The checks now belong to the
  target: `projects.<name>.checks`, else a `tutti.config.*` inside it, else the default only for `delegate.project`
  (or for any repository when it is unset); otherwise none, and the report says how to add them.
- `tutti pr review` called `gh api --slurp`, which gh before 2.62 lacks; it now reads the pages `--paginate` prints back
  to back.

## 0.1.11 (2026-11-14)

- The pacing advice when spending too fast now reads "shift work to other workers and local models" (CLI, dashboard, docs).
- FEEDBACK: `tutti run --repo` ran the wrong repo's check.

## 0.1.10 (2026-11-14)

The open items of 0.1.8 and 0.1.9, each proven with a real run.

### Fixed

- Antigravity with Claude models: they call one generic `Bash` tool that per-command allow (and deny) rules do not match,
  so every command was soft-denied in `accept-edits`. The agy-claude account's `args` carry `--settings` with
  `command(*)`; an account's args now **replace** a flag the worker (or `main.commands`) already has (`--model`,
  `--settings`) instead of being appended. `tutti run auto` and `tutti conduct --main agy` run on agy-claude while its
  window has room (a pinned conduct command no longer drops the account's model).
- The reading helper on the real Cursor spending page: "Other Models" took the reset of Grok's weekly meter below it. A
  meter is now read line by line (its label, its amount, a "Resets" line right after it), else the page's "Usage limits
  reset on <date>". The parser moved to `tutti/web/helper-parse.js` and is tested in node on the real page's text.
- Heat: a run already waiting in `slot.sh` re-reads the temperature right before it starts (`heat` reaches it as
  `HEAT_STOP_C`, `HEAT_CAP_C`, `HOT_SLOTS`); too hot, it gives its lock back and waits.
- Temperatures: the probe reads the GPU too (`nvidia-smi`, also from WSL's `/usr/lib/wsl/lib`, or an AMD/Intel hwmon
  sensor), and a WSL machine's CPU from Windows (PowerShell over its plain ssh; a placeholder ACPI value is shown as "not
  readable" with the reason). The dashboard and `tutti machines` show CPU and GPU per machine; heat limits, routing and
  `slot.sh` use the hotter of the two.
- Changed files with spaces: git's names are read with `-z`; `$TUTTI_CHANGED` and `{changed}` hold one path per line.

### Added

- `tutti usage page-script [<url part>]` and `tutti usage page-reading '<json>'`: the helper's parser for an
  orchestrator's browser tool, when nobody clicks the bookmarklet (the dashboard cannot read cursor.com).
- `costs_money: true` on a worker or account: pay-per-use entries (the Copilot CLI's premium requests) are **off by
  default**, out of routing, the conduct and review fallbacks, pacing and the dashboard, until `enabled: true`. The
  Workers panel lists what is off and why; `tutti run <it>` names the line that turns it on.
- `projects.<n>.repo: owner/name`: the Projects panel shows the project's open pull requests, a release in progress first,
  with CI and review state.

## 0.1.9 (2026-11-14)

Any agent can be the main agent; the dashboard shows the projects on every machine.

### Added

- `tutti conduct "<goal>" [--main <agent>] [--fallback a,b] [--dir] [--show]`: a CLI worker (Cursor, Antigravity, the
  Copilot CLI, Claude Code) or a local model conducts. It gets a generated instruction file (Tutti's commands, the
  project's `AGENTS.md`/`CLAUDE.md`, the safety limits, the handoff rules) and a `tutti` on its PATH; agy's permission
  list gets `command(tutti)` by itself. A local model runs in Tutti's loop: it answers `RUN: tutti ...` lines, only
  Tutti's own commands run (no shell; `ship` only as a dry run), until `DONE:` or `BLOCKED:`.
- Automatic fallback: every `main.check_seconds` the run checks the readings; when all of the agent's accounts are in
  panic or spent (or a limit message ends it, or the owner switches), it is stopped and the next agent in
  `main.fallback` starts with a written handoff note: the goal, who stopped and why, what is done, the tree's changes
  and what comes next. `tutti handoff done|next "<text>"` keeps the run's progress.
- `tutti main [<agent>]`, and the dashboard's **Main agent** panel with a switch (`POST /api/main`).
- While a main agent conducts, `tutti ship` runs only with `--dry-run` and without `--allow-protected`.
- `main:` in the config: `agent`, `fallback`, `commands` (a command per agent when it conducts), `ask_first`,
  `check_seconds`, `rounds`, `command_timeout`.
- The dashboard's **Projects** panel: each project's running jobs, agents and runs, repositories and branches per
  machine, processes and programs per machine (read-only look at every machine, including a WSL machine's Windows side
  over plain ssh), yield flags, GPU use and last activity; the Jobs panel filters by project. `projects:` in the config
  names projects (`match`, `machines`, `containers`, `windows`).

### Fixed

- Pacing: the on-pace band is a quarter of the line (1 to 5 points) instead of a fixed 5 points, so 10 % used against
  6 % by now is `ahead`, not `on pace`; each `tutti usage` row shows the math and says a shared account is judged on the
  whole quota.

## 0.1.8 (2026-11-14)

Usable by an orchestrator end to end: hand off, wait, test, review, read usage.

### Added

- `tutti run <worker|auto> "<task>" --repo <repo>`: a bounded task handed to a cheap worker end to end: a worktree of the
  repo (new, or `--track <name>` reused), the worker headless with stdin closed, the diff of everything it changed or added
  as one patch, the checks of `delegate.checks` (default `ship.checks`; routed checks share one remote copy,
  `delegate.remote_track`, one run at a time) and a report: changed files, the diff's path, each check's verdict and
  log, "coverage NOT checked", DENIED tool calls, and the lines to review, apply or discard. `--bg` returns the id at
  once; `--discard` removes a new worktree after the report. `tutti runs [show|discard <id>]`.
- `tutti wait run <id>`: waits on the run's result file, gives up when its log stalls (exit 125), reports a run whose
  process vanished at once, and ends with one line: the verdict and the report's path. Every successful `tutti wait`
  now prints one `done: ...` line.
- Worker commands take `{log}` (a log file per run; a `--flag {log}` pair is dropped without one) and `denied:` (a regex
  for the CLI's own permission refusals, scanned in the output and the log). The example agy worker runs with
  `--mode accept-edits --log-file {log}` and `denied: soft-denying`; cursor with `--force`.
- Heat: the probe reads the hottest thermal zone; `machines.<m>.heat: {cap_c, stop_c, hot_slots}` caps a hot machine to
  `hot_slots` runs and refuses new runs when very hot, so routing falls over. `tutti machines` and the dashboard show
  the temperature.
- `test.prepare: [{when, run}]`: steps a command needs first (the e2e build before Playwright), put in front once by
  `tutti test` and routed checks.
- `{changed}` and `{changed:ts,js}` in check commands: the changed files reach a check that runs on another machine.
- Coverage: a check that turns coverage off (`coverage.enabled=false`, `--no-cov`, in the command or the script it
  runs, or `coverage: false`) is reported as "coverage NOT checked" by `tutti ship` and the run report.
- `tutti pr review <n>`: requests Copilot (REST) and CodeRabbit (`@coderabbitai review`), says at once when a request is
  refused or CodeRabbit answers "Review limit reached", waits `review.timeout_minutes`, then has the first available
  `review.fallback` worker whose account has room review the diff in an empty folder, and posts its findings as a
  comment (`--no-post` prints them).
- Usage: `stale_hours` (default 2) reminders for accounts read by hand, in `tutti usage` and on the dashboard;
  `reading_file`, a file an orchestrator writes its % to, logged once per version; "use it: N bounded task(s) can go
  to <worker> now" for an account behind its line (`task_pct`, default 2); cards say how and when an account was read.
- The reading helper (`/helper`): a bookmarklet that reads the spend and the reset from the visible text of the
  provider's usage page the owner has open (Cursor), and saves it through the dashboard with a per-install key. It never
  reads cookies or tokens; when the page's text does not match it says so and opens the form.

- Jobs found by themselves on the dashboard: `tutti job register|done` for any script; `dashboard.watch_scripts` (running
  scripts by process, with the log their output goes to, its tail and progress); `dashboard.watch_agents` (cursor-agent,
  agy, copilot by process: program and folder only, flagged "started outside Tutti" unless they work in a Tutti run
  worktree or review folder); `dashboard.task_globs` (an agent session's background task outputs, by time only); and the
  test containers holding a machine's slots (`machines.<m>.containers`, read by the probe).
- `<state>/runs.jsonl`: every worker run with the account it spent; the dashboard shows runs per worker today. A tile
  says where the next tasks go ("Next tasks go to: agy").
- `accounts.<name>.enabled: false` keeps an account (an opt-in model) out of pacing, panic, routing and the dashboard; its
  readings stay in the log. `note:` is shown on the card. One `reading_file` may hold several accounts.
- The reading helper reads several meters of one page in one click (`helper_label`, e.g. Cursor's "Cursor Models" and
  "Other Models"); the reset from the meter, else "limits reset <date>".
- Copilot: a `github-premium` reader (`gh api /users/<login>/settings/billing/premium_request/usage`, `allowance`, the
  month from the 1st UTC), `credits: true` sums the "AI Credits N" lines of Tutti's runs per window, and a reader that
  fails says the CLI's own fix (`gh auth refresh -h github.com -s user`).

### Fixed

- Headless agy auto-denied every command outside its saved allow list ("jetski: ... auto-denied"): the example worker
  passes its own list with `--settings` and `denied:` matches `auto-denied`.
- `tutti test <track> echo -n x`: the command's own options are no longer read as options of tutti (FEEDBACK #4).
- Cursor's usage page is `https://cursor.com/dashboard/spending`.

## 0.1.7 (2026-11-14)

Files for other machines.

### Added

- `tutti files send <path>... --to <machine|all> [--dest <folder>] [--bwlimit <KB/s>]`: copies files and folders over the
  machine's transport (ssh, WSL, local): rsync when both ends have it (resumable partials, skips what is there), else a
  resumable byte stream; each item is verified with `sha256sum -c` on the machine and re-sent once if damaged; an
  unchanged item is skipped without copying; a changed item's record is dropped before it is overwritten. The stream
  keeps empty folders and removes files a folder no longer has. One send or clean per destination at a time, across
  projects (`$XDG_STATE_HOME/tutti/locks`).
- A cache per machine (`files.cache`, `files.budget_gb`, `files.keep_free_gb`, `files.bwlimit_kbps`, `files.compress`,
  `files.refuse`; a machine may override each): least recently used items leave first, a replaced item counts once,
  partial files count; `tutti files list` and `tutti files clean --on <m> [name...] [--all]`.
- Secrets never leave: `.env*`, `*token*`, `*secret*`, keys, `*service-account*.json`, a small `.json` holding a
  `private_key`, `.ssh/` and the like refuse the whole send, naming the file; symlinks are judged by their target too.
- `tutti test --input <path>` copies a job's inputs first and exports `$TUTTI_INPUTS` (mounted read-only in a docker
  runner); `tutti run --input <path> --on <machine>` does the same before briefing a worker. Inputs stay pinned until
  the job ends.
- Dashboard: transfers in Jobs with their progress in MB (`<log>.progress` may carry a unit); each machine's stored
  files, their size against the budget and last use, under Machines.

## 0.1.6 (2026-11-14)

Dependencies at latest stable.

### Changed

- Dev tool `ruff` 0.17.3 -> 0.17.5. PyYAML 6.0.3, `actions/checkout` v7.0.1 and `actions/setup-python` v7.1.0 were already
  the latest; the web dashboard loads no CDN script and the installers pin no versions.

## 0.1.5 (2026-11-14)

Usage readings with minimum input.

### Added

- Automatic readers per account: `reader` runs a command that prints usage without spending quota (Antigravity:
  `agy -p /usage --output-format json`, its two rolling 5-hour windows `3p-5h` and `gemini-5h` and the weekly ones);
  `plan` reads a label such as Cursor's tier from `cursor-agent about --format json`. The dashboard runs them with its
  probes (`every_minutes`, default 10); `tutti usage read` runs them now.
- Limit messages: a "usage limit reached, try again in 3 h" line in the output of `tutti run` or of a `tutti quiet`
  job of an agent CLI logs the account as spent until the reset it names (`match: {command, model, fallback}`).
- A reading may carry its reset time (`tutti usage log ... --resets`, readers, limit messages, the form); the newest one
  anchors the account's window and beats the config's `resets`. `resets_estimated: true` marks a guess.
- Dashboard: **Add a reading** on every account card opens the provider's usage page (`usage_url`) and a form (percent
  slider, reset time prefilled from the window rule); it saves through `POST /api/reading` (loopback host, same origin,
  JSON, the page's per-start token). Cards show the plan, how often the account is read automatically and where its
  reset comes from.
- Config: `accounts.<name>.usage_url`, `reader`, `plan`, `match`, `worker`, `args`, `resets_estimated` and
  `routing.workers`, each checked with a
  message that names the key and the fix.

- `tutti run auto`: the readings decide which worker takes the job (panic and spent accounts are skipped; quota that
  would be lost at the reset goes first; `routing.workers`, `accounts.<name>.worker` and `args`). The Quota tile shows
  where the next agent job goes.
- Refresh readings: a dashboard button (`POST /api/refresh`) and `tutti usage refresh` run every reader now. Readers
  run at every probe by default (`every_minutes` to slow one down).
- Job progress: a bar and percentage per job from `tutti progress <done>/<total>` (`tutti quiet` sets
  `TUTTI_PROGRESS_FILE`) or the log's progress lines; a dash when unknown.
- Claude limit messages with a clock time ("resets 3pm") are read too (`match` on the claude account).
- The example config has a GitHub Copilot CLI worker.

### Fixed

- `tutti ship` refuses to commit or push in the middle of a rebase, merge, cherry-pick or revert, or with conflict
  markers left in files (it names them); a lone `=======` heading underline is not a conflict.

### Changed

- `tutti run` passes the worker's output through line by line (stderr merged into stdout) so limit messages can be read.
- The service worker fetches the page network-first (the cache is the offline fallback), so the page's token is fresh.

## 0.1.4 (2026-11-14)

The dashboard.

### Added

- `tutti dashboard [--port 4777]`: a local web dashboard served on 127.0.0.1 only (standard library, no build step, no
  CDN), installable as an app (manifest, icons, service worker). Material 3 look that follows the OS light/dark scheme
  and, where the browser shares it, the OS accent colour (`AccentColor`; the footer says when it falls back). Panels:
  usage and pacing per account (used, the even-pace mark, the panic threshold, reset and target times, readings this
  window), machines (slots free and busy, load, paused by the yield flag, offline), workers (ready or down, and the known
  agents installed on this computer), jobs (running, stalled, failed, done, with the end of each log). `/api/state`
  serves the same data as JSON; `--once` prints it. See docs/DASHBOARD.md.
- Quota panic: `accounts.<name>.panic_at` (default 95 %). At or over it before the target time, `tutti usage` and the
  dashboard say panic and what to do.
- `tutti quiet` and quiet ship checks keep the result (`<log>.rc`) and the command (`<log>.cmd`) beside the log;
  `tutti wait` records itself under `<state>/waits/` while it runs. The dashboard reads both.
- Config: `dashboard: {port, refresh_seconds, probe_seconds, stall_minutes}`.

## 0.1.3 (2026-11-14)

Fixes for the pains agents hit shipping from linked worktrees.

### Fixed

- git: Tutti runs the system git (`/usr/bin/git`, or `TUTTI_GIT`) instead of the first `git` on PATH, which can be an
  agent runtime's wrapper without a working https helper.
- `tutti ship` from a linked worktree: `ship.share` (e.g. `[node_modules, packages/*/node_modules]`) hard-links missing
  dependency folders from the main checkout; checks get `TUTTI_GIT_MOUNT` (`-v <main .git>:<main .git>`) and local
  docker runs mount it, so git inside the container reaches the main repository.
- `runner.rewrite` maps a substring of a test command to another (a pipeline script that starts docker itself, rewritten
  to the one that runs inside the image).

### Added

- `tutti wait ... --stall <s> [--stall-file <log>]`: ends with exit 125 when the log stops changing, so an agent waiting
  on a hung job stops instead of re-announcing "still waiting".

## 0.1.2 (2026-11-14)
QA round 2 on Windows.

### Fixed

- Windows: the WSL `bash.exe -c` line carries no quotes, `&&`, `$(` or braces and no arguments after `-c`; the script
  is sourced from base64 (`source <(echo <b64> | base64 -d)`).
- Windows: `install.ps1` repairs CRLF scripts left by installs from before `.gitattributes`.

### Added

- `tutti config` warns when an account's `lead_hours` is not 1 (the old example's 3) and prints the one-line fix.

## 0.1.1 (2026-11-14)

QA round 1 on Windows and Linux. The first tagged release.

### Fixed

- Windows: local runs keep the caller's stdin (the script travels base64-encoded to a temporary file, not piped into
  bash); `wslpath`/`cygpath` output is decoded as UTF-8.
- Windows: `tutti wait pid` reads the exit code (`GetExitCodeProcess`), so a retained handle no longer looks alive, and a
  denied `OpenProcess` counts as a live process.
- Windows installers: config paths are literal (`-LiteralPath`), so brackets in `XDG_CONFIG_HOME` work.
- Config discovery honours `$XDG_CONFIG_HOME/tutti/config.yaml` (then `tutti.config.yaml` there); a missing config
  names the exact path and the `cp` from the example. Installers say when `TUTTI_CONFIG` or a project config wins.
- Usage pacing: the default lead is 1 hour before the reset (`lead_hours` still overrides).
- `tutti workers`: an ollama worker is ready only when `/api/tags` lists its model (untagged means `:latest`); otherwise
  it says `OLLAMA_HOST=<host> ollama pull <model>`. A malformed answer is a DOWN row, not a crash.
- Ollama hosts may carry their own scheme (`https://proxy:443`).

## 0.1.0 (not tagged)

The first extraction of the toolkit from the project it grew in.

### Fixed

- Windows: `waiting.pid_gone` uses `OpenProcess` (not `/proc` or `os.kill(..., 0)`, which terminates on Win32); local busy
  markers use the same check.
- Windows: `tutti ship` puts `TUTTI_*` on the bash command line so WSL bash (which does not inherit the Windows process
  environment) still sees changed files and the commit for checks and deploy.
- Installers seed `~/.config/tutti/config.yaml` from the example when missing; final message says to edit that file then
  run `tutti config; tutti workers` (not the not-yet-existing `setup` / `doctor`).
- `TUTTI_PURGE=1` removes `$XDG_CONFIG_HOME/tutti` or `~/.config/tutti` on Windows too (not `%APPDATA%\tutti`).

### Added

- `tutti test`: fall-over routing over machines with slot queues; `ssh`, `wsl` and `local` transports; a capped Docker
  runner or plain bash.
- `tutti ship`: format, commit, targeted or full checks (local or routed), push, deploy; refuses the wrong worktree and
  protected branches; `--dry-run`.
- `tutti usage`: quota windows, the "spend it 1 hour before the reset" target (override with `lead_hours`) and a verdict
  per account.
- `tutti workers`, `tutti run`: CLI workers (Cursor, Antigravity, Claude) with a generated brief; ollama readiness checks
  that `GET /api/tags` lists the configured model.
- `tutti ask`, `tutti code`, `tutti agent`: local models through Ollama's HTTP API.
- Config discovery: `$TUTTI_CONFIG`, then nearest `tutti.config.yaml`, then `$XDG_CONFIG_HOME/tutti/config.yaml` (with
  `tutti.config.yaml` there as a fallback); a missing config names the exact path and the `cp` from the example.
- `tutti yield`, `tutti machines`, `tutti guard`, `tutti tunnel`, `tutti quiet`, `tutti wait`.
- `tutti translate`: translation of flat JSON catalogues with mandatory QA and an import gate.
- Docs: architecture, workers, usage pacing, machines, lessons, roadmap, feedback log, migration.

- One-line installers and uninstallers for Linux/WSL/macOS (`install/install.sh`) and Windows (`install/install.ps1`).
