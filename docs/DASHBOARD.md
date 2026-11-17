# The dashboard

`tutti dashboard` serves a local web dashboard at `http://127.0.0.1:4777/` (`--port` or `dashboard.port` to change
it). It binds to 127.0.0.1 and refuses requests whose `Host` is not a loopback name with that port, so another
site cannot read it through DNS rebinding. Without sign-in it serves only this login: on Linux a loopback connection from
another user's process (a computer several people log in to) is dropped before it is read. It closes a connection that stays
silent for 30 s and serves at most 128 requests at once.

**Sign-in with Google** (`tutti auth setup`, [AUTH.md](AUTH.md)): with an OAuth client ID set, every page and `/api/*`
route needs a signed-in user. The first one to sign in is the admin; later ones wait for approval, or come in at once
through an invite link. Viewers see the dashboard read-only (no controls, no Settings); admins also get the **Users**
card (approve, Make admin, Make viewer, Remove, Invite someone with a link and a QR code). Without a client ID the
dashboard stays as it was, on this computer only, with a banner that offers the setup. `dashboard.bind` and
`dashboard.origins` (an `https://` LAN or VPN name) let others reach it, and then sign-in is required. It is never on
the internet: a public bind or a public origin is refused. Python standard library only: no build step, no CDN, no data leaves the
computer.

```bash
tutti dashboard                    # serve until Ctrl+C
tutti dashboard --port 4790        # another port
tutti dashboard --once             # print /api/state as JSON and stop (--no-probe: skip ssh and model servers)
```

Start it in the background with
`mkdir -p ~/.local/state/tutti && nohup tutti dashboard > ~/.local/state/tutti/dashboard.log 2>&1 &`.

Paused and disabled accounts remain visible in Usage with their status and reason. They stay excluded from routing and do not receive new work.

## Install it as an app

Open the page in Chrome or Edge and choose **Install app** (the button in the bar, or the browser's install icon). It
opens in its own window and its shell works offline; when the service is not running the page says so and reconnects
by itself once `tutti dashboard` runs again.

It updates itself. The server writes its version into the page (`<meta name="tutti-version">`) and into the service
worker's cache name, so a dashboard restarted on a new release serves a new `sw.js`. When `/api/state` names another
version than the open page, the page calls `registration.update()`, asks the waiting worker to `skipWaiting()` (it then
`clients.claim()`s) and reloads once, waiting for the next poll while a reading is being typed or saved or Chat has a
draft, unsent message, attachment selection or read, open dialog, edit, or active answer. A Settings restart checks the
same state before requesting a restart and again before reloading; it blocks new sends in this tab while waiting. These
checks protect this tab's browser state only: a server restart can still interrupt other tabs or background Chat jobs.
The page
loads its code as `app.js?v=<version>` and the worker matches those exactly, so an old worker never serves old code to a
new page. It remembers that reload per tab (`sessionStorage`): if the page is still old after
it, or the browser blocks storage, it never reloads again and says how to reload by hand (`web/update.js`).

## The Users tab

Next to Dashboard and Settings, for admins only: every user (the admin's own row marked "(you)"), their main machine,
presence, schedule and consumption, **Manage <name>** with every action an admin can take for them, and the household
audit, filterable by user; the sign-in's part of the tab (people, roles, invites, its audit) is docs/AUTH.md's, the
household's (`web/userstab.js`) is drawn into its `#users-manage` (docs/HOUSEHOLD.md, *The Users tab*). Settings shows no key
about people (`settings_schema.MANAGED`). **Me → My main machine** (in the bar) moves this user's own Tutti.

## Me and Household

The bar's **Me / Household** switch (remembered per browser): **Me** shows this user's own setup; **Household** adds the
**Household** card under *Needs you* (the members, each one's main agent, who is prompting now, online or last seen,
runs today; each shared machine with its use per person, this user's share and its fairness; whether the coordinator
answers) and, on each shared machine in **Machines**, who else it is busy for ("busy for Ana (3)"). Of other people only
names and counts are shown: never their jobs, prompts, accounts or readings. The card reads the last answer this computer
had from the coordinator (`<state>/household/grants.json`), so the page never waits on the network (docs/HOUSEHOLD.md).

## Chat

The dashboard opens on the **Chat** tab (`#chat`, the default; the cards below are the **Dashboard** tab, `#dashboard`).
Every signed-in user, viewers too, asks about the household and queues work there; it runs on the household host, so it
works while their own computer is off. Each action is a card with Undo and goes into the audit log; viewers' work has
admin-set limits, and pausing stays with admins. Like a chat IDE it has a model, effort and mode picker (Autonomous, Ask
before acting, Plan review), @-mentions, slash commands, branches and a context meter. See [CHAT.md](CHAT.md).

## Layout

The **Dashboard** tab's cards go by importance, top to bottom, on a 12-column grid whose rows fill their width, so no card stands tall and
narrow with nothing beside it; one column on a phone. Phone (375 px) to 4K (2560 px), light and dark:

1. **At a glance** (the tiles) and **Usage & pacing**, full width: the accounts as one row of compact cards (on a phone,
   a row to swipe sideways; each card snaps). An account under a longer limit shows both windows side by side (agy: the
   5 h window and the weekly limit, each with its meter, pace mark and reset) and the planner's rows (This window: 1 of 3
   weekly points, Spend by, Resets, Decision: 3 task(s) now, Why). Every card with a plan shows it as label/value rows
   (`quota_plan.rows`): Points left, Spend by, Resets, Decision, Why; times in the viewer's own time, UTC in the tooltip; the limit has no card of its own unless it needs a reading (its Add a reading
   lives there). Each card's facts fold under **Details**. Inside the card, in this order: the online agents' accounts,
   then **Local models**, then **You and the main agent**.
