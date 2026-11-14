# Changelog

The format follows Keep a Changelog; versions follow Semantic Versioning.

## 0.1.1 (2026-11-14)

QA round 1 on Windows and Linux. The first tagged release.

### Fixed

- Windows: local runs keep the caller's stdin (the script travels base64-encoded to a temporary file, not piped into
  bash); `wslpath`/`cygpath` output is decoded as UTF-8.
- Windows: `tutti wait pid` reads the exit code (`GetExitCodeProcess`), so a retained handle no longer looks alive, and a
  denied `OpenProcess` counts as a live process.
- Windows installers: config paths are literal (`-LiteralPath`), so brackets in `XDG_CONFIG_HOME` work.
- Config discovery honours `$XDG_CONFIG_HOME/tutti/config.yaml` (then `tutti.config.yaml` there); a missing config
  names the exact path and the `cp` from the example. Installers say when `TUTTI_CONFIG` or a project config wins.
- Usage pacing: the default lead is 1 hour before the reset (`lead_hours` still overrides).
- `tutti workers`: an ollama worker is ready only when `/api/tags` lists its model (untagged means `:latest`); otherwise
  it says `OLLAMA_HOST=<host> ollama pull <model>`. A malformed answer is a DOWN row, not a crash.
- Ollama hosts may carry their own scheme (`https://proxy:443`).

## 0.1.0 (not tagged)

The first extraction of the toolkit from the project it grew in.

### Fixed

- Windows: `waiting.pid_gone` uses `OpenProcess` (not `/proc` or `os.kill(..., 0)`, which terminates on Win32); local busy
  markers use the same check.
- Windows: `tutti ship` puts `TUTTI_*` on the bash command line so WSL bash (which does not inherit the Windows process
  environment) still sees changed files and the commit for checks and deploy.
- Installers seed `~/.config/tutti/config.yaml` from the example when missing; final message says to edit that file then
  run `tutti config; tutti workers` (not the not-yet-existing `setup` / `doctor`).
- `TUTTI_PURGE=1` removes `$XDG_CONFIG_HOME/tutti` or `~/.config/tutti` on Windows too (not `%APPDATA%\tutti`).

### Added

- `tutti test`: fall-over routing over machines with slot queues; `ssh`, `wsl` and `local` transports; a capped Docker
  runner or plain bash.
- `tutti ship`: format, commit, targeted or full checks (local or routed), push, deploy; refuses the wrong worktree and
  protected branches; `--dry-run`.
- `tutti usage`: quota windows, the "spend it 1 hour before the reset" target (override with `lead_hours`) and a verdict
  per account.
- `tutti workers`, `tutti run`: CLI workers (Cursor, Antigravity, Claude) with a generated brief; ollama readiness checks
  that `GET /api/tags` lists the configured model.
- `tutti ask`, `tutti code`, `tutti agent`: local models through Ollama's HTTP API.
- Config discovery: `$TUTTI_CONFIG`, then nearest `tutti.config.yaml`, then `$XDG_CONFIG_HOME/tutti/config.yaml` (with
  `tutti.config.yaml` there as a fallback); a missing config names the exact path and the `cp` from the example.
- `tutti yield`, `tutti machines`, `tutti guard`, `tutti tunnel`, `tutti quiet`, `tutti wait`.
- `tutti translate`: translation of flat JSON catalogues with mandatory QA and an import gate.
- Docs: architecture, workers, usage pacing, machines, lessons, roadmap, feedback log, migration.

- One-line installers and uninstallers for Linux/WSL/macOS (`install/install.sh`) and Windows (`install/install.ps1`).
