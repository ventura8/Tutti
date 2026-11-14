# Telemetry and self-improvement

Tutti records how it is used and how it fails, so it can be made better: on this computer always, and sent to a Sentry
project (or your own endpoint) only when you say yes. The same records feed the dashboard's **Insights** card and
`tutti telemetry report`, and, through **self-improvement**, become fixes.

## Two switches

| What | Default | Where it lives | Change it |
| --- | --- | --- | --- |
| Local recording | always on | `<state>/telemetry/events.jsonl` (rotated at 2 MB, 3 old files kept) | `TUTTI_TELEMETRY=off` in the environment records nothing at all (tests, a locked-down machine) |
| Sending | **off** until you say yes | `<state>/telemetry/consent.json` | asked once on the first interactive run; `tutti telemetry on\|off`; the **Send reports** button on the dashboard's Insights card |

The question is asked once, on a terminal only (never in CI, a pipe, or the commands other tools run: `run`, `test`,
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
  self-improvement (`selfimprove.analyse`, `merge`, `release`, `switch`), handled errors by exception type.
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
  event(s)`). A 401, 403 or 404 (a wrong or revoked DSN) keeps the queue and says so in `tutti telemetry status`. The queue (`<state>/telemetry/outbox.jsonl`, at most 2000 lines) is sent at most every 10 minutes, at the
  end of a command or by the dashboard; a crash is sent at once. Offline, throttled (429) or a server error: the queue is
  kept for the next try. A report Sentry refuses (another 4xx) is dropped.
- **Your own endpoint** (`telemetry.endpoint`, https; http only on 127.0.0.1): `POST {install_id, release, id, items}`
  in batches of 100, the same items.

## Set up sending (the owner, once)

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
   mergeable. Never with `--admin`, and only at the head commit the gates saw (`--match-head-commit`). Changes requested
   send the task back to the autopilot, and its next push is reviewed again.
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