2. **Machines**, full width, directly below Usage & pacing (the owner, 2026-11-14): each machine's slots, load, heat and
   presence, its rows in columns.
3. **Needs you**, full width: tasks that need a person, accounts in panic, stalled jobs (**Open its log**), machines
   offline, workers down, readings due, results to review (each task with its attempts and failure history), agents
   started outside Tutti. **Open its log** clears the project filter and unfolds the running list to show the job. Most urgent first
   (`web/attention.js`, a pure function the tests run in node); "Nothing needs you" when all is clear.
4. **Running jobs** (with progress, and the project filter), then **Queued**, beside **Main agent** and **Workers**. The two
   lists fill the height of that column: as many rows as fit show, and "Show more" folds only the rows that do not (measured
   after each render and on every resize with a ResizeObserver; `tuttiFit` in `attention.js`). On a phone, where the columns
   stack, each shows 8 rows. **Autopilot** follows.
5. **Autopilot**, full width (its state, runs, queue and latest decisions, each list folded).
6. **Projects**, full width, in columns.
7. **Scouts**, full width: each scout, its meter, watches, recent questions and the review trial (*Scouts* below).
8. **Devices and controls**, full width: the Android devices, pause or resume grouped as Work machines, Termux machines, Local agents, Online agents (workers, accounts, scouts), then the opt-in switches (*Controls* below).
9. **Finished jobs** at the bottom: All, Failed, Done (keys 1 to 3); two columns from 1800 px.

Long lists fold after a few items with **Show N more** (and **Show fewer**); what is open stays open across polls, and the
button keeps the keyboard focus. Cards in a row stretch to the same height.

## What it shows, and where each part comes from

