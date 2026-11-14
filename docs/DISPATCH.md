# Keep busy: no idle device or worker while work waits

Tutti's dispatcher (`tutti/dispatch.py`) keeps Android devices, machines and workers busy whenever work exists for them.
It is on by default (opt-out): the dashboard's **Keep busy** switch (Controls, opt-in list), `tutti dispatch on|off`, or
`dispatch.enabled: false` in the config.

Why it exists: a phone sat idle while a project still had e2e tests for it. Nothing in Tutti knew those tests existed.
Device runs were only ever started by hand (`tutti test --device`), so there was no queue for an idle device to pull from.

## The pending-work model

A project declares its test suites under `projects.<name>.tests`:

```yaml
projects:
  my-app:
    priority: 1                                  # 0 is the highest (config.project_priority); the Settings tab can change it
    commit: {run: 'git -C ~/src/my-app rev-parse HEAD', where: builder}   # its latest commit; a new one queues every suite again
    tests:
      e2e:
        kind: e2e                                # e2e, unit, lint or other
        targets: [android]                       # android (a phone or tablet under devices:), tv and/or machine
        run: './run-e2e.sh --serial "$ANDROID_SERIAL" --spec {shard}'   # {shard} is the shard's name
        shards: [player.spec, search.spec]       # or shards_from: {run: 'ls e2e/specs', where: builder}
        dir: ~/src/my-app                        # the tree `tutti test` copies over (default: an empty folder)
        devices: [pixel]                         # optional: only these devices (machines: [...] for machine targets)
        retries: 2                               # a failed shard runs again this many times (dispatch.retries)
        # pause: '...'  resume: '...'            # optional: how to suspend and resume a run for a phone call
```

Pending work is:

- every shard not yet passed on the project's latest commit;
- the failed shards that still have retries left;
- for workers, the queued backlog tasks of the autopilot's repos.

Without `commit:`, a suite runs once and then only its failures run again. A test run changes no code, so a project that is
read-only for Tutti still gets test runs. Their results stay in `<state>/dispatch.json` and `<state>/dispatch/<lease>.log`,
shown by `tutti dispatch status` and on the dashboard. They never become a commit.

## The dispatcher

The dispatcher runs once per dashboard probe round, after the autopilot. It also runs the moment a shard ends: the run's
own wrapper starts the next round, so a freed device takes its next shard at once. `tutti dispatch now` runs one round by
hand.

On each round, every free target pulls one compatible shard. It respects:

- **pause**: a paused device or machine, or a device's paused machine;
- **yield** and **thermal** limits: a machine's yield flag, heat or load, and a device's battery, charging and temperature policy;
- `dispatch.quiet_hours`, in local time; a remote machine's in its own zone, read over its ssh;
- **opt-ins**: lint and unit suites on an Android device are opt-in per device (`tutti optin device <d> on`, or the Controls switch).

Projects take turns by a **weighted fair share**: `tutti/priority.py` with `config.project_priority`, the same helper the
autopilot and the Settings tab use. A project's weight is `1 / (priority + 1)`, and a project without a priority comes last.
The next shard goes to the project with the least running work per unit of weight; a tie goes to the higher priority. Priorities 0, 1 and 2 split six free targets 3 : 2 : 1, so no project starves.

## Leases and the heartbeat

A shard runs as `tutti dispatch run <lease>`, which wraps `tutti test --device <d>` (or `--on <machine>`). The device's
own lock is held on its machine, as for any device run.

- The wrapper touches `<lease>.beat` every 30 s while the run lasts, and writes `<lease>.rc` when it ends.
- Each round ends a lease whose run finished, whose process is gone (a crash or a reboot), or whose heartbeat is older than
  `dispatch.lease_minutes`.
- A crashed run counts as a failed attempt and is retried; its target is free again in the same round.

A lease whose target goes away or asleep, or whose run died with its host (an earlier boot id), is interrupted, not
failed: the shard is queued again with no attempt counted. An admin can move a waiting shard to another target, and pin it
there, with `tutti job move` or the dashboard's Move now. See [PRESENCE.md](PRESENCE.md).

## The idle watchdog

A target idle for more than `dispatch.idle_minutes` (default 5) while compatible work waits gets three things:

- a warning row on the dashboard's Controls card, for example "Idle 12 min while RoRadio has 14 e2e shards pending:
  blocked by pixel is paused: ...";
- one line in the dispatch events;
- one telemetry event (`dispatch.idle`) per idle stretch.

The row's button fixes the cause: **Resume** a paused target, **Allow lint and unit runs** (the opt-in), **Turn Keep busy
on**, **Turn the autopilot on**, or **Check again now** (a round with fresh probes). A worker with quota room shows the same
row while backlog tasks are queued; the autopilot dispatches to it, so its plan's reason is the one shown.

## Suites in depth

A shard may be a name, or a mapping with its own options:

```yaml
shards:
  - SearchUITest
  - {name: TabletCastUITest, devices: [phone], extra: '-CastTablet <tablet serial>', holds: [tablet]}
```

