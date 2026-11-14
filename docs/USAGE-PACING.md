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
tutti usage                        # every account against its pace
```

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
