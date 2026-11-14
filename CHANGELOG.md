# Changelog

The format follows Keep a Changelog; versions follow Semantic Versioning.

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
