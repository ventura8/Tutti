# Changelog

The format follows Keep a Changelog; versions follow Semantic Versioning.

**Versions (the owner, 2026-11-15).** Tutti stays 0.x. A big feature raises the minor version (0.N.0, patch back to 0),
a fix raises the patch (0.N.P+1). Many agents release at once: take the next free number when you merge, not when you
start (look at `git tag -l 'v0.*'` and the open pull requests), and rename your entry here if another release took it.

## [0.23.7] - 2026-11-18

### Added

- Automatic device power management: devices now power on automatically when Tutti needs them (if `devices.power_on` is enabled),
  with night mode adjustments applied, and power off when not in use.

## [0.23.6] - 2026-11-16

### Fixed

- While the main agent is in panic, the other accounts fill their room up to their own panic line (tasks that fit at
  their cost per task), not one task at a time (owner, prio 0).

## 0.23.5 (2026-11-16)

### Added

- docs/API.md: the API reference. Every `tutti` subcommand with its arguments and defaults, the exit codes, the dashboard's
  HTTP routes (method, role, request and answer JSON, errors; LAN or VPN only), the household coordinator's calls, the MCP
  tools with their parameters and roles, every config key with its type, default and who may change it, and the Python
  modules. The CLI, MCP and config parts are generated from the code (`python3 -m tutti.apidoc --write`), and
  tests/test_apidoc.py fails when a subcommand, route or tool is missing from it or a generated part is stale.

## 0.23.4 (2026-11-16)

### Fixed

- With a person at the main machine, Tutti's share there was one core and the whole machine's load counted against it,
  so online-worker runs (agy, cursor), which barely use the machine, were held ("load 5.5 of 1 cores"). Only the load
  beyond the person's cores now counts against Tutti's share.

## 0.23.3 (2026-11-16)

### Fixed

- While the main agent's account (autopilot.main_accounts, default claude) is in panic or spent, the other online
  accounts take over its bounded work one task at a time even when they are ahead of their own pace; their own panic or
  spent still stops them. Before, Cursor sat idle as "ahead of its pace" while Claude was in panic.

## 0.23.2 (2026-11-16)

### Fixed

- Local models: a dedicated pseudo-machine (`laptop-desktop`, `nuc-desktop`, `w5090-win`) no longer gets a card of its
  own with its host's models and power counted twice; its workers join the host's card and it is probed once.
- Local models: a failed power read is retried once and the models are still read from the worker's host; this computer's
  card never blames ssh. The GPU name falls back to `lspci`; the power line says measured or estimated per part, and a
  refused reading (intel_gpu_top without CAP_PERFMON, root-only RAPL) names its one-time fix.
- Local models: the period table no longer wraps its headers and numbers; it scrolls inside its card on a phone.

## 0.23.1 (2026-11-16)

### Fixed

- Tutti runs on native Windows again: the disk-space check used `os.statvfs`, which Windows lacks, so every command
  crashed. Free space now comes from `shutil.disk_usage` where `statvfs` is missing.
## 0.23.0 (2026-11-16)

### Added

- Device classes (`tutti/devclass.py`, `device_classes.<kind>.deny`, docs/DISPATCH.md): a per-class list of adb commands
  never run on that kind of device. A TV by default never gets `wm size`/`wm density`, the rotation settings or
  `cmd device_state`; a matching prep item is dropped, a run naming one is refused, and the run's script is told
  `TUTTI_DEVICE_CLASS` and `TUTTI_DEVICE_DENY`.
- A phone's or tablet's rotation, screen timeout and screensaver are read before a run and put back after it
  (`value: keep` prep items), whatever the test changed them to.
- `projects.<p>.runner`: the default runner of a project's suites; a runner lease checks the runner's adb reaches each
  device (`get-state`) before running, and `tutti test <Project> --device <d> -- <command>` runs there unsynced.
- `tutti test`: options may follow the track; a command word with a space or quote is quoted, the others stay raw; a project's
  `windows.<machine>` tree is used as it is with `--on <machine>`; `--fetch` of a Windows path copies it from where it is.
- Windows desktop runs (`tutti/winsession.py`, suite `desktop: {user, dir, before}`): a UI suite runs in the logged-on
  user's interactive session through a one-time `schtasks /ru <user> /it` task, its output followed and its exit code
  returned, instead of ssh's session 0 where UI automation finds no input desktop.
- `machines.<m>.dedicated`: a machine only the suites naming it use (never the work queue or an open suite), for a
  Windows desktop's UI runs beside the same PC's container machine.
- `shards_from` names join a suite's listed `shards`, which keep their own options (a TV-only class beside discovered ones).

### Changed

- A run whose filter matched no test (`0 tests match`, `No tests found`) is a FAIL in every suite, even with exit 0 (the last 30 lines)
  (`verdict.none` replaces the pattern).
- `requeue_on` patterns count only after the suite's `started` line: an app in front while the runner was still starting is
  no reason to queue again.

## 0.22.0 (2026-11-16)

### Added

- All household work through Tutti (docs/INTAKE.md, `tutti/intake.py`, `tutti/claudework.py`, backlog t156). The MCP
  server gains the tools a session needs: `backlog_claim`, `backlog_update`, `backlog_release`, `backlog_complete`,
  `lease_claim`, `lease_release`, `leases`, `usage_report`, `ask_owner`, `claude_task` and `outside_work`, each mapped to
  a CLI command: `tutti backlog claim|release|note`, `tutti lease claim|release|list`, `tutti needs ask`, `tutti intake`.
  A claimed task belongs to its session (the autopilot leaves it alone; an owner's answer brings it back to the session).
- Claude as a Tutti worker: `tutti run claude "<task>" --repo <repo>` adds a backlog task pinned to the claude worker,
  starts `claude -p` in its worktree when the claude account has room (queued while it is in panic or spent) and lets the
  autopilot collect it, publish it and apply the repo's review policy. `--no-backlog` runs it untracked.
- The intake guard: the dashboard's **Work outside Tutti** card and `tutti intake [--strict]` list commits in the
  autopilot repos and leases of the last 24 h that no Tutti task explains.
- `tutti connect --repo <path>` adds the "All work goes through Tutti" section to a household repo's AGENTS.md (or
  CLAUDE.md); the connected agents' guide names the new tools.
## 0.21.3 (2026-11-16)

### Fixed

- The Windows clean-up no longer runs `dotnet nuget locals all --clear`, which emptied the global packages folder that
  builds compile against (RoRadio's pipeline then failed with WMC0621, GenXbf.dll missing). It clears only NuGet's
  http-cache and temp folders.

## 0.21.2 (2026-11-16)

### Fixed

- The planner spends a short window's unused room while its weekly limit is at or behind its pace line (use it or lose
  it): the per-window share of weekly points no longer holds work then, and the weekly limit is projected along its pace
  line instead of this window's burst, so it is never reported as binding or "projected to reach 100 %". Ahead of its pace
  the weekly limit still holds the window; quiet hours still give it nothing.
## 0.21.1 (2026-11-16)

### Fixed