| Panel | Source |
| --- | --- |
| Projects | Each project Tutti sees: from each job's folder (an agent's working directory, a script's, a delegated run's worktree) to its git repository, named by its remote; and a read-only look at every machine (its processes working inside a git repository under the user's home, a `tutti test` copy under the machine's `root` counted for its local worktree's project, and an NVIDIA GPU's use). Per project: running jobs, running agents and delegated runs, the repositories with machine and branch, per machine the processes and their programs, and its last activity. `projects:` in the config names projects and lends them machines (their yield flag and GPU), test containers and a Windows folder: a WSL machine's Windows processes are invisible from WSL, so Tutti reads them over the machine's plain ssh with PowerShell (program names and start times of processes whose command line names that folder; never a command line, never a change). **Show its jobs** and the Jobs panel's project list filter the jobs. A project with `repo: owner/name` also shows its open pull requests (gh, the owner's sign-in, at most every 2 minutes): a release in progress (a title that names a version) first, each with its CI state (failing, running, passing, with the checks' names) and its reviews (the decision, else who reviewed and how). |
| Main agent | The main agent (`main.agent`, or the owner's switch; **Auto** keeps `main.preferred` while it has room), each candidate with why it is out (all its accounts in panic or spent), and the running or last `tutti conduct` run: the goal, its agents in turn and why each stopped. **Switch** posts to `/api/main` (`auto` or a worker; admin only; same as `tutti main` and the Chat header); a running conductor hands over at its next check. When the current main is in panic or spent, each probe round also hands the MAIN AGENT ROLE on (docs/CONDUCT.md). |
| Connect your agent | Each main agent on this computer (docs/CONNECT.md): connected, needs restart (a process of it started before Tutti was added), not connected, not installed, or can't connect locally with the reason. **Connect**, **Disconnect** and **Connect all** post to `/api/connect`; **Test** starts the server the agent's own config names and lists Tutti's tools through MCP. |
| Needs you | Gathered on the page from the rest of `/api/state` (`web/attention.js`): `autopilot.parked` and `.review`, accounts in panic, with a failed reader or a reading due, stalled jobs, `summary.machines_offline`, workers not ready, `summary.outside`. |
| Usage & pacing | `accounts:` in the config and the usage log (`tutti usage log`, the automatic readers, the Add a reading form). Per account: % used, the even-pace mark (what should be spent by now), the panic threshold (`panic_at`, default 95 %), the reset and target times and where the reset comes from (a reading, the config, an estimate), the readings of this window against the pace line, the plan and how often it is read automatically, and what to do; the planner's decision as rows (`quota_plan.rows`; `quota_plan.why` is the same in one line, docs/USAGE-PACING.md, *The planner*); an account with `limits` shows them inside its card (`limits`, each a full account state). In this order: the online agents' account cards, then **Local models**, then *You and the main agent* (the owner's mode and learned week). **Local models** is a category of its own (`local_models`, docs/ENERGY.md): one card per machine running local models (the machine of each Ollama worker, else this computer, and `energy.machines.<m>.ollama`) with its models installed and loaded, busy time, jobs and tokens today, this week and this month, live GPU power and use, the whole-system estimate, kWh and cost in your currency, cost per job and per busy hour, what the same tokens would cost on a cloud API, and its availability (paused, yielding, thermal, offline) with the reason. Every figure is an estimate; *Estimates: how they are made* on each card says which parts were measured. |
| Machines | The same probe as `tutti machines`: one card per physical computer, with dedicated WSL/desktop environments nested inside; free and busy slots, load against cores, routing rank, paused (yield flag up), offline. Under each reachable environment, **Stored files**: what `tutti files send` keeps in its cache, the size against `files.budget_gb` and when each item was last used. |
| Workers | The same check as `tutti workers`, plus the known CLI agents on this computer (Claude Code, Cursor, Antigravity, GitHub Copilot CLI, Gemini CLI, Ollama): installed or not, and whether a worker uses them. **Versions on each machine**: the last `tutti update` round (the dashboard runs one every `update.every_hours`, default 24, unless `update.auto: false`; **Update now** posts to `/api/update`): each program's version before and after, up to date, updated, skipped while running, failed with the reason, and the local models; above them, what only the owner can run (an apt package with a newer version, a switched-off apt source, a root-owned npm install) as one exact command per machine. |
| Running jobs, Finished jobs | Running and stalled jobs in their own card; finished ones (passed, failed, ended) at the bottom. A running job shows its progress (see *Progress* below: a bar with its percentage and where the number comes from, or its elapsed time when nothing can say) and, under it, what it has done so far: the step, tokens and tool calls, AI credits, the account it spends with that account's % used, tests passed and failed, files changed (+/−), bytes copied. A finished job shows no bar: its result (passed, failed with the exit code, or ended), how long it took and the same facts, with the report's path. `<project.state>`: `quiet/*.log` (`tutti quiet`), `ship-*.log` (quiet ship checks), `files/*.log` (`tutti files send` and `--input`, with the bytes copied in MB), `runs/*.log` (`tutti run --repo`), `waits/*.json` (`tutti wait`), `conduct/current.json` (a running `tutti conduct`), `llm-busy-*` (local-model jobs) and `tunnel-*.pid`. Running, **stalled** (its log has not changed for `dashboard.stall_minutes`), failed (with its exit code), done, or ended (a run from before 0.1.4, whose result was not recorded). Each opens to the end of its log. |

Machines and workers are probed in the background every `dashboard.probe_seconds` (ssh answers can be slow); the
rest is read on each request. The page polls `/api/state` every `dashboard.refresh_seconds` and pauses while hidden.

## Queued

Beside Running jobs, everything waiting (`tutti/waitlist.py`, `tutti/web/queue.js`), grouped by project in priority order, with
the counts in its header (`3 running · 19 queued`): the autopilot's queued backlog tasks, Keep busy's queued test and device
shards, scout questions queued and watches that are due, and Tutti's own move while it waits for its running jobs. Each item
shows its size, where it goes (an account, a device, a machine), when it should start and **why it waits**, in the words of
the last round (e.g. `agy-claude weekly binding until 12:55 (UTC+2)`, `running 2 of 2 possible: limited by memory`, a device
held by another tool, a machine asleep). An admin acts on a task: **Run now** (first in line, still within the limits),
**Move to…** (a ladder step it goes to first), **Cancel** (dropped, with a note), **Priority up** or **down**; and cancels a
queued scout question (`POST /api/queue`, admins only). A viewer sees the list read-only. Household jobs wait at the
household's coordinator and show on the shared machine's own dashboard once claimed.

## Autopilot

The **Autopilot** card turns it on or off (`POST /api/autopilot {"enabled": true}`, same-origin and token checks as a
reading) and shows its last decision, the runs going, the results to review, the tasks that need a person and the queue, each
with its attempt count and failure history. While it is on, the probe thread runs one autopilot round after every probe.
`/api/state` carries it as `autopilot`. See [AUTOPILOT.md](AUTOPILOT.md).

## Scouts

The Scouts card shows each scout (docs/SCOUTS.md): ready, or which environment variables are missing (names only), Grok
Bot's weekly meter from its account, the plan of the next round, the watches with their last and next time, and the recent
questions with their status and the start of each answer. With the review trial on, its tallies per reviewer (raised,
real, noise, to mark) and the verdict. When a scout is not set up, or none is configured, the owner's setup steps are
shown. After every probe the dashboard runs one scouts round (`tutti scout tick`), so start it from a shell that has the
scout's variables.

## Insights and self-improvement

The **Insights** card summarises the local telemetry events of the last 7 days (most used, slowest, most failing, wasted
quota, stall causes, crashes) and holds the **Send reports** switch (`POST /api/telemetry {"consent": true}`); nothing in
the card was sent anywhere. The **Self-improvement** card shows each Sentry issue or insight on its way: task, pull
request, merged, released, and the gate it waits on, with its switch (`POST /api/self-improve {"enabled": false}`). The
page posts its script errors and the panels and controls used (ids only) to `POST /api/telemetry`, which scrubs them
before recording. After each probe the dashboard runs one self-improvement round and sends the telemetry queue when due.
See [TELEMETRY.md](TELEMETRY.md).

## Controls

Paused accounts and workers are skipped in the active autopilot ladder and trigger account list. Their configured positions return on resume;
pausing them does not invalidate the configuration or interrupt dashboard and Chat reloads.

The *Devices and controls* card: one card per Android device (`tutti/devicecard.py`, `devices.items[].card` in
`/api/state`), and the owner's switches. A device's card holds its one status pill (paused, away, blocked, idle while work
waits, busy, ready, offline: the idle watchdog's verdict and the pause switch are folded in, so the card never says ready
while a summary says blocked), why, what waits on it, its one action (Resume, the watchdog's fix such as Check again now,
else Pause), where its shards went while it was away, its readings, and its blocked hours, Do Not Disturb, Cast and night
mode as separate short lines. Durations are plain (`1 day 14 h`, never `2298 min`). The temperature is a real sensor,
labelled: the thermal HAL's skin sensor, else the hottest SoC/CPU sensor (`dumpsys thermalservice`, else `/sys` thermal
zones), plus the battery only when the device has one (a TV's battery reading is never shown); "temperature not readable"
only when no sensor answers. Only machines' and workers' idle warnings stay in the list above the cards.

- **Pause or resume** any machine, Android device, worker (a local model or a CLI agent: agy, cursor, claude, copilot) or
  account. A paused item takes nothing new: routing, the autopilot, `tutti run auto`, conduct's fallbacks and
  `tutti test --device` skip it. What already runs finishes. The row says who paused it and when.
- **Pause, stop now** ends the running jobs bound to the item (their whole process tree, then waits for them to exit, so
  nothing writes to the tree during the hand-over), marks them ended, and hands each one over:
  - an agent run: a handoff note from its worktree (the changes, the end of its log) beside the run's log, the partial
    work snapshotted on `tutti/handoff/<track>` without moving HEAD (the files stay, so the next worker's diff still holds
    them), then the task goes to the next worker with room (`tutti run auto`'s choice), which continues in the same
    worktree from the handoff, with the same files and checks (routed checks stay routed). Pausing an account stops the
    runs that spend it. The worktree is rebuilt from the config and the track name, never a path read from a file;
  - a test or check run (`tutti test`): started again from the start, in the same tree, on the machine routing picks now,
    or on a ready device (`--device any` when none is ready);
  - a file transfer: stopped; its partial file is kept, so sending it again resumes.
  The job then shows "Stopped from the dashboard: handed over to ...". A remote command whose ssh link is cut may still
  finish on its own; its slot is released when it ends. Only processes that still run tutti are stopped: a pid the system
  gave to another program is never taken for the job.
- **A planned absence**: `tutti pause device pixel9pro --from "2026-11-16 07:00" --until unknown --note "with the
  owner"` (local time; `--until` a time, or `unknown`). From 30 min before the start nothing new is scheduled on it; at
  the start its running work is handed over (as *Pause, stop now*). It ends at `--until`, or, left open, when presence
  has seen it away and then back. The row shows **away later** or **away** with the window; Resume ends the planned absence at once, but
  not the device's blocked hours (below): inside a blocked window the device stays paused until the window ends.
- **Blocked hours** (recurring): `tutti devices hours <device> 22-07[,12-13] [--days mon-fri]` (`off` removes them), the
  device card's **Set blocked hours**, or Settings (`devices.<d>.hours`). Daily local hours, all days or the listed ones,
  when the device is not used at all; a window past midnight belongs to the day it starts on. They work as a planned
  absence does: no new work from 30 min before, running work handed over at the start, and the device's own settings
  restored when its run ends. A machine gets the same with `tutti machine limit <m> <HH-HH> 0` (0 %: not used at all).
  None are set by default. They live in the config, so `tutti move` carries them.
- A device whose machine is paused is held too. When every machine is paused, off or refused by its power policy, a run
  is not queued anywhere: `tutti test` and routed checks say so and stop (exit 3).
- **Opt-in** switches: every worker or account marked `costs_money: true` (Copilot, an API key) or `opt_in: true` (a
  model the owner wants off unless chosen, e.g. Fable), and each Termux machine's lint and unit runs. The autopilot has
  its own switch on its card.

- **Autocompact**: the percentage of a worker's context window at which it is compacted (`autocompact.threshold_pct`, 60
  by default; 10 to 95). **Save** writes it into the config file (the Settings tab's key), **Use the config's** takes the
  key out. The row lists each worker and how it compacts: by itself (Claude Code), by Tutti (a local model in
  `tutti conduct`) or restarted with a handoff (docs/WORKERS.md, *Automatic compaction*). `POST /api/autocompact`
  `{"threshold_pct": 45}` (or `null`), with the same checks as every other switch.

The switches are `POST /api/control` (same origin, the page's token, as for readings): `{"action": "pause" | "resume" |
"optin", "kind": "machine" | "device" | "worker" | "account", "name": ..., "on": bool, "stop": bool, "note": "..."}`.
**Pause and resume** are a state, not a setting: they are kept in `<project.state>/controls.json` and applied every time
the config is read. **Opt-in**, the autopilot's switches (on/off, `fill_room`), autocompact and the working days and
hours of the Usage card are settings: since 0.1.38 the dashboard writes them into the config file, the same keys the
Settings tab shows (each card links to its row), so a value lives in one place. A config the dashboard cannot write (JSON,
read-only) keeps them in the state folder as before. `tutti pause|resume|optin` and `tutti autopilot on|off` still use the
state folder; the Settings tab shows such a value as *in effect* over the file's, and saving the key there moves it into
the file.

## Where Tutti runs

The host that serves this dashboard (its machine name, host name and uptime), the address members use, where worker
jobs run, the clients connected through the bridge, the last move, and **Move Tutti to…**: pick a machine and what
happens to running jobs (wait, or hand them over), **Show the plan** (the dry run: blockers, steps, the OAuth origins,
what needs you), then **Move**. The move runs detached; this dashboard stops during it and the page reconnects by itself
to the new host's dashboard, through the client, on the same address. A stopped move offers **Roll back**. On a client
whose host is down, the card says the page is a read-only copy. See [MOVE.md](MOVE.md).

## Presence and moving work

Each device and machine card shows whether it is here: *Here*, *Away since 18:40 (left with its owner)*, *Asleep since
01:10*. It also shows a strip of today's 24 hours, how often the target was here at each hour. An away or asleep target
is never flagged idle.

Machines → *Waiting on away or asleep targets* lists the jobs that wait on such a target. Each job has these controls:

- **Move now**: to a target you pick, or to *Best available*;
- **First in queue**: a priority bump;
- **Move all waiting on …**: moves everything waiting on that target.

Only an admin can move work (`POST /api/work/move` passes the auth hook; a viewer gets 403). When Tutti runs on a laptop,
the *Where Tutti runs* card says what stops while the laptop is gone, and how to move Tutti to an always-on machine. See
[PRESENCE.md](PRESENCE.md).

## Settings

The **Settings** tab (beside *Dashboard* in the app bar; at the bottom of the screen on a phone; `#settings`, or
`#settings/<section or key>` to open at one) holds every key of `tutti.config.yaml`, grouped like the file: accounts and
readings, workers, main agent, machines, devices, autopilot, pacing, autocompact, review, scouts, telemetry and
self-improvement, projects and repos, opt-ins, dashboard, you and your household, and *Other*.

When another copy of the config differs from the one in use (a project `tutti.config.yaml` beside the XDG one, say), the
tab opens with a warning that names each copy and what differs, and **Merge into one** (`POST /api/settings
{"action": "merge-configs"}`): what only the copy has is added, the file in use wins where both differ, and the copy
becomes a symlink to the one file, kept first as `<copy>.bak-merge-<time>`.

- Each setting has a plain label, a one-line help, the key, its control (a switch, a number with its unit, a choice, day
  and hour pickers, a list editor, YAML for a structured value), its value and its default, a *changed* mark and
  **Reset to default** (the key is taken out of the file, so the default applies). Named entries (each machine, worker,
  account, device, project, scout, autopilot repo) are their own folding groups, with **Paused** (the pause switch above)
  on those that can pause.
- **Project priority** is an ordered list: the most important project first. It writes `projects.<name>.priority` (0, 1,
  2…); the autopilot's dispatch, its retries and `fill_room` share the work out by it (docs/AUTOPILOT.md, *Priority*).
- One schema drives it (`tutti/settings_schema.py`). A key in `config.DEFAULTS` without a line there still shows, with a
  label made from its name; a key in your file that Tutti does not know shows under *Other* (or in its section) as YAML.
  A test checks that every key of `tutti.config.example.yaml` has a schema line.
- **Search** finds a setting by its label, help, key or entry name; **Changed from default** shows only what differs.
- **Saving**: one key per save, `POST /api/settings` `{"path": [...], "value": ...}` (or `"yaml": text`, `"reset": true`,
  for project priority `{"path": ["projects"], "order": [...]}`), with the page's `etag` (a file changed since the page
  loaded it is refused: reload and save again). The server checks the value and then the whole config by its own rules
  before anything is written, copies the file to `<project.state>/config-backups/` (the newest 20 kept), and changes only
  that key's value in place: comments, blank lines and the order of keys stay (a change it cannot make safely, such as
  a key under a YAML alias, is refused and names the key to edit by hand). The dashboard reads the config again, so the
  change applies at once; `dashboard.port`, `project.state`, `household.port|bind` and `telemetry.endpoint|environment`
  say *takes effect on restart* with a **Restart** button (`POST /api/restart`: the dashboard starts again with the same
  command, by its own pid).
- **Secrets are never shown or edited here.** A DSN, token, password or key (in the file or in `~/.config/tutti/*.env`)
  shows only *set* or *not set*, with **How to set it** naming its command (`tutti telemetry setup`,
  `tutti scout setup grok`, `tutti household join`); their values never reach the page, and a save that names one is
  refused.
- The same origin, token and loopback checks as every other POST; `GET /api/settings` and the Settings POSTs also pass one
  function, `dashboard.authorize`, which a sign-in with roles can replace (Settings for admins only). The server stays
  bound to 127.0.0.1.
- Sections carry a `scope` (`file` today), so per-user and household sections can join later without a new page.

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

**Without a click.** The work browser reads the page (docs/BROWSER.md): **Read on the work browser** on the card or
`tutti browser read`, after one sign-in there (**Sign in to <site> on the work browser**). The **Work browser** card shows
the machine, the mode, the open tabs per user or worker, the queue and each site's sign-in. An orchestrator with browser
tools can still do it by hand: `tutti usage page-script [cursor]` prints the same parser as one JavaScript
expression; it runs it in a signed-in tab of its own browser (read-only) and passes the
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

## You and the main agent

Under the account cards: the owner's mode (set, and in effect with its end), **I'm away until…** with a date, **Away
today**, **I'm back (auto)**, the working-day switches, and a 7 x 24 heatmap of the learned week (points per hour by weekday
and local hour; the hours the planner keeps for the owner are outlined). Each account card adds **On track to finish at X %
by its reset**. See docs/USAGE-PACING.md, *The owner's own prompting*.

