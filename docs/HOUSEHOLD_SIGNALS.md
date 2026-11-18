# Household and small-office signals audit

This document provides an inventory of signals detectable by Tutti, the detection methods, and the approach for aggregation and interpretation.

## Signals and Detection Methods

| Signal | Method of Detection |
| --- | --- |
| Hosts/Machines | `tutti/mainmachine.py` and `tutti/machines.py` monitor local and shared machine availability via Tutti's heartbeat mechanism. |
| Devices | `tutti/devices.py` lists connected devices; `tutti/adbhost.py` and `tutti/adbpair.py` manage Android/ADB devices. |
| Power/UPS | `tutti/power.py` reads power/UPS status from system-level interfaces. |
| Network/VPN | `tutti/vpn.py` and `tutti/netpolicy.py` manage VPN status (WireGuard/Tailscale) and network access policies. |
| Presence | `tutti/presence.py` infers presence based on machine heartbeats and client activity within the household/small office. |
| Calendars/Work Hours | `tutti/hours.py` tracks working hours based on user configuration and agent activity. |
| Quiet Hours | `tutti/quietmode.py` detects quiet mode status based on user configuration and activity. |
| Temperatures | `tutti/temps.py` reads thermal information (if available/accessible). |
| Disk/Memory | `tutti/diskroom.py` monitors disk space and memory usage of managed machines. |
| Printers/NAS/Routers | Managed as devices via `tutti/devices.py`. |
| Accounts | `tutti/auth.py` and `tutti/users.py` track user account status and activity. |

## Aggregation and Interpretation Plan

Signals are aggregated by the household coordinator (`tutti/coordinator.py`, `tutti/state.py`). The coordinator maintains the collective state, per-member activity, and machine utilization. Interpretation relies on the per-member 7x24 activity profile and demand patterns to allocate resources (slots, shared machines) effectively and prioritize interactive work over background tasks.

## Privacy and Security Considerations

Privacy and security are foundational to signal handling:
- **No Private Data Transmitted:** Only aggregated counts, machine usage, and status are shared; prompts, briefs, file content, and account names are never transmitted.
- **Signed Requests:** Every household request (`X-Tutti-Sign`) is signed with a member token.
- **Isolated Compute:** Shared compute runs in sandboxed containers (`tutti/hhjobs.py`) on the owner's machine without access to their files, tokens, or network, unless explicitly configured otherwise.
- **Network Boundary:** The coordinator only operates on LAN or VPN (RFC 1918, CGNAT, etc.), never directly exposed to the internet (`tutti/netpolicy.py`).
