# The dashboard

`tutti dashboard` serves a local web dashboard at `http://127.0.0.1:4777/` (`--port` or `dashboard.port` to change
it). It binds to 127.0.0.1 only and refuses requests whose `Host` is not a loopback name with that port, so another
site cannot read it through DNS rebinding. Python standard library only: no build step, no CDN, no data leaves the
computer.

```bash
tutti dashboard                    # serve until Ctrl+C
tutti dashboard --port 4790        # another port
tutti dashboard --once             # print /api/state as JSON and stop (--no-probe: skip ssh and model servers)
```

Start it in the background with
`mkdir -p ~/.local/state/tutti && nohup tutti dashboard > ~/.local/state/tutti/dashboard.log 2>&1 &`.

## Install it as an app

Open the page in Chrome or Edge and choose **Install app** (the button in the bar, or the browser's install icon). It
opens in its own window and its shell works offline; when the service is not running the page says so and reconnects
by itself once `tutti dashboard` runs again.

## What it shows, and where each part comes from

| Panel | Source |
| --- | --- |
| Projects | Each project Tutti sees: from each job's folder (an agent's working directory, a script's, a delegated run's worktree) to its git repository, named by its remote; and a read-only look at every machine (its processes working inside a git repository under the user's home, a `tutti test` copy under the machine's `root` counted for its local worktree's project, and an NVIDIA GPU's use). Per project: running jobs, running agents and delegated runs, the repositories with machine and branch, per machine the processes and their programs, and its last activity. `projects:` in the config names projects and lends them machines (their yield flag and GPU), test containers and a Windows folder: a WSL machine's Windows processes are invisible from WSL, so Tutti reads them over the machine's plain ssh with PowerShell (program names and start times of processes whose command line names that folder; never a command line, never a change). **Show its jobs** and the Jobs panel's project list filter the jobs. A project with `repo: owner/name` also shows its open pull requests (gh, the owner's sign-in, at most every 2 minutes): a release in progress (a title that names a version) first, each with its CI state (failing, running, passing, with the checks' names) and its reviews (the decision, else who reviewed and how). |
| Main agent | The main agent (`main.agent`, or the owner's switch), each candidate with why it is out (all its accounts in panic or spent), and the running or last `tutti conduct` run: the goal, its agents in turn and why each stopped. **Switch** posts to `/api/main`; a running conductor hands over at its next check. |
| Usage & pacing | `accounts:` in the config and the usage log (`tutti usage log`, the automatic readers, the Add a reading form). Per account: % used, the even-pace mark (what should be spent by now), the panic threshold (`panic_at`, default 95 %), the reset and target times and where the reset comes from (a reading, the config, an estimate), the readings of this window against the pace line, the plan and how often it is read automatically, and what to do. |
| Machines | The same probe as `tutti machines`: free and busy slots, load against cores, routing rank, paused (yield flag up), offline. Under each reachable machine, **Stored files**: what `tutti files send` keeps in its cache, the size against `files.budget_gb` and when each item was last used. |
| Workers | The same check as `tutti workers`, plus the known CLI agents on this computer (Claude Code, Cursor, Antigravity, GitHub Copilot CLI, Gemini CLI, Ollama): installed or not, and whether a worker uses them. |
| Jobs | A running job shows its progress (see *Progress* below: a bar with its percentage and where the number comes from, or its elapsed time when nothing can say) and, under it, what it has done so far: the step, tokens and tool calls, AI credits, the account it spends with that account's % used, tests passed and failed, files changed (+/−), bytes copied. A finished job shows no bar: its result (passed, failed with the exit code, or ended), how long it took and the same facts, with the report's path. `<project.state>`: `quiet/*.log` (`tutti quiet`), `ship-*.log` (quiet ship checks), `files/*.log` (`tutti files send` and `--input`, with the bytes copied in MB), `runs/*.log` (`tutti run --repo`), `waits/*.json` (`tutti wait`), `conduct/current.json` (a running `tutti conduct`), `llm-busy-*` (local-model jobs) and `tunnel-*.pid`. Running, **stalled** (its log has not changed for `dashboard.stall_minutes`), failed (with its exit code), done, or ended (a run from before 0.1.4, whose result was not recorded). Each opens to the end of its log. |

Machines and workers are probed in the background every `dashboard.probe_seconds` (ssh answers can be slow); the
rest is read on each request. The page polls `/api/state` every `dashboard.refresh_seconds` and pauses while hidden.

## Autopilot

The **Autopilot** card turns it on or off (`POST /api/autopilot {"enabled": true}`, same-origin and token checks as a
reading) and shows its last decision, the runs going, the results to review, the tasks that need a person and the queue, each
with its attempt count and failure history. While it is on, the probe thread runs one autopilot round after every probe.
`/api/state` carries it as `autopilot`. See [AUTOPILOT.md](AUTOPILOT.md).

## Add a reading

An account card has **Add a reading** only where a reading is needed: nothing reads the account on its own (no
`reader` command and no `reading_file`; Cursor), or its newest reading is older than `stale_hours` (default 2) or
missing. An account read automatically and recently (Antigravity) has none. An account's `note` is a warning callout
(an icon and the word "Note", not colour alone) right above the button. One tap opens the provider's usage page (`usage_url`, in a new tab) and a
short form on the card: a slider for the percent used and the reset time, prefilled from the window rule, plus an
optional note. **Save reading** posts it to `POST /api/reading`, which appends the same line as `tutti usage log`
(with `source: dashboard` and the reset). While the form is open the usage cards stop refreshing, so a poll never wipes
it.

`POST /api/reading` accepts only the dashboard page itself: a loopback `Host` with the dashboard's port, an `Origin`
equal to the dashboard's, `Sec-Fetch-Site: same-origin` when the browser sends it, a JSON body of at most 4 KB, and
the token the server writes into the page (`<meta name="tutti-token">`, new at every start). A page from another site
cannot read the token, and its requests carry its own `Origin`. After a restart the old page's token is refused with
"reload the page"; the service worker fetches the page network-first, so a reload gets the new token.

Body: `{"account": "cursor", "used": 37, "resets": "2026-12-03T00:00Z", "note": "..."}`; `resets` and `note` are
optional; `used` is 0 to 100; `resets` lies from a day ago to two months ahead.

## What is off

Under the workers, one line per worker or account that is off: `copilot (worker): costs money per use, so it is opt-in;
workers.copilot.enabled: true in the config turns it on`. See docs/WORKERS.md, *Opt-in for what costs money*.

## The reading helper

For an account no CLI reads (Cursor), `/helper` (the footer's **Reading helper** link) offers one button per account
with a `usage_url` and no `reader`. Drag it to the browser's bookmarks bar once. Then, on the provider's usage page,
signed in as usual (`https://cursor.com/dashboard/spending`), click the bookmark. It reads the page's **visible text**
only, line by line: each meter starts at the line that begins with its `helper_label` ("Cursor Models", "Other Models")
and ends at its amount ("N% used", else "$spent of $limit"); its reset is a "Resets <date>" line right after the amount,
else the page's "Usage limits reset on <date>", else the end of a "billing period <a> - <b>" (another meter's "Resets",
such as Grok's weekly one, is never taken). The parser is `tutti/web/helper-parse.js`. It
opens the dashboard at `/#reading=<json>`; the page posts that reading through `POST /api/reading` with its own token,
and the reading is saved with `source: helper`.

What it never does: read cookies, storage or tokens, call the provider's API, or send the reading anywhere but this
computer. The fragment carries this install's helper key (`<state>/helper.key`, 0600): the server refuses a reading whose
key is wrong (403), so a site that opens the dashboard with a made-up reading only fills the form for the owner to check.
**The patterns are not a stable API.** When the page's text changes and nothing matches, the bookmarklet says so and the
dashboard opens the form instead. The tests run the parser on the text of the real Cursor spending page (2026-11-14:
"Cursor Models 25% used", "Other Models 14% used", "Usage limits reset on Dec 10"), and it was checked on the live page.

**Without a click.** The dashboard cannot read cursor.com (the sign-in lives in the owner's browser). An orchestrator
with browser tools does it instead: `tutti usage page-script [cursor]` prints the same parser as one JavaScript
expression; it runs it in the owner's open, signed-in tab (claude-in-chrome's `javascript_tool`, read-only) and passes the
JSON string it evaluates to to `tutti usage page-reading '<json>'`, which checks each reading as the dashboard does and
logs it with `source: page script`. A label not found exits 1 and says to read that one by eye.

## Progress

Every running job gets a number, never a fake precise one; each says where it comes from (hover the bar, or open the job):

| Layer | Source | Shown |
| --- | --- | --- |
| 1 exact | `tutti progress n/m`; a counter in the log: `[150/177]`, pytest's `[ 13%]`, `stage 2 of 4`, `12/40 tests`, a pipeline's `static 3/51 eslint`, docker's `Step 3/12`, rsync's `45%`, a file transfer's MB; `slot.sh: waiting for a free slot (all 3 busy)` (0 %, "waiting for a slot") until `slot.sh: got a slot` | bar, %, ETA at this pace |
| 2 known phases | `dashboard.shapes` (configured ones first, then the built-in; the whole log is read, a little more at each look, so no phase or count is lost however long it grows): `phases` (the furthest phase whose `when` regex is in the log: phase k of n), or `count` lines against `total` / `total_command` (a shell command run in the job's folder, `{1}` = the title's first `match` group, used only when it exits 0, cached for an hour); built in: `tutti run --repo` (worktree, the worker works, checks); a shape with neither only names a kind (and `machine`) for layer 3 | bar, %, step |
| 3 estimated | elapsed time against the median duration (a finished job's end is the time of its result, never its last output; each job is learned from once) of at least 2 earlier finished jobs of the same kind (the shape and its group, else the kind and program) on the same machine, from `<state>/durations.json` (a job that was running and is gone counts as finished; a container only when its machine was probed); capped at 95 %; past the median "taking longer than usual" | bar, ~%, time left |
| 4 agents | a conductor's handoff notes (done of done + next); else layer 3; else `dashboard.typical_minutes` (agent 20, agent task 15, conduct 30) as a rough estimate said as such | bar, ~% |
| elapsed | everything else (a tunnel, a local-model server, a script of a kind not yet finished once) | the elapsed time, no bar |

Which job kinds get which layer: quiet runs, ship checks, registered jobs and watched scripts get layer 1 from their log,
else 2 when a shape matches their title, else 3 once two runs of that kind finished, else elapsed time. File transfers
get layer 1 (bytes). Delegated runs get layer 2 (built-in phases), then 3. Waits get layer 1 from the log they watch,
else 3. Remote test containers get layer 3 (their start from `docker ps`'s age). An agent session's background tasks:
a command's output gets layer 1 from its counters; an agent's transcript gets layer 3, else 4 (a counter in a
transcript is text the agent read, never progress). Agent CLIs found by process and a running `tutti conduct`: layer 4.
Tunnels and local-model servers: elapsed ("a service: it has no end").

Facts, running or finished: an agent's transcript (a Claude Code subagent's task output) gives tokens in (input and
cache) and out, counted once per message, and its tool calls; an agent CLI's text output gives tokens when it prints
them ("12,345 input tokens", "Tokens: 12.3k in, 4.1k out", `"output_tokens": 45`) and Copilot's AI credits; test runners'
summaries give passed and failed (pytest, vitest, Playwright, unittest); git's "N files changed" gives files and +/−; a
delegated run's report gives its account (`runs.jsonl`), checks, files and report path, and while it runs `git diff
--shortstat` of its worktree plus the files the worker added (at most every 30 s, only in a Tutti run worktree). Only counts are read; no text of a log or transcript is shown that is
not already in the log view.

## Jobs found by themselves

Besides `tutti quiet` runs, ship checks, waits and delegated runs, the Jobs card shows:

- **registered jobs**: a script calls `tutti job register --title "<what>"` (the log defaults to where its output goes)
  and `tutti job done --rc $?` at its end;
- **watched scripts** (`dashboard.watch_scripts: [nuc-test.sh, ship.sh]`): every running process of these scripts, matched
  by its program name (never by searching command lines), with the log its standard output goes to, the tail and the
  progress line (`[150/177]`). A script that runs itself again is one job;
- **agents** (`dashboard.watch_agents: [cursor-agent, agy, copilot]`): the program and its folder, never its arguments.
  One working outside a Tutti run worktree or review folder is marked "started outside Tutti: use tutti run auto", and
  the Workers tile counts them beside the runs per worker today (`<state>/runs.jsonl`);
- **background tasks** (`dashboard.task_globs`): an agent session's task output files, active when written in the last
  two minutes; only the time and a progress line are read, never shown;
- **remote containers**: the test containers holding each machine's slots (`machines.<m>.containers: [prefix-]`, read
  by the probe with `docker ps`).

## Reminders and nudges

A card whose account is read by hand shows **Reminder: ... tutti usage log <account> <percent>** once its reading is
older than `stale_hours` (default 2), and the Usage header counts such accounts. A card read automatically says how and
when ("read automatically · 3 minutes ago", "written by the orchestrator", "read by the helper"). An account behind its
line shows **Use it: N bounded task(s) can go here now**: 0 % used and "behind" means quota going unused. Machines show
their temperature, highlighted at or over `heat.cap_c`.

## Automatic readings

**Refresh readings** (beside the Usage title) posts to `POST /api/refresh` (the same same-origin and token checks) and
runs every reader at once, without waiting for the next probe; `tutti usage refresh` does the same from a terminal. The
page then updates in place. The background probe also runs each account's `reader` and `plan` command (at most every `every_minutes`) and scans
recent `tutti quiet` logs of agent jobs for limit messages (docs/USAGE-PACING.md, *Automatic readings*).

## Quota panic

An account is in panic when its latest reading is at or over `panic_at` while the target time (the reset minus
`lead_hours`) is still ahead. Then nothing new goes to it; bounded work moves to accounts with room and local models.
`tutti usage` shows the same.

## Look and accessibility

Material 3 tokens over the OS light or dark scheme. The accent is the OS accent colour where the browser shares it
(CSS `AccentColor`); otherwise Tutti's indigo, and the footer says which. The accent only paints indicators and tonal
backgrounds; text stays on the neutral colours, so any accent keeps its contrast. Keyboard: Tab through everything,
`R` refreshes, `1`–`5` pick the job filter. Layout from a phone to 4K.

## `/api/state`

`version`, `generated_at`, `config`, `refresh_seconds`, `usage` (`log`, `problem`, `accounts[]` with `plan`,
`auto_every_minutes`, `reader_problem`, `usage_url` and `reset_from`), `machines`
(`probed_at`, `items[]`), `workers` (`probed_at`, `items[]`, `agents[]`), `jobs` (`folder`, `stall_seconds`, `items[]`,
`total`), `summary` and `probe_error`. `GET /api/health` answers `{"ok": true}`.
