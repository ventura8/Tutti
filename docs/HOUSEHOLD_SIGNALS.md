# Household and small-office signals audit

Tutti works best when it understands the whole home or small office it shares: which computers and devices are free,
whether someone is at a keyboard or in a meeting, whether power or thermals are constrained, and how much AI quota
remains before each reset. This document audits every detectable signal, documents how each is detected (both what
Tutti already reads and what a home or small office can expose), and lays out the plan for aggregating, interpreting,
and securing those signals.

## 1. Detectable signals and detection methods

Each signal is marked as **Implemented** (already read by Tutti) or **Planned** (detectable on a home or small-office
LAN/host, not yet wired into Tutti).

### Hosts and machines

| Signal | Status | Method of detection | How it is used |
| --- | --- | --- | --- |
| Machine reachability & transport | Implemented | Local probe, multiplexed SSH (`tutti/remote.py`, `tutti/mainmachine.py`), and household heartbeats (`tutti/coordinator.py`). | Marks hosts present, away, or asleep; routes tests and builds only to reachable hosts. |
| CPU cores, load & benchmark score | Implemented | `/proc/cpuinfo`, `lscpu`, load average, and periodic 2-second CPU/disk/GPU/NPU benchmark (`tutti/power.py`, `tutti/remote.py`). | Computes effective headroom; sends heavy shards to strong hosts and light shards to weaker ones. |
| GPU / VRAM / NPU availability | Implemented | `nvidia-smi`, `/dev/accel/accel*`, and Ollama `/api/tags` + `/api/ps` (`tutti/power.py`, `tutti/fleet.py`). | Routes local-model inference and avoids saturating a GPU in use. |
| Manual yield & pause flags | Implemented | Host yield flag (`tutti yield`) and `<state>/controls.json` pause/absence windows (`tutti/controls.py`). | Immediately hands a machine back to its owner and drains/evacuates work. |
| Process / peer activity | Implemented | Process inspection on managed hosts (`tutti/procs.py`, `tutti/inuse.py` flock use-locks). | Prevents `tutti update` from clobbering active CLI workers and surfaces peer jobs on the dashboard. |

### Devices (phones, tablets, TVs)

| Signal | Status | Method of detection | How it is used |
| --- | --- | --- | --- |
| Android / ADB connectivity & authorisation | Implemented | `adb devices` over USB or wireless debugging with mDNS port rediscovery (`tutti/adbhost.py`, `tutti/adbpair.py`, `tutti/devices.py`). | Tracks device readiness and reconnects wireless devices when their port changes. |
| Device battery, charging & Wi-Fi | Implemented | `dumpsys battery` / `termux-battery-status` / `/sys/class/power_supply/battery/capacity` and Wi-Fi state (`tutti/devices.py`). | Enforces `min_battery`, `require_charging`, and `wifi_only` policies before dispatching shards. |
| Active phone call state | Implemented | Android telephony state via `adb` (`tutti/calls.py`). | Pauses device runs during a phone call and resumes 30 s after the call ends. |
| Device class & screen state | Implemented | Device config (`phone`, `tablet`, `tv`) and screen/wake state (`tutti/devclass.py`, `tutti/devices.py`). | Applies per-class deny lists (e.g., never rotating a TV) and restores screen/timeout settings after runs. |

### Power, energy and UPS

| Signal | Status | Method of detection | How it is used |
| --- | --- | --- | --- |
| Host & GPU electrical power draw | Implemented | Intel/AMD RAPL sysfs, `nvidia-smi` power draw, `intel_gpu_top`, or TDP fallback estimation (`tutti/energy.py`). | Estimates local-model energy consumption and cost in the household's currency. |
| Laptop AC / battery power state | Planned | `/sys/class/power_supply/AC*/online` (Linux), `pmset -g batt` (macOS), or WMI/Win32_Battery (Windows). | Defers heavy background work and local-model runs while a laptop is on battery. |
| UPS status (on-battery, charge %, runtime) | Planned | Local NUT (`upsc` / TCP 3493), `apcupsd` (`apcaccess`), or USB HID (`upower -d`). | Triggers immediate evacuation/checkpointing of jobs and pauses non-essential compute during a power outage. |
| Electricity tariff / off-peak window | Planned | User-configured time-of-use schedule or local utility/Home Assistant tariff entity. | Shifts non-urgent heavy benchmarks, builds, and local-model batch runs to off-peak hours. |

