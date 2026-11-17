# Telemetry and self-improvement

Tutti records how it is used and how it fails, so it can be made better: on this computer always, and sent to a Sentry
project (or your own endpoint) only when you say yes. The same records feed the dashboard's **Insights** card and
`tutti telemetry report`, and, through **self-improvement**, become fixes.

## Two switches

| What | Default | Where it lives | Change it |
| --- | --- | --- | --- |
| Local recording | always on | `<state>/telemetry/events.jsonl` (rotated at 2 MB, 3 old files kept) | `TUTTI_TELEMETRY=off` in the environment records nothing at all (tests, a locked-down machine) |
| Sending | **off** until you say yes | `<state>/telemetry/consent.json` | asked once on the first interactive run; `tutti telemetry on\|off`; the **Send reports** button on the dashboard's Insights card |

The question is asked once, in plain words: on a terminal (below), or on the dashboard, where the admin sees a **Help make
Tutti better?** banner with **Yes, send them** and **No, thanks** until either answer is given. The Insights card always
says whether reports are sent and where to.

The terminal question is asked once, on a terminal only (never in CI, a pipe, or the commands other tools run: `run`, `test`,
`wait`, `quiet`, `dashboard`, ...). Any answer but yes is no.

## What is collected

Every field value is a *category* (letters, digits and `_.:+-`, at most 64 characters) or a number. Anything else is
stored as `<other>`, so a task, a prompt or a path can never pass as a value.

- **Commands**: the command and its fixed action (`backlog add`, `telemetry on`), the *names* of the options given
  (`--bg`, `--repo`, never their values), the duration, the exit code and the outcome (ok, failed, usage, interrupted,
  crash).
