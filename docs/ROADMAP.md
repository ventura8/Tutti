# Roadmap

v0.1.0 is the extraction of one project's working toolkit. The backlog is fed by [FEEDBACK.md](FEEDBACK.md).

## Next (0.2)

- `tutti test`: a full run on a `wsl` machine verified end to end (the probe is verified; the copy and run are ported
  but not yet exercised by a second user).
- `tutti usage`: readers for accounts that expose usage (a command or an API), so fewer readings are typed.
- `tutti translate`: the rest of the original pipeline: several streams sharing one queue (`flock` claims), a second
  judge on a larger model for strings the first judge doubted, a per-locale glossary and register, locale punctuation
  fixes, catalogue formats beyond flat JSON.
- `tutti worktree add|remove <track>`: create and clean worktrees, so a brief can name one.
- `tutti lock <name> -- <cmd>`: named locks for devices and other things peers share.
- A `limits` section in the config that renders the shared limits file for peers.

## Later

- Heat guard (CPU and GPU temperature) beside the memory guard.
- A machine-readable `--json` output for `machines`, `usage` and `workers`.
- macOS support for the orchestrator's side (the machines stay Linux).
- Publishing: choose the licence, flip `private` in `package.json`, release on npm as `tutti-agents`.

## Not planned

- A daemon, a web UI or a hosted service. State stays in files.
- Storing or brokering credentials of any kind.

## Worker selection by task (owner, 2026-11-14)
`tutti brief` should classify a task (bounded / multi-file / cross-platform design / security / judgement) and pick the worker: local model or agy/cursor for bounded work, Claude (default model; Fable only on explicit owner opt-in) for design and judgement, splitting design-first tasks into cheap sub-tasks. It also sets the effort: low by default, medium for multi-file work, high for design, security or hard debugging (owner, 2026-11-14). A UI such as Orca or Emdash only hosts the sessions; it does not decide who works.

