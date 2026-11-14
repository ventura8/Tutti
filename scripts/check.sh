#!/usr/bin/env bash
# check.sh: everything CI runs, in one go: shellcheck, ruff (when installed), the unit tests and a CLI smoke test.
set -euo pipefail
cd "$(dirname "$0")/.."
export TUTTI_TELEMETRY=off   # the smoke runs below are not usage: record nothing
shellcheck scripts/*.sh
if command -v ruff > /dev/null; then ruff check . bin/tutti; else echo "ruff is not installed here: skipped (CI runs it)"; fi
XDG_CONFIG_HOME="$(mktemp -d)" python3 -m unittest discover -s tests   # never the owner's grok.env
# smoke: the example config is valid and the commands that need no machine answer
t="python3 bin/tutti --config tutti.config.example.yaml"
$t config
$t usage --at 2026-01-08T00:00Z > /dev/null
$t usage page-script cursor > /dev/null
$t run cursor "smoke" --show > /dev/null
$t wait file "$0" --timeout 1
$t ship "chore: smoke" --dry-run --allow-protected > /dev/null
$t dashboard --once --no-probe > /dev/null
$t conduct --show --main cursor "smoke" > /dev/null
$t main > /dev/null
$t user > /dev/null
$t household status > /dev/null
$t auth status > /dev/null
$t runs > /dev/null
$t backlog list > /dev/null
$t telemetry status > /dev/null
$t telemetry report > /dev/null
$t selfimprove > /dev/null
$t autopilot > /dev/null
$t pr --help > /dev/null
$t scout list > /dev/null
$t scout setup > /dev/null
$t scout trial > /dev/null
echo "check: all passed"
