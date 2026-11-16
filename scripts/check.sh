#!/usr/bin/env bash
# check.sh: everything CI runs, in one go: shellcheck, ruff (when installed), the unit tests, the dashboard's browser tests and a CLI smoke test.
set -euo pipefail
cd "$(dirname "$0")/.."

# Use the python from the local bin (where playwright is installed) if available, otherwise fallback to system python3.
if [ -n "${TUTTI_PYTHON:-}" ]; then
    PYTHON="$TUTTI_PYTHON"
elif command -v "$HOME/.local/bin/python3" > /dev/null 2>&1; then
    PYTHON="$HOME/.local/bin/python3"
else
    PYTHON="python3"
fi

export TUTTI_TELEMETRY=off   # the smoke runs below are not usage: record nothing
export TUTTI_OFFLINE=1      # nothing here asks GitHub (the releases-stuck check, tutti/stuck.py)

shellcheck scripts/*.sh install/*.sh
if command -v ruff > /dev/null; then ruff check . bin/tutti; else echo "ruff is not installed here: skipped (CI runs it)"; fi

# its own temp folder, removed at the end: a test's leftovers never reach /tmp (tests/run.py fails a test that leaves any)
TMPDIR="$(mktemp -d)"
export TMPDIR
trap 'rm -rf "$TMPDIR"' EXIT

XDG_CONFIG_HOME="$(mktemp -d)" "$PYTHON" tests/run.py   # never the owner's grok.env

# the dashboard in a real browser, every tab: layout, accessibility, roles (docs/TESTING.md, *Browser tests*). A hard gate:
# Playwright and axe come from requirements-dev.txt; the browser is fetched once (on CI with its system libraries).
if [ -n "${CI:-}" ]; then "$PYTHON" -m playwright install --with-deps chromium > /dev/null; else "$PYTHON" -m playwright install chromium > /dev/null; fi

XDG_CONFIG_HOME="$(mktemp -d)" PYTHONPATH=tests "$PYTHON" -m unittest discover -s tests/e2e

# smoke: the example config is valid and the commands that need no machine answer
declare -a tutti_commands=(
    "config"
    "usage --at 2026-01-08T00:00Z > /dev/null"
    "usage page-script cursor > /dev/null"
    "run cursor 'smoke' --show > /dev/null"
    "wait file \"$0\" --timeout 1"
    "ship 'chore: smoke' --dry-run --allow-protected > /dev/null"
    "dashboard --once --no-probe > /dev/null"
    "conduct --show --main cursor 'smoke' > /dev/null"
    "main > /dev/null"
    "connect --status > /dev/null"
    "mcp < /dev/null"
    "user > /dev/null"
    "household status > /dev/null"
    "auth status > /dev/null"
    "runs > /dev/null"
    "move --status > /dev/null"
    "bridge status > /dev/null"
    "backlog list > /dev/null"
    "telemetry status > /dev/null"
    "telemetry report > /dev/null"
    "selfimprove > /dev/null"
    "ai-assist > /dev/null"
    "welcome > /dev/null"
    "needs > /dev/null"
    "autopilot > /dev/null"
    "pr --help > /dev/null"
    "scout list > /dev/null"
    "scout setup > /dev/null"
    "scout trial > /dev/null"
    "setup --help > /dev/null"
    "self-update --help > /dev/null"
)

for cmd in "${tutti_commands[@]}"; do
    $PYTHON bin/tutti --config tutti.config.example.yaml "$cmd"
done
