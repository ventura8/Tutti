# Contributing

Thank you for helping. Tutti is small on purpose; changes that keep it small are the easiest to accept.

1. **Start from a problem.** Add a row to `docs/FEEDBACK.md` (or open an issue once the repository is public): what
   you ran, what happened, what you expected.
2. **Keep it general.** No real hostnames, names, paths or accounts; examples use `*.example`. No secrets.
3. **Make the change** with a test for the good case and a bad one, and update the docs it touches and `CHANGELOG.md`.
4. **Run the checks:** `bash scripts/check.sh` (needs `shellcheck`, Python 3.10+ and `pip install -r requirements-dev.txt`).
5. **Commit** with a Conventional Commit message (`feat(test): ...`, `fix(usage): ...`, `docs: ...`).

Agents follow `AGENTS.md`. By contributing you agree that your work is released under the repository's licence (see
the note in the README: the licence is chosen before the first publish).