- `devices` lists the only devices the shard may run on.
- `extra` fills the command's `{extra}`.
- `holds` names other devices the run uses too. The shard starts only when they are free. They count as busy while it
  runs, and their external lock files are held as well (`tutti test --hold-device`).

A suite can also say how to build and how to read its output:

- `prepare: {run, where}`: the build, run once per commit before any shard of the suite (`tutti dispatch build`). Its shards
  wait until it passes. A failed build holds them with its reason and is tried again after 30 minutes or on a new commit.
- `verdict: {pass, fail}` (regexes): for a runner that always exits 0. The run fails when `fail` matches, or when `pass`
  never appears.
- `started: {pattern, within_seconds}`: the tests must start in time. The clock starts at the device lock's
  `tutti: device ready` line. A run that never starts is stopped and queued again, not counted as a failure, and the device
  is left alone for `dispatch.requeue_cooldown_minutes` (10).
- `requeue_on: [regex]`: output that means "not the test's fault", such as another app in front. The shard is queued
  again, with the same cooldown.

A project can name a report file: `projects.<p>.report: {path, where}`. Each time a shard or a build ends, its results on
the latest commit are written there as Markdown: the counts, the builds, and each failed shard with why and the end of its
log. Put it where the project's own sessions read it.

## Devices shared with other tools

`devices.<d>.external_lock` names the lock file other tools keep for the device, with `{serial}` filled in, for example
`/mnt/c/.../adb-{serial}.lock`.

- **Waiting**: a run waits while that file exists and is fresher than `external_lock_stale_minutes` (30).
- **Holding**: it then writes the file with "tutti <time>" and removes it when the run ends, however the run ends.
- **Readings**: the readings show the device as busy, held by the file's first line, so it is neither idle nor dispatched to.

`devices.<d>.kind` is `phone` (the default), `tablet` or `tv`:

- A TV is a Cast receiver. It has no battery and no call state, so its policy reads neither. Only suites with
  `targets: [tv]` use it, though a shard may hold it with `holds:`.
- `calls: false` skips the phone-call check on any device.

A wireless-debugging serial (`<name>._adb-tls-connect._tcp`) whose port changed is found again each round. The readings
look it up in `adb mdns services` and `adb connect` to today's ip:port, so no port is written in the config.

## Prep and restore

`devices.<d>.prep` lists the settings a run needs (`tutti/prep.py`):

```yaml
prep:
  - {setting: 'secure screensaver_enabled', value: 0}        # Android's settings get|put|delete
  - {name: music volume, read: 'cmd media_session volume --stream 3 --get', parse: 'volume is (\d+)',
     set: 'cmd media_session volume --stream 3 --set {value}', value: 2, cap: true}   # cap: only lowers it
```

1. Once the device's lock is taken, every item's current value is read and saved to `~/.tutti/prep/<device>.saved` on the
   device's machine. This happens before anything changes. Then the new values are set.
2. When the run ends, however it ends, exactly the saved values are put back and the file is removed. A setting that was
   unset (`null`) is deleted again.
3. A file left behind (the machine rebooted, the device went away) shows in the readings as `prep_left`. The next dispatch
   round restores it once the device answers and nothing holds it.

A read that gives nothing, such as a binary the device lacks (`media` on a Google TV Streamer), skips that item cleanly.
Nothing is set that could not be put back.

## Phone calls

An Android device that rings or is in a call takes no new work (`tutti/calls.py`). The call state comes from
`dumpsys telephony.registry`: `mCallState` 0 is idle, 1 ringing, 2 offhook, and a call on any SIM slot counts.

- **Not running**: each round reads the call state with the device's other readings. The device card shows
  **Paused: phone call**, and `tutti test --device` waits too.
- **Running**: the run's wrapper looks every `dispatch.call_poll_seconds` (3), one adb call. When a call comes in:
  1. It presses Home, so the app under test leaves the front and the call screen works.
  2. If the suite has `pause:` and `resume:` commands (run on the device's machine with `ANDROID_SERIAL` set), it pauses
     the run, waits for the call and the delay, then resumes it.
  3. Otherwise it stops the run cleanly (SIGTERM to its process group, then SIGKILL after 20 s) and queues the shard
     again. A shard stopped for a call is not a failure: no attempt and no retry are counted.
- **After the call**: the device takes work again `dispatch.resume_after_call_seconds` (30) after the call returns to idle.

The owner picking the phone up is opt-in, with `dispatch.pause_on_owner_use: true`. A device whose screen is on while no
run of ours drives it is held: "Paused: someone is using it". Leave it off on a device with "Stay awake" on, whose
screen is always lit while it charges.

## Settings

```yaml
dispatch: {enabled: true, idle_minutes: 5, lease_minutes: 10, retries: 2, quiet_hours: [],
           call_poll_seconds: 3, resume_after_call_seconds: 30, pause_on_owner_use: false, requeue_cooldown_minutes: 10}
```

The dashboard's switch is kept in `<state>/controls.json` (`settings.dispatch_enabled`) and overrides `enabled`.
