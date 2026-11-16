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

## Machine power

Every machine gets a measured power score (`tutti/power.py`): a two-second benchmark (one core hashing 256 MB, the cores
and threads, total memory, GPU memory, an NPU, a 64 MB synced disk write) runs in the background once the machine is
free, and again every `power.bench_days` (default 7); results are in `power.json` in the state folder. Points: CPU (one
core's MB/s / 100 per core, a hyper-thread a third), memory (2 per GB), GPU (8 per GB of VRAM, 20 for an NPU), disk (up
to 100). At each probe the score is scaled by the headroom now (load per core, free memory under 4 GB, heat at its cap,
Tutti's share: quiet, presence and limited hours) into the effective power.

The dispatcher orders free targets by how likely they are to stay, then by effective power. A heavy shard (kind or key
e2e, build, suite, full, docker, integration, or `minutes` of 15 or more) goes to a strong machine first, a light one to a
weak machine first (a machine whose effective power is under half the strongest usable machine's). A weak machine under
30 % free takes no heavy shard: it waits for a stronger one, and the blocked line says so. Each lease keeps why it went
where it did; the Machines card shows the power, the free share and the effective power, and the running shards' reasons.
Until a machine is measured its score is 0 and the old order holds.

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
- `verdict: {pass, fail, none}` (regexes): for a runner that always exits 0. The run fails when `fail` matches, or when
  `pass` never appears. A run whose filter matched no test (`0 tests match`, `No tests found`) fails in every
  suite, whatever its exit code (read in the last 30 lines, the summary): `none` replaces that pattern, `none: ''` turns it off.
- `started: {pattern, within_seconds}`: the tests must start in time. The clock starts at the device lock's
  `tutti: device ready` line. A run that never starts is stopped and queued again, not counted as a failure, and the device
  is left alone for `dispatch.requeue_cooldown_minutes` (10).
- `requeue_on: [regex]`: output that means "not the test's fault", such as another app in front. The shard is queued
  again, with the same cooldown. With `started` set, only the output after the started line counts: an app in front while
  the runner was still installing or unlocking is not a reason.
- `shards_from: {run, where}`: a command that prints shard names, one per line (for example the test classes ending in
  `UITest` or `E2ETest`). Asked with the commit check; the names it prints join the listed `shards`, and a listed shard
  keeps its options (`{name: TvRemoteE2ETest, devices: [googletv]}`).

## Device classes

Some adb commands are unsafe on some kinds of device (`tutti/devclass.py`). By default a TV (`kind: tv`, Android's
leanback) never gets `wm size`, `wm density`, the rotation settings (`user_rotation`, `accelerometer_rotation`) or
`cmd device_state`: a panel with no rotation or fold can stay broken until a person resets it. Tutti applies the list
three ways: a prep item that matches is dropped, a run whose own command line names one is refused (exit 5, the reason
printed), and the run's command gets `TUTTI_DEVICE_CLASS` and `TUTTI_DEVICE_DENY` (one regex) so the project's script can
skip them too (a Windows script reached through WSL needs them in `WSLENV`). Replace a class's list in the config:

```yaml
device_classes:
  tv: {deny: ['\bwm\s+(?:size|density)\b', '\bcmd\s+device_state\b']}
  phone: {deny: []}
```

A TV gets no keep-awake key either (0.21.1).

## A project's runner and its own work tree

`projects.<p>.runner: <machine>` is the default `runner` of every suite of the project: a device run leases the device,
connects the runner's own adb to it over the LAN, checks `adb get-state` answers `device` there (else the run never starts,
with the reason), then runs the command on the runner. `tutti test <Project> --device <d> -- <command>` goes the same way,
in the project's own tree, nothing synced:

```bash
tutti test RoRadio --device pixeltablet -- 'powershell.exe -NoProfile -Command "& C:\R\scripts\android\run-e2e.ps1 -Serial $env:ANDROID_SERIAL -Filter ReadabilityUITest"'
```

`tutti test <Project> --on <machine> -- <command>` with `projects.<p>.windows.<machine>: C:\...` runs in that Windows
tree as it is (seen from WSL as `/mnt/c/...`), never synced. Options may follow the track (`tutti test RoRadio --on
w5090-win --fetch C:\R\out.txt -- <command>`); of several command words, one with a space or a quote is quoted and the
others stay raw (`&&`, `|`, `$VAR` still work); one word is the line as written; a `--fetch` of a Windows path is copied from where it is.

## Dedicated machines

`machines.<m>.dedicated: true` keeps a machine for the suites that name it under their `machines:` (for example a
Windows desktop entry with a shell runner for one project's UI pipeline and emulators). The work queue never routes a
job there, and a suite open to every machine never takes it. Give it `slots: 1` so its suites never overlap.

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

A phone or tablet also has items that are only read and put back (`value: keep`): the rotation settings, the screen
timeout and the screensaver. Tutti never sets them; a test that rotates the screen or shortens the timeout leaves the
device as it found it.

Every device run is a device session (`devices.external_wrap`, `tutti/prep.py`). In order:

1. **The lock first.** It waits while another tool's lock file (`devices.<d>.external_lock`) is fresh, takes it, and
   registers the run among the device's users (`~/.tutti/devices/<d>/users/run-<pid>` on its machine). The lock file is
   refreshed every minute while the run lasts.
2. **Prep.** Each setting's current value is read and saved to `~/.tutti/prep/<device>.saved` before anything changes;
   a saved file already there (another user still has the device prepared, or a crash) is kept as it is, so prepared
   values never become the "originals". The defaults of its kind, then its own `prep:` list (an item with a default's
   name replaces it; `prep_defaults: false` turns the defaults off):
   - phone and tablet: brightness about 30 % (`system screen_brightness` 77, manual mode), media volume (Cast plays
     on it) about 10 % (2 steps, only lowered);
   - TV: screensaver off, screen on for 30 min, media volume (Cast plays on it) about 10 %.
3. **Awake and unlocked.** The screen is woken and a swipe lock dismissed; while the run is active a harmless key
   (`input keyevent KEYCODE_UNKNOWN`) goes every 15 s, because the phone's admin policy caps the screen timeout at 5 min.
   The loop stops with the run.
4. **The start check.** `tutti: device ready` is printed; within 2 minutes the run must show a test running (the
   suite's `started` pattern, else any output after the marker), or it is stopped and queued again.
5. **Release only when every user is done.** When the run ends, however it ends, the keep-awake loop stops and its lock
   file and registration go. The device is released only when no Tutti run, no claim (`tutti devices claim`) and no fresh
   lock file of another tool is left. Then exactly the saved values come back (a setting that was unset is deleted
   again): phones and tablets sleep again, a TV gets its screensaver and sleep timers back and turns itself off. The
   output says `tutti: device released <d>`, the dashboard's Keep busy events say it is free, and a line goes to
   `<state>/devices/free.jsonl` for the other sessions and the coordinator. Otherwise it says who still uses it, and the
   last user's release restores it.
6. A saved file left behind (the machine rebooted, the device went away, or the last user was another tool) shows in the
   readings as `prep_left`. The next dispatch round releases it the same way once nobody uses it.

A read that gives nothing, such as a binary the device lacks (`media` on a Google TV Streamer), skips that item cleanly.
Nothing is set that could not be put back.

**A person picks it up.** `getevent` on the device sees only physical touches and keys, never the test's injected taps or
Tutti's keep-awake key, so a person is told apart from the run. Then the run stops cleanly and its shard is queued again,
no key is sent, nothing new starts until the device has been idle `devices.<d>.idle_minutes` (3), and the card says
"in use by a person — waiting". At release an item goes back only while it still has Tutti's value: what the person
changed (an alarm, the brightness) stays.

**Night mode** (on by default, `night: {enabled: true, hours: '22-08', devices: []}`; `tutti devices night ...`, Settings
or the device card): while Tutti holds or works a device in the night hours it runs at 1 % brightness (auto off) with every
volume stream at 0 (music and Cast, ring, notification, system; the alarm is never touched), in place of the day's values.
The person's own values come back at hand-back as usual.

Other rules a device run keeps:

- **An e2e filter is never empty.** `--grep`, `--filter`, `--tests`, `-g`, `--test-name-pattern` or a Gradle
  `...Arguments.class=` / `package=` with no value would run every test or none: the run is refused before it starts.
- **One app install per device at a time, debuggable only.** `tutti devices install <d> <file.apk>` refuses an APK that
  is not a debuggable build (read from its manifest before anything is sent) and installs behind the device's install
  lock: a second install waits for the first.
- **Sessions outside Tutti** say they use a device with `tutti devices claim <d> --who <session>` and `tutti devices
  release <d> --who <session>`; the release puts the device's own settings back when it was the last user.

## When a device goes away

A device that is away (a planned absence, presence lost, a person using it, its blocked hours, paused or unreachable) has
its pending shards moved to devices that can run them (`tutti/reroute.py`): the same form factor (a phone or a tablet for
touch work, a TV for TV work), and the API level and capabilities the suite asks for (`requires: {kinds: [phone, tablet],
min_sdk: 34, capabilities: [camera]}`). Work no other device fits waits, with the reason. The move is decided afresh each
round, so the work goes back to the device as soon as it returns. The device card's list shows it ("Pixel 9 Pro away
(planned absence): 3 shards moved to pixeltablet, 1 waits for it"), each change is an event, and a line goes to
`<state>/devices/moves.jsonl` for the projects' sessions and the coordinator.

## Paired devices (roles)

Cast roles are detected (`tutti/castroles.py`, once a day and at pairing; the device and machine cards show them; an
admin's `roles:` overrides): a device is a receiver when it is a TV, has the Cast receiver service or Play services'
mirroring receiver (a Pixel Tablet, a phone), and a sender when it has Play services' Cast and a screen to send from; a
machine is a sender with Chrome, Chromium or the project's desktop sender (`machines.<m>.desktop_sender: true`). A Cast
suite with `pair: {role: cast-receiver, sender: true}` runs on any sender here (a device or a machine) with any receiver
here: the tablet to the TV while the phone is away, the phone to the tablet when the TV is busy. A suite that needs a partner says so (`pair: {role: cast-receiver}`): each of its shards holds the first device with
that role that is here, so both are leased at once, and it waits, with the reason, while none is. By hand:
`tutti devices claim pixeltablet googletv --who <session>` leases the pair (both or none; each prints its ADDR).

## Newest artifact, else a guarded build

A suite's build (`prepare`) can reuse what is already built and build only when it must:

```yaml
prepare:
  where: pc
  run: <the build>
  reuse: {paths: [/mnt/c/.../app-Signed.apk, ...], newer_than: 'git -C /mnt/c/.../repo log -1 --format=%ct'}
  guard: {hours: ['20-24'], yield: [pc]}
```

An artifact newer than the commit (`newer_than` prints its time) is used as it is and nothing is built. A build that is
needed waits, with the reason, in the guard hours and while its machine (or a `guard.yield` machine) yields; it is tried
again later.

## A suite on a runner machine

A suite whose runner lives elsewhere than its devices (RoRadio's PowerShell `run-e2e.ps1` and adb on the PC, the devices
on the NUC) names it: `runner: pc`. Each shard then runs as `tutti devices lease <device> [<partner>] --runner pc --
<command>`: the devices are leased (both or none), the runner's own adb connects to each lease address, the command runs
there with `{addr:<device>}` filled in (`ANDROID_SERIAL` is the first device's, `TUTTI_ADDR_<DEVICE>` each one's), and adb
disconnects and the devices are released however it ends. Example (the paired Cast suite):

```yaml
run: powershell -File C:\Users\me\Projects\RoRadio-tr\scripts\android\run-e2e.ps1 -Serial {addr:pixeltablet}
     -CastTablet {addr:googletv} -CastReceiverName "Google Streamer" -Filter {shard} -Package com.example.dev -Apk <apk>
runner: pc
devices: [pixeltablet]
pair: {role: cast-receiver, sender: true}
```

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
