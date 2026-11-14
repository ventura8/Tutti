# Changelog

The format follows Keep a Changelog; versions follow Semantic Versioning.

## 0.1.0 (unreleased)

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
- `tutti usage`: quota windows, the "spend it 3 hours before the reset" target and a verdict per account.
- `tutti workers`, `tutti run`: CLI workers (Cursor, Antigravity, Claude) with a generated brief.
- `tutti ask`, `tutti code`, `tutti agent`: local models through Ollama's HTTP API.
- `tutti yield`, `tutti machines`, `tutti guard`, `tutti tunnel`, `tutti quiet`, `tutti wait`.
- `tutti translate`: translation of flat JSON catalogues with mandatory QA and an import gate.
- Docs: architecture, workers, usage pacing, machines, lessons, roadmap, feedback log, migration.

- One-line installers and uninstallers for Linux/WSL/macOS (`install/install.sh`) and Windows (`install/install.ps1`).
