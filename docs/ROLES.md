# Fleet roles

Open **Role matrix** on a machine or device card, or run `tutti roles matrix` in a terminal. Use `--interactive` to
refresh the matrix with Enter and exit with q. Each row lists all roles, the current assignment and the reason for any
exclusion. A device that is offline or held has no confirmed assignments until it can be read again.

| Role | Evidence |
| --- | --- |
| Host | Supported machine transport, Git, Python and enough free disk |
| Backup host | Host prerequisites and inclusion in Tutti's failover candidate pool |
| Worker | Supported command such as `cursor-agent`, `agy` or `ollama`, or a responding Ollama service on the dashboard |
| Test runner | Git and Python on a machine, or an authorized adb connection on a device |
| Android build | Java and the Android SDK manager on a machine |
| Windows UI | WSL connection or Windows shell |
| Device host | Android platform-tools on a machine |
| Cast sender | Android Cast probe, desktop Cast probe, or configured Cast sender role |
| Cast receiver | Android Cast capability probe or configured Cast receiver role |
| Presence source | Machine idle reading or Android screen or owner-activity reading |
| Energy meter | Measured CPU or GPU power on the dashboard |
| Notification target | Reserved for a verified notification integration |
| Dashboard viewer | Desktop browser detected by the fleet probe |
| Storage/cache | Free work disk space at or above the configured threshold |
| CI runner | Git and Python on a machine |

The dashboard recomputes assignments every regular fleet probe, including daily while the dashboard is running. The
Android Cast probe refreshes its own cached hardware and software facts daily. The terminal command runs the regular
machine and Android probes on demand. Service and power evidence is collected by the dashboard's separate energy probe.

Assignments describe capability. Dispatch still checks quiet hours, owner use and control switches, battery, heat, and
free slots before starting work. The normal host election and worker configuration govern serving and spending. Targets
without a supported probe, such as standalone AirPlay speakers and accessories, need an integration before they can appear
with verified roles.

On an eligible Linux x86-64 machine, an administrator can click **Install Android platform-tools** for the device-host role.
This uses Tutti's existing checksum-pinned, user-space installer. The next probe verifies the install. Other exclusions
show their missing prerequisites. External devices and integrations may require setup on the device itself.

## Wear OS

Choose **Pair a watch** and use the six-digit pairing code from the watch's Wireless debugging screen. Tutti detects Wear OS
during pairing. The watch uses the phone's daytime prep preset, with 30% brightness, and sets brightness and volume to zero
at night. Its default run policy requires charging, Wi-Fi, an inactive screen, at least 70% battery and a battery
temperature below 35 °C.
