# Usage pacing

**The rule: spend every quota fully about 1 hour before its reset.** Not earlier: work stalls while you wait. Not
later: the unused part is lost at the reset. The hour is a margin for readings that lag and tasks that overrun;
override it per account with `lead_hours`.

## Accounts

Each account under `accounts:` has:

- `window`: how often it resets: `5h`, `7d`, `2w` or `monthly`;
- `resets`: any one known reset time: UTC (`2026-01-03T02:00Z`), with an offset, or with a zone (`2026-01-03 04:00 Europe/Bucharest`); Tutti rolls it forward by itself;
- `lead_hours`: the margin before the reset (1 by default); the window's **spend-by time** is the reset minus it;
- `target_pct`: the % of the quota to have used by the spend-by time (100 by default). A lower target (80) leaves headroom on
  purpose. The main agent's accounts take `main.target_pct` (100 by default) when they have none of their own; the owner's
  reserve still keeps the owner's working hours, the target only says where the spend ends. Both are in Settings (admins
  only);
- `share`: this session's part when a peer session uses the same account (0.5 = half);
- `source`: where a reading comes from (a usage command, a dashboard page).

Most agent CLIs have no usage command. Read the dashboard, in the owner's signed-in browser, and record it:

```bash
tutti usage log claude 62          # 62 % of the window's quota is spent now
tutti usage log cursor 40 --resets 2026-12-03T00:00Z   # with the reset the usage page shows: it anchors the window
tutti usage                        # every account against its pace
tutti usage read                   # run the automatic readers now (the dashboard runs them by itself)
```

## The target, and straight on after the reset

The even line runs from the window's start to `target_pct` at its spend-by time, and the planner's budget is the target (not
100) minus the reserve. Inside the on-pace band but under the line, an account still gets the tasks that top it up to the
line, so a window ends at its target instead of a band's width short. The autopilot's adaptive limit (docs/AUTOPILOT.md)
starts as many runs as the room and the machines allow.

When a reset has passed and the new window has no reading yet, the account starts from 0 % at once (`after_reset`): queued
work goes on with no gap and no "nothing more this window" from the old window survives; the next real reading replaces it.

Each account card says `Target 100 % · on track to finish at 97 % by 23:00 (UTC+2, in 5.0 h)` (the last day's rate carried to the spend-by
time). Heading more than 5 points short, it says why (no work queued, the machines or devices busy, the account blocked) and
what Tutti already does about it (fill_room adds tasks, up to N runs at once, a stronger model while behind).

## The owner's real day

The owner's reserve follows how the owner really works today (`tutti/ownerday.py`), read from the live readings of the main
agent's accounts (keep their reader at `every_minutes: 30` or less while the owner works):

- **Burning faster than planned.** The owner's rate over the last 2 hours, above the workers' baseline, against the plan's: the
  reserve until the target is scaled by that factor at once, so the workers' share on that account drops and their load goes
  to the other accounts and local models. If the account would reach 100 % before its reset at the owner's pace alone, the
  card warns early (`At today's pace you run out on Thu 19 Nov 10:00 (UTC+2) ...`) and names what Tutti already did. **Hard safety:**
  the main agent never runs out because of Tutti's workers; that outranks the target.
- **Using less than planned.** The reserve counts only the hours still ahead, so every owner hour that passes unused goes to
  the workers from that hour on, and the target is still reached by its spend-by time.
- **Earlier or later than the schedule.** The real start is the first hour today whose rise stands above the baseline by half
  the planned rate: an earlier start moves today's reserve to begin there. The real stop is no such rise for
  `pacing.stop_after_minutes` (60) inside the planned hours: the rest of today is released. Still working after the planned
  stop keeps the reserve an hour at a time. The learned 7 x 24 profile learns from the same readings, so repeated early starts
  move the schedule by themselves. The owner panel says `Today: started 07:00 (planned 09:00); ...`.

## Automatic readings

Tutti reads CLI usage, scoped usage pages and limit messages. All readings go to the same usage log as
`tutti usage log`:

| Source | Config | What it gives |
| --- | --- | --- |
| A reader command | `reader: {command: [...], bucket: <id>, every_minutes: 10}` | Antigravity: `agy -p /usage --output-format json` lists every quota bucket (`3p-5h` and `gemini-5h`: the two rolling 5-hour windows; `3p-weekly`, `gemini-weekly`) with the share left and the reset time, and spends no quota. One run serves every account that names the same command. |
| A plan command | `plan: {command: [...], field: <key>}` | A label for the card, such as Cursor's tier from `cursor-agent about --format json` (`subscriptionTier`). Only that field is read. |
| Codex CLI app-server | `reader: {format: codex-app-server, every_minutes: 15}` | Codex CLI's `account/rateLimits/read` returns its 5-hour and weekly windows without reading auth files or spending quota. Accounts with `worker: codex` get this reader automatically when `reader` is omitted, including existing configs. Tutti chooses the valid-duration window closest to `accounts.<name>.window` and reports invalid reset times. A reader whose stdout remains open after its process exits cannot hold the dashboard: further Codex reads wait until that collector reaches EOF; this bounds output-pipe cleanup only, not all possible stdin or OS stalls, and does not prove descendants have stopped; `bucket: primary\|secondary` can select one explicitly. |

The Codex reader keeps its local app-server session open until initialization and the rate-limit request finish; if the
server cannot answer within 20 seconds, Tutti reports the reader error and retries at its normal interval.
When the Codex worker is on a remote machine, set `accounts.<name>.read_on` to that machine so the bridge reads the
correct signed-in account; Tutti refuses to substitute the coordinator's account.
| Scoped usage page | `usage_url` and, when needed, `helper_label` | Accounts with a usage page and no reader are read automatically every 15 minutes through the Work browser's read-only usage scope, except Codex accounts, which default to the app-server reader. Set `reader.format: browser-usage` to use the browser explicitly. Grok Bot's `helper_label: Grok Bot` selects its weekly meter from Cursor's spending page. Sign in once on the Work browser; no cookies or tokens are read or exported. |
| Cursor's account call | `reader: {format: cursor-api, field: autoPercentUsed}` (or `apiPercentUsed`) | Both meters of cursor.com/dashboard/spending ("Cursor Models", "Other Models") and the billing cycle's end, from the call the spending page makes (`DashboardService/GetCurrentPeriodUsage` on api2.cursor.sh), signed in with cursor-agent's own login. No quota spent, no owner click; one call serves both accounts. The token is sent only there and never logged or printed; a refused call says to run `cursor-agent login`. |
| Limit messages | `match: {command: <regex>, model: <regex>, fallback: true}` | A "usage limit reached, try again in 3 h" line in the output of `tutti run` or of a `tutti quiet` job whose command matches logs the account as spent (100 %) until the reset it names. With several accounts on one CLI (`agy`), `model` picks the one the command or the message names; `fallback` takes the rest. |

A reader this version cannot read (an unknown `format`, a missing `command` or `bucket`) and an account key it does not
know are warnings, not errors: the reader is skipped, the account still takes readings by hand, and the config loads.

A reading that names its reset (a reader, a limit message, the dashboard form, `--resets`) anchors the account's
window from then on: it beats the config's `resets`, which is only a first guess. Mark such a guess with
`resets_estimated: true`; the dashboard then says "an estimate" until a reading sets it. Automatic readings are kept
only when they say something new (a point or more, a moved reset, or an hour since the last one).

`usage_url` is the provider's usage page: accounts without their own reader are read automatically in the Work browser,
except Codex accounts, which default to the Codex app-server. Set `reader.format: browser-usage` to select the page instead.
The dashboard's **Add a reading** still opens the page and a short form for a manual correction. `worker` names the worker
whose card shows this account.

Two more sources for what no CLI prints:

- **A reading file** (`reading_file: <path>`): an orchestrator that knows an account's % from its own tool (Claude Code's
  usage, which no CLI prints) writes `42`, or `{"used": 42, "resets": "2026-11-21T01:59Z"}`, to that file. `tutti usage`
  and every dashboard probe log each new version of the file once (`source: file`). `tutti usage log claude 42` does the
  same from a shell.
