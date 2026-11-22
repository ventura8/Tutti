# Machines

Heavy work (lint, tests, builds) leaves the computer the orchestrator and its browser run on.

## Roles

Every machine and device has a set of roles: what it is for (`tutti/roles.py`). They are detected once a day (a small
probe over the machine's own transport: OS, docker, adb, dotnet, pwsh, the Android emulator and its AVDs, Chrome, Ollama,
an NVIDIA GPU and its memory, a desktop session, WireGuard or Tailscale, Xcode or Playwright WebKit), read from the config
(the main machine, `failover.backup`, `browser.machine`, the devices a machine hosts) and the Android readings (leanback,
Cast). Media devices Tutti cannot run tests on (a soundbar, a smart TV, speakers) are found on the LAN over mDNS
(`_airplay._tcp`, `_raop._tcp`, `_googlecast._tcp`, `_spotify-connect._tcp`) and UPnP SSDP (MediaRenderer) by
`tutti/lanmedia.py` and listed under Devices, *On the network*.

| Role | Means |
| --- | --- |
| `windows-test-runner`, `linux-test-runner`, `macos-test-runner` | runs that OS's suites (WSL counts for both Windows and Linux) |
| `android-test-runner`, `android-device`, `tv-device`, `emulator-host` | an adb host, an Android device, an Android TV, Android emulators with AVDs |
| `ios-webkit-runner` | Xcode's simulators or Playwright WebKit |
| `docker-host`, `build-machine`, `gpu-ai`, `ui-desktop` | containers, 8+ cores with 16+ GB, local models (6+ GB VRAM or Ollama), an interactive desktop |
| `chrome-browser`, `main`, `backup-main`, `vpn-node` | the household browser machine, where Tutti runs, the failover backup, on the private network |
| `cast-sender`, `cast-receiver`, `chromecast-builtin`, `airplay-sender`, `airplay-receiver`, `dlna-renderer`, `spotify-connect`, `bluetooth-audio`, `smart-tv` | media roles |

An admin edits them in the config or the dashboard's Settings: `roles:` is the whole list (an override), `roles_add` and
`roles_off` change what was detected (`machines.<m>.*`, `devices.<d>.*`, `lan_devices.<address>.*`). From a terminal:
`tutti machines --roles [--refresh]`, `tutti roles list [--all]`, `tutti roles set|unset <name> <role>`.

A suite asks for roles with `roles: [windows-test-runner, ui-desktop, gpu-ai]`: only a target with every one gets it, and
among those the power score and routing pick as before. An unknown role is a config error.

## Home Assistant

Tutti shows a Home Assistant instance on the dashboard's **Machines** tab (t268, `tutti/homeassistant.py`): its name,
address, online or offline, version, time zone and its role, `home-automation-hub`. Configure it once:

```yaml
home_assistant:
  url: http://homeassistant.local:8123   # no user or password in it
  token_env: TUTTI_HA_TOKEN              # optional; the default
```

Make a long-lived access token in your Home Assistant profile and put it in `~/.config/tutti/home-assistant.env` (mode
600) as `TUTTI_HA_TOKEN=...`. Tutti reads it only when it asks Home Assistant (`GET <url>/api/config`), never stores,
prints or sends it elsewhere. Without a token the card still says whether the address answers. A refused token, a wrong
address or an instance that is off each say what to do. Over mDNS (`_home-assistant._tcp`) an instance not configured
yet shows on the **Discovered** tab with the same role; the configured one is left out there.

## Set up a found computer

A computer on the **Discovered** tab has **Set up**: one command to run in a terminal here (t225, `tutti/hostsetup.py`):

```bash
tutti machines add --host 192.0.2.10 [--name nas] [--user me] [--port 22]
```

It makes an ssh key if this computer has none, checks key login, and only if that fails runs `ssh-copy-id`, which asks
for that computer's password once: you type it into ssh there, and Tutti never sees, keeps or sends it. It checks key login
again and only then writes `machines.<name>` (one slot to start with, a backup of the config first). Each failure says
its step and the fix: ssh off on the host, a wrong user, keys refused, or a config that cannot be saved. An Android
device gets its `tutti devices add` command the same way; a media device needs nothing (its roles are detected).

## Slots

`scripts/slot.sh` is the queue on each machine: `slots` lock files, taken with `flock`. A run takes a free lock, waits
until `min_free_gb` of memory is available, runs, and the kernel releases the lock when it ends or dies. Nothing is
polled by PID and nothing needs cleaning up after a crash.

