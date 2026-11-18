# Presence: devices and machines that come and go

People leave the house with their phones, tablets and laptops, and come back later. They shut a PC down for the night or
close a laptop's lid. Tutti expects all of this. Work is not lost, a departure does not count as a failure, and work starts
again by itself when a target comes back.

## States

Every target has a state and the time it was last seen. A target is an Android device, a machine, a household member's
laptop or a local model server (Ollama). The state is kept in `<state>/presence.json`, and `tutti presence` prints it.

| State | Meaning | Read from |
|-------|---------|-----------|
| present | It answers. | adb readings (devices), ssh probes (machines), household heartbeats (members), `/api/tags` (Ollama) |
| away | It left: a laptop gone in the day, or a device gone while its owner's laptop (`devices.<d>.with`) is also gone ("left with its owner"). | the same signals, missing for `presence.grace_seconds` (120) |
| asleep | It is off or asleep: a machine that does not answer in its night hours (`presence.night`, default `23-08`, in the machine's own time zone, which `tutti/tz.py` reads over ssh, DST included), or this computer after logind's PrepareForSleep. | the same signals, and systemd-logind |
| unknown | Never seen yet, or a model server that does not answer while its machine does. | |

A target missing for less than the grace period stays present, so one lost probe is not a departure. The dashboard's
cards say "Away since 18:40 (left with its owner)" or "Asleep since 01:10", and show a small strip of today's 24 hours:
how often the target was here at each hour.

## What happens

- **A target leaves mid-run without a result.** Its dispatch lease is held because the execution boundary may still be
  running. An admin can inspect it and confirm a stop before requeuing; see [DISPATCH.md](DISPATCH.md#leases-and-the-heartbeat).
- **An absence "until it is seen again"** (`tutti pause --from` with no `--until`) ends by itself once presence saw the
  target away and then here, or saw it here for 2 hours after the absence started (it never looked away): it resumes, no
  manual `tutti resume` needed (`controls.SEEN_BACK_MINUTES`).
- **The idle watchdog** never flags a target that is away or asleep.
- **A target comes back.** It is used within one dispatch tick. A wireless-debugging phone is reconnected by itself: its
  new port is found over mDNS (`adb mdns services`), then `adb connect` runs on its machine. The dispatcher tries every
  60 s while the phone is away.
- **This computer goes to sleep or shuts down** (Linux with systemd). Tutti holds a logind *delay* inhibitor. When it gets
  PrepareForSleep or PrepareForShutdown, it does four things within the time the OS allows (`InhibitDelayMaxUSec` less
  one second):
  - stops its running jobs, as "stop now" does, keeping each agent run's partial work on `tutti/handoff/<track>`;
  - holds its dispatch leases until an admin confirms the execution stopped;
  - notes the jobs to start again;
  - flushes its state.

  Then it lets go of the lock. On wake, or at the next start, the noted jobs start again. Turn this off with
  `presence.sleep_watch: false`.
- **Learned profiles.** Each target keeps a 7 × 24 profile (hours present out of hours seen, by local weekday and hour in the target's own zone), as the
  owner's profile does. A long shard (suite `minutes` of at least `presence.long_minutes`, default 30) never goes to a
  target that usually leaves or sleeps in the hours the shard would take: its share present must stay above
  `presence.leave_soon`, 0.5. The most always-on targets choose first.
- **The owner's laptop** (`presence.owner_laptop: <machine>`): while it is away or asleep, owner pacing counts the owner as
  away. The check runs again each round.
- **Tutti on a laptop.** the *Where Tutti runs* card warns that household work, Keep busy and the coordinator stop while
  the laptop is gone, and suggests `tutti move <an always-on machine>`. Tutti detects a laptop by its battery; to set it
  yourself, use `presence.host_laptop`.
- **A member's laptop** is away when its heartbeats stop. The coordinator carries on with the household's machines and
  devices, and the member's shared tasks wait. On return, the member syncs (docs/HOUSEHOLD.md).
- **Wake-on-LAN** (`presence.wake: true`, off by default) wakes an asleep machine that has a `machines.<m>.wol_mac` once
  work has waited for it `presence.wake_after_minutes` (10). Tutti sends at most one packet every 15 min. The packet goes
  to a private LAN broadcast address only, never a public one.

<a id="nothing-is-lost"></a>

## Recovery after interruption

Tutti repairs durable state on startup and resumes recorded work where it can. Work since the last successful checkpoint
and external or remote effects can require verification; a local restart does not prove that remote processes stopped.

- **Durable writes** (`tutti/durable.py`). These state files are written atomically: the backlog, dispatch's leases and
  queue, the autopilot's state, controls, the owner's profile, scouts and presence. Each is written to a temp file, then
  fsynced, renamed and its folder fsynced. A crash at any point leaves the old file or the new one. The usage log gains
  one fsynced line at a time. A line torn by a power-off is skipped on read and cut off at the next start.
- **On start** (`tutti/recovery.py`, run by the dashboard):
  - removes the temp files of writers that died;
  - repairs the usage log;
  - starts again the jobs a shutdown stopped.
- **Interrupted, not failed.** Dispatch leases and autopilot attempts record the local boot (`/proc/sys/kernel/random/boot_id`).
  Recovery can identify an earlier local boot and requeue an interrupted run from its worktree without counting an attempt.
  A changed local boot does not prove a routed remote process or escaped descendant stopped; verify those effects before
  relying on a retry as exclusive. Some ambiguous launches remain held instead of being requeued.
- **WIP checkpoints.** Every `autopilot.checkpoint_minutes` (5), each running attempt is snapshotted through a separate
  git index, so the worker's files, HEAD and index are not touched. Untracked files are included. The snapshot is kept on
  `tutti/wip/<name>` and pushed to the project's remote as `refs/tutti/wip/<name>`, never to main, master or any other
  branch. To turn the push off for a repo, set `autopilot.repos.<r>.wip_push: false`; to push somewhere else, set
  `wip_remote`. `<name>` comes from the task's title, so every member's computer uses the same name. A power-off can lose
  work since the last successful checkpoint, and the remote copy may be older if its push failed.
  A checkpoint with no new SHA is reported as unverified: the tree may be unchanged or Git may have failed. A local-only
  checkpoint or failed push is not reported as pushed; an earlier SHA is retained separately as an unverified reference.
  Checkpoint metadata does not prove that a local or remote execution boundary stopped (t309).
  `tutti autopilot checkpoint` snapshots running attempts now without dispatching or collecting work, including when
  periodic checkpoints are disabled. It reports each push or uncertainty and returns a nonzero status for any unconfirmed
  result. Workers may change files during capture; this does not stop them, fence new work, or establish restart safety.
- **Reconcile.** Before an interrupted or moved attempt resumes, Tutti fetches the remote checkpoint and applies it to a
  worktree that lost its changes. The further-along copy wins, and a worktree that still holds changes is never
  overwritten.
- **Auto-start.** `tutti setup` installs the dashboard as a systemd user service with `Restart=always`. It also runs
  `loginctl enable-linger`, so the service starts at boot before anyone logs in. On Windows, a scheduled task starts it at
  logon. Once started, Tutti attempts the following recovery steps:
  - re-joins the household at its first heartbeat;
  - reconnects its devices;
  - reads its queues and continues.

## Move work now

An admin can move work that waits on an away or asleep target, for example a laptop that left or a PC that was switched
off, onto another target at once.

- **Dashboard.** Machines → *Waiting on away or asleep targets* lists every waiting job. Each one has these controls:
  - **Move now**: to a chosen target, or to *Best available*;
  - **First in queue**: a priority bump;
  - **Move all waiting on …**: moves everything waiting on that target.
- **CLI.**
  - `tutti job waiting` lists the waiting jobs.
  - `tutti job move <id> --to <device|machine|auto> [--bump]` moves one job.
  - `tutti machine evacuate <machine>` moves everything waiting on a machine.

What a move does depends on the job:

- **A shard** is queued again with no attempt counted. It is pinned to the chosen target, or with `auto` to any target of
  its kind, over the suite's own `devices` or `machines` list.
- **An autopilot task** has its run stopped and a last checkpoint taken. It is queued again with no attempt counted and
  pinned to a worker on the chosen machine. Its brief says it moved, and it resumes from the last synced checkpoint.
- **A household member's task** (on the coordinator) loses that member's lease, which goes to the chosen member or stays
  free for the first member with room. When the old holder comes back, the coordinator refuses its claim and tells its
  sync that the task moved. The old holder stops its stale run, attempts a `tutti/backup/<name>` snapshot, preserves the
  local worktree even if that snapshot fails, and marks the task `moved` so it is not dispatched locally again. Its recovery
  note says whether the backup is unconfirmed. The new household holder may already be running; inspect and reconcile the
  preserved work before discarding it.

**Automatic moves** (opt-in): set `presence.move_after_minutes: N`. Urgent jobs that have waited N minutes on an absent
target then move by themselves. A job is urgent when its project has `projects.<p>.urgent: true`, when its task has
`urgent`, or when an admin bumped it.

**Admins only.** `POST /api/work/move` passes the dashboard's auth hook, so a viewer gets 403.

## The coordinator going down

When the household coordinator's computer is off, members keep their local queue and work alone (the household's away
mode, docs/HOUSEHOLD.md), then sync when it is back. A coordinator on an always-on machine, such as a NUC, avoids the pause: `tutti move <machine>`.

## Settings

```yaml
presence:
  grace_seconds: 120        # missing this long: away or asleep
  night: '23-08'            # local hours in which a silent machine is asleep
  long_minutes: 30          # a shard this long avoids targets likely to leave
  leave_soon: 0.5           # share present in the coming hours under which a target is leaving
  owner_laptop: null        # the machine your main agent lives on
  host_laptop: null         # null: detected (a battery); true/false to say so
  sleep_watch: true         # Linux: hand work over before sleep or shutdown
  move_after_minutes: null  # opt-in: move urgent work waiting this long on an absent target
  wake: false               # opt-in: Wake-on-LAN (machines.<m>.wol_mac, wol_broadcast)
  wake_after_minutes: 10
```

## What it does not do yet

- A model server down while its machine is up is shown as unknown, not as away.
- Windows power events and PC-helper sleep signals are not read yet. A Windows PC is judged by ssh reachability and its
  night hours.
- A wireless phone whose port changed is reconnected, but readings still use the configured serial: keep a fixed port or
  pair it again.
- Run state and handoff notes reach other machines only through the checkpoint refs and, for shared projects, the
  household sync. A single-user Tutti host that is off cannot hand its local queue to anyone.