## Reminders and nudges

A card whose account is read by hand shows **Reminder: ... tutti usage log <account> <percent>** once its reading is
older than `stale_hours` (default 2), and the Usage header counts such accounts. A card read automatically says how and
when ("read automatically · 3 minutes ago", "written by the orchestrator", "read by the helper"). An account behind its
line has room, and its card says what happens to it. With the autopilot and `fill_room` on, a trigger account
(`autopilot.accounts`) shows **Autopilot: filling N slots — R running here, Q queued** and what the last fill did; otherwise
"N bounded tasks fit here now" and **Let autopilot fill it**, which turns both on (docs/AUTOPILOT.md). A scout's account
(Grok Bot: it answers questions and takes no bounded tasks) shows its scout facts instead: the allowance used and left,
the next watch and the last answer. Machines show
their temperature, highlighted at or over `heat.cap_c`.

## Automatic readings

**Refresh readings** (beside the Usage title) posts to `POST /api/refresh` (the same same-origin and token checks) and
runs every reader at once, without waiting for the next probe; `tutti usage refresh` does the same from a terminal. The
page then updates in place. The background probe also runs each account's `reader` and `plan` command (at most every `every_minutes`) and scans
recent `tutti quiet` logs of agent jobs for limit messages (docs/USAGE-PACING.md, *Automatic readings*). A reader whose answer did not change is asked half as often the next
time, down to once every eight intervals; a change or **Refresh readings** brings it back to its own interval. The
account's `auto_every_minutes` shows the interval in use now.

