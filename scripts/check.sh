#!/usr/bin/env bash
# check.sh: everything CI runs, in one go: shellcheck, ruff (when installed), the unit tests, the dashboard's browser tests and a CLI smoke test.
set -euo pipefail
cd "$(dirname "$0")/.."

export TUTTI_TELEMETRY=off   # the smoke runs below are not usage: record nothing
export TUTTI_OFFLINE=1      # nothing here asks GitHub (the releases-stuck check, tutti/stuck.py)

# Create and activate a virtual environment to ensure Playwright is available.
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate

# Install Playwright and its dependencies into the virtual environment.
pip install --quiet playwright
python -m playwright install --with-deps chromium > /dev/null || python -m playwright install chromium > /dev/null

shellcheck scripts/*.sh install/*.sh
if command -v ruff > /dev/null; then ruff check . bin/tutti; else echo "ruff is not installed here: skipped (CI runs it)"; fi

# its own temp folder, removed at the end: a test's leftovers never reach /tmp (tests/run.py fails a test that leaves any)
TMPDIR="$(mktemp -d)"
export TMPDIR
trap 'rm -rf "$TMPDIR"' EXIT

XDG_CONFIG_HOME="$(mktemp -d)" python tests/run.py   # never the owner's grok.env

# the dashboard in a real browser, every tab: layout, accessibility, roles (docs/TESTING.md, *Browser tests*). A hard gate:
# Playwright and axe come from requirements-dev.txt; the browser is fetched once (on CI with its system libraries).
if [ -n "${CI:-}" ]; then python -m playwright install --with-deps chromium > /dev/null; else python -m playwright install chromium > /dev/null; fi

XDG_CONFIG_HOME="$(mktemp -d)" PYTHONPATH=tests python -m unittest discover -s tests/e2e

# smoke: the example config is valid and the commands that need no machine answer
t="python bin/tutti --config tutti.config.example.yaml"
$t config
$t usage --at 2026-01-08T00:00Z > /dev/null
$t usage page-script cursor > /dev/null
$t run cursor "smoke" --show > /dev/null
$t wait file "$0" --timeout 1
$t ship "chore: smoke" --dry-run --allow-protected > /dev/null
$t dashboard --once --no-probe > /dev/null
$t conduct --show --main cursor "smoke" > /dev/null
$t main > /dev/null
$t connect --status > /dev/null
$t mcp < /dev/null
$t user > /dev/null
$t household status > /dev/null
$t auth status > /dev/null
$t runs > /dev/null
$t move --status > /dev/null
$t bridge status > /dev/null
$t backlog list > /dev/null
$t telemetry status > /dev/null
$t telemetry report > /dev/null
$t selfimprove > /dev/null
$t ai-assist > /dev/null
$t needs > /dev/null
$t autopilot > /dev/null
$t pr --help > /dev/null
$t scout list > /dev/null
$t scout setup > /dev/null
$t scout trial > /dev/null
$t setup --help > /dev/null
$t self-update --help > /dev/null

echo "check: all passed"