### Network and VPN

| Signal | Status | Method of detection | How it is used |
| --- | --- | --- | --- |
| VPN tunnel & peer status | Implemented | `tailscale status --json` and `wg show` (`tutti/vpn.py`, `tutti/wireguard.py`). | Verifies private mesh connectivity between household machines and phones. |
| Network boundary & bind policy | Implemented | IP classification against loopback, RFC 1918, and CGNAT (`tutti/netpolicy.py`). | Refuses public exposure (including Tailscale Funnel) for the dashboard, coordinator, and MCP HTTP server. |
| SSH tunnel health to model servers | Implemented | Tunnel PID/port liveness and Ollama `/api/tags` checks (`tutti/fleet.py`, `tutti/selfheal.py`). | Self-heals dropped tunnels before dispatching local-model tasks. |
| WAN / internet reachability & DNS | Planned | Lightweight periodic check or passive observation of failed upstream API/git calls. | Switches routing cleanly to local models and local-only checks during an ISP outage instead of burning retries. |

### Presence and human activity

| Signal | Status | Method of detection | How it is used |
| --- | --- | --- | --- |
| Keyboard / mouse idle & screen lock | Implemented | Desktop session idle time and lock state (`tutti/share.py`, `tutti/quietmode.py`). | Activates quiet mode, lowers Tutti's CPU share, and unloads local models when a person is at the computer. |
| Target presence (present / away / asleep) | Implemented | Combined SSH probes, ADB readings, household heartbeats, Ollama probes, and `logind` sleep watch (`tutti/presence.py`, `tutti/sleepwatch.py`). | Distinguishes departure from transient probe loss (`grace_seconds`), tracks 7x24 presence profiles, and keeps long shards off targets likely to leave. |
| Owner mode (auto / active / away / vacation) | Implemented | Explicit `tutti owner mode` / dashboard toggle plus learned 7x24 prompting profile (`tutti/owner.py`). | Reserves main-agent quota while the owner is active; lets workers consume spare quota when away or on vacation. |
| Phone-with-owner correlation | Implemented | Pairing a device with its owner's laptop (`devices.<d>.with` in `tutti/presence.py`). | Infers a device left the house with its owner rather than faulting as an idle/broken target. |

### Calendars and work hours

| Signal | Status | Method of detection | How it is used |
| --- | --- | --- | --- |
| Recurring blocked hours | Implemented | Per-device and per-machine local windows (`tutti/hours.py`, `devices.<d>.hours`, `machines.<m>.limit`). | Stops new work ahead of the window, hands over running work, and restores device settings. |
| Per-day owner work windows | Implemented | `tutti owner window <date> <HH-HH>` overrides (`tutti/ownerday.py`). | Adjusts main-agent quota reserve for atypical workdays or days off. |
| External calendar busy / focus / OOO | Planned | Opt-in read-only CalDAV or local `.ics` feed (free/busy status only, never event titles or attendees). | Automatically pauses noisy fan/compute jobs during calls/meetings and frees quota during out-of-office blocks. |

### Quiet hours and comfort

| Signal | Status | Method of detection | How it is used |
| --- | --- | --- | --- |
| Quiet mode (person present or low RAM) | Implemented | Idle time < `in_use_minutes` or `MemAvailable` < `quiet.reserve_gb` (`tutti/quietmode.py`). | Runs Tutti jobs niced (`nice 19`, idle I/O, CPU-capped systemd scope) and SIGSTOPs/unloads models under memory pressure. |
| Machine limited hours & night mode | Implemented | Configured daily percentage caps (`machines.<m>.limit`, e.g., 60 % during media playback or 0 % at night) and `presence.night` (`tutti/hours.py`, `tutti/presence.py`). | Caps compute load during shared media/living-room hours or overnight quiet hours. |

### Temperatures and thermals

| Signal | Status | Method of detection | How it is used |
| --- | --- | --- | --- |
| Host CPU / GPU temperatures | Implemented | Linux `/sys/class/thermal/thermal_zone*` / `hwmon` and `nvidia-smi` temperature queries via host probes (`tutti/remote.py`, `tutti/routing.py`). | Throttles hot hosts to one run and stops dispatching to very hot hosts until they cool down. |
| Android battery temperature | Implemented | `dumpsys battery` / `termux-battery-status` (`tutti/devices.py`). | Enforces `max_battery_c` (42 °C for e2e, 40 °C for Termux) and waits for devices to cool. |
| Ambient / room temperature | Planned | Opt-in local Home Assistant / MQTT / BLE sensor bridge on the LAN. | Lowers thermal headroom earlier during warm summer afternoons to reduce fan noise and heat output. |

