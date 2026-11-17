# Lessons

Each entry: what happened, why, and the rule it left behind. Add a lesson in the same change that fixes its cause.

## `tutti dispatch build` hid failing builds behind exit 0 (2026-11-17)

**What happened.** `tutti dispatch build <project> <suite> <rc file>` always exited `0`, even when the suite's build command
failed and wrote a non-zero code to the rc file.

**Why.** The CLI wrapper ignored `shards.run_prepare(...)`'s return code and returned success unconditionally, so callers
could not see that the build command itself failed.

**Rule.** A command wrapper returns the wrapped command's exit code unless it has an explicit, documented contract not to.
`dispatch build` still writes its rc file for the dispatcher, and now also reports failure to its caller.

## Untrusted text reached a command by a side door (security audit, 2026-11)

**What happened.** After a scout's `check:` stopped being run, the audit (docs/SECURITY-AUDIT-2026-11.md) found siblings:
another member's task `check` and a spare-room offer's ran through the autopilot's acceptance; a Sentry-derived check went
into a worker's brief; a stranger's comment counted as a scout's answer; a member's job escaped its container through a
device's shell runner and through the git cache a linked worktree mounts read-write.

**Why.** Each path was checked where the text entered, not where it was used: the acceptance runs `check` from wherever
the task came from, and the sandbox check looked at the machine the job named, not the one that ran it.

**Rule.** A field that ends up in a shell (`check`, a command, a runner) is only ever taken from this login's own config.
Check a sandbox at the place that runs the command, with the runner it really uses. Data from outside goes into a brief
marked as data, never as a command, and the process that reads it gets no token it does not need.

## The test suite filled /tmp's inodes (2026-11-15)

**What happened.** During the audit `/tmp` (a tmpfs) ran out of inodes: 53,000 leftover `tmp*` folders, about 85 files each,
from repeated `scripts/check.sh` runs by many agents. Every tool that needed a temp file failed with ENOSPC.

**Why.** Many tests use `tempfile.mkdtemp()` (the Household fixture among them) without removing it.

**Rule.** A test removes what it makes (`TemporaryDirectory`, or `addCleanup(shutil.rmtree, d)`); run the suite with
`TMPDIR` on a disk folder when several agents share a computer.

## A wait loop that matched its own command line

**What happened.** Agents waited for a job with `while pgrep -f '<pattern>'; do sleep ...; done`. Three of them
stalled for hours after the job had ended.

**Why.** `pgrep -f` matches full command lines, and the shell running the loop has the pattern in its own command
line. The loop found itself, forever. `ps | grep` fails the same way.

**Rule.** Wait on a PID (`wait $pid`, `tail --pid=$pid -f /dev/null`), a done-file or a log line, **always under a
timeout**. `tutti wait pid|file|grep ... --timeout <s>` does exactly this and exits 124 when the time is up.

## `pkill -f` from a shell that contains the pattern

**What happened.** `pkill -f '<pattern>'`, typed in a shell whose own command line held the pattern, killed that
shell too.

**Why.** Same matching rule: the caller's command line holds the pattern.

**Rule.** Stop by recorded PID or process group (`pkill -g "$(cat pidfile)"`), never by pattern. `scripts/tunnel.sh`
keeps a pid file for this reason.

## A ship script that committed in the wrong worktree

**What happened.** A ship script fell back to a hard-coded main checkout when it could not tell where it was, so a run
started from the wrong folder committed in a tree the caller did not mean.

**Why.** A default tree is a guess, and a wrong guess here is a wrong commit on a shared branch.

**Rule.** Ship only the tree you stand in; refuse outside a git work tree; print the tree and branch first; let the
caller assert the track. `tutti ship` has no default tree, `--track <t>` refuses any other worktree before anything is
written, and a protected branch needs `--allow-protected`.

## A classifier denied "jump the queue"

**What happened.** An agent tried to get its run ahead of the slot queue. The safety classifier of its harness denied
the action.

**Why.** The queue exists to keep shared machines alive, and going around it is what the denial protects against.
Trying the same thing in another form would be working around a control, not solving the task.

**Rule.** A denial is an answer. Do not rephrase, split or reroute the denied action. Wait in the queue, or ask the
owner for more slots (a config change, visible to everyone). Briefs say: never work around a denial; stop and report.