- Never start a heavy command on a machine outside its queue.
- A run that waits is normal, not a hang.
- Every session that shares a machine must use the same lock files: `lock_prefix`, or an explicit `locks:` list to
  share a lock that an older script already uses.
- `flock` needs a POSIX filesystem. On a Windows `local` machine put locks under bash's `/tmp/...` (WSL or Git Bash),
  not under a Windows path that appears as `/mnt/c/...`: those report every slot busy.

## The runner

`runner.kind: docker` starts each command in a container with a memory cap, **no extra swap** and a CPU cap; without
caps, three lint runs can take a whole machine down. On the orchestrator's own computer add `--oom-score-adj=900` so the
kernel kills the test container first, never the desktop. `runner.kind: shell` is plain bash for machines without
Docker. A machine may override any runner field.

## Fall-over routing

`routing.order` lists machines by preference. For `tutti test`:

1. A machine is **fit** when it answers, its yield flag is down, a slot is free, if `max_load_ratio` is set its
   1-minute load is under that share of its cores, and it is not too hot (`heat`, safe by default; below).
2. The first fit machine wins. Later machines are not even asked.
3. If none is fit, the run queues on the first reachable machine whose flag is down.

Put the dedicated build box first and shared machines after it, each with a `max_load_ratio`.

`test.prepare` lists steps a command needs first: `{when: <regex>, run: <command>}`. When the command matches and does
not already contain the step, `tutti test` and routed checks run `{ <step>; } && { <command>; }` (grouped, so a failed
step stops a compound command too). Example: Playwright's webServer
times out unless the stamped e2e build ran, so `{when: 'playwright|e2e/', run: 'python3 scripts/ci/e2e_build.py'}`.

## Transports

- `ssh` (default): a POSIX machine reached by an ssh alias with key-based login.
- `local`: this computer (`host: local`): no copy, the tree is used in place.
- `wsl`: a Windows machine whose commands run inside WSL. Windows' ssh hands a line to its own shell and then to
  `wsl.exe`, so quoting does not survive; every remote script travels base64-encoded to a small helper inside WSL
  (`scripts/wsl-run64.sh`, named by `wsl_helper`). git and rsync reach WSL through `tutti rsh`.
- `android`: a phone or tablet running Termux's ssh server (`ssh -p 8022 [user@]host`); opt-in, see
  [Termux machines](#termux-machines-opt-in).

**One physical computer, several environments.** A dedicated session that shares `host` with a regular machine folds
into that machine's dashboard card automatically (`machines.<m>.dedicated`). When Windows and WSL use different ssh
aliases, set `machines.<environment>.physical_machine` to the main entry's name so the Machines tab nests them under
one card; routing and slot accounting stay separate. See [docs/DASHBOARD.md](DASHBOARD.md).

Every ssh-based transport shares one connection per machine: the first call starts a background master (`ssh -N -f`
with `ControlMaster`, kept 10 minutes after its last use, its socket under `$TMPDIR/tutti-ssh-<uid>/`), and the
dashboard's probes, `tutti test`, rsync and git join it instead of a new handshake each. While there is no master a call
connects on its own; a machine that is down is not dialled for a new master more than once a minute.
`machines.<name>.ssh_reuse: false` turns it off for one machine; Windows never uses it.

## Files for machines

`tutti files send <path>... --to <machine|all>` gives a machine what it needs to work on: a big archive, a dataset,
build inputs. `all` is every machine that is not this computer.

- **Transport.** The machine's own: ssh, WSL (through `tutti rsh`) or a local copy. rsync when this computer and the
  machine both have it: `--partial` keeps an interrupted file, so sending again resumes it, and files already there are
  skipped. Without rsync a stream appends the missing bytes to `<file>.tutti-part` and moves it into place; it also
  creates empty folders and removes files a folder no longer has, as rsync's `--delete` does.
- **Checked.** Every item is verified with `sha256sum -c` on the machine before it counts as stored; a mismatch removes
  the copy and sends it once more, a second one stops with the fix. The manifest and a small record stay in
  `.tutti-files/` beside the items, so the next send of an unchanged item is skipped without copying a byte. A changed
  item's record is dropped before its first byte is overwritten, so an interrupted update never passes for the old
  version. Checksums of big local files are remembered (`<project.state>/files/sha256-cache.json`).
- **The cache.** Without `--dest` an item lands in `files.cache` (default `~/.cache/tutti/files`). Before a byte moves the
  cache is kept within `files.budget_gb` (default 50) by removing the least recently used items (a skipped send counts as
  a use); a replaced item counts once, at its new size; partial files of interrupted transfers count against the budget;
  an item larger than the budget is refused. `--dest <folder>` puts it elsewhere, outside the budget. Either way
  the machine must keep `files.keep_free_gb` (default 2) of disk free.
- **Bandwidth.** `--bwlimit <KB/s>` or `files.bwlimit_kbps` (0 = none); `files.compress: true` adds rsync's `-z` (leave it
  off for archives that are already compressed). One send or clean per destination folder at a time from this computer,
  whichever project asks (a lock under `$XDG_STATE_HOME/tutti/locks`, keyed by host and folder).
