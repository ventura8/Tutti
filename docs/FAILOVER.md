# A backup main machine, with automatic failover

Tutti runs on one **main machine**: its dashboard, the household coordinator, the autopilot and the device leases. A
**backup main machine** holds a warm copy of Tutti's state and takes over by itself when the main machine is away, then
hands Tutti back when it returns. Nobody has to do anything.

## Choosing the backup

- An admin picks it: **Settings → Machines → Backup main machine**, or `tutti main backup <machine>`.
- With no pick (`auto`), Tutti elects one: the healthiest machine that answers, preferring one on a UPS
  (`machines.<m>.ups: true`), then one that is never turned off (`machines.<m>.always_on: true`), then the most uptime.
  Never the main machine itself, a phone or another member's machine. An elected backup is kept until an admin changes it.
- `tutti main backup none` turns failover off. Changing the backup first tells the old one to stand down; if it does not
  answer, the change is refused (it could still take over when it is only cut off) unless you add `--yes`.

`tutti failover setup` copies Tutti, its config (turned into the backup's: the backup becomes `host: local`, paths move
under `failover.dir`, default `~/.tutti-failover`) and the state to the backup, and starts the failover round as a user
service (`tutti-failover.service`) on both machines.

## Keeping it current

Every `failover.sync_seconds` (60) the main pushes its state with rsync over its own ssh (the backup never needs ssh back):
config, backlog, users and sign-in state, leases, needs, the runs index (`runs.jsonl`). Logs, run folders, screenshots,
caches, telemetry and live-process files stay behind (`failover.SYNC_EXCLUDE`). Everything arrives at mode 600 and goes
only to the backup. Each round sends only what changed; `tutti failover` shows the last delta.

## Failing over

Each push ends with a heartbeat the backup acknowledges. The main holds a **lease** while acknowledgements come: it may act
only for `failover.minutes` (5) minus 30 s after the last one. The backup takes over only after `failover.minutes` plus 30 s
of silence, or at once when the main says it is shutting down (`tutti failover leave`, the service's stop step). Taking over
raises the **term** and starts the backup's dashboard (with its coordinator and autopilot).

Members' Tuttis learn the **failover list** (main, then backup; each address from `failover.addresses.<m>`, else
`machines.<m>.address`, else a plain `host`) from every coordinator answer and try the next address by
themselves when one does not answer or says it is not active. A computer that is a client after `tutti move` tries
`host.failover: [{ssh, config, tutti}]` the same way.

## Failing back

A main that comes back sees the backup's higher term: it stays **read-only** (no autopilot, no coordinator writes) and copies
what changed on the backup. Once it has been healthy for `failover.failback_minutes` (5) and caught up, it asks for the
hand-back: the backup stops (its dashboard, by the pid it recorded), the term rises again, the main copies the last changes
and resumes at once with a fresh lease (the backup has just stepped down and stands by). Between the two steps nobody
serves.

A busy main keeps its lease: the heartbeat goes first, on its own (retried once, ssh waits up to 15 s), and the state copy
follows only after it; a slow or failed copy never costs the lease.

## Why split brain cannot happen

- Only the active machine of the highest term acts; every term change is stored on each machine (`failover.json`, never synced).
- The main's lease always ends at least a minute before the backup may start, whatever the network does: cut the cable
  between them and the main turns read-only before the backup takes over (`tests/test_failover.py` simulates it every 10 s).
- A heartbeat or hand-back from an older term is refused; a coordinator that is not active refuses members' calls.
- The lease is a duration the backup stamps with its own clock: the two machines' clocks never have to agree. A backup that
  just started waits a full lease before it may take over. A damaged `failover.json` never lets a machine act.
- On a machine that is not active, every round that starts work or takes a lease (autopilot, dispatch and device leases,
  scouts, updates, needs) is off, and commands that change the state exit with code 75, which clients treat like an
  unreachable host and try the next one.

The cost of this guarantee: when the backup is down, the main turns read-only after 5 minutes. Start the backup again, or
`tutti main backup none` if it is gone for good. `tutti doctor` and the Machines card say which.

## Light and self-healing

- `tutti state size` (and `tutti doctor`) shows the state's size against `failover.budget_mb` (50 MB) and its biggest items.
- `tutti state prune` (and every failover round) removes run folders beyond `runs_keep` (200) or older than `runs_days` (14),
  screenshots and ship logs older than `junk_days` (3), and cuts logs over `log_mb` (2 MB) to their newest part. The backlog,
  users, sign-in state and settings are never touched.
- The watchdog (each round) snapshots every state JSON file that parses into `snapshots/` and puts back a corrupt one from
  its last good snapshot (the broken copy kept as `<name>.corrupt`); it starts the dashboard again where Tutti serves when it
  does not answer.

## Tutti's own AI picks the best local model (backlog t80)

Tutti's own AI questions (the welcome, needs, self-analysis) go to the largest local model that is up and free across every
household machine (the size from the model's tag, or `workers.<w>.size_b`), skip a server that is down or already running
another model within a second, and then go online through `ai_assist.online_worker` (a CLI worker that costs nothing per
use, only when `ai_assist.online` is on).

## Network

The dashboard stays on the LAN or a VPN: the backup's copy keeps the same `dashboard.bind`. Nothing is made public.
