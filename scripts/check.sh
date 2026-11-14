#!/usr/bin/env bash
# check.sh: everything CI runs, in one go: shellcheck, ruff (when installed), the unit tests and a CLI smoke test.
set -euo pipefail
cd "$(dirname "$0")/.."
shellcheck scripts/*.sh
if command -v ruff > /dev/null; then ruff check . bin/tutti; else echo "ruff is not installed here: skipped (CI runs it)"; fi
python3 -m unittest discover -s tests
# smoke: the example config is valid and the commands that need no machine answer
t="python3 bin/tutti --config tutti.config.example.yaml"
$t config
$t usage --at 2026-01-08T00:00Z > /dev/null
$t run cursor "smoke" --show > /dev/null
$t wait file "$0" --timeout 1
$t ship "chore: smoke" --dry-run --allow-protected > /dev/null
$t dashboard --once --no-probe > /dev/null
echo "check: all passed"