## `tutti setup`: a wizard that does everything (owner, 2026-11-14, top priority)
Minimum input from the user. `tutti setup` detects and installs what is missing and asks only for the steps a person must do (sign-ins), one at a time, with the exact command or click:
- Workers: install/verify Cursor CLI (`cursor-agent`), Google Antigravity (`agy`), Ollama + the best local coding/translation models that fit the GPU; run each one's login and verify with a 1-line test prompt.
- Permissions: write the allow-rules each tool needs, merged safely (never replacing a file): Claude Code `permissions.allow` for `cursor-agent`/`agy`; Antigravity `permissions.allow` for the read-only commands it needs headless (and detect the exact command it was denied from its `cli.log`, then offer that rule). Show the diff and ask once.
- Machines: add test boxes over SSH (copy the key with the user's OK), detect cores/memory/GPU/NPU, set slot counts, install Docker/WSL where missing.
- Accounts and pacing: read plan limits and reset times where an API exists (Claude `get_usage`), ask once for the others (Cursor, Antigravity) and store them.
- Peers: discover other sessions (Claude Code cross-session messaging) and the shared-machine yield flag.
- Re-runnable and idempotent; `tutti doctor` checks the same things and prints one fix line per problem.
Lessons that drove it: LESSONS.md (Gemini CLI free tier closed → agy; agy headless needs per-command allow rules; Claude Code needs allow rules to start other CLIs).

## An excellent TUI now, a GUI next (owner, 2026-11-14)
- **TUI (`tutti` with no arguments, or `tutti ui`)**: one live screen — workers (cursor/agy/local/Claude sessions) with their task and state, machines with slots/load/heat/GPU, the test queue, accounts with % used and the pace line to "1 h before reset", peers and yield flags, recent ship/test verdicts; keys to start/stop a task, open its log, review its diff, yield a machine. Keyboard-first, mouse optional, works over SSH, colour + no-colour, narrow terminals. Candidate library: Textual (Python, MIT) — fits the Python CLI.
- **GUI (after the TUI; planned right after the 2026-11-14 reset)**: the same model served locally (web UI on localhost, phone-friendly), sharing the TUI's data layer; evaluate embedding or integrating Orca/Emdash for the worktree/session view instead of rebuilding it (docs/COMPETITORS.md).

## Continuous pacing after every reset (owner, 2026-11-14)
`tutti pace` runs on its own (a timer or the TUI loop) from each account's reset: target = a steady, even burn so every quota hits ~100 % exactly 1 h before its next reset (Claude: Saturday 03:00 local; Cursor/agy: their monthly days). Every check compares actual % with the straight line and acts: behind → start queued work, more parallel workers, raise worker effort where useful; ahead → pause low-priority agents, move work to cheaper workers or local models, lower effort. It shares the plan with peer sessions (50/50 Claude split with the second project) and logs each decision. It never stalls work before the target and never leaves quota unused at the reset.
- Antigravity ($20 plan) has rolling 5-hour limits, two separate ones: Claude models and Gemini models (owner, 2026-11-14). `tutti pace` models each as its own window (5 h, rolling), routes agy work to whichever model family has room (`agy --model`), and paces both so neither sits unused for long; accounts can have several windows (weekly, monthly, 5 h) and pacing respects all of them.

## Sharing with another agent session, the easy way (owner, 2026-11-14)
`tutti peers`: find other Claude Code sessions (cross-session messaging / Remote Control), pair once, then share plans, quota splits, machine yields and QA findings as structured messages instead of hand-written ones: `tutti share plan|usage|finding|yield`, an inbox in the TUI, and GitHub issues/PRs as the durable channel. Modern defaults throughout (2026): zero-config discovery, one-line installs, sane defaults, no copy-paste setups, JSON output for every command, shell completions, `--help` with examples.
- Effort coordination (owner, 2026-11-14): every task Tutti starts gets an effort level chosen by task type (low default; medium multi-file/verification; high design/security/hard debugging; higher only on request), adjusted by pacing (behind the line → higher effort where it improves results; ahead → lower), and shared with peers so both sessions follow the same policy. Logged per task.

## Backlog gathered from the first project's history (before the name "Tutti", 2026-11-12..14)
- Machine guards: memory guard (pause containers under a free-memory floor), heat guard (NUC hit 104 °C: lower slots when hot), per-machine slot counts changed safely (queued waiters keep the old count), never an uncapped container.
- Test routing: remote slots with fall-over to the next machine, a priority lane for urgent work (the "third slot" case) that an agent may use without tripping safety checks, idle-machine discovery.
- Waiting: never `pgrep -f`/`pkill -f` from a shell holding the pattern; wait on PIDs, done-files or log lines, always with a timeout (three agents stalled for hours).
- Stall detection: flag agents idle while "waiting" on a run that no longer exists; nudge or restart them.
- Ship: commit/push from the caller's worktree, fast-forward only, never deploy an older commit; conflict-marker guard before any push.
- Translation: the full ltrans pipeline (two translation streams, laptop + GPU QA, judge2, glossary, CJK sentence rules, owner-approved exceptions, a yield file for another project's GPU use) instead of the lean port.
- Shared GPU etiquette: the other project's flag pauses our model use and workers; unload models on request.
- Remote machines run by another agent: ask that agent (peer message) for admin actions instead of the user.
- Browser agents: check sign-in state themselves, never sign in, open tabs from a signed-in tab, close what they opened.
- Logins helper: one-liners to give the user for each CLI login and permission rule, verified afterwards.
- Best local models for coding and translation that fit each GPU, not the cheapest; GPU/NPU for all vendors.
- Adopt from the competitor review: Claude Code cross-session messaging for peers, `ccusage` as a usage reader, an Orca/Emdash trial for the session UI, Jules as a worker if the plan includes it, Cline/Kilo/Aider + Ollama to replace the hand-written local agent loop.
- Living plan: keep every request in a plan file and update it continuously; memory notes for standing rules.
- Stall watchdog (owner, 2026-11-14): `tutti watch` checks every running task (agents, CLI workers, remote test runs, deploys) on a timer: is it still making progress (output, tool calls, CPU, log lines)? Finished but unreported → collect the result and report; waiting on something gone (killed run, self-matching wait loop, lost container) → restart the wait or the step; really stuck or failing → stop it, record why, retry once or escalate to the orchestrator with the exact cause. Shown in the TUI with the last sign of life per task.
- Quota panic mode (owner, 2026-11-14): when an account nears its cap well before the target time (e.g. Claude at 98–99 % with hours left), Tutti switches on its own: no new agents on that account, running ones told to commit work-in-progress and stop or finish minimally, short replies, effort low, and all new work routed to accounts with room (Cursor, Antigravity's two 5 h windows) and local models; peers are told at once; it switches back automatically at the reset and resumes the parked work from the plan. Work never stops: at 1 % left on the main agent (default, configurable) Tutti keeps every bounded task flowing to Cursor, agy and local models (owner, 2026-11-14).

## Owner's product vision, 2026-11-14 (night)
**Panic threshold.** Panic mode starts at 3 % remaining by default (configurable). The threshold rises dynamically when consumption runs ahead of the line: Tutti computes it from the burn rate and the time left to the reset, so it slows down early instead of hitting the cap.

**Steady burn.** Target an even ~14 % of the Claude week per day after each reset (100 % / 7), corrected in real time; recompute the line when the user changes the main agent (each agent has its own quota and burn).

**Web app (PWA, no native apps for now)**, Material Design, following the OS light/dark theme and accent colour:
- Left menu panel: main window, projects, dashboards, tasks, agents, devices, live plan, terminal, settings (and anything else needed, above projects).
- Main window: a vibe-coding workspace for 2026 — tasks, a chat with the main agent, the live context.
- Dashboards with everything: usage and limits per account/window, pace line, task history (what, who, effort, result, tokens), running tasks and their worker, devices and their load.
- Context panel: the main agent's context use, compaction state and quota.
- A terminal view, and a view of the live plan.
- GUI setup wizard (the user chooses TUI or GUI setup); easy "connect a device" (local AI and non-AI work) and "connect an agent" (Claude Code, agy, Cursor, …) flows.

**Main agent.** The user chooses the main agent: Claude Code (default), Cursor or agy. Switching shares the context through Tutti's own state (plan, task history, memory), so no context is copied or re-explained between agents and switching costs no extra tokens.

**Token savings.** Tutti manages compaction for every agent at the right moment (before quality drops, and when cheapest); uses the MCP servers that save tokens; keeps the main agent lean by delegating.

**Goal.** The best IDE and the best framework for coordinating AI and local agents and machines in 2026.

**Quality.** Excellent e2e tests for the PWA on Windows (with and without WSL) and on Linux; works perfectly on both.
- Pause controls (owner, 2026-11-14): pause/resume a worker, a device or machine (until resumed, for 1 h, until tomorrow; the job goes back to the queue without losing work), and pause using an online agent account (Claude, Cursor, agy) — routing and pacing skip it until resumed.
- Android support: the PWA installs and works fully on Android phones and tablets (as used today: Pixel phone and tablet), with e2e tests on Android Chrome; Android devices can also be connected as devices for non-AI work.
- Browser work (owner, 2026-11-14): Tutti drives the user's signed-in browser as we do today (Chrome DevTools MCP / Claude in Chrome / the built-in browser pane): browser tasks go to small dedicated agents, which check the sign-in state themselves, never sign in or type credentials, open tabs from a signed-in tab, leave consent/payment clicks to the user, and close what they opened; the app shows browser tasks and their screenshots in the task history.

## macOS release, project website, telemetry (owner, 2026-11-14)
- **macOS**: release for Mac too. We have no Mac, so test on GitHub Actions' macOS runners (free for public repos; Apple Silicon and Intel): install one-liner, CLI smoke, unit tests, the PWA in Safari/WebKit via Playwright, uninstall/reinstall; mark any untested path clearly.
- **Website**: a project page on GitHub Pages (tutti …github.io) with what it does, the banner, screenshots, and download/install buttons per OS (Windows, macOS, Linux, Android PWA) auto-detecting the visitor's OS; docs linked.
- **Telemetry to improve Tutti**: opt-in, privacy-first analytics (installs, OS/arch, setup steps reached, which workers connect, failures by step with a reason code) and Sentry crash reporting for the CLI, the TUI and the PWA; no code, prompts, tokens, paths or personal data ever leave the machine; a clear first-run choice and `tutti telemetry off`. Extra detail on macOS (install steps, Homebrew/Python presence, Gatekeeper/quarantine, shell, Apple Silicon vs Intel) until it works as well as elsewhere.
- One install, both parts (owner, 2026-11-14): the one-line installer also installs the PWA — it starts the local service, then opens the app's URL in the default Chromium browser in install mode (`--app=` / a first-run page with the browser's install prompt), creates a desktop/start-menu shortcut that launches the app window (Windows: Start menu + desktop .lnk to `msedge --app`/`chrome --app`; Linux: a .desktop entry; macOS: an app shortcut via Chrome's `--app` or Safari "Add to Dock" guidance), and registers the service to start at login. Uninstall removes the shortcut too. Mobile devices install the PWA from the same page by scanning a QR code shown on the desktop.
- The chat (owner, 2026-11-14): a prompt queue (queue, reorder, edit or delete prompts not yet sent; send now; send later at a time or when a condition holds — e.g. after the reset, when a task finishes); switch the main agent and the effort level per message or per conversation; and everything a 2026 AI IDE chat needs: @-mentions of files, tasks, agents and machines; attachments and screenshots; slash commands; branch/fork a conversation; message history with search; diff previews and apply/reject; inline task cards with live status; voice input; per-message cost and tokens; stop/retry/regenerate; drafts kept across sessions; keyboard-first, works on phone.
- Reset moments are user-editable (owner, 2026-11-14): for every account and window (weekly, monthly, rolling 5 h) the user can set or change the reset day/time and time zone in Settings, the TUI and `tutti usage set-reset <account> <when>`; auto-detected values (e.g. Claude's get_usage) are shown as suggestions the user can override; pacing, panic thresholds and the daily target recompute at once.
