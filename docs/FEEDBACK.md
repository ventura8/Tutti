# Feedback log

First users log **every** problem they hit here, the small ones too. This file is the backlog: an entry is closed by
the change that fixes it, and a lesson worth keeping moves to [LESSONS.md](LESSONS.md).

How to add an entry: append a row. Say what you ran, what happened, and what you expected. No hostnames, names, paths
of your project or secrets: describe the setup in general words.

| # | Date | Command | What happened | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| 15 | 2026-11-18 | Chat run status | A missing run report prompted repeated status requests. | Check automatically for a bounded time and report a timeout honestly. | Bounded Chat status polling added (t296); restart-persistent monitoring remains open. |
| 14 | 2026-11-18 | Dashboard update during Chat | An automatic page reload can discard an unsent Chat draft or attachment. | Wait until Chat is idle before reloading the page. | Page-reload guard added (t326); safe server handoff remains open. |
| 13 | 2026-11-17 | Chat account pause | Pausing a ladder account made configuration reloads fail, blocking dashboard probes and chat. | Keep the ladder valid while paused and restore the account on resume. | Fixed in the next release (t321). |
| 1 | 2026-11-14 | `tutti quiet --summary <worker>` | A failing command that printed nothing got an invented failure summary from the model. | No summary for an empty log. | Fixed in 0.1.0: an empty log prints "(the command printed nothing)". |
| 2 | 2026-11-14 | `tutti usage` | With `share: 0.5` a whole-account reading was compared with half the quota, so 60 % read as "spent". | A reading is the whole account's use; the share is information. | Fixed in 0.1.0: the pace is judged against the whole quota and the share is shown beside the verdict. |
| 3 | 2026-11-14 | `tutti test` on a `wsl` machine | Only the probe was exercised (the machine's yield flag was up); the copy and the run are untested there. | A verified full run. | Open (roadmap 0.2). |
| 4 | 2026-11-14 | `tutti test <track> echo -n x` | An option-like word in an unquoted command is read as an option of `tutti`. | Works either way. | Fixed in 0.1.8: the command is the rest of the line (`argparse.REMAINDER`); options of `tutti test` go before the track. |
| 5 | 2026-11-14 | `tutti ship --full` | The routed checks copy the tree you stand in; the scripts it replaces copied the main checkout with its full `.git`. Gates that read git history see a shallow repository. | Same result as before. | Open: watch the first full run. |
| 6 | 2026-11-14 | unit tests on Windows | `pid_gone` true for live PID; ship check saw empty `$TUTTI_CHANGED`; probe `free=0` with locks on a Windows tempfile; install pointed at missing `setup`/`doctor` and purge used `%APPDATA%\tutti`. | Portable waits, ship env, flock on `/tmp`, real config path. | Fixed in 0.1.1 (Windows round 1). |
| 7 | 2026-11-14 | `tutti run auto "<task>"` | The worker edited the tree the orchestrator stood in; no diff file, no checks, no report; the orchestrator had to do the rest by hand. | A bounded task handed off end to end. | Fixed in 0.1.8: `tutti run ... --repo <repo>` (worktree, headless worker, diff, routed checks, report), `--bg` and `tutti wait run <id>`. |
| 8 | 2026-11-14 | `tutti test` with Playwright | The webServer timed out: the e2e build had not run. | The build first, automatically. | Fixed in 0.1.8: `test.prepare` puts the needed step in front. |
| 9 | 2026-11-14 | `tutti test` on the build box | The build box reached 104 C with three runs and throttled every one of them. | Fewer runs when hot, fall over to another machine. | Fixed in 0.1.8: `machines.<m>.heat` (`cap_c`, `stop_c`, `hot_slots`); proven: a real run went to the laptop with "nuc: hot: 89 C, slots capped to 1". |
| 10 | 2026-11-14 | targeted checks | Coverage was switched off on targeted runs and nothing said so. | Say when coverage was not checked. | Fixed in 0.1.8: the run report and `tutti ship` print "coverage NOT checked". |
| 11 | 2026-11-14 | PR reviews | CodeRabbit's free limit was reached and Copilot's review never arrived; the PR waited for hours. | Know that no review will come, and get one anyway. | Fixed in 0.1.8: `tutti pr review <n>` with a timeout and a local fallback review posted to the PR. |
| 12 | 2026-11-14 | `tutti usage` | The Claude reading went stale for hours without a reminder; Cursor's usage page link was the old one. | A reminder, a file the orchestrator can write, the right link. | Fixed in 0.1.8: `stale_hours` reminders, `reading_file`, the reading helper, `cursor.com/dashboard/spending`. |

- 2026-11-14 (map project): the installer's `git clone` failed on a laptop whose PATH had a broken git first (another tool's bundled git with a mismatched libcurl). Works with /usr/bin/git. `tutti doctor` should detect a git that cannot do HTTPS and suggest fixing PATH. Fixed in 0.1.15: `tutti doctor` checks git.
- 2026-11-17 (owner): `tutti run auto "<task>" --repo tutti` (and MCP `delegate_task` with project `tutti`) from a
  non-git cwd failed with "`tutti` is not inside a git work tree". Expected: a worktree of `autopilot.repos.tutti.path`.
  Fixed in 0.25.4: `--repo <name>` resolves through `autopilot.repos.<name>.path` before `git worktree add`.
- 2026-11-14 (map project): `tutti run auto "<task>" --repo <another repo>` routed to agy and its diff was right, but the run's check failed: it ran the config's default check (`npx tsc`) in the other repo's worktree instead of the target repo's own `scripts/check.sh`. The diff was applied by hand. `--repo` should pick the target repo's check. Fixed in 0.1.12: `projects.<name>.checks`, a config inside the target repo, `delegate.project`.
- 2026-11-14 (map project): `tutti pr review` asked both reviewers but could not read the reviews: it calls `gh api --slurp`, which gh 2.60 does not have. Polled by hand. Fixed in 0.1.12: no `--slurp`; the pages are read back to back.
- 2026-11-14 (map project): most running jobs on the dashboard showed "—" for progress (agents, scripts whose output goes through a pipe, containers); the owner could not tell how far anything was. Fixed in 0.1.12: layered progress with its source, and job facts.

- 2026-11-14 (owner): the dashboard still said "Use it: 46 bounded task(s) can go here now (tutti run auto …)" while the autopilot was on; agy took none of the queued tasks and three results waited for a manual review. Fixed in 0.1.23: `fill_room` fills the room by itself, retries fall back to agy when the steps above have no room, `auto_review` lands results, and the card says what the autopilot does with the room.

- 2026-11-14 (owner): pacing ignored how the owner works (about 8 h of prompting on weekdays, agents alone on weekends, days away, holidays), so either the main agent risked its limit or quota expired. Fixed in 0.1.25: the weekday x hour owner profile, the reserve on working days only, the mode with I'm away until…, and a 7 x 24 heatmap.
- 2026-11-14 (owner): Settings edits (energy price, working days and hours) landed in one of two config files; the
  autopilot read the other. Fixed in 0.1.62: one config per user, writes through symlinks, the doctor's and Settings'
  merge.
- 2026-11-14 (owner): "Claude Code: needs-restart" while `claude mcp list` said Connected, and "Claude Desktop (chat):
  not connected" after the app dropped Tutti's entry. Fixed in 0.1.62: the status comes from `claude mcp list`; a dropped
  Desktop entry is put back with a backup.

- Automatic account usage should reveal stale or unavailable readings and recover without requiring manual entry (t317).

- Cached automatic quota results must not be presented as fresh reads, and failed or skewed readers must not make panic rescue look safe (t317).
