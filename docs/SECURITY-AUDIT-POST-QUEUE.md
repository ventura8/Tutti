# Security audit after the post-0.19 queue (t169)

A follow-up to [SECURITY-AUDIT-2026-11.md](SECURITY-AUDIT-2026-11.md) for the features that landed after that audit:
Google roles and invites, RoRadio / dispatch onboarding, all-work-through-Tutti (intake + session MCP tools),
resilience (failover + self-heal, t167), security intake (`depbots`), remote execution (ssh, WSL, Windows scheduled
tasks), secrets handling (`secretenv`), and self-update / CLI update. Severity labels match the November audit.
Findings below are new unless marked *confirmed*.

Tutti version audited: 0.23.x (worktree at the post-queue tip). The November audit's fixes remain in force; this
document only adds surfaces and residual risk.

## Threat model (extended)

Actors from the November model still apply. New or sharper surfaces:

| Actor | New reach |
| --- | --- |
| **The internet** | Dependabot / code-scanning / review-bot text becomes backlog notes (security intake). AGENTS.md intake snippet and public issue text still never become shell. |
| **The LAN or VPN** | Dashboard with Google sign-in (device flow on non-localhost); `tutti mcp --http` (viewer unless `mcp.role: admin`); coordinator as before. |
| **A signed-in viewer** | May look, report, answer their own schedule / main-machine / manage-self; never Settings, dispatch, update, pair, move, or queue admin actions. |
| **A signed-in admin / stdio MCP** | Full control of this household's Tutti: backlog `check`, leases, `test_on` commands, settings. Treated as the owner of this install. |
| **A worker CLI** | Reads task titles and notes (including intake and depbot text); runs headless with commands allowed in a worktree; no Tutti-owned tokens in its env (`workers.worker_env`, `secretenv`). |
| **Config / state on disk** | Whoever can edit `tutti.config.yaml` or the state folder is already admin-equivalent; fields that become shell (`desktop.user`, `desktop.before`, suite `run`, `total_command`) are trusted only from this login's config. |
| **Failover backup** | Holds a warm copy of state over the main's ssh; never needs ssh back; acts only as the highest term's active main (no split brain). |

