# Android devices: pairing over Wi-Fi and moving them

Tutti runs end-to-end tests on Android phones, tablets and TVs through adb on the machine that reaches them
(`tutti test --device`, the dispatcher, the dashboard's Devices card). This page is how a device gets there with
wireless debugging (Android 11 and newer), and how devices move to another machine. The readings and the safe-use
policy (battery, heat, screen, calls) are in [MACHINES.md](MACHINES.md#android-devices).

## Pair a device

Dashboard: **Devices and controls > Pair a device** (a phone or tablet) or **Pair a TV**. Terminal:

```bash
tutti devices pair                    # on this computer; --machine <name> for another machine's adb
tutti devices pair --tv               # a TV: it shows a code (no camera to scan with)
tutti devices pair --flow usb         # fallback: plugged in by USB, then adb over the LAN on port 5555
```

What the user does, and nothing else:

1. Turn on Developer options: **Settings > About phone**, tap **Build number** 7 times (a TV: **Settings > System >
   About > Android TV OS build**, press OK 7 times).
2. **Settings > System > Developer options > Wireless debugging**: on; **Allow** ("Always allow on this network").
3. **Pair device with QR code** and scan the code Tutti shows (on the dashboard, and drawn in the terminal). A TV:
   **Pair device with pairing code**, and type its 6-digit code into Tutti.

When a device is plugged in by USB, Tutti opens its Developer options page itself. Then Tutti:

- makes a one-time pairing name and password and shows them as `WIFI:T:ADB;S:<name>;P:<password>;;`, the QR code
  Android Studio uses;
- watches `adb mdns services` for the device's `_adb-tls-pairing._tcp` service under that name (the code flow: the
  pairing service the device opened), runs `adb pair`, then connects through its `_adb-tls-connect._tcp` service;
- adds it to the config, keeping every comment: the name from its model (`Pixel 9 Pro` → `pixel9pro`), the machine,
  its `kind` (phone, tablet or tv, from `ro.build.characteristics` and the leanback feature), `model` and
  `capabilities` (camera, telephony, touchscreen, tv, ...). Paired again, the same entry is updated.

The adb used is found by itself: `devices.<name>.adb`, else PATH, `~/Android/Sdk/platform-tools`, Tutti's own copy in
`~/.local/share/tutti/platform-tools` (`--install-adb` downloads it, user space only), or a Windows SDK seen from WSL.
Wireless pairing needs platform-tools 31 or newer.

## Ports change: the mDNS name is the serial

A device's wireless port changes on every reboot and every time Wireless debugging is turned on. The serial in the
config is therefore the mDNS name (`adb-<serial>-<id>._adb-tls-connect._tcp`), never an ip:port. Every reading
(`tutti devices`, the dashboard's probe, a run's start) reconnects a device that is not connected, through the ip:port
its connect service announces now (`resolved=<ip:port>` in the readings). A device paired through
the USB fallback keeps `ip:5555`, which a reboot ends: pair it with the QR code when it can.

## Move devices to another machine

```bash
tutti devices move --to nuc --dry-run          # the plan and the preflight; nothing changes
tutti devices move --to nuc                     # every device not on nuc yet
tutti devices move --to nuc pixel9pro googletv  # only these
```

Dashboard: **Move devices**, pick the machine and the devices, **Show the plan**, then **Move now** (it runs
`tutti devices move` in the background; the log is `<state>/devices-move.log`). `tutti move --to <machine> --devices` (moving Tutti
itself) runs the same move for every device once Tutti is there.

The best path needs no re-pairing: the old machine's adb key pair (`~/.android/adbkey` and `adbkey.pub`; a Windows
adb's `%USERPROFILE%\.android`) is copied to the new machine over ssh, at mode 600, held in memory only and written
through standard input (never in argv, a log or the terminal). Every device already trusts that key.

- The new machine has no key: it is copied.
- It has the same key: nothing to copy.
- It has its own, different key: it is backed up as `adbkey.tutti-backup-<time>` and the old one is copied (the
  default: the user does nothing; `--replace-key` says the same for old scripts). `--keep-key` keeps it instead, and
  then only `--repair` moves the devices.
- Re-pairing (`--repair`: a checklist, the QR code or the code a TV shows) is the last resort, for when the old key
  cannot be read (adb does not run on the old machine, or a policy forbids the copy). It is never part of a normal move.

A move asks the user nothing. Before anything changes, each device's Wireless debugging setting is read and kept (the
result's `wifi_was`) and set on over the old machine's live connection (`settings put global adb_wifi_enabled 1`); after
the device connects on the new machine it is set on again from there. Android keeps the trusted key itself (its list of
allowed keys is not readable without root); since the copied key is the one already on that list, it keeps working.

Preflight, before anything changes: adb on the new machine (else `--install-adb`), platform-tools 31+ on both, every
device seen over mDNS from the new machine (so it is on the devices' network and multicast passes), and no run holding
a device: a dispatch lease, Tutti's device lock, or another tool's lock file while it is fresher than `external_lock_stale_minutes` (30; `devices.<name>.external_lock`,
docs/DISPATCH.md; its text names the holder).

Then, on the new machine: the adb server restarts to load the key (`adb kill-server`, `adb start-server`), each
device is connected and checked (`adb -s <serial> shell getprop ro.product.model`, and against `model` when the config
has it), `devices.<name>.machine` (and `adb`) change in the config, and the old machine's adb server lets go of it
(`adb disconnect`). A device that does not connect stays on the old machine with its config unchanged (rollback); when
none moved, the new machine's key is put back as it was.

## Wireless debugging stays on

Android turns Wireless debugging off after a reboot or a Wi-Fi change. Whenever a reading finds a wireless device not
connected (`tutti devices`, the dashboard, the dispatcher), Tutti asks every machine's adb whether it still reaches the
device, over Wi-Fi or a USB cable on any household machine (the USB serial is part of the mDNS name), and turns the
setting back on through the first one; the device's row says through which. Each device is tried at most every 5 min.
Only when no machine reaches it does the row ask the one step left, with the reason: turn Wireless debugging on, on the
device (or plug it into any household computer by USB). See tutti/keepon.py.

## Using a device from another computer (a lease)

A session on another household computer (a PowerShell runner on the PC, say) uses a device that lives on the NUC with a
lease. Its own adb reaches the device over Wi-Fi: every household machine uses the same adb key (`tutti devices move`
copied it), so the device already trusts it.

```bash
ssh nuc tutti devices claim googletv --who roradio --minutes 120   # prints ADDR=<ip:port> and SERIAL=...; exit 3: in use
adb connect <ADDR>                                                  # then run the tests with ANDROID_SERIAL / -s <ADDR>
adb disconnect <ADDR>
ssh nuc tutti devices release googletv --who roradio                # its own settings come back when it was the last user
```

The claim is exclusive (another claim or run gets exit 3 with who holds it; competing claims check and write under one lock).
Only the exact same claim holder can renew it. A claim preps the device (its own values saved once), wakes it and keeps
it awake while the lease lasts (one keep-awake loop per device, its pid in `~/.tutti/devices/<name>/awake.pid`), and
ends by itself at `--minutes` (1 or
more) if the requester never releases it: the loop then puts the device's own settings back at once. Where Tutti and
its config are on the requester's computer, one command does all four steps:
`tutti devices lease googletv --who roradio -- <command ...>` (ANDROID_SERIAL set to the address). It stops with the
reason, and releases the claim, when the device's wireless address is not found or `adb connect` fails.

## Other tools that use the same devices

A tool with its own lock file per device names it in `devices.<name>.external_lock` (a path on the device's machine,
`{serial}` filled in; docs/DISPATCH.md). Tutti mirrors it: the device shows busy while the file is fresh, a Tutti run
waits for it and writes its own there for the run, and a move waits for it while it is fresh. That tool's agents keep working unchanged.

To use Tutti's leases instead, an agent runs its device commands through Tutti, which holds the device's lock for the
command, checks battery and heat first and queues a second run:

- CLI: `tutti test --device <name|any> -- <command>` (for example `tutti test --device pixel9pro -- ./gradlew
  connectedAndroidTest`); `ANDROID_SERIAL` is set for the command. `tutti devices` shows which run holds a device.
- A session that uses a device outside a Tutti run: `tutti devices claim <name> --who <session>` before, `tutti devices
  release <name> --who <session>` after; the device is released (its own settings back) when the last user is done.
- MCP (`tutti mcp`): the `test_on` tool with `device` (and `track`, `command`); it returns a job id to poll with
  `job_status`. The `devices` tool shows each device's state and lock.