## Quota panic

An account is in panic when its latest reading is at or over `panic_at` while the target time (the reset minus
`lead_hours`) is still ahead. Then nothing new goes to it; bounded work moves to accounts with room and local models.
`tutti usage` shows the same.

## Numbers

Every percentage and number on every card and tab goes through one formatter, `web/format.js` (loaded first):

- `tuttiPct` for percentages in points: whole points from 10 up, one decimal under 10 (0.3 %), in the browser's own locale
  (`32%` in English, `32 %` in German or Romanian). The grok-bot card's `32.122999999999976 %` reads `32%`.
- `tuttiNum` for counts and measures (grouped by the locale, rounded by size), `tuttiShare` for fractions, `tuttiCompact`
  for token counts (`1.2M`), `tuttiCssPct` for a meter's width (clamped to 0–100).
- A missing value shows `–`, never `NaN` or `undefined`.
- The planner's own sentences (an account's outlook, its "Do:" line and plan rows, such as "heading for
  32.122999999999976 %") lose their float noise through `tuttiTidy`; logs, titles and chat text are shown as they are.

## Worker status: one for the CLI and the dashboard

`tutti workers`, the Workers card, the Local models cards and *Needs you* all read one status, `workers.statuses()`: each
worker asked through its own host (a local model through its tunnel, as `tutti workers` does). A local-models card is
available while one of its machine's workers answers, even when the machine's ssh probe fails (the card then says so); it
is offline only when none answers, with the worker's own reason.