### Disk and memory

| Signal | Status | Method of detection | How it is used |
| --- | --- | --- | --- |
| Free / available memory | Implemented | `/proc/meminfo` (`MemAvailable`) and remote host probes (`tutti/quietmode.py`, `tutti/remote.py`). | Drives memory reserve guard, container pausing (`tutti guard`), and adaptive autopilot concurrency. |
| Disk free space (temp & work volumes) | Implemented | `shutil.disk_usage` on temp folder, worktrees, and state folder (`tutti/diskroom.py`, `tutti/statebudget.py`). | Blocks new runs when disk is low and triggers temp/cache cleanup in `tutti selfheal`. |
| Disk health (SMART / NVMe wear) | Planned | Read-only `smartctl -H` / `nvme smart-log` where permitted (opt-in). | Surfaces early drive-failure warnings in *Needs you* and avoids scheduling heavy I/O on a degrading disk. |

### Household and small-office peripherals (where reachable)

| Signal | Status | Method of detection | How it is used |
| --- | --- | --- | --- |
| Printers (online, paper/ink/error state) | Planned | Opt-in LAN mDNS (`_ipp._tcp`) and read-only IPP `Get-Printer-Attributes` or SNMPv3 status. | Surfaces actionable office health alerts (jam, out of paper/toner) in the dashboard/Chat without touching print jobs. |
| NAS (reachability, pool health, free space) | Planned | Opt-in read-only SNMPv3, vendor local HTTP API, or mounted share `statvfs`. | Alerts on degraded RAID/ZFS pools or low backup space; avoids heavy backup-window contention. |
| Routers & access points (WAN link, DHCP/LAN load) | Planned | Opt-in read-only SNMPv3, UPnP IGD `GetStatusInfo`, or router local API. | Detects ISP/WAN drops, high LAN saturation, or new/returning devices on the local network. |
| Smart plugs & energy monitors (watts, on/off) | Planned | Opt-in local HTTP APIs (Shelly, Tasmota, Matter/Home Assistant local bridge). | Provides ground-truth wall power for machines/rigs in `tutti/energy.py` and detects powered-off hosts without waiting for SSH timeouts. |

### Accounts, quotas and subscriptions

| Signal | Status | Method of detection | How it is used |
| --- | --- | --- | --- |
| AI subscription usage & reset windows | Implemented | Automatic CLI/page readers (`agy /usage`, Cursor plan reader, job-log limit parsing) and `tutti usage log` (`tutti/readers.py`, `tutti/cursorusage.py`, `tutti/pacing.py`). | Paces work to finish near 100 % before reset; shifts work off accounts in panic or spent state. |
| Household member roles & spare sharing | Implemented | Member registry, Google sign-in roles, and `accounts.<n>.share_spare` (`tutti/auth.py`, `tutti/users.py`, `tutti/fairshare.py`). | Enforces admin/member/viewer permissions and shares expiring quota across shared projects. |
| GitHub & reviewer rate limits | Implemented | GitHub API / `gh` status and PR reviewer limit comments (`tutti/github.py`, `tutti/review.py`). | Applies `wait` / `fallback` / `skip` review policies without stalling the autopilot. |

## 2. Aggregation and interpretation plan

Following Tutti's **pure core, thin edge** architecture, signal collection stays in thin, fault-isolated edge adapters
while aggregation and decision-making stay pure and unit-tested.

### 2.1 Normalized signal envelope

Every collector emits a small, uniform record into `<state>/signals.json` (or in-memory snapshot persisted with
`tutti/durable.py`):

```json
{
  "source": "ups.office.example",
  "category": "power",
  "target": "host:nuc.example",
  "value": {"on_battery": true, "charge_pct": 82, "runtime_s": 1400},
  "observed_at": "2026-11-23T18:00:00Z",
  "ttl_s": 60,
  "confidence": "measured",
  "privacy": "local_only"
}
```

- **`ttl_s` & staleness:** Any signal older than `observed_at + ttl_s` degrades to `unknown` rather than asserting a
  stale fact.