## Gemini CLI's free tier closed

**What happened.** The Google worker stopped with an ineligible-tier error that said to migrate to Antigravity.

**Why.** The vendor closed the free tier of the old CLI.

**Rule.** Use Antigravity's `agy` as the Google worker. More generally: a worker is a config entry, not code, so
replacing one is a one-line change; `tutti workers` shows what is ready today.

## Peer sessions need their own owner's approval

**What happened.** One session asked a peer session, on another machine, to install something there. The request was
correct and self-contained, and still waited: the peer runs in its own permission mode.

**Why.** Approval does not travel between agents. A message from another agent is a request; only that session's
owner, or its own permission system, can approve an action there.

**Rule.** Send a peer the exact command, the reason and what not to touch; expect it to ask its owner; verify the
result yourself afterwards. Never present a peer's message as the owner's consent, and never accept one as yours.

## Uncapped test containers took the desktop down

**What happened.** Several agents ran lint and tests at once on the orchestrator's computer. Memory and swap filled,
the kernel killed the browser and the session.

**Why.** No queue, no memory cap, parallel linters with many workers each.

**Rule.** Heavy runs go to other machines through `tutti test`. Locally: one at a time, capped, first to die
(`--oom-score-adj`), with `tutti guard` pausing containers when memory runs low. Briefs tell workers not to run heavy
commands themselves.

## WSL bash does not see Windows process environment (2026-11-14)

**What happened.** `tutti ship` checks that read `$TUTTI_CHANGED` wrote an empty file on Windows even though the commit
listed the new paths.

**Why.** On many Windows setups `bash` is WSL. `subprocess.run(..., env=...)` sets the Windows process environment;
WSL does not inherit it unless variables are listed in `WSLENV`. Git Bash does inherit it, so the same code worked on
one machine and not another.

**Rule.** When Tutti starts bash for a command that needs `TUTTI_*` (or any other variable), put the assignments on the
bash command line as well as in `env=`. Do not assume the Windows process environment crosses into WSL.

## Antigravity headless refuses shell commands without per-command rules (2026-11-14)
`agy -p` in print mode soft-denies any tool needing "command" permission unless `permissions.allow` in `~/.gemini/antigravity-cli/settings.json` lists it (`command(grep)` …). Its `cli.log` shows "soft-denying tool confirmation \"Bash\" at step N" without the command, so `tutti doctor` must find it another way (re-run with logging, or start with a broader read-only rule set). Claude Code likewise needs `Bash(agy *)` / `Bash(cursor-agent *)` allow rules before it can start these CLIs; the user must write them (an agent may not edit its own permissions).

## Windows bash.exe mangles the -c line (2026-11-14)

**What happened.** Local runs on Windows broke again in QA round 2: the command handed to WSL `bash.exe -c` arrived
split or with its quotes stripped, and arguments after `-c` were lost.

**Why.** Windows re-quotes the command line on its way to WSL; quotes, `&&`, `$(` and braces do not survive it reliably,
and WSL does not pass positional arguments after `-c` through as bash expects.

**Rule.** A command bound for Windows `bash.exe` avoids quotes, `&&`, `$(` and braces in the `-c` line and never relies
on arguments after `-c`. Send the script base64-encoded: `bash -c source <(echo <b64> | base64 -d)`.

## A CLI's rolling-window reset is a few seconds after ours (2026-11-14)

`agy /usage` reports a window nobody has started as resetting one full window after its own clock. Recorded as is, the
window started just after Tutti's `now`, so the reading fell into the window before and the account showed "late" with
no reading. Read and record with one `now`, and clamp a reset more than one window ahead to `now + window`
(`readers.not_started`).

## Ship committed a half-resolved merge (2026-11-14)

Twice in one day a ship ran while a rebase was stopped on a conflict, and committed files with `<<<<<<<` markers. Ship
now checks `git rev-parse --git-path` for `rebase-merge`, `rebase-apply`, `MERGE_HEAD`, `CHERRY_PICK_HEAD` and
`REVERT_HEAD`, and `git grep --untracked` for opening or closing markers, before the commit and again before the push.