- **Never secrets.** A path that is, or a folder that holds, `.env*`, `*token*`, `*secret*`, `*.pem`, `*.key`, ssh keys,
  `*service-account*.json`, `*credentials*.json`, a small `.json` with a `private_key`, or anything under `.ssh/`,
  `.gnupg/`, `.aws/` refuses the whole send, naming the file; a symlink is judged by where it leads too.
  `files.refuse` adds patterns.
- **Jobs.** `tutti test --input <path>` copies inputs to the chosen machine's cache before the run and exports
  `TUTTI_INPUTS` (the cache folder), mounted read-only when the runner is docker. The inputs are **pinned** until the job
  ends: no eviction, clean or send of a different version touches them (a pin older than 24 hours, from a job that
  crashed, no longer counts). `tutti run <worker> --input <path> --on
  <machine>` does the same before briefing a worker and names the folder in its brief.
- **See and clean.** `tutti files list [--on <machine>]`; `tutti files clean --on <machine|all>` removes partial files
  untouched for a day and trims to the budget, `clean --on <m> <name>...` removes items, `--all` empties the cache and
  every partial file. Pinned items stay. The dashboard shows every transfer in Jobs (with
  its progress in MB) and each machine's stored files under Machines.

A machine may override any of these with its own `files:` (for example a larger `budget_gb` on the build box).

## Android devices

A phone or tablet is first an **e2e target**: a host machine (this computer, the build box, the PC) reaches it over adb, by
USB or by wireless debugging, and runs the UI tests against it. Lending its CPU to lint and unit runs (Termux, below) is a
second use, **off by default**.

Pairing a device over Wi-Fi (`tutti devices pair`, a QR code or the code a TV shows) and moving devices to another
machine (`tutti devices move --to <machine>`): [DEVICES.md](DEVICES.md).

```yaml
devices:
  pixel:
    serial: R58M123ABC                # what `adb devices` lists; ip:port for wireless debugging
    machine: pc                       # the machine its adb sees it on (default: this computer's machine)
    policy: {min_battery: 30, require_charging: false, max_battery_c: 42, wifi_only: false, screen: wake}
    # lock: ~/.tutti/locks/device-pixel.lock   # on that machine; every session that shares the device uses the same file
    # runner: {kind: shell}                # a device run may use its own runner, e.g. plain bash for Gradle
```

- **Readings.** `scripts/device-readings.sh` runs on the device's machine (copied there by ssh when it is not this
  computer): `adb devices` (connected, `unauthorized`, `offline` or missing), `dumpsys battery` (level, AC/USB/wireless
  power, temperature), `dumpsys power` (`mWakefulness`: the screen), `cmd wifi status`, and whether a run holds the lock.
  Read-only: it changes no setting. `tutti devices` and the dashboard show them.
- **Policy.** A run starts only while the device is connected and authorised, its battery is at `min_battery` or more,
  it is charging when `require_charging`, its battery is under `max_battery_c`, it is on Wi-Fi when `wifi_only`, and with
  `screen: off` only while its screen is off (nobody uses it). A reading that cannot be taken refuses the run: running
  blind is how a battery gets drained or cooked. Every refusal names the fix (tap Allow, plug it in, `adb connect`).
  `screen: wake` (the e2e default) presses the wake key before a run; a screen that stays dark is a warning, not a refusal.
- **One run at a time.** `tutti test --device <name> -- <command>` holds the device's lock (`slot.sh` with the device's
  lock file as its only slot, memory floor off) inside the machine's own slot queue, so a second run waits in line, the
  same way a run waits for a machine slot. The run gets `ANDROID_SERIAL`; in a docker runner it is passed to the container
  too. The policy is checked when the run is queued (waiting up to `--wait-minutes`, default 30, for a phone to charge or
  cool down); `--device any` takes the first ready device. The run is a job on the dashboard (`<state>/devices/`), with
  its output and progress.