- **The reading helper** (docs/DASHBOARD.md): a bookmarklet that reads the spend from the provider's page the owner has
  open, signed in, and saves it in one click. It reads only the page's visible text, never cookies or tokens; automatic
  page readings use that same extractor in the scoped Work browser.

**Opt-in accounts.** `enabled: false` keeps an account (a premium model you use only on request) out of pacing, panic,
routing and the dashboard; `tutti usage log` still keeps its readings. `note:` is shown on the card (for example "includes
your own IDE use, which Tutti does not start"). One `reading_file` can serve several accounts:
`{"claude": {"used": 7, "resets": "..."}, "claude-fable": {"used": 0}}`.

**Copilot.** `reader: {command: [gh, api, /users/<login>/settings/billing/premium_request/usage], format: github-premium,
allowance: 300}` reads the month's premium requests (the gh token needs the `user` scope: `gh auth refresh -h github.com
-s user`, a step for the owner). `credits: true` adds the "AI Credits N" that `copilot -p` prints at the end of each run,
summed over the window from Tutti's own run logs (runs started elsewhere are not in it).

On a dashboard usage graph, hover, click or touch near a recorded point to show its local time and used percentage below
the graph. Focus the graph and use the arrow keys or Home/End to inspect the same readings; the UTC time is available on
the reading's tooltip. The graph selects an actual observation, not an interpolated quota value.

**Stale readings.** Missing, future-dated or old automatic measurements are marked stale instead of looking current.
The dashboard and `tutti usage` identify the account and suggest checking the reader or producer and retrying
`tutti usage refresh`; they do not ask for manual entry for an automatic account. This includes configured readers,
implicit Codex/browser readers and reading files. The default freshness limit is two hours; `stale_hours` changes it.
For automatic accounts `stale_hours: 0` keeps the two-hour failure indicator, so a broken reader is never silently
shown as current. For manual accounts, `stale_hours: 0` still turns off the reminder; otherwise the dashboard offers
manual entry and `tutti usage` suggests `tutti usage log`. An estimate or synthetic post-reset value does not refresh
the last actual measurement. Normal pacing continues to use the account state; cautious panic rescue refuses stale or future readings until every
linked quota window has a recent measurement. The stale label is not itself a monetary limit.

**Reader observations and recovery.** Cached reader results keep the original provider observation time; they are not
logged again as fresh measurements. A successful retry creates a new observation, and backoff is capped by the account's
stale threshold. Unchanged genuine observations are retained often enough for that threshold. The automatic reader's
current failure is shown even when an older reading was recent, and cautious panic rescue refuses any failed reader in
its linked windows until it succeeds again. Normal pacing still follows its existing rules. A standalone `tutti autopilot
tick` has no live reader status and therefore cannot use automatically read accounts for cautious panic rescue; the
dashboard can do so after its current readers succeed.
For an account read only from `reading_file`, the dashboard rechecks the file even when its modification time is unchanged;
an unavailable or malformed file, or changed content with the same modification time, blocks cautious panic rescue until
it can be verified again. For an account with both a file and a reader, a current failure in either also blocks rescue.

Remote bridge readers must report observation protocol version 1 and a valid, non-future timestamp. An older bridge or a
missing, malformed or future observation is unavailable with an upgrade/retry warning; it is not stamped with local time.
Upgrade the bridge on its login host before relying on that remote reader. This fail-closed compatibility change can leave
an old remote account without refreshed usage until both hosts are updated. A remote observation made during a read is
compared with the local receipt time, not the earlier round start; a clock-skewed future observation is rejected. A local
read's conservative timestamp is its start time, so slow reads never appear fresher than they are.
For `tutti usage read --at`, the requested time still controls job-log interpretation and reporting; actual automatic
provider observations keep their real start or remote observation time rather than being backdated or postdated.

**Room for work.** An account `behind` its line (or `late`) says how many bounded tasks it can take now: the points it is
short of divided by `task_pct` (default 2, the points one bounded task costs; tune it from your runs). The action and the
dashboard card say "use it: about N bounded task(s) can go to <worker> now".

## Readings decide where work goes

`tutti run auto "<task>"` reads the latest readings (automatic and typed) and picks the account for the job: never one in
panic (at or over `panic_at` before its target time) or spent; first one that is `late` or `behind` (quota that would be
lost at the reset), then `on pace` or unread, then `ahead`; `routing.workers` is a preference order that breaks ties
(every account that names a worker still joins; it is not an allow-list). It runs that
account's `worker` with its `args` (agy: `--model` of the window's model family). When no account has room it exits 3
and says to use a local model. The dashboard's Quota tile shows where the next agent job goes.

## The planner: what expires soonest is spent first

Quota that expires unused is waste. The planner (`tutti/planner.py`, re-run at every probe from fresh readings) keeps it
small, and stays simple enough to check by hand:

- **Soonest first.** Among accounts with room, a job goes to the one whose planned room expires first (a 5 h window ending
  in an hour before a monthly quota ending in three weeks); an account ahead of its pace still comes last.
- **Windows under a limit.** `accounts.<name>.limits: [<other account>]` names a longer quota that also bounds an account
  (agy's 5 h windows under their weekly quota: `agy-claude-weekly`, `agy-gemini-weekly`, read from the same `/usage`
  answer). The limit's room (its budget, minus `reserve`, minus what was used when this window started) is shared over the
  windows left until the limit's target (its reset minus the lead) that can run:

  `allowance = max(room / runnable windows, room - what the later runnable windows can hold)`, at most what this window has left.

  A window in `autopilot.quiet_hours` (local-time spans, `time.zone`, e.g. `['22:00-06:00']`) gets nothing and its share moves to the windows
  that can run; with no quiet hours, overnight windows are filled by the autopilot instead of lost. What a whole window
  costs of the limit is `window_points`, else measured from this window's readings (the limit's rise over the window's
  rise), else unknown (no cap from the later windows). When this window has spent its allowance, nothing new goes to it until
  it resets; a limit spent or in panic closes it at once. A panic caused by a linked limit names that limit and its
  usage, so a freshly reset short window is not reported as itself being in panic at zero percent.
- **Both limits together.** An even share is a floor, not a cap: while the limit's projection (its last day's rate carried
  to its reset) ends under its budget, this window may also spend the difference, up to what the window itself holds. Early
  in the week a window can take far more than 1/33 of the weekly quota, and it is never cut off while the week still fits.
- **Use it or lose it.** While the weekly limit is at or behind its pace line (used % at most its pace %), a window's unused
  room is spent whatever this window already took of the weekly points, and the limit is projected along its pace line, not
  this window's burst: it is never binding and never "projected to reach 100 %" then. Ahead of its pace it holds the window.
- **Each window keeps its own badge.** The 5 h window's verdict is its own line's; the card's Do line and the Decision say
  which limit binds and why (`5h window has room; the limit agy-claude-weekly (ahead of its pace; projected to end at 104 %)
  is binding: new work waits for the 00:55 (UTC+2) reset`). The Do line always comes from the plan, so it never says "spending
  too slowly" while the Decision says "nothing more", nor "too fast" while the plan says spend.
- **Model and effort follow the pace** (`tutti/tiers.py`). `accounts.<name>.tiers: {strong: {args: [--model, x], sizes: [L,
  M]}, cheap: {args: [--model, y], sizes: [S]}}`. Behind on the binding limit (a window's limit projected to end under 90 %,
  else the account's own verdict behind or late): the strong tier's flags replace the account's (`--model`, `--effort`), and
  the autopilot gives it the L and M tasks first; ahead (projected over 100 %): the cheap tier and the small tasks; on pace:
  the account's own `args`. The model names come from the worker's own list (`agy models`, `cursor-agent --list-models`,
  `claude --help`), never guessed; an account without tiers keeps its args. A running task shows its choice and why
  (`claude-opus-5-5-high/high: agy-claude behind: agy-claude-weekly projected to end at 60 %`), and so does `tutti run auto`.
- **The plan's highest effort** (`accounts.<name>.max_effort`: low, medium, high, xhigh or max, next to its plan label
  `subscription`). Whatever the pace asks, a run never goes above it: a model's effort suffix (`gemini-4-neon-xhigh` ->
  `-low`) and an `--effort` flag are both lowered, and the run's reason says *effort capped at low by the AI Pro plan*.
  Empty: no cap.
- **Room in tasks.** The allowance left, in the window's own points, divided by `task_pct` (0: no tasks), is how many tasks
  the autopilot sends now (its trigger accounts) and the room `tutti run auto` sees. With several limits, the one that allows
  the fewest tasks binds (points of different limits do not compare). An account without limits gets its pace's room, never
  more than what is left after `reserve` and its background spend; when that is used, routing skips it.
- **Quiet hours** stop new dispatches at the current time too; finished runs are still collected.
- **Reserve.** `reserve` (points, default 0) is left for work outside Tutti; the default assumes all work goes through it.
- **Background spend.** `background: true` (Cursor: its own automatic security and dependency reviews spend it outside
  Tutti) estimates the rise per hour between readings with no Tutti run of its worker (`<state>/runs/*.log.cmd`) and keeps
  that much of the room for it until the target, instead of a fixed reserve. Read on the dashboard and by the autopilot.

The dashboard says each decision on the account's card: `agy-claude: spend by 20:44 (UTC+2, in 2.1 h), resets 21:44 (UTC+2, in 3.1 h); this window: 1 of 3
weekly points (30 of 34 windows left can run); 40 % would be lost at its end → 3 task(s) now`. The tests simulate a week
(`tests/test_limits.py`): with a third or half of the windows quiet, the planner wastes nothing where spreading the
weekly room evenly over every window (the min() rule) loses 4 to 7 points.

## The owner's own prompting

The owner prompts the main agent (`main.agent`, its accounts, or `pacing.accounts`) part of the day on working days, and
its use is higher then; on weekends, on holiday, or while away the agents work alone. Tutti plans around that
(`tutti/owner.py`), so every limit is reached without starving the main agent:

- **The profile** is per weekday x local hour. `pacing.schedule` seeds it (default Mon-Fri, `hours: 8` within `window:
  "01-14"`, `rate: 1.0` points per hour; Sat-Sun nothing) until the owner accounts' readings cover 48 h on 3 weekdays;
  then each weekday seen for 12 h takes its learned row. Workers spend the same account, so each day's lower quartile
  (their even spread) is taken off before the owner's hours are learned. A learned window wider than `hours` keeps its
  busiest `hours` contiguous hours. Days not in `schedule.days` never keep a reserve.
- **The reserve.** On the main agent's accounts the planner keeps the owner's expected burn for the owner hours left
  before the window's target, weekly windows across the whole week (a reset on a Wednesday keeps Monday's and Tuesday's
  hours, nothing after). On a Saturday it is the next working days' expected use that falls before the reset: none when
  the reset comes before Monday's hours, Monday to Friday's when the reset is the next Saturday. The card names those days
  and whether the profile is learned yet (`Kept for the owner: 40 points for Mon 16, Tue 17, Wed 18, Thu 19, Fri 20 (auto,
  from pacing.schedule until learned)`); until 48 h of readings are seen the amount is the schedule's `rate x hours` a day. An account with `limits` keeps it out of the weekly limit's budget, and the planner's windows
  spread the rest (*The planner* above). Without `limits` the rest is spent by the
  workers (Claude-backed autopilot too, when the main account is in `autopilot.accounts`), evenly: at most the rest x 5 h /
  the hours left at a time, so it ends near 100 % about an hour before the reset with no burst that would block the owner.
- **The mode** (`pacing.mode`, `tutti owner mode <m>`, or the dashboard): `auto` (the profile; once it is learned, away
  for the rest of the day when the last `away_after_hours` of today's owner hours rose under a quarter of the expected burn
  above the workers' baseline; a seeded window says nothing about when the owner starts, so it never infers away), `active` (always keep the reserve), `away` (nothing kept for the rest of today), `vacation-until-YYYY-MM-DD`
  (nothing kept until that local date). Kept in `<state>/owner.json` with the dashboard's edits of the working days.
- **The check.** `tests/test_owner.py` simulates a week hour by hour (five owner days and a weekend, resets on Monday,
  Saturday and Wednesday, a day away, a two-week vacation, the owner prompting an hour a day): every window ends at 95 % or
  more, and in the owner's hours `used + the reserve still kept` stays under 100.

```yaml
pacing:
  schedule: {days: [mon, tue, wed, thu, fri], hours: 8, window: '01-14', rate: 1.0, utc_offset: null}   # null (best): time.zone, daylight saving included
  mode: auto
  away_after_hours: 3
  accounts: null                       # null: the main agent's accounts
```

Each account card also says **On track to finish at X % by its reset**: the last day's rate carried on.

## Time zones

Tutti finds its own time zone and never asks (`tutti/tz.py`): `time.zone: auto` reads `TZ`, `/etc/timezone`, the
`/etc/localtime` link, `timedatectl`, Windows' `tzutil /g` (mapped to IANA) or macOS' `systemsetup -gettimezone`; an IANA
name in Settings (Time zone) overrides it, and the dashboard's browser zone is used only when nothing else is known.

- Every stored time is UTC (ISO with `Z`); local time is used only at the edges: the owner hours (`pacing.schedule.window`),
  the learned 7 x 24 profile, quiet hours, away and vacation dates, time-of-day tariffs and runs today.
- A profile bucket is a local weekday x hour, so the switch to summer or winter time never moves what was learned by an
  hour; `pacing.schedule.utc_offset` is a fixed offset with no daylight saving, left empty unless you need one.
- The CLI prints local time with the offset, e.g. `Sun 15 Nov 02:00 (UTC+2)`; the dashboard shows the viewer's own
  zone with UTC in the tooltip.
- A household member's hours are read in that member's own zone; a remote machine's zone is read over its ssh, and its
  `dispatch.quiet_hours` are its own local time.
- A reset stated in UTC, with an offset or with a zone (`resets 3pm (Europe/Bucharest)`) is read in that zone.

## The arithmetic

For a window from `start` to `reset`, the target time is `reset - lead_hours`. The share of the quota that should be
spent by `now` is `(now - start) / (target - start)`, capped at 100 %. Against a reading `used`:

| Verdict | When | Do |
| --- | --- | --- |
| `on pace` | within the band around the line | keep going |
| `behind` | under the band | start more parallel work from the backlog |
| `ahead` | over the band | shift work to other workers and local models; eligible trigger accounts can still take slow, bounded work when every measured quota permits it |
| `spent` | the quota is used up | nothing new on this account until the reset |
| `late` | past the target with quota left | start parallel work now; the rest is lost at the reset |
| `no reading` | nothing logged in this window | read the source and log it |

The band is a quarter of the line, at least 1 point and at most 5 (0.1.9): early in a week 10 % used against a line of
6 % is `ahead` (the band is ±1.5), where a fixed ±5 called it `on pace`. Each row of `tutti usage` ends with the math,
`[10 % vs 6.6 % by now; the on-pace band is ±1.6]`. An account shared with a peer session (`share`) is judged against the
whole quota's line, since a reading counts every session's use; the row says so.

A reading from an earlier window is ignored: it says nothing about this one.

## The routine

- Check at the start of each work block and about every 2 hours; log each reading.
- Keep a backlog of ready, bounded tasks: being behind pace is only fixable if there is work to start.
- A reading is the whole account's use, whoever spent it. With a shared account, agree the split once (for example
  half each, every window), and tell the peer early when you will not use your part.

## A day with other hours

One day's owner hours can differ from its weekday's: `tutti owner window 2026-11-16 20-24` (or `off` for none, `config` to
take the day back), the owner panel's **One day with other hours**, or `pacing.dates` in the config
(`{"2026-11-16": "20-24"}`). That day the planner keeps the main agent's reserve for those hours only, at `schedule.rate`
points an hour. Its usual hours, and every hour outside the window, are free for the agents at full speed. A vacation that
ends before the day (`vacation-until-2026-11-16`) hands over to it at midnight. A day's window ends by 24; hours past
midnight are the next day's own window (`00-02`). Taking back a day the config sets keeps a null in `owner.json`. The dashboard's edits live in `owner.json`
and win over the config's.