Trust boundary: **untrusted text** (GitHub, Sentry, scouts, chat, another member, bot alerts) must never become a
command Tutti runs. **Trusted config** (this login's yaml) may name commands; it is still validated where it is
interpolated into a shell (hosts, Windows user names, MCP args that look like options).

## Module coverage (attack surfaces reviewed)

Grouped by attack surface. Each row was read against the threat model (code paths that bind, authenticate, run remote
commands, touch secrets, or ingest untrusted text). "Ok" means no new finding beyond residuals already accepted in the
November audit; it is not a formal proof that every line is free of bugs.

| Surface | Modules | Verdict |
| --- | --- | --- |
| Bind / net | `netpolicy`, `httpguard`, `dashboard`, `coordinator`, `mcpserver` | Ok (LAN/VPN only; caps; Origin; loopback owner on Linux) |
| Sign-in / roles | `auth`, `authweb`, `authdevice`, `authcmd`, `users`, `invites`, `official`, `manage` | Ok (default-deny; CSRF; last-admin; device-flow rate limit) |
| MCP / intake | `mcpserver`, `intake`, `claudework`, `connect` | Ok (mutating = admin; LAN HTTP = viewer; args cannot be options) |
| Security intake | `depbots` | Ok (branch names `shlex.quote`d in notes; repo `check` from local config only) |
| Dispatch / RoRadio | `dispatch`, `shards`, `prep`, `devclass`, `castroles`, `devices*`, `reroute`, `workq` | Ok (roles override is admin config; device prep restore is scripted) |
| Remote exec | `remote`, `local`, `transfer`, `winsession`, `winhelpers`, `rsh` | **P1 fixed** (Windows `desktop.user` / `dir`); ssh never disables host keys; WSL packs via base64 |
| Secrets | `secretenv`, `scrub`, `groksetup`, `workers`, `move`/`movetools` | Ok (secrets not in environ; move copies login files unread) |
| Update / install | `update`, `setup`, `install`, release workflow | Ok (Tutti tarball Sigstore; npm CLI `--before` cooldown) |
| Resilience | `failover`, `selfheal`, `durable`, `statebudget`, `watchdog`, `recovery` | Ok (term fencing; corrupt state fail-closed; self-heal repairs are local allow-listed) |
| Autopilot / workers | `autopilot`, `fill`, `review`, `sharing`, `hhjobs`, `conduct`, `chat*` | Ok (November H2–H9 / M3–M7 still hold; chat backlog has no `check`) |
| Telemetry / privacy | `telemetry`, `scrub`, `dashtelemetry` | Ok (consent + DSN; scrubber) |

## Findings

| # | Severity | Where | Finding | Status |
| --- | --- | --- | --- | --- |
| P1 | medium | `winsession` | `desktop.user` (and a hostile `desktop.dir`) were concatenated into the WSL bash wrapper and the temp path. A value with `;` or `$()` would run as shell on the PC when a desktop suite started. Needs write access to config (admin), but config validation must refuse it the same way `machines.*.host` refuses a leading `-`. | **fixed** |
| P2 | low | MCP `backlog_add` | The tool still accepts `check`. A LAN MCP is viewer (cannot call it); stdio / loopback MCP is admin by design. Chat's `backlog_add` still carries no `check` (H9). | accepted: admin MCP is the owner |
| P3 | low | `jobprogress.total_of` | `findings.*.total_command` runs with `shell=True`; capture groups are `shlex.quote`d. The command string is only from this login's config. | accepted: trusted config |
| P4 | info | failover sync | State (hashed sessions, backlog, household) is rsynced to the backup over the main's ssh. Intentional for take-over; the backup is a trusted machine. | accepted |
| P5 | info | depbots / workers | Alert titles and review comments become task notes a worker reads. Same residual as H7: worktree + gates + review; no Tutti tokens in the worker env. | accepted (H7 residual) |

### What P1's fix does, and its test

`winsession.USER_RX` / `DIR_RX` use `\Z` (not `$`): a Windows login name is letters, digits, `.`, `_`, `-` only;
`desktop.dir` is a `X:\...` path without `&|<>"`, CR or LF. Python's `$` also matches before a trailing newline, so a
value like `ventu\n` would have passed `$` and still broken the bash path; `\Z` refuses it. `errors()` validates the
raw string (no `.strip()` that would hide the newline); `wrap()` raises `ValueError` if called with a bad value.
`tests/test_security_audit.py` (`DesktopSessionInjectTest`, including trailing `\n` / `\r`) and `tests/test_devclass.py`
play a hostile user and dir.

## Auth verification (dashboard, API, MCP)

- **Dashboard / API** (`authweb`): default-deny. Every GET needs an approved user except the public sign-in assets;
  every POST needs admin except `VIEWER_POST` (logout, telemetry, report, github, schedule, self main-machine, manage
  self). CSRF + Origin + Sec-Fetch-Site; sessions hashed, HttpOnly, SameSite=Strict. Confirmed against
  `ADMIN_PATHS` / `SELF_POSTS` in `dashboard.py` (Settings, restart, setup, pair, move, VPN write, queue, AI assist,
  machine-limit = admin).
- **MCP**: mutating tools need `admin`. `--http` on a non-loopback address defaults to `viewer`; foreign `Origin` is
  403; free text after `--`; name args may not start with `-`. Session tools (claim, lease, ask_owner, claude_task,
  outside_work) follow the same gate.
- **Coordinator**: unchanged from November (signed requests and answers, join takeover refused).

## Remote execution

- **ssh / WSL** (`remote.py`): argv list, no `shell=True`; host may not start with `-` or contain spaces (`config`);
  scripts for Windows/WSL travel base64-encoded so the Windows command line cannot mangle quotes; ControlMaster reuse
  is local-uid scoped; host-key checking stays on.
- **Windows scheduled tasks** (`winsession`, `winhelpers`): one-shot `/ru <user> /it` with no password (`/rp` never
  set); task name and temp folder removed on EXIT; presence/sensors helpers register Limited/Highest only for the
  logged-on user. P1 closes the user/dir injection gap.
- **Android / devices**: prep and release scripts run on the device's machine over the existing remote path; cast
  roles are probe output or admin `roles:` override, not remote shell from the device.

## Secrets and update

- **Secrets** (`secretenv`, t116): owned secrets are dropped from the process environment at start; long-running
  commands re-exec clean; point-of-use reads `~/.config/tutti/*.env` (mode 600). Workers keep
  `workers.NOT_FOR_WORKERS` stripped. Doctor reports names only, masked.
- **Self-update of Tutti**: Sigstore attestation on `SHA256SUMS` (H8); `--insecure-skip-verify` is loud and manual.
- **Worker CLI update** (`update.py`): npm installs use `--before` (3-day cooldown, M8); a busy binary is skipped;
  apt packages only print the owner's exact sudo command.

## Residuals the owner still owns

- `v*` tag ruleset so only the maintainer can cut a signed release (H8).
- Sign-in on shared macOS/Windows machines (H4 platform gap without `/proc/net/tcp`).
- Keep `self_improve.auto_accept` off for public projects if H7's residual is not enough.
- Reserve the npm name `tutti-agents` (M11).

## Scanners

Not re-run in this follow-up (orchestrator runs `tutti test` / `scripts/check.sh`). November results stand until the
next full scanner pass (bandit, pip-audit, gitleaks).
