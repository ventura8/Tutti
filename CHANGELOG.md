# Changelog

The format follows Keep a Changelog; versions follow Semantic Versioning.

## 0.1.0 (unreleased)

The first extraction of the toolkit from the project it grew in.

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
