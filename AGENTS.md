# Instructions for agents working on Tutti

Tutti is a small orchestration toolkit: a Python package (`tutti/`, standard library plus PyYAML) behind one command
(`bin/tutti`), and bash scripts that run on machines (`scripts/`). Read `README.md` and `docs/ARCHITECTURE.md` first.

## Commands

```bash
bash scripts/check.sh                       # shellcheck, ruff, unit tests, CLI smoke test: run it before saying done
python3 -m unittest discover -s tests       # the unit tests alone
python3 bin/tutti --config tutti.config.example.yaml config
```

## Rules

- **Nothing personal in the repository.** No real hostnames, user names, emails, project names, paths or account
  details, in code, docs, tests or examples. Hosts are `*.example`; everything real belongs in a user's own
  `tutti.config.yaml`, which is git-ignored here.
- **No secrets, ever.** Do not read `.env` or credential files. Tutti does not store, print or send credentials. The one
  exception is the owner's: `tutti move`, run by an admin, copies the agent tools' login files to the new host unread
  (docs/MOVE.md, *Logins*).
- **Config, not code.** A new host, model, worker or account is a config entry. If a change needs a name hard-coded,
  the design is wrong.
- **Say what happened, why, and the fix.** Every error message names the key or step, the cause and the next action.
- **Pure core, thin edge.** Routing, pacing and validation stay pure and unit-tested; I/O stays in `remote.py`,
  `transfer.py`, `local.py`, `ship.py` and the scripts.
- **No inline suppressions** (`# noqa`, `# type: ignore`, `# shellcheck disable=`). Fix the finding, or change the
  rule once in `ruff.toml` with a comment saying why.
- **Tests with behaviour.** A change in behaviour comes with a test for the good case and for a bad one.
- **Docs in the same change.** Update the README table, the matching file in `docs/` and `CHANGELOG.md`. A failure
  that taught something goes into `docs/LESSONS.md`; a user's problem into `docs/FEEDBACK.md`.
- **Style.** Python: 4 spaces, 140 columns, single quotes, no new dependencies without a reason in the pull request.
  Bash: tabs, `shellcheck` clean, a usage comment at the top. Conventional Commits.
- **Versions.** Tutti stays 0.x: a big feature is the next minor (0.N.0), a fix the next patch; take the next free
  number at merge time (CHANGELOG.md, top).
- **Tests clean up.** `tests/run.py` (what `scripts/check.sh` runs) gives each test its own temp folder and fails a test
  that leaves anything there: use `tmpdirs.mkdtemp()`, a `TemporaryDirectory` or `addCleanup`.
- **Pins.** GitHub Actions by full commit SHA with a version comment; Python tools with `==`.
- **Worktrees only.** The household's install tree (the checkout the live dashboard runs from) stays on `main`: never check
  out a branch there. Feature work happens in its own worktree (`git worktree add ../tutti-wt-<name> <branch>`).
- **Ask first:** pushing, tagging, publishing to npm, changing the licence, editing `.github/workflows/`.
- **Never** run a command against a real machine from a test; tests use the `local` transport and temp folders.
- Quiet mode: before a heavy local run (tests, docker, builds, a local model) run `tutti quiet status`; on exit 0 a person
  is at this computer or memory is short, so run it on another machine or `tutti quiet wait`, and wrap what must run here
  in `tutti quiet <cmd>` (docs/ARCHITECTURE.md, *Quiet mode*).