- **Features**: routing decisions (`routing.choice`: routed, `--on` or a device; the machine's *kind*: ssh, local, wsl,
  android; queued or not), test and device runs (outcome, minutes, how many inputs and fetches), delegated runs
  (`run.delegated`: the worker's tool, `claude`, `cursor`, `agy`, ... or `other`, outcome, minutes, checks), the autopilot
  (`autopilot.plan` state and counts, `autopilot.attempt` outcome, attempt number, tool, step kind, size, minutes,
  `autopilot.escalate`, `autopilot.park` with its cause `no_progress` or `max_attempts`), waits (`wait.stall`,
  `wait.timeout` with the kind of wait and seconds), scouts (`scout.ask`, `scout.tick`, ...: the skill and the outcome),
  self-improvement (`selfimprove.analyse`, `merge`, `release`, `switch`), AI assist (`aiassist.analyse`, `tune`, `undo`, `switch`), the household (`user.<action>`,
  `household.init`, `household.join`, `household.sync`, `household.grant` (machines, slots granted, machines limited): the
  outcome and counts; a user only as `user`, 12 hex digits of
  SHA-256 of a random id, never a name: docs/HOUSEHOLD.md), handled errors by exception type.
- **Dashboard**: which panel and which control was used (`usage` / `refresh-readings`): the card's heading id and the
  control's id, never its text or a value; script errors and unhandled rejections of the page.
- **Crashes** (Python and the dashboard's script): the exception type, its message scrubbed (below), and the stack frames
  as file, function and line number. Tutti's own files are named relative to the package (`tutti/cli.py`); any other file
  only by its last two path parts.
- **Every sent event** carries the release (`tutti@<version>`), the environment (`telemetry.environment`, default
  `production`), the OS name and release, the Python version, the command, and a random install id made once on this
  computer (it counts installs; it says nothing about you or the computer).

## What is never collected

Prompts, task titles and text, briefs, code, diffs, file contents, the source line of a stack frame, local variables,
arguments and option values, paths under your home folder (replaced with `~`), host names (this computer's, every
machine's `host` and `user` from the config, and any name that looks like a domain: `<host>`), IP addresses, e-mail
addresses and `user@host`, URLs (`<url>`), tokens and keys (GitHub, OpenAI/Anthropic-style `sk-`, AWS, Google, JWTs,
`Bearer ...`, `password=`/`token=`/`key=` values, private key blocks, long hex and base64 runs), and anything a message
quotes (`"..."`, `'...'`, `` `...` ``: usually what you typed). No `server_name` is sent. The scrubbing is in
`tutti/scrub.py` and tested in `tests/test_telemetry.py` against every kind above.

In the Sentry project, also turn on **Settings, Security & Privacy, Prevent Storing of IP Addresses**: Sentry would
otherwise keep the address a report came from.

## Where it goes

- **Sentry** (the envelope API over HTTPS; no SDK): a crash or a failed command (exit code other than 0, 124, 125 and
  130) is one event; usage events go in batches of 100 as the breadcrumbs of one `info` event (`tutti usage: N
  event(s)`). A 401, 403 or 404 keeps the queue and says which cause it is in `tutti telemetry status` and on the dashboard: Sentry's own reason (`ProjectId`: the key no longer belongs to an active project, so the project was deleted or moved or the key disabled or rotated; 401: the auth header; 404: the DSN was copied wrong), and a clue: when `$TUTTI_SENTRY_DSN` supplies the DSN, that it wins over the file (set it to the new DSN or unset it); otherwise whether the DSN file was written after the last accepted send (check that DSN first) or before it (look at the key in Sentry first). The fix is always the same: copy the active DSN from the project's Client Keys and run `tutti telemetry setup`. Items a send moved aside and never gave back (its process ended mid-send, or the file is older than an hour) return to the queue on the next send; each file is claimed by a rename first, so two senders never return it twice. The queue (`<state>/telemetry/outbox.jsonl`, at most 2000 lines) is sent at most every 10 minutes, at the
  end of a command or by the dashboard; a crash is sent at once. Offline, throttled (429) or a server error: the queue is
  kept for the next try. A report Sentry refuses (another 4xx) is dropped.
- **Your own endpoint** (`telemetry.endpoint`, https; http only on 127.0.0.1): `POST {install_id, release, id, items}`
  in batches of 100, the same items.

## The release default: no setup for new installs

A release carries the maintainer's Sentry DSN in `tutti/release.json` (DSNs are public; the release workflow writes it from
the Actions variable `TUTTI_SENTRY_DSN`). So a stranger's install that says yes reports to the maintainers with no setup.
The order is `$TUTTI_SENTRY_DSN`, then `~/.config/tutti/telemetry.env`, then `telemetry.sentry_dsn`, then the release default
(`tutti telemetry status` says `release default`). Whatever the DSN, **nothing is sent until sending is on**.

## Set up sending to your own Sentry (optional)

1. Create a Sentry project for Tutti (platform Python) in your Sentry organisation: an account step, done by you.
2. Copy its DSN: Sentry, **Settings, Projects, <the project>, Client Keys (DSN)**.
3. Run `tutti telemetry setup` and paste it (the input is hidden). It is checked and kept in
   `~/.config/tutti/telemetry.env` (mode 600), never in a config file or a repository. `$TUTTI_SENTRY_DSN` wins over the
   file, the file over `telemetry.sentry_dsn` in the config.
4. `tutti telemetry on` (or **Send reports** on the dashboard), then `tutti telemetry send --test`: one event
   "tutti telemetry test" appears in the project.
5. Turn on **Prevent Storing of IP Addresses** in the project's settings.

`tutti telemetry status` shows whether sending is on, where to (the Sentry host, never the key), what is queued and the
last send problem.

## The report

`tutti telemetry report [--days 7] [--json]` and the dashboard's **Insights** card read only the local events: the most
used commands, the slowest (median and p90), the most failing, quota wasted on failed autopilot attempts and delegated runs
(per tool, with minutes), stall causes, why tasks were parked, escalations, crashes by type and place, and dashboard use.

## Self-improvement

Crashes and insights become fixes. `self_improve.enabled` is **on by default**; switch it on the dashboard's
Self-improvement card or with `tutti selfimprove on|off`. The pipeline:

1. **Sources.** Sentry issues, triaged by a scout (Grok Bot, connected to Sentry; docs/SCOUTS.md, *Sentry triage*), and
   the local analysis every `self_improve.every_hours` (6): a crash, a command that fails more often than not
   (`min_count`, 3, failures or more), repeated stalls, quota wasted on failed runs.
2. **Tasks.** Each becomes a backlog task (`source: sentry` or `insight`, tagged with the project) with an acceptance check
   and a note: *first add a regression test that reproduces it and fails, then fix it*. The acceptance check is always
   the repository's configured one: a command suggested in a scout's answer (which read attacker-influenced Sentry
   events) is passed to the worker as a hint and never run. One task per Sentry issue or
   insight; at most `max_new_per_day` (3) new ones a day. They are queued by themselves (`auto_accept`, default
   `true`); with `auto_accept: false` they wait as `candidate` until `tutti backlog accept <id>`. Local insights go to `self_improve.home`, the project Tutti's
   own fixes belong to.
3. **Fix.** The autopilot works on them like any task: retries up its ladder, parked after the no-progress guard.
4. **Pull request.** A passed task becomes a ready pull request, to `self_improve.base` for a project that may not merge
   (a work branch; never `main` or `master`: such a project with no base stops at a committed branch).
5. **Reviews.** `tutti pr review <n>` runs once per pull request (Copilot, CodeRabbit, or a cheap worker's review).
6. **Merge** (only a project with `merge: true`): when every check is green and at least one passed (CI, Sonar), the
   review run ended well, no review asks for changes or is still required, no review thread is open, and GitHub found it
   mergeable. Review threads are paginated; an incomplete or invalid thread list blocks merging. Never with `--admin`,
   and only at the head commit the gates saw (`--match-head-commit`). Changes requested
   send the task back to the autopilot, and its next push is reviewed again. Existing PRs from ordinary backlog tasks
   follow this loop too when their project explicitly enables both self-improvement tasks and merging. The retry pushes
   to the existing PR only after checking its repository, original branch and task marker; a mismatch is not pushed or
   merged. For both self-improvement and ordinary tasks, review and merge remain blocked after a retry until GitHub confirms
   the exact pushed head; a failed push cannot cause the old head to be merged. Projects without merge permission retain
   their existing review and publishing behavior. A posted local fallback review with findings is retried once per
   normalized finding fingerprint; repeated identical findings wait for inspection instead of cycling. Review text is
   untrusted data, never an instruction to execute commands or read credentials. When all threads are resolved but a
   changes-requested decision remains, Tutti checks all review records and their commit IDs. A decision only on an older
   head waits for the reviewer to update it; missing or incomplete evidence also waits. Tutti never treats that inference
   as approval or dismisses the decision.
   Tutti checks every page of review threads; missing, malformed, partial or cyclic results block a merge until GitHub can
   return a complete list. This check alone does not bind the destination branch atomically at merge time.
   Each review has its own completion file and a per-PR process lock. If no result arrives after the review timeout and
   the recorded process is provably dead on this boot, Tutti retries at most twice; a held lock, unknown PID, another boot,
   or an unsupported process lock waits for inspection instead of risking overlapping reviews. Older records with no
   verifiable process identity also wait; an operator must confirm the old review is no longer running before clearing
   the stale record. Automatic review requires bot evidence on the exact head and ignores dismissed reviews and scout
   comments without commit identity. The local fallback reviews immutable base/head patches, verifying the complete file
   count and line totals; binary, missing, oversized or truncated patches block automatic merging. Local rounds are
   counted per base/head pair and an exhausted round never counts as a successful new review. The head and base are checked again
   after review, and a previously stored unbound result cannot authorize a merge. Before requesting a merge, Tutti
   rechecks the recorded base and requires classic branch protection with strict up-to-date required status checks
   enforced for administrators and bypass-capable roles, with no configured PR bypass allowances;
   unknown protection and rulesets it cannot verify block the merge. GitHub's merge API can condition on the head but
   has no base-SHA compare-and-swap, so server-side enforcement is still necessary and there is no atomic base guarantee.
   Repositories without this strict protection must configure it before autonomous merges can proceed.
7. **Release** (only with `release: true` and a `release_command`): a batched patch release when fixes were merged and
   the last release (or failed try) is `release_every_hours` (24) ago. A release counts only once its command exits 0; a
   failed one leaves its fixes for the next. Tutti's own is `scripts/release-patch.sh`: it bumps the patch,
   writes the CHANGELOG from the merged commits, opens and merges the release pull request after its checks, tags it and
   creates the GitHub release.

Per project, under `projects:`:

```yaml
projects:
  Tutti:
    repo: octo/tutti
    sentry: {org: octo, project: tutti}
    self_improve: {tasks: true, autopilot_repo: tutti, merge: true, release: true, release_every_hours: 24,
                   release_command: bash scripts/release-patch.sh}
  My App:                        # stops at a ready pull request to its work branch; the owner merges and tags
    repo: octo/my-app
    sentry: {org: octo, project: my-app}
    self_improve: {tasks: true, autopilot_repo: app, base: develop}
  Other:                         # read-only for Tutti: the triage goes on an issue only, never a task
    sentry: {org: octo, project: other}
self_improve: {enabled: true, auto_accept: true, home: Tutti, every_hours: 6, max_new_per_day: 3, min_count: 3}
```

The dashboard's **Self-improvement** card shows each item's way: the Sentry issue or insight, the task, its pull request,
merged, released, and the gate it waits on. The gates are pure functions in `tutti/selfimprove.py`, tested in
`tests/test_selfimprove.py`.

## AI assist

Tutti uses AI, local and online, to make itself better. **Off by default**: an admin opts in with `ai_assist.enabled`
(Settings, *Opt-ins* or *Telemetry and self-improvement*, or **Turn on** on the dashboard's *Tutti is improving itself*
card; `POST /api/ai-assist` is an admin request). The other switches count only while it is on:

```yaml
ai_assist:
  enabled: true        # the opt-in (default false)
  local: true          # local models: the ollama workers (default true)
  online: true         # the scout's research and design review, with spare quota only (default false)
  online_code: false   # briefs, failure output and code may go online too (default false: they stay local)
  auto_tune: true      # small parameter changes apply by themselves, undoable (default true)
  every_hours: 6       # the self-analysis round
  max_proposals: 5
  local_worker: ''     # kept for old configs: the most capable free model always wins (below)
  online_worker: auto  # the online fallback: a subscription CLI worker, or auto (the first with room within pacing)
  preempt: true        # Tutti's own short jobs go first on the best model (false, or a list of kinds)
  unblock: true        # the unblocker (below)
  unblock_repos: []    # owner/name: failed pull-request checks rerun once per commit
  scout: ''            # the research scout; empty: the one with the research skill
  research_every: 7d
```

### Tutti's own AI: the best free local model, always

Every internal use of a model (a needs-question's wording, the welcome-back summary, Sentry triage, a scout's draft, task
sizing, chat compaction, self-improvement proposals, the unblocker's diagnosis) goes through one helper,
`tutti/brain.py`'s `ask`. `tests/test_brain.py` fails if any other module calls a model itself (the exceptions are the agent
harnesses the owner picks: the conductor, chat, `tutti run` and `tutti translate`).

- **Best** means the most capable model that is up and free now on *any* household machine: every model each machine has
  (the fleet reading, refreshed after every probe, so a new model is a candidate at the next call), ranked by tier and
  parameters scaled by the quantisation (Q8 1.0, Q5-6 0.95, Q4 0.9, below 0.8) plus fleet.score's per-kind bonus. No small
  default wins while a larger model is free.
- **Household members' machines count too** (opt-in, `ai_assist.household_models: true`; off by default because the
  prompts, which hold failure output and task titles, then go over the network to a machine another person controls): a member's machine this login uses (`tutti household machine use`) whose owner
  shares a model server (`host` and `ollama_port` in the share) adds its models, read from `/api/tags` once a minute. It is
  never pre-empted (Tutti goes first only on its own machines); a busy or paused one is skipped.
- **Sentry triage and scout drafting** go through it too: a triage report the scout wrote without TUTTI-TASK lines gets them
  drafted by the best free model (then online within pacing). Only lines naming a Sentry id found in the report are kept
  (at most 5), and the tasks wait for the owner's approval because the report is strangers' text. A report no model could
  read is tried 3 rounds. and the design review's question carries three focus
  questions a local model drafted from the week's numbers.
- A machine whose yield flag is up (its owner's) or a server that does not answer within a second is skipped.
- **Tutti first** (`preempt`, default true): when the best model is busy with other work (a delegated run, a project task,
  chat), Tutti does not settle for a smaller one. It raises the machine's pre-emption marker; the other work's next request
  waits (`local.wait_for_preempt`), keeping its conversation, worktree and run as they are; Tutti's job runs (bounded to
  180 s) and the marker is lowered, so the other work goes on with nothing lost. A stale marker counts for 180 s at most.
- No local model: the online fallback (`online_worker`; `auto` is the first subscription CLI worker whose account has room
  within pacing), scrubbed as above.
- The dashboard's AI assist card shows *Tutti's own AI: <model> on <machine> (best free)*, the week's local and online
  calls with their watt-hours (`brain-use.json`), and lines such as *parked coder:7b work on pc for Tutti's own job (12 s)*.

### The unblocker

Each round (every 10 minutes, on the active main machine only) `tutti/unblocker.py` looks for blocked or failing work and
clears what it can, at most 3 items a round and each at most once in 6 hours:

| Found | What it does |
| --- | --- |
| a task whose recent failures are all usage limits | forgives those attempts and clears the backoff (no model needed) |
| the same real failure again and again | the best free local model diagnoses the last failure; the task is queued again with the fix in its notes, so its next attempt edits a worktree and reruns the check. Local fails or the task is size L: an online agent, within pacing |
| a parked task whose question the history decides | answers it (e.g. retry after usage limits); a real choice stays for the person |
| a result that no longer rebases | queued again from scratch on the current main |
| a failed PR check in `unblock_repos` | its failed jobs rerun once per commit |

Idle machines with queued work: work held back by usage-limit failures is the first row; any other backoff (pacing, a quota window) is left alone on purpose. Only true decisions reach the needs queue. It never kills a process (the autopilot's watchdog ends a stuck run by its exact
pid). Every attempt (what, local, online or rule, the outcome) is in `unblock.json` and on the AI assist card.

What it does (`tutti/aiassist.py`, tested in `tests/test_aiassist.py`):

1. **Self-analysis** (local). Every `every_hours` a local model reads the Insights summary of the last 7 days (the
   categories and numbers above, nothing else) and writes ranked `PROPOSAL:` lines. Each becomes a self-improvement task
   in `self_improve.home` with its evidence and a regression-test-first note, inside the day's cap and the dedupe.
2. **Smarter decisions** (local, each with a cheap fallback when AI is off or every model is down):
   - a failed autopilot attempt is classified **flaky** (a dropped connection, a DNS failure), **environment** (a missing
     tool, a full disk, out of memory, a usage limit, a CLI permission refusal) or **real** (regular expressions at once,
     a local model's `CLASS:` line later): a flaky or environment failure retries once on the same ladder step, then
     escalates; a real one escalates at once. A usage limit is environment but still moves the ladder on (the account is
     spent). With AI assist off the ladder works as before;
   - the model's one-line cause of the failure goes into the retry's brief;
   - a queued task gets acceptance criteria (*Done when:* in its brief) and a size, S, M or L (the model tier that takes
     it); the fallback sizes it from its scope and title, and the criteria are asked again once a model is back.
   The model is asked outside the backlog's lock, at most 5 tasks a round; after a round where no model answered, the
   next waits 30 minutes.
3. **Auto-tuning.** Measured outcomes (and the model's `TUNE:` lines) propose new values for `autopilot.no_progress`,
   `autopilot.max_attempts`, `dashboard.stall_minutes`, `dashboard.probe_seconds` and `self_improve.every_hours`. A change
   inside the key's bounds and step is written to the config file by itself (with the usual backup), at most two a day and
   one per key a day, and logged with its old value: **Undo** on the card, or `tutti ai-assist undo <id>`, puts it back.
   A larger one waits on the card, where the admin applies it (logged and undoable the same way) or dismisses it; a
   proposal older than 7 days goes. A small one past the day's room waits for the next round. The card shows the measured
   gain since each change (wasted quota, stalls).
4. **Research and design** (online). The scout gets two generated watches, `tutti-ai-release-notes` (the agent CLIs'
   release notes, new models, prices, usage APIs) and `tutti-ai-design` (a design review of the scrubbed summary). Both
   are `spare_only`: sent only while the scout's account is behind its pace, quota the planner says would be lost, never
   the owner's reserve. Their `TUTTI-TASK:` lines become *candidates* in the home project (outside text: the admin accepts
   them with `tutti backlog accept <id>`); an answer that arrives after online AI was turned off is dropped.
5. **Privacy.** What goes online is scrubbed as above (`tutti/scrub.py`) and is the summary of categories and numbers, or
   a research question naming the agent CLIs. With `online_code: true` the design review also gets the titles of the
   failed and parked tasks (scrubbed); briefs, failure output and code always stay on local models. Local-only
   (`online: false`) makes no online watch at all. `false` written in the config always wins over the dashboard's switch.

`tutti ai-assist` shows the switches, the latest proposals, the tuned parameters and the gains; `tutti ai-assist tick`
runs one round now. Telemetry records `aiassist.analyse` (counts), `aiassist.tune`, `aiassist.undo` (the key) and
`aiassist.switch`.