- **Results.** `--fetch <path>` (repeatable) copies a folder or file of the tree back from a remote machine after the run:
  Playwright's `test-results/`, `playwright-report/`, Gradle's `app/build/outputs/androidTest-results/`.

### E2E recipes

- **Android app UI tests** (Gradle instrumentation), from the PC the device is plugged into:
  `tutti test --device pixel --fetch app/build/outputs/androidTest-results -- ./gradlew connectedDebugAndroidTest`.
  Gradle and adb pick the device from `ANDROID_SERIAL`.
- **A web app or PWA in Chrome on the device** (Playwright's experimental Android support, `_android`): the test connects
  to the adb server on the host and drives Chrome over CDP. Screenshots and videos land in the host's `test-results/`.

  ```ts
  import { _android as android, test as base } from '@playwright/test';
  export const test = base.extend({
      context: async ({}, use, info) => {
          const device = (await android.devices()).find((d) => d.serial() === process.env.ANDROID_SERIAL);
          if (!device) throw new Error(`no device ${process.env.ANDROID_SERIAL}: check adb devices on this machine`);
          await device.shell('am force-stop com.android.chrome');
          const context = await device.launchBrowser({ recordVideo: { dir: info.outputPath('video') } });
          await use(context);
          await context.close();
          await device.close();
      },
  });
  ```

  Run it as its own Playwright project or config: `tutti test --device pixel --fetch test-results -- npx playwright test
  --config playwright.android.config.ts`. Inside a docker runner give the container the host's adb server (`--network
  host`, the device's `runner.args`), since USB does not reach a container.

### Termux machines (opt-in)

With [Termux](https://termux.dev) and its ssh server, a phone runs light jobs (python or node unit tests, lint) and a small
local model. Off until the owner opts in (the dashboard's Controls, or `tutti optin machine <name> on`).

```yaml
machines:
  phone:
    transport: android                # ssh -p 8022 [user@]host
    host: 192.168.1.50
    user: u0_a123                     # `whoami` in Termux
    port: 8022
    policy: {min_battery: 50, require_charging: true, max_battery_c: 40, wifi_only: true, screen: 'off'}
```

- It runs natively (`runner.kind: native`, `nice -n 10 bash`): Android has no Docker, so `runner.kind: docker` is refused.
  One slot, a 2 GB memory floor, its locks in Termux's own `tmp` (Android has no `/tmp`).
- It always routes last (Tutti moves it to the end of `routing.order`), and only when fit: its battery, charging, battery
  temperature and Wi-Fi come from the Termux:API app (`termux-battery-status`, `termux-wifi-connectioninfo`) in the same
  probe as its slots; the default policy is charging, 50 % or more, under 40 C, on Wi-Fi. It is never the queue of last
  resort while its policy refuses it. The screen is readable only over adb, so `screen: off` applies when the phone is
  also a device above.
- `tutti files send` reaches it like any ssh machine (rsync over `ssh -p 8022`). A model server in Termux (`pkg install
  ollama`) is a worker through a tunnel: `tunnel: {machine: phone, remote: '127.0.0.1:11434'}` uses its user, host and port.
- Keep heavy Docker gates off it; the policy is checked when a run is routed, so keep its runs short.

`tutti devices add` and `tutti machines add-android` print the owner's one-time steps on the device and the config lines.

## The yield flag

`yield_file` is a path on the machine. While the file exists:

- routing treats the machine as unfit;
- a local-model worker bound to the machine (`workers.<name>.machine`) sends nothing and waits;
- if the file's text holds the word `unload`, the worker also unloads its model; otherwise the model stays loaded for
  whoever raised the flag.

`tutti yield on --machine <m> --note "release build"` raises it, `tutti yield off --machine <m>` lowers it, and
`tutti yield status` shows every machine. Any tool can raise the flag by creating the file; it is the whole protocol.

**A flag expires (0.18.0).** `tutti yield on` writes one JSON line: owner (`--owner`), reason (`--note`), the owner's
process (`--pid`) and an expiry (`--hours`, default 4). `tutti yield renew --machine <m> [--hours N]` extends it. The
self-heal round lowers a flag that expired, or whose owner's process is gone while the machine is idle, and logs it; a
plain-text flag (any other tool's) expires 4 h after it was last written, so a tool that holds a machine longer rewrites
(touches) its file at least every 4 h. The machine card shows the owner, the reason and the age.

**Disks.** The probe reads the least free space of the home disk and, on a WSL machine, the Windows C: (`/mnt/c`): under
15 GB the card warns with the biggest reclaimable items, under 8 GB the machine takes nothing new until the clean-up or a
person frees space (docs/SELFHEAL.md, *Yield flags, disks and clean-up*).

## Heat and memory guards

- **Heat.** The probe also reads the CPU, the hottest `/sys/class/thermal/thermal_zone*/temp`, and the hottest GPU
  (`nvidia-smi`, which WSL keeps in `/usr/lib/wsl/lib`, else an `amdgpu`, `xe` or `i915` hwmon sensor); `tutti machines`
  and the dashboard show both. WSL has no thermal zones, so a WSL machine's CPU is read from Windows over its plain ssh
  (every 2 minutes at most): LibreHardwareMonitor's `data.json` on 127.0.0.1:8085 (CPU Tctl or Package temperature and
  package power), else WMI (Open/LibreHardwareMonitor) or the ACPI thermal zone. Tutti installs LibreHardwareMonitor
  itself (`tutti/winhelpers.py`: winget, else choco) and starts it minimised at logon through a scheduled task with highest
  privileges, created once from the elevated ssh session: no prompt, no person. The same setup starts a hidden presence
  helper in the console session (GetLastInputInfo and the lock state into `%LOCALAPPDATA%\tutti\idle.txt`), because
  `query user` reports a console session's IDLE TIME as `none` however long it sat untouched. A desktop's ACPI zone is often a fixed placeholder (about 17 C): a value under 20 C or
  over 110 C is shown as "CPU temperature not readable" with the reason, never as a number. Heat limits apply to the
  hotter of CPU and GPU. `heat: {cap_c: 85, stop_c: 98, hot_slots: 1}`: at `cap_c` or more the machine takes a run only while fewer
  than `hot_slots` run there; at `stop_c` or more it takes nothing new. Each dispatch round also writes an automatic heat
  pause at `stop_c` (hands running work over) and clears it once the machine cools below `cap_c`; an owner's pause and
  blocked-hours pause stay as they are. A machine without a `heat` block gets
  `{cap_c: 85, stop_c: 95, hot_slots: 1}` (a Termux machine keeps its battery policy); `tutti setup` writes it. Routing then falls over to the next machine in
  `routing.order`, so put a `local` machine with one capped slot last as the fallback. A machine refused for heat is never
  the queue of last resort either; `--on` does not bypass `stop_c`. A machine without any readable
  sensor is never refused for heat. Routing looks once, when the run is queued; `slot.sh` looks again right before a
  waiting run starts (`HEAT_STOP_C`, `HEAT_CAP_C`, `HOT_SLOTS`, passed from `heat`): when the machine is too hot it gives
  its lock back, says `slot.sh: waiting, too hot: 99 C ...` once, and starts when the machine has cooled down.
- **Memory guard.** `tutti guard` watches available memory. Under `guard.low_gb` it pauses (never kills) the runner
  image's containers; over `guard.high_gb` it resumes them.
- **One heavy local job at a time.** A `local` machine with one slot is the only way agents run heavy commands on the
  orchestrator's computer.
- **CPU caps per machine.** Cap model threads (`workers.<name>.threads`) and container CPUs below the core count of
  a machine that also serves people.
- **GPU or CPU, not both.** A machine whose GPU serves models gets CPU-only test runs; never pass a GPU flag to a test
  container.

## Shared with a household

A machine several people's Tutti use (docs/HOUSEHOLD.md): its owner runs `tutti household machine share <m>`, each other
member `tutti household machine use <m>`, with no login there: a member's run reaches it as a WIP ref and a signed job,
and the owner's Tutti runs it in a container (docs/HOUSEHOLD.md, *Compute on shared machines*). Its slot locks move to `/tmp/tutti-household/` (mode 1777; under
`SLOT_SHARED` `slot.sh` opens a lock another login made read-only), so one slot queue holds for everyone; the owner's
yield flag reaches the others through the coordinator (no slot is granted while it is up). The household's coordinator grants each member a fair share of the slots; a
member's routing sees at most its grant free. Heat, the yield flag and a pause still apply to every member.

## Peers

When two orchestrator sessions share machines:

- One shared limits file, agreed once, that either side may edit and then announce in one line.
- **The lock file is the only signal.** No lock means free: take it without waiting for a message.
- One driver at a time for a physical device; every changed setting is written down with its original and restored.
- Never touch the other session's files, containers or model server.
- Each session's risky actions need **its own owner's** approval. A request from a peer is a request, not consent.