- A TV (an Android device with `android.software.leanback`) gets no keep-awake key any more. A TV stays on through its
  prep (stay on while plugged in, no sleep, no screensaver), and a key that arrived before an app had a focused window
  made Google TV raise an input ANR and kill the app under test (RoRadio's TV runs, 2026-11-16).

## 0.21.0 (2026-11-16)

### Added

- Dependency updates and vulnerability reports as autopilot tasks (`tutti/depbots.py`, `tutti security [scan]`,
  docs/SECURITY-INTAKE.md): for every autopilot repo, open GitHub Dependabot pull requests and Cursor's dependency
  pull requests (`cursor[bot]`, `cursor/` branches, dependency or security titles) become tasks that bring the bot's
  branch in, run the repo's check and fix what breaks; green patch and minor bumps are grouped into one task. Dependabot
  and code-scanning alerts and the security findings Cursor (Security Review, Bugbot) and Greptile post as review comments
  are deduped per package, rule and file, ranked by severity and queued worst first. Tasks drop by themselves when the pull
  request closes or GitHub marks the alert fixed. The dashboard's Dependencies and security card shows counts, the worst
  severity, links and, for an unreadable source, the exact settings link or `gh auth refresh -s security_events`.
- `scripts/check.sh` brings its own pinned shellcheck (`shellcheck-py`, hashed in requirements-dev.txt) into its
  cached venv when the machine has none, so the check runs unchanged on a machine without sudo (the NUC had none).

## 0.20.0 (2026-11-16)

### Added

- A review policy for rate-limited review bots, CodeRabbit and now Greptile (`review.coderabbit`, `review.greptile`,
  per repo under `review.repos`; Settings > Review, admin only): `wait` (the default) keeps the pull request unmerged
  (exit 4) and runs the review again right after the minutes the limit message names ("Next included review available
  in N minutes"); `fallback` has another reviewer review now and asks the bot again later, its findings handled in a
  follow-up when already merged; `skip` never asks it; `skip-when-unavailable` asks it and merges on the other reviews
  when it is limited. The re-requests are queued in the state folder and sent by the autopilot round.
- `review.rounds`: how many rounds (ask, fix, ask again) each reviewer gets per pull request, for copilot, coderabbit,
  greptile, a scout and `local` (the fallback worker); 1 each by default, 0: not called. A limit answer is no round.

## 0.18.0 (2026-11-16)

## 0.19.1 (2026-11-16)

### Security

- The Grok key never travels in a process environment (t116; it leaked into agent transcripts three times). Tutti reads
  `~/.config/tutti/grok.env` at the point of use; every command drops `TUTTI_GROK_KEY`/`TUTTI_GROK_URL` from its own
  environment at start (children, restarts, self-update, self-heal and failover inherit none); the dashboard re-executes
  itself without them; `tutti scout setup grok` no longer sources the file from `~/.profile` and removes the old line;
  process listings mask secret-looking values; `tutti doctor` names a key still exported in the shell. A test starts a
  real dashboard with the key set and checks its `/proc` environ.

## 0.19.0 (2026-11-16)

### Added

- Machine power, measured: a two-second benchmark per machine (one core's hashing speed, cores and threads, memory, GPU
  memory, NPU, synced disk write) runs once when the machine is free and again every `power.bench_days` (7). Its score
  times the headroom now (load per core, free memory, heat, Tutti's share in quiet, presence and limited hours) is the
  effective power. Keep busy sends heavy shards (e2e, builds, docker, full or long suites) to the strongest machine first
  and light ones to the weaker machines; a weak machine with under 30 % free takes no heavy shard. Each lease says why it
  went where it did (Machines card, `tutti dispatch status`).
- Local model tiers: every household model is light, mid or strong by its size class and quantisation, moved by its
  measured pass rate on Tutti's own task history (4 attempts or more). A task's difficulty (size S/M/L, a lint-fix or
  review at least mid, one up per failed attempt) picks the weakest tier that meets it, so easy work goes to small
  models and weaker machines and the strong models stay free; a failure escalates. The local models' cards show each
  model's tier and speed; a running task shows which model was picked and why.
- Yield flags have an owner, a reason and an expiry (default 4 h, `tutti yield renew`); the self-heal round lowers an
  expired one, or one whose owner's process is gone while the machine is idle. The machine card shows owner and age
  (backlog t135: a flag stayed up 15 h on an idle PC).
- A hung WSL (Windows answers, `wsl -e true` times out) is restarted when no flag or lease holds the machine (t136).
- Disk watch on every machine, the Windows C: of WSL machines too: a warning under 15 GB with the biggest reclaimable
  items, no new work under 8 GB (t137).
- Self-cleaning, daily or under 20 % free and never while the machine is held: Docker build cache, dangling and old unused
  images (never one the config names, t140), old logs, run worktrees and caches, old APK outputs and npm/NuGet caches on
  Windows, Remote Desktop traces on the owner's yes, the WSL disk compacted; "Reclaimed N GB this week" (t138, t139).

### Fixed

- Tutti's own tests no longer leave helper processes behind (about 129 a day that self-heal then stopped): the test runner
  stops a test's leftovers by exact pid again until none is left, so a loop's next `sleep` or fake `adb`, started between
  two scans, is stopped too.

## 0.17.4 (2026-11-16)

### Fixed

- Quiet mode now sees a person at a GNOME Wayland laptop. GNOME never sets logind's idle hint and the dashboard runs outside
  a login session (`loginctl show-session self` fails), so the idle time was always unknown and the "someone is at its
  keyboard" share never applied. It is now read from GNOME's idle monitor (`org.gnome.Mutter.IdleMonitor.GetIdletime`
  over the session D-Bus), here and on remote machines; a locked screen reads as away.
- While a person is at a machine, Tutti starts no new work there (`in_use_pct` 0, was 30): it goes to the other machines.
- The dashboard's quiet guard, every probe: Tutti's own `tutti quiet` jobs here (exact pids from their `.pid` file and their
  process trees) get nice 19 and idle I/O, are stopped with SIGSTOP while free memory is under the 4 GB kept for the
  person (`quiet.reserve_gb`) and continued after; the local Ollama models are unloaded.
- `tutti quiet status` (exit 0 while quiet, 1 when free), `tutti quiet wait [s]` and `tutti quiet guard`: one signal any
  script or agent can obey; commands run by `tutti quiet` while quiet run niced in a CPU-capped `systemd-run --user` scope.

## 0.17.3 (2026-11-16)

### Fixed

- Devices and controls: one card per device with one status. A device whose only runner is unavailable shows the same
  blocked pill and reason on its card (never "ready" under a "blocked" summary); the card holds why, what waits on it, its
  one action (Resume, Check again now, else Pause), where its shards went while away, its readings, and hours, Do Not
  Disturb, Cast and night mode as separate sentences. Device warnings and reroute lines are no longer repeated above the
  cards, and devices no longer repeat in the Pause list. Idle times read in plain words ("1 day 14 h", not "2298 min").
- An absence "until it is seen again" resumes by itself once the device is seen here for 2 hours, even if presence never
  saw it away first (pixel9pro stayed paused for a day while adb was ready).
- Device temperature from real sensors, labelled: the thermal HAL's skin sensor, else the SoC, battery only when the
  device has one (a Google TV no longer shows its meaningless 2.5 °C battery reading); "not readable" only when none answers.

## 0.17.2 (2026-11-16)

### Fixed

- Machine cards tell the truth (t130, t87): the slot boxes are busy (filled), held back by the share or the yield flag
  (hatched) and free (empty), always matching "F of N slots free"; a machine with its yield flag up has 0 free slots and
  "Tutti's share 0 % (its yield flag is up)" instead of 1 free and 30 %; "someone is at its keyboard" names the other
  reason; a slot count from shared lock files or the default says so (the laptop's 1 slot is its q.sh lock); a Windows
  PC without a CPU sensor says to start LibreHardwareMonitor; empty limited hours read "none set" with a "none"
  placeholder instead of a greyed 18-01.
- "Self-healed: N today" counts only real repairs; stopping a finished test run's helpers and removing old temp folders
  are counted apart as routine clean-ups (it read 132, of which 129 were test leftovers).

## 0.17.1 (2026-11-16)

### Added

- Tutti's own AI may also use household members' shared machines (opt-in, `ai_assist.household_models`: its prompts then
  go to a machine another person controls): a shared machine whose owner shares its model server is a candidate (its models
  read once a minute, a dead one every 10 minutes), never pre-empted; a paused or busy one is skipped.
- Sentry triage and scout drafting go through Tutti's own AI: a triage report without TUTTI-TASK lines gets them drafted
  (local first, online within pacing; only lines naming an issue in the report, held for the owner's approval), and the
  design review carries focus questions a local model drafted once a day.

### Fixed

- The unblocker forgives usage-limit attempts at most once a day per task, and only an hour after the last limit: forgiving
  faster fed a loop on a capped model (t12 went from 13 to 18 attempts on a spent per-model cap within the hour).

## 0.17.0 (2026-11-16)

### Added

- Tutti's own AI is local first and always the best free model in the household (backlog t80): one helper
  (`tutti/brain.py`) for every internal model use ranks every model on every machine by capability (tier, parameters,
  quantisation, task kind), skips down or yielding machines within a second, picks up a new bigger model at once, and falls
  back online (`ai_assist.online_worker: auto`: a subscription worker with room) within pacing. A test fails any module
  that calls a model directly. The dashboard shows *Tutti's own AI: <model> on <machine> (best free)* with the week's local
  and online calls and energy.
- Tutti first on the best model (`ai_assist.preempt`, default on; off or a list of kinds in Settings): other work on that
  model waits between its requests while Tutti's short job runs, then goes on with nothing lost; shown as *parked … for
  Tutti's own job (12 s)*.
- The unblocker (backlog t81): each round it clears blocked or failing work itself, local first (usage-limit-only
  failures forgiven, repeated failures diagnosed and retried with the fix, parked questions the history decides, rebase
  conflicts redone, red checks of open PRs rerun once); only true decisions reach the needs queue; every
  attempt is logged on the dashboard.

### Fixed

- A CLI worker stopped at the autocompact threshold no longer leaves its children running: the stop ends its whole
  process tree by exact pids (t128; `test_perf` left a `sleep 20` behind under load).

## 0.16.9 (2026-11-16)

### Fixed

- A limit message whose reset is given to the hour ("refreshes in 4 days, 13 hours") still finds its per-model cap: the
  reset may now be up to 70 minutes from the cap's own (was 10), so Gemini 4 Argon's message no longer lands on agy-gemini.

## 0.16.8 (2026-11-16)

### Fixed

- A spent per-model cap (an account with a `match.model` and no worker, such as `agy-gemini-argon`) takes its models out
  of the tiers until its reset: an agy-gemini run behind its pace names the account's own model or another tier with room
  (gemini-3.8-flash-high) instead of the capped Gemini 4 Argon, so it no longer fails on Argon's weekly limit.

## 0.16.7 (2026-11-16)

### Fixed

- A limit message that names no model goes to the account whose cap resets at the time it names: agy's "weekly limit"
  message for Gemini 4 Argon now marks `agy-gemini-argon` spent, not every Gemini model.

## 0.16.6 (2026-11-16)

### Fixed

- A reader's reading now replaces a newer limit message that says something else: agy-gemini no longer stays at 100 %
  from a job's "limit reached" while `agy /usage` reads it at 0 %.
- A limit message goes to the account with the most specific `match.model` (`gemini-4-argon` beats `gemini`), so a
  per-model cap such as Gemini 4 Argon's weekly limit can have its own account and not block the other Gemini models.
- `tutti self-update` asks again with backoff (15, 30, 60, 120 s) on a 404 while the latest release's `SHA256SUMS` is still being
  uploaded, instead of failing with a 404 right after a release.

## 0.16.5 (2026-11-16)

### Fixed

- `tutti usage refresh` no longer crashes on agy's disabled `3p-5h` bucket (no `reset_time` while the 3p weekly is spent):
  a disabled bucket reads 100 % used until its group's weekly reset, a bucket without a reset reads with none, and one
  broken account no longer stops the other accounts' readings.

## 0.16.4 (2026-11-16)

### Fixed

- The autopilot's retry cap holds: after a person's retry a task gets half of `autopilot.max_attempts` more, not a full
  new round (tasks reached 12 and 14 attempts with a cap of 8).
- A step that failed a task only because its account hit its usage limit stays a candidate once the account has room
  again (agy-gemini had room for 50 and took nothing); the stopped line now names the real reason.
- `scripts/check.sh` makes its own venv from `requirements-dev.txt` when the machine's python3 lacks Playwright, so
  the work-queue job `tutti-check` runs on a plain system Python.

## 0.16.3 (2026-11-16)

### Fixed

- After a failback the main machine was read-only for a round ("this machine is not the active main machine now (Tutti is
  back on laptop)"): it resumed without a lease. It now resumes with a fresh lease at once (the backup has just stepped
  down). A test runs the whole cycle: take-over, failback, writes allowed on the main.
- A busy main no longer loses its backup: the heartbeat goes first and on its own (one retry, ssh waits up to 15 s), the
  state copy only after it, and the default grace (`failover.minutes`) is 5 minutes instead of 3.

## 0.16.2 (2026-11-16)

### Fixed

- The self-heal loop now really reaps strays (t124): the failover service kept the code it started with, so a service
  started before 0.16.1 never ran the `strays` check, and 76 device-claim loops pushed the laptop's load to 70. The
  service now exits with code 75 when any file of Tutti's code changes, and systemd starts it on the new code.
- `tutti test`, routed and local checks (`ship`, `run --check`, the autopilot's acceptance check) set
  `TUTTI_NO_DAEMONS=1` on the command itself, so an old branch's tests (before 0.16.1) can no longer start helper loops.

## 0.16.1 (2026-11-15)

### Fixed

- Tutti no longer leaks long-lived processes (the laptop at load 60, 2026-11-15). Tests start no keep-awake loop, tunnel or
  inhibitor (`TUTTI_NO_DAEMONS`), and `tests/run.py` fails a test that leaves a process behind. A device's keep-awake loop
  is one per device and ends when its folder is gone; a tunnel's loop is one per tunnel (`flock`); the sleep inhibitor is
  one per Tutti process, held only while a job runs and ended with Tutti (`tail --pid`), never `sleep infinity`.

### Added

- Self-heal's `strays` check and `tutti reap [--dry-run]`: Tutti's own helper processes that nothing owns, stopped by exact
  pid and logged (docs/SELFHEAL.md, *Stray processes*).

## 0.16.0 (2026-11-15)

### Added

- "Newest artifact, else a guarded build" for a suite's build step: `prepare.reuse: {paths, newer_than}` uses an artifact
  newer than the commit as it is; a needed build waits in `prepare.guard.hours` and while its machine yields
  (docs/DISPATCH.md). RoRadio's e2e suite reuses its newest E2E APK and runs on the PC (`runner:`) again.

## 0.15.1 (2026-11-15)

### Fixed

- A TV no longer starts a run with its screensaver able to come on (RoRadio, 2026-11-15: the Google TV Streamer's photo
  screensaver covered the app; its prep had logged "the device does not answer" and run anyway). A read that gets nothing
  reconnects by the mDNS name and reads again with a 45 s timeout; on a TV, screensaver_enabled,
  screensaver_activate_on_sleep (new, `secure`), screen_off_timeout, sleep_timeout and stay_on_while_plugged_in are
  required and read back after they are set: one that cannot be read or does not take stops the run with the reason.
- A suite on a runner machine (`runner: pc`) waits while that machine yields (its yield flag up: the PC is running another
  project's pipeline), is held, paused or off; `tutti devices lease --runner` takes nothing then (exit 3).

## 0.15.0 (2026-11-15)

### Added

- Self-healing (docs/SELFHEAL.md, the owner, 2026-11-15): every `selfheal.every_seconds` (300) on each machine running the
  failover round, ten checks each with its repair: declared needs installed, worker CLIs present and signed in (a
  `login_check`), ollama up with its models, tunnels up, the adb server and wireless devices, disk and inodes, the
  dashboard, state files (their snapshot), the config (its last good backup) and self-update. Every repair is logged;
  the Machines card shows "Self-healed: N today" with the details; `tutti selfheal [--once]`. Only a person's step (sign
  in, a root command, room the disk still lacks) becomes one Needs-you item.
- Declared needs (backlog t95): `needs:` per project, suite and worker (npm, pip, Playwright browsers, ollama models,
  docker images by digest, dotnet workloads, pinned downloads with sha256, apt). A dispatched suite installs its needs on
  its machine before it runs; a missing tool is installed at user level, never piped into a shell, not reported as a
  failure; what needs root is one admin command in Needs you. Unpinned needs are config errors.

### Fixed

- Temp janitor (backlog t86): Tutti's own left-over temp folders (`tutti-tests-*`, `tutti-t-*`, `tutti-review-*` and
  `tmpXXXXXXXX` folders holding only Tutti test files) older than 2 h are removed every self-heal round, in the temp
  folder, `~/.cache/tutti-tmp` and `selfheal.temp_dirs`; the test helper names its folders `tutti-t-*`. This replaces the
  stopgap cron.

## 0.14.1 (2026-11-15)

### Fixed

- A lease never holds devices while nothing runs on them (RoRadio, 2026-11-15: a waiting runner held the Pixel Tablet and
  the Google TV Streamer for 25 min and blocked another session). `tutti devices lease` (and a runner suite's lease) takes
  the devices only when the command starts, for 5 minutes, renewed every 2 minutes while the command runs, and releases
  them when it ends; a holder that crashed or only waits lets them lapse within 5 minutes.

## 0.14.0 (2026-11-15)

### Added

- Connect GitHub (docs/GITHUB.md): Tutti's own GitHub connection, so a household without the GitHub CLI works. The My
  GitHub card (and `tutti github connect|status|disconnect`) runs GitHub's device flow, a code and a QR, through Tutti's
  GitHub App with least-privilege permissions; each person's token is kept mode 600, refreshed by itself and removed by
  Disconnect, and never reaches a log, argv or a worker's environment.
- One GitHub client, `tutti/github.py`: the autopilot's pull requests and merges, the Projects panel's pull requests, CI,
  reviews and now the latest release, Report a problem (filed under the person's own account), `tutti doctor` and the
  releases-stuck check go through the API with the person's, the household's or `gh auth token`'s token; anything else
  (self-improvement's gate reads and `tutti pr review`, maintainer features) still runs through `gh`. Reads use ETags, a spent rate limit waits for GitHub's reset; no webhooks.
- `tutti github manifest` and docs/github-app.html: the official App's manifest for the maintainer to create it once.

## 0.13.1 (2026-11-15)

### Fixed

- The backup main machine's own copy of the config was refused there ("failover.backup ... cannot be the backup"): on
  its copy the backup is `host: local`, and it stays a candidate.

## 0.13.0 (2026-11-15)

### Added

- A backup main machine with automatic failover (docs/FAILOVER.md): an admin picks it (Settings, Machines; `tutti main
  backup <machine>`), or Tutti elects the healthiest always-on machine with a UPS or the most uptime. The main pushes a
  small, frequent copy of its state (no logs or caches; mode 600, to the backup only); when the main is silent for 3
  minutes or shuts down, the backup takes over the dashboard, coordinator, autopilot and device leases; when the main is
  back and healthy, it catches up and Tutti returns to it. Terms and a lease make split brain impossible; members and
  clients follow the failover list by themselves. `tutti failover setup|status|tick|run|leave`.
- `tutti state size|prune` and `tutti doctor`: the state's size against a 50 MB budget, its biggest items, old runs,
  screenshots and logs trimmed; a watchdog repairs corrupt state from the last good snapshot and restarts a crashed
  dashboard.
- Tutti's own AI asks the largest local model that is up and free on any household machine, skips busy ones at once, then
  goes online (`ai_assist.online_worker`) (backlog t80).

## 0.12.0 (2026-11-15)

### Added

- A suite can run on a named runner machine with its devices on another (`runner: pc`): each shard leases its devices
  (and a pair's partner, both or none), the runner's own adb connects to the lease addresses, the command runs there with
  `{addr:<device>}` filled in, then adb disconnects and the devices are released. `tutti devices lease <d> [<d2>]
  --runner <machine> -- <command>` does it by hand (docs/DISPATCH.md, *A suite on a runner machine*).

## 0.11.0 (2026-11-15)

### Added

- Work follows the devices that are here: a device that is away (a planned absence, presence lost, a person using it, its
  blocked hours, paused) has its pending shards moved to devices that can run them (the same form factor, the API level
  and capabilities a suite asks for with `requires:`); work no other device fits waits with the reason, and it all goes
  back when the device returns. The dashboard says it ("Pixel 9 Pro away (planned absence): 3 shards moved to
  pixeltablet, 1 waits for it"), each change is an event and a line in `<state>/devices/moves.jsonl` (docs/DISPATCH.md).
- Paired devices: `devices.<d>.roles: [cast-receiver|cast-sender]` and a suite's `pair: {role: cast-receiver}` hold a
  partner device, so both are leased at once; `tutti devices claim <a> <b> --who <s>` leases a pair, both or none.
- Cast roles detected, not typed (tutti/castroles.py): a device is a receiver (a TV, the Cast receiver service, or Play
  services' mirroring receiver: a Pixel Tablet's hub mode, a phone) and a sender (Play services' Cast on a device with a
  screen), read once a day and at pairing; a Windows, Linux or macOS machine is a sender with Chrome, Chromium or the
  project's desktop sender (`machines.<m>.desktop_sender`). The device and machine cards show it; `roles:` overrides. A
  Cast suite (`pair: {role: cast-receiver, sender: true}`) runs on any sender here with any receiver here.

## 0.10.1 (2026-11-15)

### Security

- Every remaining audit finding fixed (docs/SECURITY-AUDIT-2026-11.md, *Follow-up*): the source installers take the newest
  release tag and hash-checked requirements (`requirements*.txt` now carry hashes, from `requirements*.in`); the uv and pipx
  lines name a release tag; Android platform-tools are one version checked by SHA-256; at most 6 sign-in codes a minute
  from the device flow; a member's own share is capped at 1 unless the admin sets it, with caps on projects and lease keys;
  credentials on watched command lines are hidden from the dashboard; links from data are never `javascript:`; an invite
  join is proved before a household is founded; the dashboard without sign-in serves only this login on macOS (lsof) and
  Windows (the TCP table and the process token) too; the work browser's tab limit holds across processes.

### Fixed

- `release.yml`: a dispatch for a tag without a release creates the signed release; a dispatch on a branch stops with the
  fix. v0.9.3 and v0.9.4 are published and signed.
- `tutti doctor` and the dashboard warn "releases stuck: N merged version(s) unpublished" with why and the fix.

#### Review findings left open on merged pull requests

- Chat: outside Auto, two refused tool calls no longer end the answer (the model is told why and tries again; an answer
  with only refused calls says so in plain words); the refusal and not-sure checks accept the curly apostrophe; "review
  the test results" is no longer code work; an attachment anywhere in the history the model sees marks the answer as
  reading untrusted data; repository file lists use `git ls-files -z`.
- Devices lease: the requester's name and the device's values are shell-quoted; the in-use check and the claim write
  hold a lock; one keep-awake loop per device, which restores the device as soon as the lease runs out; `tutti devices
  lease` stops with the reason when the address is unknown or `adb connect` fails; `--minutes` must be 1 or more.
- Needs: a default or an answer in another case names the listed option; a copied `OUTPUT>>>` in a run's output cannot
  end the prompt's data block; the backfill words at most 5 questions per round and records telemetry only when it made
  one; a Chat message `q3 <free text>` is no longer taken as an answer; `autopilot.stuck_minutes: 0` turns the watchdog
  off everywhere (it was refused by the check); docs/NEEDS.md no longer names a setting `autopilot.backoff`.
- Scout week: a `late` account counts as behind; a failed scouting send is asked again; merged pull requests are listed
  up to 500 (was 30); audits are sent outside the scouts lock, a missing webhook secret is reported instead of ending the
  round, and a failed audit is retried on its own record after 6 hours, at most 3 times, instead of a new record each
  round.
- Dashboard: the plan text's number tidy no longer uses a regex lookbehind, which broke the page on Safari before 16.4.
- Work queue: a heavy `where: here` job waits in this computer's limited window or quiet hours like any machine's;
  findings are matched against the whole run log (its last 8 MB when longer), not only its last 16 KB.
- The machine-limit API names `machines.<m>.limit.pct` and its 0 to 100 range when pct is not a number;
  `pacing.dates` that is a list or a string is a config error, not a crash.
- The idle probe sent over ssh is POSIX sh (no bash-only `< <( )`), so a machine whose login shell is dash reports its
  idle time again.
- Work browser: a task takes a downloaded file only up to `browser.max_file_mb` (new setting, 50 MB), with a clear
  refusal above it; a `read` whose `url` is not text is refused with the fix instead of failing inside.
- docs/DASHBOARD.md: Resume ends a planned absence, not the device's blocked hours.

## 0.10.0 (2026-11-15)

### Added

- Night mode for Android devices, on by default (`night: {enabled, hours: 22-08, devices: []}`): while Tutti holds or
  works a device in those hours it runs at 1 % brightness with every volume at 0 (music and Cast, ring, notification,
  system; the alarm stays the person's own), in place of the day's brightness and volume. The person's own values come
  back at hand-back, a night prep is put back by a day release (the saved file keys items by name now), and a person's
  changes are never undone. `tutti devices night [on|off|hours 22-08|only <d,...>|all]`, Settings (*Night mode*) and the
  device card.
- A device's own Do Not Disturb: the readings report it (`settings global zen_mode`) and its schedule is learned per
  local hour (Android exposes no rule list to adb); the device card shows "Do Not Disturb on now, usually 22-07".

### Fixed

- A person who picks up a device during a run (the owner setting an alarm on the phone) is noticed and left alone:
  `getevent` sees only physical touches and keys (the test's taps and Tutti's keep-awake key are injected and never reach
  it). The run stops cleanly and its shard is queued again (not a failure); no key is sent; nothing starts on the device
  until it has been idle `devices.<d>.idle_minutes` (3). The device card says "in use by a person — waiting".
- A setting a person changed while the device was in use is never undone: at release an item goes back only while it
  still has the value Tutti set (an alarm, their brightness stay).

## 0.9.7 (2026-11-15)

### Fixed

- Chat on Auto hands a request the local model cannot handle to an online model with room (docs/CHAT.md, *Escalation*):
  before answering when it would not fit the local context, has an image and no vision model, is code work for a model
  under 14B, or the effort is max; after answering when the local model declined, was not confident, gave nothing, called
  its tools wrongly twice or timed out, with the same context. The answer says "Answered by <model> because <reason>".
  Pacing and opt-ins hold: an account without room is never used.

## 0.9.6 (2026-11-15)

### Fixed

- A session on another computer could not use a device that moved (RoRadio, on the PC, after the devices moved to the
  NUC). `tutti devices claim <d> --who <s> [--minutes 120]` is now a lease: exclusive (exit 3 with who holds it), the
  device prepped and kept awake while it lasts, the address for the requester's own adb printed (`ADDR=`), and over by
  itself at its end time. `tutti devices lease <d> --who <s> -- <command>` does claim, adb connect, the command, adb
  disconnect and release in one (docs/DEVICES.md, *Using a device from another computer*).

## 0.9.5 (2026-11-15)

### Fixed

- Dashboard: folds, Show more and the filters stay as the owner left them across refreshes and reloads (kept by item id
  in sessionStorage); a task's "N attempt(s): failure history" no longer collapses on the next poll.
- Dashboard: Pause or resume and Devices and controls list the config's machines, devices and local workers as
  "checking" until the first probe answers, instead of "No work machines in the config" and "0 devices".
- Dashboard: a running agent task's context % is the newest turn's prompt against its model's window (1M for current
  Claude models), not 174-361 % of a 200k default; nothing is shown when the window is unknown or exceeded.
- Dashboard: the Private network card's POST crashed (`AttributeError`: the raw body was used as a dict), which was
  both "Failed to fetch" on the card and Insights' "AttributeError in api"; any body now gets an answer.
- Dashboard: a hidden element stays hidden (`[hidden]` rules beside classes that set a display), so the empty Users view (Invite, Invites, Changes) no longer
  shows on the main dashboard; it stays in the Users tab for an admin with sign-in on.
- Telemetry: a refused send says which cause it is: Sentry's reason (`ProjectId`: the project was deleted or moved, or
  the key disabled or rotated), a clue (the DSN from `$TUTTI_SENTRY_DSN`, or when the DSN file was written), and the fix. A send cut off midway
  gives its items back to the queue.
- Autopilot: agy's quota message "Resets in 147h44m29s" is read (the units touch), so the spent account stops getting
  work; a worker's exit says whether it hit its limit or its time; a CLI with an allow-list may read and write its own
  worktree (headless agy auto-denied every read); the same failure twice on one step drops that step for the task, and
  twice on different workers parks it (no more 23 wasted runs on one failure).
- Autopilot: a result that does not rebase onto its base never waits for a person: it is closed as superseded when the
  base already has it, else redone on the fresh base with the old diff as a guide (parked only after 3 redos); a worktree
  with uncommitted edits is never redone, it waits with a note.
- A full disk stops work instead of every command: `tutti doctor`, the Machines card and the autopilot and dispatch
  rounds check the temp folder and the work disk. Under 10 % free file slots, or under 10 % free space with less than
  20 GB left, they say so in plain words with the fix, and no new work starts until there is room (`tutti/diskroom.py`).
- Dashboard: "My main machine: … (no other machine of yours to move to)null" lost its "null" (a missing value handed to
  `replaceChildren` is printed as text). A test renders every tab in headless Chrome, with the example config and with a
  single computer, and fails on "null", "undefined", "NaN" or "[object Object]".
- Dashboard: the Private network card reads its state with `GET /api/vpn/status`, 200 for every signed-in viewer and with
  no VPN set up; the page's POST was an admin's and failed on load for others (Lighthouse best practices).
- Safe heat by default: a machine without a `heat` block gets `{cap_c: 85, stop_c: 95, hot_slots: 1}`, judged on the
  hotter of its CPU and GPU (a laptop ran at load 16/16 and 95 C with no limit); `tutti setup` writes it, and the
  machine card shows the cap and the stop.
- The tests clean up after themselves: `tests/run.py` (run by `scripts/check.sh` in its own temp folder, removed at the
  end) gives each test a temp folder and fails a test that leaves anything there. About 45,000 left-over test folders
  had filled /tmp's file slots. Three autopilot tests no longer read this computer's load and disks.

## 0.9.2 (2026-11-15)

## 0.9.4 (2026-11-15)

### Added

- Browser tests for every dashboard tab (docs/TESTING.md, *Browser tests*), run by `scripts/check.sh` on every pull request
  as a hard gate: Playwright's Chromium (`playwright==1.65.2`) and axe-core 4.12 (`axe-playwright-python==0.1.8`), pinned
  in `requirements-dev.txt`. The admin and a viewer sign in through a fake Google; phone, tablet, laptop and 4K, light and
  dark, 200 % zoom and WCAG 1.4.12 text spacing. Layout: no overlap, no clipped or truncated text, no sideways scroll,
  nothing past the viewport. Accessibility: axe WCAG 2.2 A/AA and best practices, keyboard (every control reached, focus
  shown, no trap, Esc), 24 px targets, contrast in both themes, landmarks and headings, labels, live regions, reduced
  motion, icon names, form errors. Roles: no admin control for a viewer, all of them for an admin.
- One formatter for every percentage and number on every card and tab (`web/format.js`): locale-aware, rounded by size,
  never float noise. The grok-bot card's `32.122999999999976 %` reads `32%` (replaces backlog task t60).

### Changed

- **Machines** sits directly below **Usage & pacing**, full width; **Autopilot** is full width. Usage & pacing keeps its
  order: online agents, Local models, You and the main agent.
- One worker status for `tutti workers` and the dashboard (`workers.statuses`): a local-models card is available while its
  worker answers through its tunnel, even when the machine's ssh probe fails, and offline only with the worker's own
  reason. The dashboard reads its config again when the file changes, so a worker added after it started is probed.

### Fixed

- The "nuc worker unavailable" mismatch: the machine probe took `nvidia-smi`'s driver error (printed on stdout on the NUC,
  which has no NVIDIA driver) as the GPU field, the summary line did not parse, so the NUC and its Local models card read
  offline while `tutti workers` reached its Ollama through the tunnel. A probe line with such text now keeps the machine
  reachable with no GPU reading.
- What the browser tests found (fixed in the UI, no check relaxed):
  - a job's title cut with an ellipsis and overlapping its time;
  - a long chip pushing the phone page sideways;
  - local-model cards overflowing into each other on a phone;
  - long e-mails and kinds overlapping in rows under text spacing;
  - controls under 24 px (fields, selects, checkboxes, disclosures, card links);
  - a date field with no focus indicator;
  - the vacation date field shown while marked hidden;
  - the slot pips' and device actions' ARIA, and an empty table header;
  - no announced error for a refused form value;
  - Esc did not close an open disclosure;
  - the phone's bottom tab bar ran past the screen's edge with four tabs (Chat, Dashboard, Settings, Users);
  - the config path and My main machine in the app bar cut with an ellipsis;
  - a select with a long option (My notifications) ran past a phone's edge.

## 0.9.3 (2026-11-15)



### Security

- **Signed releases** (docs/SECURITY-AUDIT-2026-11.md, H8). The release workflow signs `SHA256SUMS` keylessly with
  Sigstore (GitHub OIDC, actions/attest-build-provenance pinned by SHA) and attaches `SHA256SUMS.sigstore.json`.
  `tutti self-update`, `bootstrap.sh` and `bootstrap.ps1` check it with `gh attestation verify` (signer
  `ventura8/Tutti/.github/workflows/release.yml`, made on the version's tag) before the tarball's checksum, and install
  nothing otherwise, saying why. `--insecure-skip-verify` (`-InsecureSkipVerify`) is the offline fallback, with a loud
  warning, never used by the daily or login update. An older gh than 2.49 gets Tutti's own pinned copy in the bootstrap.
  Releases before 0.9.2 have no signature (docs/INSTALL.md, *How a release is checked*).
- The work browser's worker broker refuses a negative body size and closes silent connections.

## 0.9.1 (2026-11-15)

### Fixed

- The scout said "192 run(s) can go now (6 useful item(s) ready)" while `tutti scout tick` sent nothing: the 6 were
  watches due only later, and everything else was reviewed. The Do line now says how many are ready now and how many
  later. While the scout is behind its pace, so its allowance would be lost at the reset, it does useful work instead of
  idling: watches every `min_gap_hours`, a scouting question for each queued backlog task not started yet, and an audit
  of each pull request merged in the last 7 days.

- Tasks parked before the needs-input queue existed get their one question (options and a default) at the dashboard's
  start or with `tutti needs backfill`, worded by a local model from the last failure (its output marked as data, only a
  listed option as the default), the template when no model answers, so they take the same answer flow (docs/NEEDS.md).

## 0.9.0 (2026-11-15)

### Added

- The Chat works like a chat IDE (docs/CHAT.md, *Modes* and *Controls*):
  - **Modes**, per conversation with a default in your settings: **Autonomous** (the default: it acts, then shows the card
    with Undo), **Ask before acting** (Confirm on every change) and **Plan review** (it proposes a plan; you leave steps
    out or change their text, then the whole plan runs, each step with its result and Undo). The server's role limits,
    the viewer's daily limit, pacing and opt-ins hold in every mode.
  - **Effort** (Auto, low, medium, high, max), mapped to each model's real setting: Ollama's thinking and `num_ctx`
    (within `workers.<n>.max_ctx`), Claude Code's `--effort`, `workers.<n>.effort_args` for other agents, and the tool
    rounds an answer may take.
  - Under the message box: the model, effort and mode pickers, the context meter, and the conversation's tokens with what
    they would cost at API prices. Older messages are summarised by the model itself when the context passes your
    threshold (`autocompact.threshold_pct`, 60 %), and the chat marks where.
  - **Edit and resend** any of your messages, **Regenerate**, and **Branch** a new chat from any message.
  - **@-mentions** add a project, a job, a machine or a repository's file (`@file:map/src/app.py`, never outside the
    repository or a secret) as context, with a keyboard list.
  - **Attachments**: images (paste a screenshot; PNG, JPEG, WebP, GIF up to 1 MB) beside text and logs; a long pasted log
    is attached. Models with `workers.<n>.vision: true` see images.
  - **Slash commands** `/new`, `/model`, `/effort`, `/mode`, `/clear`, `/help`, and keyboard shortcuts (Ctrl+K search,
    Alt+N new chat, `/` to the message box, `?` for the list).

### Fixed

- A local model said it had queued tasks without calling a tool: the prompt now forbids claiming an action no tool call did.
- A model's `XS` backlog task failed (`tutti backlog add` takes S, M or L): it is queued as S.
- Mentions and attachments are data, never instructions: fenced in the prompt, and an answer that read any asks before
  acting even in Autonomous mode. Viewers mention projects, jobs' lines and machines (no files or job logs); a file is read
  only when git tracks it. Images live in their own files (600) beside the user's chats; only a message may be large.
- A plan always ends in a state (a crashed step no longer leaves it running for good), and one cut off by a restart goes on.

## 0.8.0 (2026-11-15)

### Added

- Recurring blocked hours: `tutti devices hours <device> <HH-HH>[,HH-HH] [--days mon-fri] | off`, the device card's **Set
  blocked hours** and Settings (`devices.<d>.hours`). Daily local hours when the device is not used at all (a window past
  midnight belongs to the day it starts on). They work as planned absences do: no new work from 30 min before, running work
  handed over at the start, the device's own settings restored when its run ends. None by default; kept in the config, so
  `tutti move` carries them.
- `tutti machine limit <m> <HH-HH> 0`: 0 % means the machine is not used at all in that window (the same blocked hours).

### Fixed

- The dashboard reads its config again every round, so time-bound switches (planned absences, blocked hours) start and end
  as the clock passes them, not only after a restart.

## 0.7.0 (2026-11-15)

### Added

- Run to completion (docs/NEEDS.md): a failure climbs the ladder at once and each further one waits 1, 2, 4... minutes
  (at most 30); a watchdog ends a run with no new output for `autopilot.stuck_minutes` (30) and queues it again; one
  failure never stops the rest, and a blocked round says why on the Autopilot card.
- The needs-input queue: a task that needs a decision is parked with one question (what, why, options, a default) and the
  rest goes on. The autopilot asks it when it parks a task (retry, from scratch, or drop), a worker with a
  `TUTTI-QUESTION:` line. Answered in the Chat (`q3 retry`), on the Me tab's *My questions* card, with `tutti needs answer`
  or through the main agent (MCP `pending_questions`, `answer_question`); the task resumes with its tries counted afresh.
- At work the questions reach the main agent and a *Questions from Tutti* chat in batches (one every 30 minutes, urgent
  at once); off work they wait for the welcome-back summary's *Needs you*. *Get questions outside work hours*, a per-user
  opt-in (off by default): off work they still come once N wait (default 3), urgent ones at once, never in the user's
  quiet hours. `tutti user notify always|work-hours [--batch N] [--quiet HH:MM-HH:MM] [--for <email>]`, the Me tab, and an
  admin for any member from the Users tab, audited. Admins see everyone's questions, members their own.

## 0.6.0 (2026-11-15)

### Added

- **The work browser** (docs/BROWSER.md): the household's one Chrome on one machine (`browser.machine`, default this
  one) for every browser task through Tutti, users' and workers' alike. `browser.mode`: `existing` (the owner's own
  Chrome profile and its sign-ins; Tutti only reads its DevToolsActivePort and never starts, restarts or closes it),
  `dedicated` (Tutti's own profile, DevTools on 127.0.0.1) or `isolated` (a fresh Chrome as the `tutti-browser` user in a
  systemd sandbox, no TCP DevTools port: a Unix socket for Tutti's broker only). One connection, one tab per task closed
  after, a queue with `browser.max_tabs`, idle close.
- **The broker**: allowed sites per task type (`browser.scopes`), every action logged without content or queries, a
  page that leaves the scope returns nothing, downloads per task. Workers on other machines use it through
  `tutti browser remote <machine> <url>` (a one-time token on 127.0.0.1, forwarded over ssh), never the DevTools port.
- `tutti browser status|sign-in|sites|read|page|remote|isolate|close`; the **Work browser** card (machine, mode, tabs per
  user or worker, the queue, each site's sign-in with **Sign in to <site> on the work browser**, Check sign-ins); Settings
  → Work browser; the move plan lists `tutti browser isolate --script | sudo bash` when the work browser moves.

### Changed

- Tutti no longer opens the owner's browser by itself: `tutti vpn` (Tailscale), `tutti household join` (the invite) and
  the first `tutti setup` (the dashboard) print the link for the owner to click.

## 0.5.1 (2026-11-15)

### Security

The November audit (docs/SECURITY-AUDIT-2026-11.md): 2 critical, 9 high and 11 medium findings, each fixed with an
exploit-style test; signed releases (H8) followed in 0.9.2. The changes you may notice:

- A household member's job runs from plain files (`git archive`) in a container on the machine and runner that really run
  it; a job naming a device needs a device of that shared machine with a container runner (or `member_shell`).
- Another member's task `check` and a spare-room offer's `check` are never copied; the coordinator signs its answers; a join
  never takes over a member's id; a removed member needs a code made after the removal.
- The dashboard without sign-in serves only this login (Linux: another user's process on 127.0.0.1 is dropped); a viewer can
  no longer act for the owner from the Users tab; the chat never passes a task `check`.
- Every server closes connections silent for 30 s and caps requests at once; the coordinator refuses a negative body size.
- `tutti mcp --http` on a LAN or VPN address is a viewer unless `mcp.role: admin`; another site's Origin is refused.
- A scout's answer counts only from its account (`scouts.<name>.github_login`) or a repository writer. **Upgrade step:** a
  scout that posts as an account without write access needs `scouts.<name>.github_login` set; until then its comment is
  ignored and the request says so.
- The autopilot merges only the reviewed commit and never a branch that changes its own gates; self-improvement asks the
  reviews again after a new push, and `tutti pr review` exits 3 when the local review has findings.
- Workers and reviewers run without GH_TOKEN, Sentry, npm and the scouts' variables; tasks from outside text carry a
  safety note; a Sentry-suggested check is never passed on; a local conductor model may not pass `--check` or touch files
  outside its folder.
- npm CLIs update to versions out for 3 days (`--before`); slot.sh keeps the caller's umask and never follows a lock link;
  the activity scan uses a private temp folder; `machines.<m>.host` may not start with `-`.

## 0.5.0 (2026-11-15)

### Added

- Limited hours for any machine (docs/FLEET.md, *Limited hours*). In a daily window (days and hours, past midnight too) Tutti
  uses at most `pct` % (default 60) of it: fewer slots, fewer model threads, a lower adaptive limit and no new heavy jobs.
  Set it with `tutti machine limit <m> <HH-HH> [pct] | off`, the machine card, or Settings. `main: true` follows the main
  machine on `tutti move`.

- A day with other owner hours (docs/USAGE-PACING.md, *A day with other hours*). Set it with `tutti owner window <date> <HH-HH|off|config>`,
  the owner panel's **One day with other hours**, or `pacing.dates`. That day the main agent's reserve follows its own
  window, and its usual hours run the agents at full speed. A vacation that ends before it hands over at midnight.

## 0.4.0 (2026-11-15)

### Added

- Planned absences: `tutti pause <device|machine|...> <name> --from <time> --until <time|unknown>` (local time: "2026-11-16
  07:00", "tomorrow 07:00", "07:00"). Nothing new is scheduled on it from 30 min before; at the start its running work
  is handed over; it ends at `--until`, or, left open, when presence has seen it away and then back. The dashboard's row
  shows **away later** / **away** with the window (docs/DASHBOARD.md, *Controls*). An end before the start, and `--from`
  with `--stop-now`, are refused.

### Fixed

- `tutti rsh <wsl machine>` (backlog t38): a script with `;` or new lines failed ("unexpected end of file") and a command
  lost its first word, because the first argument was always dropped as the ssh host. The host is now dropped only when it
  is the machine's host or LAN address (git and rsync put it first); one word runs as a bash script (base64-encoded to
  WSL), several words as a command with each argument quoted. `tutti rsh w5090 adb.exe devices` works.

## 0.3.0 (2026-11-15)

### Added

- Welcome back, on for every user (docs/WELCOME.md): when a user returns (the owner's first prompt or the end of their
  away time, their laptop or main machine present again, or a first dashboard visit after a gap), one summary of the time
  since they left: what shipped, what runs, what failed or was parked and why, usage by each reset, and a numbered list of
  what only they can do, each with its exact step and a link. A local model writes the opening words (the template when
  AI is off). A viewer sees their own and the household's items, an admin everything. The dashboard's *Welcome back*
  panel and *What did I miss?*, `tutti welcome [show|miss|on|off]` and the MCP tool `welcome_summary`; each user may turn
  theirs off.
- AI assist: Tutti uses AI, local and online, to make itself better; off until an admin opts in (`ai_assist.enabled`,
  Settings or the dashboard), with separate switches for local AI, online AI and code to online AI (docs/TELEMETRY.md,
  *AI assist*). A local model reads Tutti's own telemetry and writes ranked proposals that become self-improvement tasks
  with their evidence and a regression-test-first brief; failed attempts are classified flaky, environment or real
  (flaky and environment retry on the same step instead of escalating) and summarised for the retry; queued tasks get
  acceptance criteria and a size. Each has a fallback with no AI.
- Auto-tuning: small changes to Tutti's own parameters apply by themselves within bounds (two a day), logged with undo;
  larger ones wait for the admin. The dashboard's *Tutti is improving itself* card shows proposals, tasks done, tuned
  parameters with Undo and the measured gains. `tutti ai-assist [status|tick|undo <id>]`.
- Online research and design review through the scout (`tutti-ai-*` watches), sent only with spare quota (`spare_only`:
  the account behind its pace), never the owner's reserve; what goes online is scrubbed and is never code.

## 0.2.0 (2026-11-15)

### Added

- The compute work queue (docs/FLEET.md, *The work queue*). Machines whose model is busy or missing take `work_queue.jobs`:
  each project's gates and tests on its latest commit (heavy ones on the machine with the most cores), coverage reports and
  translation QA, by project priority, within each machine's share and quiet hours. A failure runs again (flaky hunting).
  Findings such as coverage FAIL rows become backlog tasks. Run `tutti workq [tick]`.
- Someone at the keyboard of a remote machine: the probe reads a Linux machine's graphical-session idle hint and a WSL
  machine's Windows console idle time (`query user`), so their share backs off too.

### Fixed

- `/api/vpn` crashed on every POST (`bytes` has no `get`): the body is read as JSON, and a bad one is refused with a 400.
- A desktop that never sets logind's idle hint counted as "in use" for ever: now as not known.
- The autopilot's round tests read this computer's free memory and failed on a busy laptop: they use a fixed measure.

## 0.1.69 (2026-11-15)

### Added

- **Chat**, the dashboard's first tab and default view, for every signed-in user, viewers too (docs/CHAT.md). It runs on the
  household host inside `tutti dashboard`, so it answers while your own computer is off.
  - Answers questions from the dashboard's state: what is running, quota left, why a machine is idle, what waits.
  - Turns requests into work through the MCP tools' own calls: `add task in <project>: …`, `queue tests for <project>`,
    `delegate in <project>: …`, `ask <scout>: …`, and (admins) `pause|resume <name>`. Each action is a card with its live
    status, Undo where possible, and a line in the audit log.
  - Model order: the household's local models on machines that are here (never the asker's own computer), then accounts
    with room that the asker owns or that are shared (`share_spare`); never another member's private quota; a built-in
    answer from the state when none answers. Each answer names its model.
  - Roles enforced on the server (`auth.act_for`): viewers ask and queue backlog tasks for `chat.viewer_projects` up to
    `chat.viewer_tasks_per_day`; hand-offs, scout questions, test runs and pausing are for admins; conversations are private; admins see Household activity (actions
    only, never messages).
  - The page: streamed answers, Stop (Esc), Retry, Edit (↑), Not sent with Send again; safe Markdown (a parsed tree drawn
    with `textContent`, code blocks with Copy); follow-the-end scrolling with Jump to latest; suggestions from the state;
    text attachments (3 × 32 KB); New, Rename, Delete, search; live-region announcements, reduced motion; a phone layout
    with the composer above the keyboard; light and dark.
  - A model picker per conversation (the last choice is the user's default): Auto (local first, then online), Local only,
    Online only, or one model; each answer says why it fell back.
  - A tool loop for every model: local models through Ollama's native function calling (the most capable first, 7B and
    up get the tools), online agents through `ACTION` lines. Read-only tools (usage, machines, devices, backlog, job
    status) run at once; work (tasks, runs, scout questions, device test runs, pause and resume) is validated against
    its schema and the names here, then proposed as a card that runs only on Confirm.
  - Stored per user in `<state>/chat/` (mode 600), carried by `tutti move`; Clear my chat history.
  - `chat:` settings (Settings, Chat): `viewer_tasks_per_day`, `viewer_projects`, `history_turns`.

## 0.1.68 (2026-11-14)

### Added

- Tutti's own sign-in, built in (docs/AUTH.md, *Tutti's own sign-in*): a release carries the public OAuth clients of the
  maintainer's `tutti-dashboard` project, so a new household opens the dashboard, chooses **Sign in with Google** (or scans
  the QR code) and is admin, with no Google Cloud console. The LAN client's installed-app secret is written into the
  release at build time from the Actions secret `TUTTI_GOOGLE_DEVICE_SECRET` (`.github/workflows/release.yml`,
  `scripts/set-device-secret.sh`); a household's own clients still win, and `auth.official: false` turns them off.
- **Report a problem** on the dashboard (every signed-in user) and `tutti report` (docs/REPORT.md): what happened and what
  was expected, with a scrubbed diagnostic bundle previewed before sending; a `user-report` issue through gh, else GitHub's
  prefilled page; Sentry user feedback linked to the last crash when sending is on. Self-improvement and the scout's triage
  pick up `user-report` issues. Issue templates for problems and ideas.
- The release's default Sentry DSN (Actions variable `TUTTI_SENTRY_DSN`), used only once sending is on.
- The first-run consent question on the dashboard: a **Help make Tutti better?** banner for the admin until answered.
- The privacy policy (docs/PRIVACY.md), the terms (docs/TERMS.md) and a homepage (docs/index.md) for GitHub Pages.

### Changed

- The device sign-in asks Google for `openid email` only (no `profile`): non-sensitive scopes.
- An install from a release that never set a client now has sign-in on: after the upgrade, the dashboard asks for Google
  sign-in and the first person to sign in becomes the administrator. The dashboard still listens on 127.0.0.1 by default,
  so that person is on the computer itself; keep it there until you have signed in (docs/AUTH.md, *Threat model*).
- `scripts/release-patch.sh` publishes a release only after its files are attached (a draft first), so the release
  workflow's rebuild with the release defaults is always the last upload.

### Later

- Translations of the dashboard and the messages (every message is in plain English for now).

## 0.1.67 (2026-11-15)

### Fixed

- The admin's own row, checked live: it said "away" at the computer they sit at (a Tutti no client ever connected to is
  where its user is: present; only a host whose client left counts them away), "runs today: [object Object]" (the runs
  are summed over the workers; the household heartbeat's total had been dropped for the same reason) and readings with
  fifteen decimals (rounded to one).

## 0.1.66 (2026-11-15)

### Added

- A machine's share for Tutti (docs/FLEET.md, *The machine's share*): `machines.<m>.use_pct` (Settings) caps its slots, this
  computer's cores in the adaptive limit and the models' threads. The share drops to `in_use_pct` (30) while someone uses
  the machine, `quiet_pct` (50) in its quiet hours and `hot_pct` (50) while it is hot. The machine card shows it and why.
- Model servers kept alive. A dropped tunnel is started again at the next probe (after a reboot or sleep too). A quiet Ollama
  is restarted (user service, else `ollama serve`). When it cannot be, the exact one-step fix is shown.
- Household use: "Household use: X % of available capacity (last hour)" on the Usage card. Each machine shows its use over
  the last hour and today, and why it is idle.

### Fixed

- A machine with `nvidia-smi` but no NVIDIA driver (the NUC) read as unreachable: the driver's error on stdout broke the
  probe's summary line, so its local model and its slots were never used.

## 0.1.65 (2026-11-14)

### Fixed

- With sign-in on, the Users tab's household part and **My main machine** answered "this path is not part of the
  dashboard": the signed gate knew only the admin POSTs, not the ones that check self-or-admin themselves
  (`/api/users/manage`, `/api/user/main-machine`). Found checking the owner's own row live; a test now posts them through
  the signed gate.

## 0.1.64 (2026-11-14)

### Added

- **Your main machine** (docs/HOUSEHOLD.md): every user, the admin too, moves their own Tutti, agent logins, connected
  agents' MCP entries, readings forwarder, schedule and profile to another of their machines with one click (**Me → My
  main machine**) or `tutti user main-machine <m>`; it is `tutti move` for that user (no new machinery), WIP refs stay on
  origin, and it is never the coordinator's move for others. The server refuses a signed-in user who is not that Tutti's
  user, whatever their role. Presence follows: a host with clients counts its user present only while a client is
  connected, so a laptop that leaves makes them away.
- **The Users tab** (admin-only, next to Dashboard and Settings): every user, the admin's own row marked "(you)", with
  main machine, presence, last seen, Tutti joined, mode, schedule and consumption by account kind; **Manage <name>** with
  every action an admin can take for them (schedule, away and vacation, main agent, main machine, reconnect agents, pair a
  device, use a shared machine, pause or resume, update, join or rejoin, make admin, remove); the audit, filterable by
  user ("schedule by admin Ana for Bo"). The admin's dashboard queues the action at the coordinator; the member's own
  Tutti does it on the member's computer. Steps only the member can do show as the one step to hand them. The last-admin
  guard: the only admin cannot remove themself. One tab: the sign-in's (#39) Users tab, the household drawn into it.
- Settings shows no key about people any more (`settings_schema.MANAGED`: user, household, pacing, main.agent).
- The admin's own row on the Users tab has every field and control another row has (consumption, presence, main agent
  and main machine, schedule and away, Tutti joined and last seen, every help action), also in a household of one,
  where its actions run here at once; the last-admin guard only disables remove and make-admin, with the reason as a
  tooltip.

### Fixed

- The Users card showed on every view, Settings included: `.users-view`'s `display: grid` undid its `hidden`. It shows
  only on the Users tab, for admins.

## 0.1.63 (2026-11-14)

### Added

- Local models as first-class workers (docs/FLEET.md). Every machine's Ollama is read at each probe and its models are
  classed (tier, kinds). A machine with models and no worker gets `<machine>-local` through a tunnel, and `tutti fleet`
  lists them.
- Overflow routing. While an online account is ahead of its pace, in panic or spent, or held by the owner's reserve, a fresh
  task goes to an idle machine's model first: by capability, then by energy. A failed local attempt escalates to the cloud.
- Local runs have no quota.
  - A free model server takes one task of any size its models fit, worked in steps of a few files.
  - Local runs count only against the machines' gate slots, not the online limit.
  - `fill_room` counts free local models as room.
- A task that names no file gets the files its title names (`local_scope`), so a local model can take it.
- The governor for local models: owner pause, offline, the yield flag, heat or load, `machines.<m>.quiet_hours`,
  `fleet.memory_floor_gb`, and a CPU-only machine with every slot busy.
- The block resolver. When work waits and no worker takes it, the round tries:
  1. give the task files and steps;
  2. route it to any local worker;
  3. lower the tier;
  4. borrow household spare;
  5. wake a tunnel.

  Then the card offers one action (`tutti autopilot allow <account>`, one click on the dashboard) and logs every attempt.
- Speed: local runs log Ollama's eval time, and tokens per second are measured per model.

### Fixed

- `claude` in `autopilot.accounts` was "not usable (unknown, turned off, or it costs money)": an account without `worker:` now
  uses the worker of the same name, and an unusable step names its real reason.
- "pc: room for 1, but the queued tasks need 2": a local model's room counts runs, not quota points.

## 0.1.62 (2026-11-14)

### Fixed

- One config per user: `~/.config/tutti/config.yaml` (or `$TUTTI_CONFIG`) wins over a project `tutti.config.yaml`, so the
  dashboard, the MCP server, the service and every command read the same file whatever their working directory. A
  project file is used only when no XDG file exists.
- Every config write (Settings, `tutti setup`, `tutti move`, device saves, `tutti connect`'s files) goes through
  `config.write_text`: atomic, into the real file, so a symlinked config stays a symlink and keeps its mode.
- `tutti doctor` reports another copy of the config that differs from the one in use; `tutti doctor --merge-configs` and
  Settings' **Merge into one** add what only the copy has (the file in use wins), keep the copy as
  `<copy>.bak-merge-<time>` and link it to the one file (`tutti/configone.py`).
- Connect your agent: Claude Code's row comes from `claude mcp list`, so a registered, healthy server is *connected*. A
  session started before Tutti was added is a soft note, not "needs restart". Claude Desktop (chat) and Claude Code (Code
  tab) stay separate rows. When Claude Desktop rewrites its settings and drops Tutti's entry, the status check adds it
  back with a backup and says the chat part needs a full quit and reopen.

## 0.1.61 (2026-11-14)

### Added

- The owner's device rules (docs/DISPATCH.md, *Prep and restore*), extending prep and restore:
  - A device is released only when every user is done: no Tutti run, no session's claim (`tutti devices claim|release
    <d> --who <session>`) and no fresh lock file of another tool. Then its own values come back exactly (phones sleep
    again; a TV gets its screensaver and sleep timers back and turns itself off) and the other users are told: a Keep busy
    event and `<state>/devices/free.jsonl`.
  - Default prep by kind: phone and tablet brightness about 30 %, media volume (Cast plays on it) about 10 %, a TV's screensaver
    off; a saved file already there is never overwritten, so prepared values never become the originals.
  - The lock is taken first and refreshed while in use; the screen is woken and unlocked, and a keep-awake key goes every
    15 s only while a run is active.
  - Within 2 minutes of the ready marker a device run must show a test running, or it is queued again.
  - An empty e2e filter is refused; `tutti devices install <d> <apk>` refuses a non-debuggable APK in plain words and
    allows one install per device at a time.

## 0.1.60 (2026-11-14)

### Fixed

- `tutti move` never gives the target ssh access to itself (it copied the old computer's key for the target onto the
  target), and the plan names as "probed through this computer" only the machines whose access could not be carried.

## 0.1.59 (2026-11-14)

### Added

- The scout's week (docs/SCOUTS.md, *The week's plan*): Grok Bot works all week, not only at its end.
  - The cost of one run is measured from real readings around its runs (estimates ignored); the estimates logged after each
    run use it. The guess of 2 % a run was about 10x too high and kept 85 % of the week unused.
  - An even line to `stop_at` (95 %) paces every round; a burst in the last `hours` (6) sends what is left as fast as
    `burst_per_tick` and `burst_max_open` allow.
  - Regular work by project priority: Sentry triage, dependency notes, live-site checks, `scout:`/`research:` questions,
    second opinions on autopilot tasks that failed twice (the retry waits for the answer, which joins its brief), and
    reviews of every open pull request of `review_repos` not reviewed since it changed.
  - The card shows the week's plan: runs left, the mix, what can go now, what it did today.
- A scout in `review.reviewers` (e.g. `grok`) is a regular reviewer of `tutti pr review`; its `grok-scout` comment counts.
- Adaptive autopilot parallelism (docs/AUTOPILOT.md): the smallest of the accounts' room, this machine's free memory and idle
  cores, the machines' free gate slots, and `max_parallel` (now an optional cap). The dashboard says `running N of M
  possible: limited by <bound>`. Every probe reports the machine's free memory.
- Heavy gates (ESLint, tsc, tests, ...) of a repo whose checks run `local` go to a routed machine, and so does any gate
  while this machine is short of memory.
- The dashboard's **Queued** list beside Running jobs (docs/DASHBOARD.md): autopilot tasks, Keep busy shards, scout questions
  and due watches, Tutti's waiting move; grouped by project priority, with size, destination, estimated start and why it
  waits; an admin's Run now, Move to, Cancel and Priority up or down. The header counts `N running · M queued`.
- Running jobs and Queued fill the height of the main-agent and workers column beside them: as many rows as fit, then "Show
  more" for the rest; re-measured on resize; 8 rows on a phone (docs/DASHBOARD.md, *Layout*).
- Targets (docs/USAGE-PACING.md): `accounts.<name>.target_pct` and `main.target_pct` (100 by default, Settings) set where each
  window's spend ends by its spend-by time (reset minus lead); the owner's reserve still holds. Each account card says
  `Target 100 % · on track to finish at X % by HH:MM`, and when short, why and what Tutti is doing about it.

- The owner's real day (docs/USAGE-PACING.md): the reserve follows today's real start, stop and burn rate; a heavy-burn day
  cuts the workers' share at once and warns early when the main agent would run out even with no worker; unused owner
  hours go to the workers hour by hour; the owner panel says `Today: started 07:00 (planned 09:00)`.

### Changed

- After a reset with no reading yet, an account starts the new window from 0 % at once: queued work goes on with no gap.
- An account inside its on-pace band but under its line gets the tasks that top it up, so a window ends at its target.
- A backlog task whose title starts with `research:` now goes to a scout, like `scout:`, never to a worker.
- `scouts.<name>.spend_rest.hours` (the burst before the reset) defaults to 6, was 24: the even weekly line now spends the
  allowance all week, so the burst only sends what is left.
- The dashboard's order: Running jobs and Queued beside Main agent and Workers, then Autopilot beside Machines.
- With no cap and free memory unknown (no /proc/meminfo), the adaptive limit stays at 2 runs.
- `autopilot.max_parallel` has no default: the limit is adaptive unless the owner caps it.

## 0.1.58 (2026-11-14)

### Changed

- `tutti move` gives the new host the same ssh access as the old computer to the machines it cannot reach yet (their
  `~/.ssh/config` entry, key file with mode 600 and host key; the owner's decision), then tries ssh from there, so tests
  and model tunnels to them run from the host directly (docs/MOVE.md, *Machines the target cannot reach yet*).
- A local model on the old computer's loopback is reached by the host through the client's ssh (`host.reverse`).
- The cut-over is short: the final sync copies only the state, the git folders and the autopilot's own worktrees, and the
  move waits for (or hands over) a review or merge the autopilot has in flight.

## 0.1.57 (2026-11-14)

### Added

- Sign-in with Google from the home network or any VPN (the router's, OpenVPN, IPsec; no Tailscale or WireGuard
  needed): on a plain LAN address over http the page uses Google's device flow (a code at google.com/device; the second
  OAuth client, "TVs and Limited Input devices", its secret mode 600 and read from stdin). The flow is bound to the
  browser that started it; the server asks Google once per interval. Clients from any private subnet are accepted, a
  public source address is dropped. `tutti auth setup --client-id <web> --device-client-id <tv> --device-secret-stdin`
  takes everything with no prompt.
- Sign-in with Google for the dashboard (docs/AUTH.md). `tutti auth setup` prints the Google Cloud console page and the
  exact fields (consent screen: External, Testing, test users; OAuth client: Web application, the JavaScript origins),
  then saves the client ID (`--client-id <id>`, public; no client secret). The server checks the ID token's RS256
  signature against Google's JWKS (cached), `iss`, `aud`, `exp`, `iat`, a nonce bound to this browser and the verified
  e-mail, with no new dependency.
- Viewers see only the dashboard: one CSS rule hides every control for a viewer unless it is marked viewer-safe (new
  controls are hidden by default), and the header says "Viewer: read-only". Every user, viewers included, edits their own
  **My schedule** (working days, hours, away or vacation mode; `POST /api/me/schedule`, 403 for someone else's unless
  admin; `<state>/schedules.json`, read by the household's per-user pacing through `myschedule.read`).
- `auth.may_act_for(actor, target)` and `auth.act_for(...)`: an admin may do any member's action for them (the household's
  Manage panel calls it), a member only their own; each one is audited as "by actor for target".
- Role changes from the Users tab (Make admin, Make viewer, also one's own while another admin exists), each one
  click and a confirm, enforced on the server, and an audit log (`<state>/auth-audit.jsonl`, mode 600) shown under
  **Changes**: who changed which role, and when.
- Roles: the first user to sign in becomes admin (atomic under a file lock); later users wait for approval as pending,
  then viewer; an admin can Make admin, Make viewer or Remove; the last admin can be neither. Viewers see the dashboard
  read-only; every POST is admin-only on the server unless listed, and Settings, the reading helper and the Users list
  are admin-only GETs.
- Invites: **Invite someone** creates a single-use, 7-day, revocable link with a QR code and a role (viewer by default);
  signing in through it approves at once. Only the token's hash is stored. The same invite lets the person's Tutti join
  the household (`tutti household join <invite-url>`, wired by the household's increment 4; `tutti/invites.py` is the
  shared interface). A bad invite fails only a new or pending user's sign-in; an invite stays revocable while its
  household use is open; `reset-admin` revokes every invite.
- Sessions: HttpOnly, SameSite=Strict cookie with a random id, stored server-side as a hash with an expiry
  (`auth.session_hours`, 72) and a CSRF token checked on every POST; Sign out. Signed-in answers are `no-store` and the
  service worker never keeps them.
- `dashboard.bind` and `dashboard.origins` (https, or http only on a loopback name): reaching the dashboard from other
  computers requires sign-in, and only through the LAN or a VPN. A public bind, `0.0.0.0`, or an origin that resolves to
  a public address is refused at start (`tutti/netpolicy.py`, shared with the household); the OAuth origins and the invite
  links use only localhost and LAN or VPN names. `tutti auth setup` detects Tailscale; `--tailscale` serves the dashboard
  on this computer's tailnet-only https name (`tailscale serve`, never funnel) and adds that origin.
  `auth.required_origins(cfg)` is the interface `tutti move` shows after a move. Recovery from a
  shell: `tutti auth users|promote <email>|reset-admin --yes`.

## 0.1.56 (2026-11-14)

### Added

- Presence (`tutti/presence.py`, docs/PRESENCE.md): every device, machine, member's laptop and Ollama server is present,
  away, asleep or unknown, with a last-seen time. The state comes from the adb readings, ssh probes, household heartbeats
  and `/api/tags`, and away is told apart from asleep: a device that left with its owner's laptop, or a machine silent in
  its night hours. Cards say "Away since 18:40 (left with its owner)" and show today's 24-hour profile strip. `tutti presence`
  prints the same.
- A run whose target leaves is queued again and not counted as a failure, retry or no-progress event. The idle watchdog
  never flags an away or asleep target. A returning phone is reconnected over mDNS and picked up within one tick.
- Learned 7 × 24 availability profiles: long shards (suite `minutes`) avoid targets likely to leave or sleep soon, and
  always-on targets choose first.
- Before this computer sleeps or shuts down, a logind delay inhibitor hands running work over (as "stop now" does),
  requeues leases and flushes state, within the time the OS allows (`tutti/sleepwatch.py`). On wake, the work starts
  again.
- Nothing lost on a hard power-off: atomic, fsynced state writes and an fsynced usage log whose torn last line is
  repaired at start (`tutti/durable.py`, `tutti/recovery.py`). Runs from an earlier boot are interrupted, not failed.
  Autopilot attempts are checkpointed every 5 min through a separate git index and pushed to `refs/tutti/wip/*` (never
  main or master). They are reconciled before resuming.
- Move work now (`tutti/jobmove.py`): `tutti job waiting`, `tutti job move <id> --to <target|auto> [--bump]`, `tutti
  machine evacuate <m>` and the dashboard's admin-only Move now, First in queue and Move all buttons (`POST /api/work/move`,
  403 for viewers). A member whose task moved never runs it again and keeps its work on `tutti/backup/<name>`.
  `presence.move_after_minutes` moves urgent work by itself (opt-in).
- The owner counts as away while `presence.owner_laptop` is away or asleep. the Where Tutti runs card warns when Tutti
  runs on a laptop. Wake-on-LAN for an asleep machine with waiting work is opt-in, on the LAN only.

### Changed

- The dashboard service restarts always and lingers (`loginctl enable-linger`), so it starts at boot after a power cut.

## 0.1.54 (2026-11-14)

### Changed

- `tutti move` brings everything Tutti relies on to the new host, so it keeps working while the old computer is away
  (docs/MOVE.md, *Everything Tutti relies on*): the agent CLIs at the same versions (their install folders copied, node
  from nodejs.org checked against its sums when an npm CLI needs it), their logins (the owner's documented exception:
  copied unread with mode 600, gh's token the way gh documents, each checked with the tool's own status command, a
  device-code sign-in only for a tool that refuses the copy), the repositories and their worktrees (one rsync with hard
  links kept, then `git worktree repair`), and every path under the home in the config. Jobs, readers, reviews, git and
  gh run on the host; the old computer is a client that forwards its commands and agents and sends its session's
  reading files (Claude's) to the host whenever they change.
- The plan no longer asks to mask the host's sleep targets (presence handles a host that sleeps), and gh needs no sign-in.
- Verify runs `tutti doctor` and `tutti workers` on the new host.

## 0.1.53 (2026-11-14)

### Changed

- Moving devices asks the user nothing: the old machine's adb key is the default (a different key on the new machine is
  backed up first; `--keep-key` keeps it), Wireless debugging is read, kept and set on over the old machine's live
  connection and re-asserted from the new one, and re-pairing is only the last resort (docs/DEVICES.md).
- A wireless device whose Wireless debugging went off (a reboot, a Wi-Fi change) is switched back on by itself through any
  machine that still reaches it, over Wi-Fi or USB; only when none does, its row asks the one step left, with the reason.
- `tutti move --to <machine> --devices`: every Android device follows Tutti to its new machine (the same zero-step move).

### Fixed

- `tutti devices move` counts another tool's lock file (`devices.<d>.external_lock`) as held only while it is fresher than
  `external_lock_stale_minutes` (30), as the readings and the dispatcher do: a file left behind no longer blocks a move.

## 0.1.52 (2026-11-14)

### Changed

- Usage & pacing: **Local models** sits directly below the online agents' usage cards, and *You and the main agent* comes
  after it (before, it sat between them). Settings follows the same order: Energy right after Accounts and readings. A
  layout test checks both orders.

## 0.1.51 (2026-11-14)

### Changed

- Shared machines are compute only, and a member needs **no login** on them (docs/HOUSEHOLD.md, *Compute on shared
  machines*). `tutti household machine use <m>` adds the machine as transport `household`; a `tutti test` routed there
  pushes the worktree as it is (uncommitted changes included) to a `refs/tutti-wip/` ref with the member's own git
  sign-in and sends a signed job; the owner's Tutti claims it in its dashboard round, fetches the ref with the owner's
  sign-in into a sandboxed worktree and runs it with its own `tutti test --on <m>` in the machine's container runner (a
  shell runner only with `household.member_shell: true`), then returns the exit code and the end of the output through
  the coordinator. No credential, token or login crosses; the WIP ref and the sandbox are deleted. The trust model is
  in HOUSEHOLD.md. A household machine is never reached over ssh; its probe comes from the grants.
- Safe by construction (from the review of #61): a job's repo must be a shared project's `https`/`ssh` remote (git's
  file and ext protocols off, no password prompt), so a member cannot point the owner's git sign-in at another host; a
  claimed run is not taken again before its timeout plus 15 minutes; a paused machine takes no more queued runs; a
  result the coordinator missed is kept and sent again; the WIP ref stays until the result is in.
- Tests: a member's run on the owner's machine with his uncommitted change, a failing run's exit code and output, the
  job carrying only repo, ref and command, the shell-runner opt-in, only the owner claiming and only the member reading
  a job, a repo without origin.

## 0.1.50 (2026-11-14)

### Added

- Tutti finds its own time zone (`tutti/tz.py`, `time.zone: auto`, an override in Settings > Time zone): TZ,
  /etc/timezone, /etc/localtime, timedatectl, Windows `tzutil /g` mapped to IANA, macOS `systemsetup`; never a question.
  Windows installs get `tzdata`.
- A remote machine's zone is read by its probe over ssh; its `dispatch.quiet_hours` are its own local time.
- A household member's beat carries their IANA zone (their Tutti host's, else their browser's), and the coordinator
  reads each member's hours in that zone.

### Fixed

- Owner hours, the learned 7 x 24 profile, away and vacation ends, quiet hours, time-of-day tariffs, energy days and runs
  today follow the local zone with daylight saving: a profile bucket is a local hour, so the March and October changes no
  longer shift the learned pattern by an hour (the old fixed offset did).
- No bare UTC for people: `tutti usage`, the planner's "spend by" text (was "Wed 09 Dec 21:00 UTC"), `tutti owner` and
  `tutti files list` print local time with the offset, e.g. "Sun 15 Nov 02:00 (UTC+2)"; the dashboard shows the
  viewer's zone with UTC in the tooltip. A test greps the sources for bare UTC times.
- Resets stated with a zone or an offset (`resets 3pm (Europe/Bucharest)`, `2026-01-03 04:00 Europe/Bucharest`) are read
  in that zone; a bare one is still UTC.

## 0.1.49 (2026-11-14)

### Added

- `tutti vpn status|setup`: an optional private network between the household's devices (docs/NETWORK.md). Detects
  Tailscale and WireGuard on every machine (over ssh, read-only) and the apps on every phone (over adb); `setup` picks
  `vpn.prefer` (Tailscale suggested) without asking, does every step it can (install and join with passwordless sudo, opens
  each sign-in page, opens the Play Store page on phones) and writes the rest into one ordered checklist. `--authkey <m>`
  joins a headless machine with a hidden, never-stored key; `--cert` makes the private https certificate; `--dry-run`.
- `tutti vpn setup --wireguard`: plain WireGuard with no account. A free private /24 (no clash with the LAN or 100.64/10),
  keys made in Python (X25519) and sent only to their own machine (600), configs with no private key, one sudo line per
  machine, a phone QR code shown once, and WireGuard's UDP port opened on the router (UPnP, NAT-PMP) only after a click.
- The dashboard's **Private network (optional)** card ("Already have a VPN to your home? Nothing to do.") and
  `POST /api/vpn`.
- Android: `devices.<n>.wireless` adb addresses; every look and run picks USB, then LAN Wi-Fi, then the tailnet, and
  reconnects a dropped wireless one. Machines: `lan:` is the ssh fallback when `host` (a VPN name) does not answer.

### Security

- Tailscale Funnel is never enabled: `tutti vpn funnel` refuses and the dashboard refuses to start while Funnel forwards
  to one of Tutti's ports. `tutti/netpolicy.py` (shared with the household) keeps every bind private.

## 0.1.48 (2026-11-14)

### Added

- `tutti move --to <machine>` and the dashboard's **Where Tutti runs** card with **Move Tutti to…** (docs/MOVE.md): one
  guided, resumable move of the dashboard, the state and the loops to another machine: preflight (ssh, OS, disk, Tutti,
  LAN and VPN addresses, lingering, suspend), the one-line install there, copy, freeze, running jobs waited for or handed
  over, final sync, the config from the target's point of view (comments kept), secrets with mode 600 over stdin, the
  login service started there, a health check that rolls back by itself, and this computer turned into a client.
  `--dry-run` prints the plan, `--rollback` undoes it, `--status` says where Tutti runs.
- Clients: commands that read or change the state run on the host over ssh (so every connected agent follows); with the
  host down, reads use a read-only copy and say so, `tutti run` works alone, changes are refused with the fix.
- The bridge (`tutti client`, `tutti bridge`): the client's one ssh to the host carries the dashboard's port and lets the
  host run jobs, readers and reading files that need the client's repositories and sign-ins, from the client's own config
  only (`host.jobs`, `accounts.<n>.read_on`, `accounts.<n>.reading_from`, `machines.<m>.via`).
- `/api/health` names the host and the version.

## 0.1.47 (2026-11-14)

### Added

- `tutti devices pair` and the dashboard's Pair a device / Pair a TV buttons: wireless debugging with a QR code (drawn on
  the dashboard and in the terminal, no library), the 6-digit code a TV shows, or USB then adb over the LAN. The device
  is added to the config with its name from the model, its machine, kind (phone, tablet, tv) and capabilities; the only
  user steps are Developer options, Wireless debugging and the scan (docs/DEVICES.md).
- `tutti devices move --to <machine> [devices]` and the Move devices button: copies the old machine's adb key over ssh at
  mode 600 (never printed), so devices need no re-pairing; a different key on the new machine is only replaced with
  `--replace-key` (backed up) or kept with `--repair`. Preflight (adb, versions, mDNS from the new machine, dispatch
  leases, the device lock and `devices.<d>.external_lock`), `--dry-run` (plans only: it connects nothing), and a rollback
  for a device that does not connect. docs/DEVICES.md says how another tool's agents switch to Tutti's leases.

## 0.1.46 (2026-11-14)

### Added

- Several users in one household, increment 4 of 4 (docs/HOUSEHOLD.md). **One command to join:** `tutti household join
  <invite-url>`, the link from the dashboard's Users card (docs/AUTH.md). It detects the main agent and the
  subscriptions and asks nothing (the guided rule of `tutti/guided.py`: defaults recorded as choices, the one human step,
  a Google sign-in, listed at the end), opens the invite to sign in with Google, waits for it (every 3 s, up to 2
  minutes) and pairs: the member's identity is that Google user (`sub`, e-mail, name), the token is derived on both sides
  from the invite and never travels, and the invite is used up. The admin's dashboard starts the household by itself
  with the first invite. The Users card shows whether each person's Tutti joined and when it was last seen
  (`household.members_by_sub`). `householdcmd.invite_lines(invite)`: the invite page's single install line per platform
  (`install.install_line(platform, join=...)`), so a member with no Tutti installs, sets up and joins in one paste. The
  invite join needs the dashboard sign-in (#39); its end-to-end tests run once that lands.
- **Never on the internet** (`tutti/netpolicy.py`): the coordinator and the dashboard listen only on localhost or a
  private, LAN or VPN address (RFC 1918, 100.64/10, fc00::/7, WireGuard interfaces); `household.bind: auto` (new
  default) is localhost plus every LAN and VPN address; a public address or a wildcard is refused with the reason, in the
  config check too; join codes and invites never name a public address. HOUSEHOLD.md says how members join over the VPN.
- **The dashboard:** a **Me / Household** switch and the **Household** card (members with their main agent, who is
  prompting, online or last seen, runs today; each shared machine's use per person, your share and its fairness;
  whether the coordinator answers); a shared machine in **Machines** says who else it is busy for. Others' jobs are never
  shown, only their names and counts.

- **Away from home with no VPN:** a member who cannot reach the coordinator keeps working on their own (own accounts,
  readings and machines), starts no shared task and takes or renews no lease, and syncs everything on the first round
  back home ("back home: synced ..."); a task another member took meanwhile keeps both results with a note. The dashboard
  says "away from the household: working on your own until you're home". Tested offline, then sync.

### Fixed

- Jain's fairness index divided by zero when nobody got a slot (a paused machine).
- `netpolicy` (from the review of #44): the config check knows WireGuard and Tailscale addresses outside the private
  ranges; without `ip` the interfaces come from `ifconfig -a` (macOS, BSD); `auto` adds `::1`, IPv6 ULA, link-local with
  its interface and the default route's own address (the one join codes name); an explicit bind on a container bridge
  is refused. The coordinator listens on IPv6 too.
- From the review of #47: invite links are quoted in the docs and hints (a shell drops `#`); "back home" is said only
  after being away longer than a blip; the docs say exactly which servers `netpolicy` guards.

## 0.1.44 (2026-11-14)

### Added

- Suites in depth (docs/DISPATCH.md):
  - A shard may carry its own `devices`, an `{extra}` for its command and `holds`: other devices it needs free, which are
    held with it (`tutti test --hold-device`).
  - `prepare` builds once per commit before any shard.
  - `verdict` judges a runner that always exits 0.
  - `started` stops a run whose tests never start in time.
  - `requeue_on` names output that is not the test's fault.
  - A requeued device rests for `dispatch.requeue_cooldown_minutes`.
  - `projects.<p>.report` writes the results, with failed shards' log ends, to a file the project's sessions read.
- Devices shared with other tools:
  - `devices.<d>.external_lock`: Tutti waits while another tool's lock file is fresh, writes its own and removes it after.
    The readings show the device as busy, held by that tool.
  - `kind: tv` is a Cast receiver: no battery or call state, and used only by `targets: [tv]` suites or `holds`.
  - `calls: false` skips the phone-call check.
  - A wireless-debugging serial whose port changed is reconnected through `adb mdns services`.
- Device prep and restore (`devices.<d>.prep`, docs/DISPATCH.md):
  - Once the lock is taken, each setting's current value is read and saved on the device's machine before anything
    changes, then the run's values are set.
  - The saved values are put back however the run ends; a file left by a crash is restored by the next round.
  - A command the device lacks is skipped cleanly.
- Lenient shards: a shard entry this version cannot read (an unknown form or key, a device not configured) is a warning
  and that shard is skipped, never a config error that stops every command.

### Fixed

- The device-lock test (failing on main under load) reads the run's output until slot.sh queues behind the lock, instead of
  sleeping 1.5 s: deterministic, no sleeps.

## 0.1.43 (2026-11-14)

### Security

- Tutti is never exposed to the internet: `tutti mcp --http` listens on 127.0.0.1, or with `--bind` (`mcp.bind`) on a
  LAN or VPN address only (loopback, 10/8, 172.16/12, 192.168/16, 100.64/10, fc00::/7); a public address and `0.0.0.0`
  are refused with the reason and the fix. Other people reach it only over a VPN. The rule is `tutti/netpolicy.py`, shared
  with the household's coordinator and dashboard (WireGuard and Tailscale interfaces count as VPN whatever their range).
- The ChatGPT row no longer suggests an HTTPS tunnel: ChatGPT's app cannot connect; use Codex (`tutti connect codex`).

### Changed

- The restarts and refreshes `tutti connect` asks for are guided steps (`connect.human_steps`, `guided.render`): `tutti
  setup` lists them once in its end-of-setup list, and `tutti connect` prints the same list.

## 0.1.42 (2026-11-14)

### Added

- Local models under Usage & pacing, in a category of their own (docs/ENERGY.md): one card per machine running local
  models (each Ollama worker's machine, else this computer, and `energy.machines.<m>.ollama`) with the models installed
  and loaded, busy time, jobs and tokens (Ollama's `eval_count`) today, this week and this month, live GPU power and use
  (nvidia-smi, rocm-smi, xpu-smi, intel_gpu_top, hwmon energy counters; RAPL for the CPU) over the existing ssh, a TDP x
  utilisation estimate where nothing is readable, the whole-system estimate, and availability (paused, yielding, thermal).
- Estimated energy and cost: kWh sampled at each probe while a Tutti run is on the machine, plus the idle draw while Tutti
  keeps a model loaded; cost in the user's currency, detected from the time zone, then the locale (RON for a computer in
  Romania with an English locale), at a bundled typical household price per country (sources and date in docs/ENERGY.md),
  marked estimated until `energy.price_per_kwh` is set; per job, per busy hour, per day, week and month, and what the same
  tokens would cost on a cloud API.
- `energy:` settings (the Settings tab's Energy section): country, currency, price per kWh, a time-of-day tariff, `prefer_cheap_hours` (local-model autopilot
  runs wait for the cheap hours), `include_idle`, and per machine the base draw and the GPU's TDP.
- Every local-model call through `tutti ask`, `code`, `loop` and translation is logged in `<state>/local-runs.jsonl`
  (worker, start, end, tokens); a local `tutti conduct` is not yet (docs/ENERGY.md, *Limits*).

## 0.1.41 (2026-11-14)

### Fixed

- A config written for a newer Tutti never stops this one: an account key it does not know, or a reader whose format it
  cannot read, is a warning (`tutti: warning: ...` on stderr) and that reader is skipped; the config still loads and the
  dashboard still starts (docs/LESSONS.md).

- A scout's account (Grok Bot) never offers backlog tasks: its card's Do line and the planner's Decision no longer say
  "start more parallel work from the backlog" or "42 task(s) now"; they say what the scout does with the allowance.
- The owner's reserve names the working days it is kept for and whether the profile is learned yet; on a Saturday it is
  the expected use of the working days before the reset (none when Monday's hours come after it). The Do line of the
  owner's account comes from the plan: it never says "spending too fast" while the plan says spend.
- A 5 h window under a weekly limit keeps its own badge (6 % used against 33 % by now is behind, not ahead); the card's Do
  line and Decision name the binding limit and why, and never contradict each other.

### Added

- Spend the rest (`scouts.<name>.spend_rest`, on by default): near its reset, a scout behind its pace spends what is left
  by itself on Sentry triage, dependency notes, the other watches, `scout:` research questions from the backlog and the
  review trial's open pull requests, in project priority order, within its caps and up to 95 %
  (`tutti/spendrest.py`, docs/SCOUTS.md).
- Joint planning of a window and its weekly limit: a window may spend over its even share while the limit's projection
  still ends within its budget.
- Model and effort by pace (`accounts.<name>.tiers`, `tutti/tiers.py`): behind on the binding limit, an account runs its
  strong tier (`--model`, `--effort`) and takes the L and M tasks first; ahead, its cheap tier and the S tasks. Running
  tasks and `tutti run auto` show the choice and why.
- Cursor's two meters read automatically (`reader: {format: cursor-api, field: autoPercentUsed | apiPercentUsed}`,
  `tutti/cursorusage.py`): the spending page's own account call, signed in with cursor-agent's login, with no owner click.
  Grok Bot's weekly meter is not in that answer and keeps the reading helper.

## 0.1.39 (2026-11-14)

### Fixed

- Windows: every command failed with "No module named 'fcntl'" (`tutti/transfer.py` imported it unguarded). The import is
  guarded like the other modules' (a send waits on `msvcrt.locking` there), and a test imports the CLI with no fcntl.

## 0.1.38 (2026-11-14)

### Added

- One-line install per OS (docs/INSTALL.md): Linux, WSL and macOS (`curl` the bootstrap, check its SHA-256 written in the
  line, run it) and Windows (PowerShell, the same with `Get-FileHash`). The bootstrap, attached to every GitHub release,
  installs uv 0.13.8 (Python without sudo) and gh 2.104.0 against pinned SHA-256s, Git through winget in user scope on
  Windows, then the release tarball checked against the release's `SHA256SUMS`, into `~/.local/share/tutti/versions/<v>`
  with `current` and a `tutti` command on PATH, and runs `tutti setup`. Nothing unverified is piped into a shell.
- `tutti setup`: the first run with no questions: every default picked and shown on the Set up card with Change, and
  only what no tool can do (sign-ins in the tool's own window, a sudo line, a phone tap) listed once at the end. It finds the agent CLIs (claude, cursor-agent, agy, copilot) and
  whether each is signed in (its status command or its login file's existence; no credential is read), Cursor's plan,
  GPUs, ollama models, machines (~/.ssh/config, Tailscale, LAN ssh services), Android devices (adb), git repositories
  under the usual folders (priority by recent work) and gh's sign-in; writes the config keeping every comment (entries
  only added, the old file kept as `.bak`, the result checked before it is written); `--ask` asks about each default instead; opens each tool's own sign-in at the
  end; installs the dashboard as a
  login service (systemd --user, launchd, a Windows scheduled task with no console window); opens the dashboard on the first run
  to install the app; fixes a first git that cannot do HTTPS with a wrapper; saves its report. Again: fills only what is
  missing. `--join <invite>` then joins a household (what a new member's install line passes). It also connects every
  agent it found to Tutti's MCP server (`tutti connect --all`; `--no-connect` skips it).
- `tutti self-update [--check]`: the newest release, checked against `SHA256SUMS` before `current` moves. On by default
  at login (the service) and daily from the dashboard, which restarts in place; `self_update: {enabled: false}` opts out.
- The dashboard's Set up card: what setup found and what is left, each row with its command (Copy) or Fix (`tutti setup
  --yes` in the background, POST /api/setup).
- uv and pipx installs from GitHub (`pyproject.toml`; the machine scripts ship inside the package); npm is a ready hook
  (`install.CHANNELS['npm']`, `published: False`).
- `scripts/release-assets.sh`: the tarball, both bootstraps and `SHA256SUMS`; `release-patch.sh` attaches them.
- The shared interface for `household join` and other callers: `tutti/install.py`, and `tutti/guided.py`, the rule every
  setup follows (detect, else a default, then the human steps listed once; docs/INSTALL.md, *The rule for every setup*).
- The dashboard's **Settings** tab (docs/DASHBOARD.md, *Settings*): every key of `tutti.config.yaml`, grouped like the
  file, each with a plain label, a one-line help, its control (switch, number with unit, choice, day and hour pickers, list
  editor, YAML), its value, its default and **Reset to default**; search and a *Changed from default* filter; a phone
  layout with the views' bar at the bottom. One schema (`tutti/settings_schema.py`) drives it; a key in the defaults
  without a line still shows, an unknown key of the file shows under *Other* as YAML.
- Saving one key at a time: checked by the server and by the config's own rules, the file backed up to
  `<state>/config-backups/`, the value changed in place with comments and order kept (`tutti/yamledit.py`, no new
  dependency; every edit is parsed back and compared), applied at once or *on restart* with a **Restart** button. The
  same origin, token and loopback checks as every POST, plus one `dashboard.authorize` hook for a later sign-in.
- Secrets are never shown or edited there: set or not set, and the `tutti … setup` command that sets each.
- Project priority in Settings: an ordered list that writes `projects.<name>.priority` (0 is the most important). The
  autopilot's dispatch, its retries and `fill_room` now share the work out by it too, through the same helpers Keep busy
  uses (`config.project_priority`, `tutti/priority.py`), so a less important project still gets its turn
  (docs/AUTOPILOT.md, *Priority*).
- Settings sections for Keep busy (`dispatch`) and Updates (`update`).

### Changed

- The dashboard's opt-in switches, the autopilot's on/off and `fill_room`, autocompact and the Usage card's working days
  and hours now write the config file (the same keys as Settings, each card links to its row); a JSON or read-only config
  keeps them in the state folder as before. Pause and resume stay a state in `controls.json`.

## 0.1.37 (2026-11-14)

### Added

- `tutti mcp`: Tutti as an MCP server (stdio; `--http [PORT]` on 127.0.0.1 with a bearer token), so any MCP-capable main
  agent drives it natively: `handoff_advice`, `delegate_task` (project and size), `job_status`, `backlog_list`,
  `backlog_add`, `usage`, `machines`, `devices`, `test_on`, `scout_ask`, `away_mode`. Changing tools need the `admin` role
  (`$TUTTI_ROLE`, `mcp.role`; default admin, the owner of this computer); a refusal names the role and where to get it.
- `tutti connect [agent|--all] [--undo] [--status [--test]]` wires Tutti into Claude Code (`claude mcp add-json --scope
  user` and a skill), Claude Desktop, Codex CLI (config.toml block, AGENTS.md block), Antigravity (`~/.gemini/config/
  mcp_config.json`, GEMINI.md), Cursor (mcp.json, a skill), Copilot CLI, VS Code and Gemini CLI; ChatGPT says why a local
  server cannot work and the steps instead. Merges only Tutti's entry and a marked block, backs each file up first, is
  idempotent and undoable. `connect.connect_all()` is the interface `tutti setup` calls (docs/CONNECT.md).
- The dashboard's **Connect your agent** card: each agent's state (connected, needs restart, not installed, can't connect
  locally), Connect, Disconnect and Test (a real MCP round trip through the agent's configured command); `POST /api/connect`.
- Tests: every connector against fake homes (merge, idempotency, undo, other servers kept, broken files refused), the
  tool schemas, the stdio protocol and role enforcement on every changing tool.

## 0.1.35 (2026-11-14)

### Added

- Keep busy (docs/DISPATCH.md), on by default: projects declare test suites (`projects.<p>.tests`: kind, target kinds
  android and machine, `run` with `{shard}`, `shards` or `shards_from`, retries) and their latest commit (`commit:`).
  Every shard not passed on that commit is pending. Idle devices and machines pull it on every dashboard round and the
  moment a run ends. Pause, yield, heat, battery, quiet hours and opt-ins are respected; lint and unit runs on an Android
  device are opt-in. A read-only project still gets test runs; the results stay in the state folder.
- Leases with a heartbeat: `tutti dispatch run <lease>` touches its beat every 30 s and writes its exit code. A crashed or
  silent run frees its target and its shard is retried.
- The idle watchdog: a device, machine or worker idle past `dispatch.idle_minutes` (5) while compatible work waits gets a
  warning row on the Controls card ("Idle 12 min while RoRadio has 14 e2e shards pending: blocked by ..."), an event, a
  telemetry event and a fix button. The Keep busy switch is in the opt-in list; `tutti dispatch status|on|off|now`.
- Phone calls: a device that rings or is in a call (`dumpsys telephony.registry`, every SIM slot) takes no new work and
  shows "Paused: phone call". A running shard is looked at every 3 s: Home frees the screen, then the suite's
  `pause`/`resume` hooks hold it, or it is stopped cleanly and queued again with no failure counted. Work resumes
  `dispatch.resume_after_call_seconds` (30) after the call. Opt-in `dispatch.pause_on_owner_use`: a screen a person
  turned on while no run drives the device holds it too.
- `device-readings.sh` reports `call=` and an adb that is found but does not run (a Windows adb.exe from WSL without
  interop), with the fix.
- Project priorities: `projects.<p>.priority` (0 the highest), read through the shared `config.project_priority` and
  `tutti/priority.py`'s `fair_order` (the same helper as the Settings tab and the autopilot). Free targets take pending
  work by a weighted fair share (weight `1 / (priority + 1)`), so the higher priority goes first and no project starves.

## 0.1.34 (2026-11-14)

### Added

- Several users in one household, increment 3 of 4 (docs/HOUSEHOLD.md): fair shares of shared machines. `tutti household
  machine share <m>` lends a machine to the household (its slot locks move to `/tmp/tutti-household`, mode 1777, made by
  Tutti over ssh and opened read-only by `slot.sh`, so every member's login takes one slot queue), `machine use <m>` adds another member's (kept in
  `user.json`, never in the config file; ssh is checked and the one human step is named), `machine pause|resume|unshare`
  (its owner or the admin). The coordinator grants each member a share of each machine's slots every round: interactive
  demand (prompting, runs by hand) before background autopilots, then weighted max-min fair shares weighted by each
  member's expected activity this hour from their 7 x 24 profile (the household plan), one slot for a member starved 20
  minutes, never idle while someone has work. `tutti test`'s routing and the autopilot see at most the grant free, with
  who the machine is busy for. Autopilot runs are marked background (`TUTTI_BACKGROUND`).
- The main agent is a per-user setting: `tutti user main-agent <kind>` (claude, codex, agy, cursor, copilot, gemini,
  local), kept in `user.json`; the owner reserve moves to the accounts behind the new agent, and `tutti connect` runs for
  it. Tested: a switch moves the reserve.
- From the review of #35: a status look keeps the last demand; queued work and the prompting slot are counted once, not
  on every machine; one's own machine reports its fit slots (hot: `heat.hot_slots`); shared lock folder mode 1777 with
  read-only lock fds instead of a world-writable folder.
- `share_spare`: an account whose planner room would be lost within `household.share_spare_hours` of its target makes its
  member *spare*; the coordinator then offers other members' shared projects, cloned once with the member's own git
  sign-in and taken only by the spare accounts, results as draft pull requests.
- A member reserves only as many shared tasks as it could start now; the coordinator reserves in the member's order,
  skipping tasks others hold, so one member never keeps shared tasks from another.
- Tests: a 48-hour simulation of two members with different working hours on two shared machines and one shared project
  (every round within capacity and work-conserving, each member's prompting served in their hours, nobody without a slot
  past the starvation limit, 30 tasks with an overlap deduplicated and each done exactly once, by both members).

## 0.1.33 (2026-11-14)

### Added

- `tutti update`: updates every worker CLI on every machine without sudo (the program's own updater, or npm for a user-owned
  install), pulls the ollama workers' models again, and names one exact sudo command for apt packages (`update.packages`:
  the Cursor editor, Grok Bot) with a newer version or a switched-off apt source. `--check` only reads versions; `--machine`
  limits it to one machine and merges into the last full round.
- Use locks (`tutti/inuse.py`): a run of a CLI worker holds its program's lock shared, and the update takes it exclusively,
  so a program is never replaced while a Tutti run uses it; a running program, a loaded model or a lent-out machine is skipped.
- The dashboard runs a round every `update.every_hours` (24) unless `update.auto: false` (one round at a time per config; a
  failed one is retried after an hour), and its Workers card lists the versions per machine, what needs the owner, and
  **Update now** (`POST /api/update`).

## 0.1.31 (2026-11-14)

### Added

- Several users in one household, increment 2 of 4 (docs/HOUSEHOLD.md): the coordinator. `tutti household init` makes an
  always-on computer the household's coordinator (its dashboard serves it on port 4778, or `tutti household serve`) and
  prints a join code; `tutti household join <code>` in each member's login asks only their name. The member's token is
  derived on both sides from the code's secret and two nonces and never travels; every request is signed (time, nonce,
  body hash), replays and stale times are refused, ten wrong codes close the join to that address for an hour, and both
  sides keep their files mode 600. `household status|leave|code`.
- Shared projects: `autopilot.repos.<r>.household: true` or `tutti household project share <r>`. One coordinator call per
  autopilot round (outside the backlog's lock) publishes new tasks to the project's shared backlog with dedupe by folded
  title, closes finished ones for everyone, renews the leases of running and in-review tasks (30 minutes, the heartbeat)
  and reserves the next ones (5 minutes); the other members' tasks are mirrored in and a task leased by another member
  waits ("leased by Bo"). Unreachable, a member works alone: private repos go on, shared tasks wait.
- Tests: two simulated users on one shared project never run the same task, a task one adds is done by the other and
  closed for both, the coordinator over real HTTP on loopback (join, a wrong, expired or forged request, mode 600).

## 0.1.30 (2026-11-14)

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

- The planner on every account card is label/value rows (Points left, Spend by, Resets, Decision, Why; `quota_plan.rows`)
  instead of one run-on sentence, without the account's name; times in the viewer's own time, UTC in the tooltip.

### Fixed

- Self-improvement insight tasks also wait as `candidate` when `self_improve.auto_accept` is off (cursor's review).
- The telemetry *Sending* tests are hermetic: a real DSN in `~/.config/tutti/telemetry.env` or a `TUTTI_*` variable on
  the machine can no longer make a test send to Sentry or fail.

## 0.1.29 (2026-11-14)

### Added

- Several users in one household, increment 1 of 4 (docs/HOUSEHOLD.md: the design, the threat model and the migration).
  The user model: `tutti user` shows who this Tutti works for (the name, default the login's; a random id kept in
  `<state>/user.json`, shown and sent only as a 12-digit hash; the main agent; the config and state folders; the household
  role). `tutti user name <n>` and `tutti user share <account> on|off` (`accounts.<n>.share_spare`, off by default; never
  an account that costs money) set it. `user:` and `household:` config keys, both optional; `tutti household status`.
- A single user is a household of one: no new key, no file moved, every command as before (tested on the example config).

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