- The machine probe keeps numbers only in its GPU field: an `nvidia-smi` installed without a working driver prints its
  failure on stdout, and that text once made the NUC read as offline while its model answered (2026-11-14).
- The dashboard reads its config again when the file changes after it loaded it, so a worker added after it started is
  probed at the next round, as `tutti workers` sees it. An edit that does not load keeps the last good config, and the page
  says why at every probe until the file loads again.

## Look and accessibility

Material 3 tokens over the OS light or dark scheme. The accent is the OS accent colour where the browser shares it
(CSS `AccentColor`); otherwise Tutti's indigo, and the footer says which. The accent only paints indicators and tonal
backgrounds; text stays on the neutral colours, so any accent keeps its contrast. Keyboard: Tab through everything,
`R` refreshes, `1`–`5` pick the job filter, **Esc** closes the open disclosure that holds the focus and gives the focus
back to its summary. Layout from a phone to 4K.

- Every control is a target of at least 24 × 24 px; a field always shows its focus (a date's picker part too).
- A form field the browser refuses gets `aria-invalid` and a `role="alert"` message tied to it by `aria-describedby`
  (`web/a11y.js`), cleared as the value is fixed.
- Long text wraps, never an ellipsis: a job's whole title, a long chip, an e-mail in a row.
- `[hidden]` always hides, whatever display a class sets.

The browser tests check all of this on every tab, both roles, four widths, both themes, 200 % zoom and WCAG text spacing
(docs/TESTING.md).

## `/api/state`

`version`, `generated_at`, `config`, `refresh_seconds`, `usage` (`log`, `problem`, `accounts[]` with `plan`,
`auto_every_minutes`, `reader_problem`, `usage_url` and `reset_from`), `machines`
(`probed_at`, `items[]`), `workers` (`probed_at`, `items[]`, `agents[]`), `jobs` (`folder`, `stall_seconds`, `items[]`,
`total`), `summary` and `probe_error`. `GET /api/health` answers `{"ok": true}`.

The answer carries an `ETag` and `Cache-Control: no-cache`: the page asks again with `If-None-Match`, an unchanged
answer is a `304` with no body, and the page neither parses nor renders it again. A browser that sends
`Accept-Encoding: gzip` gets it gzipped. One assembled answer serves every tab that asks within a second. A running
job's `facts.context_pct` is its context use in % of its window, where it is known: an agent's transcript gives the
newest turn's prompt (never the run's total) against its model's window (Claude models: 1M, the older ones 200k), or the
worker's `context_tokens`; what a CLI printed about its context. An unknown window, or a count above it, shows nothing.

No tab shows a missing value as text ("null", "undefined", "NaN", "[object Object]"): `tests/test_rendered_text.py`
renders each one in headless Chrome. The Private network card reads `GET /api/vpn/status` (any signed-in viewer, 200
with no VPN set up); its actions stay admin POSTs to `/api/vpn`.

The Machines card warns when this computer's temp folder or work disk is low (under 10 % free file slots, or under 10 %
free space with less than 20 GB left; `tutti/diskroom.py`), says the fix, and no new work starts until there is room;
`tutti doctor` reports the same.

Before the first probe answers, the machines, devices and workers of the config are listed as **checking**, never as
"none in the config". Every fold the owner opens or closes (a job, a task's failure history, Details, Show more), the
finished-jobs filter and the project filter are kept by item id across refreshes and reloads of the tab
(sessionStorage); the render's own opening of a fold is not taken for the owner's choice. A class that sets a
`display` gets a `[hidden]` rule beside it (a test lists them), so the Users view shows only in its tab, for an admin
with sign-in on.
