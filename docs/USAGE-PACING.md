# Usage pacing

**The rule: spend every quota fully about 1 hour before its reset.** Not earlier: work stalls while you wait. Not
later: the unused part is lost at the reset. The hour is a margin for readings that lag and tasks that overrun;
override it per account with `lead_hours`.

## Accounts

Each account under `accounts:` has:

- `window`: how often it resets: `5h`, `7d`, `2w` or `monthly`;
- `resets`: any one known reset time, in UTC; Tutti rolls it forward by itself;
- `lead_hours`: the margin before the reset (1 by default);
- `share`: this session's part when a peer session uses the same account (0.5 = half);
- `source`: where a reading comes from (a usage command, a dashboard page).

Most agent CLIs have no usage command. Read the dashboard, in the owner's signed-in browser, and record it:

```bash
tutti usage log claude 62          # 62 % of the window's quota is spent now
tutti usage log cursor 40 --resets 2026-12-03T00:00Z   # with the reset the usage page shows: it anchors the window
tutti usage                        # every account against its pace
tutti usage read                   # run the automatic readers now (the dashboard runs them by itself)
```

## Automatic readings

Tutti reads what the CLIs print, never their credential files, cookies or private APIs. Three sources, all written to
the same usage log as `tutti usage log`:

| Source | Config | What it gives |
| --- | --- | --- |
| A reader command | `reader: {command: [...], bucket: <id>, every_minutes: 10}` | Antigravity: `agy -p /usage --output-format json` lists every quota bucket (`3p-5h` and `gemini-5h`: the two rolling 5-hour windows; `3p-weekly`, `gemini-weekly`) with the share left and the reset time, and spends no quota. One run serves every account that names the same command. |
| A plan command | `plan: {command: [...], field: <key>}` | A label for the card, such as Cursor's tier from `cursor-agent about --format json` (`subscriptionTier`). Only that field is read. |
| Limit messages | `match: {command: <regex>, model: <regex>, fallback: true}` | A "usage limit reached, try again in 3 h" line in the output of `tutti run` or of a `tutti quiet` job whose command matches logs the account as spent (100 %) until the reset it names. With several accounts on one CLI (`agy`), `model` picks the one the command or the message names; `fallback` takes the rest. |

A reading that names its reset (a reader, a limit message, the dashboard form, `--resets`) anchors the account's
window from then on: it beats the config's `resets`, which is only a first guess. Mark such a guess with
`resets_estimated: true`; the dashboard then says "an estimate" until a reading sets it. Automatic readings are kept
only when they say something new (a point or more, a moved reset, or an hour since the last one).

`usage_url` is the provider's usage page: the dashboard's **Add a reading** opens it and a short form (the percent and
the reset time, prefilled from the window rule), for what no CLI prints (Cursor's spend this month). `worker` names the
worker whose card shows this account.

## Readings decide where work goes

`tutti run auto "<task>"` reads the latest readings (automatic and typed) and picks the account for the job: never one in
panic (at or over `panic_at` before its target time) or spent; first one that is `late` or `behind` (quota that would be
lost at the reset), then `on pace` or unread, then `ahead`; the order of `routing.workers` breaks ties. It runs that
account's `worker` with its `args` (agy: `--model` of the window's model family). When no account has room it exits 3
and says to use a local model. The dashboard's Quota tile shows where the next agent job goes.

## The arithmetic

For a window from `start` to `reset`, the target time is `reset - lead_hours`. The share of the quota that should be
spent by `now` is `(now - start) / (target - start)`, capped at 100 %. Against a reading `used`:

| Verdict | When | Do |
| --- | --- | --- |
| `on pace` | within 5 points of the line | keep going |
| `behind` | more than 5 points under | start more parallel work from the backlog |
| `ahead` | more than 5 points over | shift work to cheaper workers and local models |
| `spent` | the quota is used up | nothing new on this account until the reset |
| `late` | past the target with quota left | start parallel work now; the rest is lost at the reset |
| `no reading` | nothing logged in this window | read the source and log it |

A reading from an earlier window is ignored: it says nothing about this one.

## The routine

- Check at the start of each work block and about every 2 hours; log each reading.
- Keep a backlog of ready, bounded tasks: being behind pace is only fixable if there is work to start.
- A reading is the whole account's use, whoever spent it. With a shared account, agree the split once (for example
  half each, every window), and tell the peer early when you will not use your part.
