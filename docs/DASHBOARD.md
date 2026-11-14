# The dashboard

`tutti dashboard` serves a local web dashboard at `http://127.0.0.1:4777/` (`--port` or `dashboard.port` to change
it). It binds to 127.0.0.1 only and refuses requests whose `Host` is not a loopback name with that port, so another
site cannot read it through DNS rebinding. Python standard library only: no build step, no CDN, no data leaves the
computer.

```bash
tutti dashboard                    # serve until Ctrl+C
tutti dashboard --port 4790        # another port
tutti dashboard --once             # print /api/state as JSON and stop (--no-probe: skip ssh and model servers)
```

Start it in the background with
`mkdir -p ~/.local/state/tutti && nohup tutti dashboard > ~/.local/state/tutti/dashboard.log 2>&1 &`.

## Install it as an app

Open the page in Chrome or Edge and choose **Install app** (the button in the bar, or the browser's install icon). It
opens in its own window and its shell works offline; when the service is not running the page says so and reconnects
by itself once `tutti dashboard` runs again.

## What it shows, and where each part comes from

| Panel | Source |
| --- | --- |
| Usage & pacing | `accounts:` in the config and the usage log (`tutti usage log`). Per account: % used, the even-pace mark (what should be spent by now), the panic threshold (`panic_at`, default 95 %), the reset and target times, the readings of this window against the pace line, and what to do. |
| Machines | The same probe as `tutti machines`: free and busy slots, load against cores, routing rank, paused (yield flag up), offline. |
| Workers | The same check as `tutti workers`, plus the known CLI agents on this computer (Claude Code, Cursor, Antigravity, GitHub Copilot CLI, Gemini CLI, Ollama): installed or not, and whether a worker uses them. |
| Jobs | `<project.state>`: `quiet/*.log` (`tutti quiet`), `ship-*.log` (quiet ship checks), `waits/*.json` (`tutti wait`), `llm-busy-*` (local-model jobs) and `tunnel-*.pid`. Running, **stalled** (its log has not changed for `dashboard.stall_minutes`), failed (with its exit code), done, or ended (a run from before 0.1.4, whose result was not recorded). Each opens to the end of its log. |

Machines and workers are probed in the background every `dashboard.probe_seconds` (ssh answers can be slow); the
rest is read on each request. The page polls `/api/state` every `dashboard.refresh_seconds` and pauses while hidden.

## Quota panic

An account is in panic when its latest reading is at or over `panic_at` while the target time (the reset minus
`lead_hours`) is still ahead. Then nothing new goes to it; bounded work moves to accounts with room and local models.
`tutti usage` shows the same.

## Look and accessibility

Material 3 tokens over the OS light or dark scheme. The accent is the OS accent colour where the browser shares it
(CSS `AccentColor`); otherwise Tutti's indigo, and the footer says which. The accent only paints indicators and tonal
backgrounds; text stays on the neutral colours, so any accent keeps its contrast. Keyboard: Tab through everything,
`R` refreshes, `1`–`5` pick the job filter. Layout from a phone to 4K.

## `/api/state`

`version`, `generated_at`, `config`, `refresh_seconds`, `usage` (`log`, `problem`, `accounts[]`), `machines`
(`probed_at`, `items[]`), `workers` (`probed_at`, `items[]`, `agents[]`), `jobs` (`folder`, `stall_seconds`, `items[]`,
`total`), `summary` and `probe_error`. `GET /api/health` answers `{"ok": true}`.