## Antigravity's `--settings` replaces the allow list (2026-11-14)

`agy -p ... --settings '{"permissions":{"allow":["command(node)"]}}'` ran with `Allow:[command(node)]` only: the saved
list (`grep`, `git`, ...) was gone for that session (agy 1.5.9, its log's "CLI settings initialized" line). A per-run
allow list must name every command the task needs. agy 1.5.9 also ran `node -e` headless without any rule, where 1.3.3
soft-denied commands, so denials are detected from the run (`denied:` and `{log}`), not assumed from the version.

## Two runs in one second shared a worktree (2026-11-14)

Run ids were `<time>-<pid>`; two delegated runs started by one process within a second got the same id, so the second
reused the first's worktree and its diff. A unit test caught it. Ids now get `-<n>` when the log already exists.

## A routed check cannot read $TUTTI_CHANGED (2026-11-14)

A check that runs on another machine gets only its command line: the environment of `tutti ship` stays here. The list
of changed files travels in the command (`{changed}`, `{changed:ts,js}`). Inside one bash line, `${F:+cmd &&}` does not
make `&&` an operator: test with `[ -z "$F" ] || { ...; }`.

## A reviewer's acknowledgement is not a review (2026-11-14)

The first real `tutti pr review` reported "coderabbit reviewed" while CodeRabbit had only answered "Action not completed:
Review rate limited", and it edits its summary comment in place (its `created_at` stays old, `updated_at` moves) to say
"Review limit reached". Only a pull request review counts as a review; comments are read for limit messages, at the later
of their created and updated times.

## PowerShell flattens a one-pair list (2026-11-14)

The Windows look-around script listed projects as `@(@('RoRadio', 'C:\\...'))`; PowerShell flattened the single pair into two
strings, so `$x[0]` was the letter `R` and the dashboard showed projects named "R" and "C". A hashtable (`@{'RoRadio'='C:\\...'}`)
keeps one entry whole. The Windows side of a WSL machine is read over the machine's plain ssh (the Windows host): its
WSL had no interop (`Exec format error` for every `.exe`), and WSL's `/proc` never shows Windows processes.

## A fixed band hid "ahead" early in a window (2026-11-14)

`on pace` meant within 5 points of the line. On the second day of a week the line is about 6 %, so 10 % used (two thirds
over) read as `on pace`. The band is now a quarter of the line, 1 to 5 points, and each row prints the math.

## A headless agent denies a compound shell line (2026-11-14)

The first agy conductor runs passed; a later one ended after its CLI auto-denied a `Bash` call: the model had joined
commands (`cd`, `&&`, `>>`) that no `command(...)` rule allows, and Claude models in agy use a `Bash` tool the list does
not cover at all. The turn had exited 0, so it would have read as done: a turn now counts as done only with a `DONE:` line
and no denied tool call, and the instructions ask for one command per line and edits through the agent's own edit tool.

## agy's Claude models ignore per-command rules (2026-11-14)

`agy -p --model claude-sonnet-5-5-low --mode accept-edits` soft-denied `python3 -c ...` although `command(python3)` was
in the allow list, and ran `echo` although `deny: [command(echo)]` was set: Claude models call one generic `Bash` tool
(`Step_Generic` in agy's log), which per-command rules do not match. Only `command(*)` allowed it. So agy-claude carries
its own `--settings` with `command(*)`, and an account's args replace the worker's flag instead of being appended after it.

## A brief rule can veto the task (2026-11-14)

The first agy-claude proof asked the worker to run the tests; it fixed the bug but ran nothing, because the brief's
standing rule said not to run tests on this computer. A proof of a permission needs a command no brief rule forbids
(`uname -s`, `git log`).

## A meter's reset is not the next "Resets" on the page (2026-11-14)

The reading helper cut a meter at 400 characters, so "Other Models 14% used" took "Resets Nov 15" from Grok's weekly
meter below it instead of the plan's "Usage limits reset on Dec 10". A meter now ends at its amount line, and only a
"Resets" line right after it is its own.

## Changed files with spaces (2026-11-14)

`$TUTTI_CHANGED` and `{changed}` were space-joined and read back with `split()`, and git's `--name-only` quotes unusual
names: `my file.ts` reached a check as two paths. Names are read with `-z` and passed one per line.

## WSL has no temperature, and Windows' ACPI zone may lie (2026-11-14)

The PC showed no temperature: WSL has no `/sys/class/thermal` zones and `nvidia-smi` is not on WSL's PATH (it lives in
`/usr/lib/wsl/lib`). Windows' `MSAcpi_ThermalZoneTemperature` answered 2900 (16.9 C), a fixed placeholder on that board.
The GPU now comes from `nvidia-smi` there, the CPU from Windows only when the value is plausible, else "not readable".

## A counter in a transcript is not progress (2026-11-14)

An agent's background task output is its JSONL transcript; a "[37/37]" in a file it read showed the agent at 100 % while
it worked. Counters are read only from command output; a transcript gives tokens and tool calls, and its progress comes
from history or a labelled rough estimate.

## Scripts never finish where the dashboard can see it (2026-11-14)

A watched script's job disappears when its process ends; it is never "done", so no duration was ever learned. The
history now remembers running jobs (`durations.json`) and counts one that is gone as finished; a test container only when
its machine was probed, so a failed probe does not record every container as ended.

## A narrow allow list costs the autopilot one attempt per task (2026-11-14)

The first autopilot runs sent tasks to agy-gemini, whose `--settings` allowed a list of read-only commands; any task that
had to run a command (a test, a check) was soft-denied, changed nothing and moved up to agy-claude on the retry. The ladder
recovered, but each such task spent an attempt for nothing. Every account the autopilot uses gets the same `command(*)`
allow as agy-claude (the worktree, the brief and ship's guard are the limits).

## `pkill -f <pattern>` can match the shell that runs it (2026-11-14)

`pkill -f "dashboard --port N"` inside a `bash -c '...'` matched that shell's own command line and killed it mid-command.
Stop a dashboard by its pid (`ps`, then `kill <pid>`), never by a pattern.

## A task's own check hid the gates it broke (2026-11-14)

**What happened.** Six autopilot results on a project's backlog were marked passing and waited for review, yet each failed
the repository's static gates (ruff check, ruff format, mypy, the scenario registry).

**Why.** A task's `check` replaced the repo's: it ran only the task's new tests, which passed. Nothing ran the lint and type
gates of the changed files.

**Rule.** Acceptance is the task's check AND the repo's `check` (0.1.16, `autopilot.acceptance`); a repo's `check` names its
gates for the changed files, not only its tests.

## A field name shared by two meanings (2026-11-14)

**What happened.** The planner's decision, stored on an account's state as `plan`, came out empty on the dashboard.

**Why.** `plan` was already the account's subscription label from its `plan` reader, written after pacing; it overwrote the
planner's dict with `None`.

**Rule.** The planner's field is `quota_plan`. Before adding a key to a shared state dict, grep for every writer of it.

## An escalation ladder that only climbs strands its retries (2026-11-14)

**What happened.** t13 to t15 had failed on agy-gemini and agy-claude. Retries start one step up, so only cursor (ahead of
its pace) and the PC (yield flag up) could take them; both agy accounts had room and sat idle, and the stopped line named
only cursor and the PC, so it looked as if agy had been forgotten.

**Rule.** A rule that narrows where work may go needs a fallback for when the narrowed set has no room, and a "why nothing
happened" line names every candidate, the ones with room too, with the reason they took nothing.

## A "missing test" by file name is noise in a repo with a coverage gate (2026-11-14)

**What happened.** The first `fill_room` round queued tests for content-hashed bundles in `cdn/` and for `TODO(owner)`
quoted in a changelog and a README; in the map repo 495 of its files had no test named after them, while its per-file
90 % coverage gate already proves each one tested.

**Rule.** A task source is checked against the real repositories before it runs unattended: built output, prose and
quotes are not tasks, a test that imports a file tests it, and a repo can turn a heuristic source off.

## A test's empty env dict fell back to the owner's real telemetry DSN (2026-11-14)

**What happened.** The telemetry *Sending* tests passed `env={}` to keep the environment out; `env_file()` then fell back
to the real `~/.config/tutti/telemetry.env`, whose DSN (env, then file, then config) beat the fake Sentry's. Once the
owner saved a DSN, four tests posted to the real Sentry and failed (403) on the laptop only; CI has no such file.

**Rule.** A test that isolates the environment passes a dict that names its own temporary `XDG_CONFIG_HOME` (never `{}`)
and patches `os.environ` to drop every `TUTTI_*` variable, so nothing on the machine can make it send or fail.

## GitHub answers 201 to a review request it drops (2026-11-14)

**What happened.** `tutti pr review` asked Copilot through `POST .../requested_reviewers`; GitHub answered `201 Created`
with an empty `requested_reviewers` list, so Tutti counted Copilot as asked and waited for a review that never came. The
bare login `copilot-pull-request-reviewer` is refused with 422 "not a collaborator".

**Rule.** A request's status is not its result: read the answer back (is the reviewer listed?) and fall back (GraphQL
`requestReviewsByLogin` with `botLogins`), else say where the switch is.

## A probe that forks per process costs more than the work it watches (2026-11-14)

**What happened.** The dashboard's activity scan ran `readlink`, `tr` and `head` for each of ~800 processes every 30 s:
12 s a probe and ~10 % of a core, most of the dashboard's cost. agy's `/usage` reader ran 120 times an hour with an
answer that did not change, and each ssh call paid a new handshake.

**Rule.** Measure a probe's wall time, CPU and spawns before adding to it (`time`, a Popen counter). One process for a
list (find, ps, awk), shared connections, and back-off when an answer repeats.

## A device nobody queues work for sits idle (2026-11-14)

**What happened.** A phone sat idle while a project still had e2e tests for it. The phone was attached to the PC's
Windows adb. That PC's Tutti was an old install with no device support and an example config, and the laptop's config
listed no devices. The project was read-only for Tutti, and its e2e ran only when its own session started it by hand. No
queue existed for the phone, so nothing could tell it was idle.

**Rule.** Work that a target can do must be declared where Tutti sees it (`projects.<p>.tests`), and idle targets must pull
it themselves. A watchdog that shows "idle while work waits" turns a silent gap into a visible fix.

## A YAML scalar dumped alone ends with a document marker (2026-11-14)

`yaml.safe_dump('site.github.io', default_flow_style=True)` is `site.github.io\n...\n`: a bare scalar gets the `...`
document end, so a config line built from it broke the file on the first real run of `tutti setup` (a repository named
like a domain). `configmerge.flow` dumps the value inside a list and strips the brackets; a test round-trips names with
dots and values with colons.

## Config written for code that had not shipped took the dashboard down (2026-11-14)

**What happened.** An agent added `reader: {format: cursor-api, ...}` to the owner's live `tutti.config.yaml` while the
code that reads that format was still in an open pull request. The installed Tutti refused the whole config (the reader
had no `command` or `bucket`), and the dashboard could not restart until a person commented the lines out.

**Why.** The config is shared by every Tutti process on the computer, and the installed version, not the branch the agent
works on, is what reads it. Validation treated a reader it did not know as a fatal error.

**Rule.** Never write a key or value into a live config before the version that reads it is merged, fast-forwarded and
running; change the config in the same step that restarts on the new version. Tutti is lenient too (0.1.41): an account key
it does not know or a reader format it cannot read is a warning (`tutti: warning: ...`), and that reader is skipped; the
rest of the config and the dashboard still start.

## The same live-config outage twice (2026-11-14)

**What happened.** Hours after the reader outage, an agent wrote a per-shard mapping (`{name, devices, holds, extra}`) into
the owner's live config while the version that reads it was still a pull request. The installed Tutti refused the config
again, and every `tutti` command failed.

**Rule.** The same as above, now for shards: change the live config only after the release that reads it is merged,
fast-forwarded and restarted. Since 0.1.42, a shard entry this version cannot read is a warning and only that shard is
skipped.

## `adb mdns services` is empty right after the adb server starts (2026-11-14)

The first dry run of `tutti devices move --to nuc` said no device was seen over mDNS from the NUC, while `avahi-browse`
there listed all three. The preflight's `adb mdns services` had started the adb server, which had not heard any
announcement yet. **Rule.** Ask adb's mDNS list after `adb start-server` and again for a few seconds until a connect
service shows (tutti/adbhost.py `services`); never conclude "not on this network" from one look.

## Google's sign-in button needs inline styles (2026-11-14)

**What happened.** With `style-src 'self' https://accounts.google.com/gsi/style` the GIS button rendered as a page-sized
Google logo: GIS injects inline styles, and the CSP blocked them.

**Rule.** The login page alone allows `'unsafe-inline'` styles (it renders no data as markup); the dashboard's own CSP is
unchanged. Check a third-party widget in a real browser, not only through the server's headers.

## Two scripts of one page declared the same constant (2026-11-14)

**What happened.** users.js declared `const DAY_NAMES`, which another script of the dashboard already declared. Classic
scripts share one global scope, so users.js stopped with a SyntaxError and the whole sign-in UI (role, Users tab, My
schedule) silently did nothing. The unit tests passed; only a real browser showed it.

**Rule.** A test reads every script that index.html loads and fails on a top-level name declared twice. Check a page
change in a browser, with the service worker's cache cleared.
## Two config files that drifted apart (2026-11-14)

**What happened.** The owner had `~/.config/tutti/config.yaml` and a project `tutti.config.yaml`. The dashboard and the
MCP servers read the first, the autopilot (started in the project folder) the second; Settings edits landed in one copy.
**Rule.** One config per user: the XDG file wins over a project file (0.1.62), every write goes through
`config.write_text` (a symlink stays a symlink), and `tutti doctor` reports a copy that differs.

## Claude Code "needs restart" while `claude mcp list` said Connected (2026-11-14)

The card compared the connect time with the start time of every `claude` process. In Claude Desktop's Code tab old
sessions always run, so the row never left "needs restart". **Rule.** Ask the agent's own health check when it has one
(`claude mcp list`); an older session is a soft note, not the status.

## 2026-11-15: a probe line broken by a tool's error, an idle hint that is never set

- **What happened:** the NUC read as unreachable for hours, so its model and slots sat idle. On the NUC, `nvidia-smi` without
  its driver printed its error on stdout, inside the probe's one summary line.
- **The rule:** every value a probe line carries is filtered to its expected shape (numbers only) before it is printed.
- **Also:** logind's `IdleHint=no` with `IdleSinceHint=0` means the desktop does not report, not that someone is at the
  keyboard. Trust a hint only when its time is set.
## A class's `display` beats the `hidden` attribute (2026-11-14)

`.users-view { display: grid }` overrode `hidden`, so the empty Users view showed on the main dashboard. The same day the
Private network card's POST handed raw bytes to a function that expected a dict: every click crashed the request
("Failed to fetch") and filled Insights with "AttributeError in api".

**Rule.** A class that sets `display` on an element that can be `hidden` gets a `.<class>[hidden]` rule; a test lists
every hidden element of index.html whose class sets a display. A POST handler parses its body itself: `/api/vpn` was
the one that did not (the others were checked), and it now answers any body.

## Test folders filled /tmp's file slots (2026-11-15)

`tempfile.mkdtemp()` in tests (test_household, test_hhjobs, test_connect and others) was never removed: about 45,000
folders piled up in /tmp until it had no free inodes, and every command on the laptop failed, the agents' included.

**Rule.** A test cleans up what it makes (`tmpdirs.mkdtemp()`, a `TemporaryDirectory`, `addCleanup`); `tests/run.py`
fails a test that leaves anything in its own temp folder, and `scripts/check.sh` runs in a temp folder it removes. Tutti
itself watches the room on its disks (`tutti/diskroom.py`) and starts no work when it is low.

## Autopilot tests that read the laptop's own load (2026-11-15)

Three `test_autopilot` tests failed on the laptop whenever the whole suite ran, on main too, and passed in CI and alone.
`autopilot.tick` reads this computer's free memory and load (`parallel.host()`) when the config has no `_host_raw`. Under the
suite's own load the adaptive limit left no online slot, and the local model in the test config took the task.

**Rule.** A test that plans or ticks the autopilot sets `cfg['_host']` and `cfg['_host_raw']`. It never measures the
machine it runs on.

- **One argv string is at most 128 KB** (2026-11-15): a test that passed the Settings data to `node -e` broke when the schema grew (E2BIG, "Argument list too long"). Pass big scripts through stdin (`node -`).