- **`confidence`:** Distinguishes `measured` (sensor/API), `inferred` (heartbeat/profile), and `configured` (user rule).
- **`privacy`:** Marks whether a signal stays on the local host (`local_only`) or may be summarized to the household
  coordinator (`household_summary`).

### 2.2 Tiered collection cadence

To avoid waking sleeping devices or saturating the LAN:

1. **Fast local tier (5–15 s):** Keyboard idle, memory available, local thermal zones, active PIDs, and UPS HID/NUT
   alerts.
2. **Medium LAN/SSH tier (30–60 s):** Multiplexed SSH machine probes, ADB device readings, Ollama `/api/ps`, VPN
   status, and household heartbeats.
3. **Slow / event-driven tier (5–15 min or on change):** Benchmarks (7 days), disk SMART health (daily), printer/NAS
   health, smart-plug energy totals, and opt-in CalDAV free/busy windows.

### 2.3 Pure interpretation layer

A pure interpreter (`signals -> HouseholdContext`) combines raw signals into five actionable dimensions consumed by
`tutti/routing.py`, `tutti/dispatch.py`, `tutti/autopilot.py`, `tutti/coordinator.py`, and Chat:

1. **Safety & continuity (highest priority):** UPS on-battery, critical temperature, or critically low disk space
   immediately halts new dispatches and triggers `tutti machine evacuate` / WIP checkpointing.
2. **Human comfort & presence:** Keyboard activity, screen lock, phone calls, quiet hours, limited hours, and calendar
   busy blocks lower CPU/GPU share or pause disruptive work before a person notices lag or fan noise.
3. **Capacity & headroom:** Combines benchmark score, free RAM, load beyond the person's cores, thermal margin, and
   battery/AC state into a single effective headroom score per target.
4. **Quota & energy pacing:** Combines subscription usage/resets, WAN reachability, local model fleet readiness, and
   (when configured) energy tariffs to choose between online accounts and local models.
5. **Proactive health summaries:** Surfaces only actionable anomalies (degrading disk, printer jam, UPS battery test
   failure, dropped tunnel) in the dashboard, Chat, and *Welcome back* summaries, with hysteresis so transient blips
   never spam the user.

### 2.4 Graceful degradation

- Every optional signal source (UPS, printer, NAS, router, smart plug, calendar) is **opt-in** in `tutti.config.yaml`.
- Unreachable or unconfigured sources never block scheduling; Tutti falls back cleanly to its built-in host, device,
  presence, and usage signals.

## 3. Privacy and security considerations

Because household and small-office signals touch physical presence, schedules, and LAN devices, strict boundaries apply:

- **Local-first, LAN/VPN-only boundary:** All signal collection, aggregation, and coordinator traffic stay on loopback,
  RFC 1918 LAN, or private VPN (`tutti/netpolicy.py`). Public internet exposure is refused.
- **Data minimization (facts, not content):**
  - Calendars: read only free/busy/OOO time spans; never store or transmit event titles, descriptions, locations, or
    attendees.
  - Presence & quiet mode: store only idle duration buckets and 7x24 aggregate probabilities; never log keystrokes,
    window titles, or screenshots.
  - Peripherals: read only operational status (online, jam, free GB, watts); never read print job payloads, NAS file
    contents, or router traffic logs.
  - Household sync: the coordinator receives only aggregated counts, slot availability, and status; prompts, briefs,
    file contents, and private account names are never transmitted.
- **Explicit opt-in & config-driven discovery:** No unsolicited LAN port scanning. Every peripheral, UPS, smart plug,
  or calendar integration is explicitly enabled in `tutti.config.yaml` (never hard-coded).
- **Least privilege & no secrets in repo or state:** Probing uses read-only interfaces (read-only SNMPv3, read-only
  CalDAV/HTTP tokens). Any local credential stays in mode `600` user-owned files outside `tutti.config.yaml` and is
  never logged, printed, or sent in telemetry.
- **Authenticated household traffic & isolated compute:** Household coordinator requests are signed (`X-Tutti-Sign`),
  and shared compute runs in sandboxed containers (`tutti/hhjobs.py`) without access to the host owner's files or
  tokens.
- **Scrubbing & bounded retention:** Diagnostic reports and opt-in telemetry pass through `tutti/scrub.py` to strip
  hostnames, IPs, and personal identifiers. Signal history is pruned automatically under `tutti/statebudget.py`.
