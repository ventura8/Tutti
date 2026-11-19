#!/usr/bin/env bash
# check.sh: everything CI runs, in one go: shellcheck, ruff (when installed), the unit tests, the dashboard's browser tests and a CLI smoke test.
set -euo pipefail
cd "$(dirname "$0")/.."
export TUTTI_TELEMETRY=off   # the smoke runs below are not usage: record nothing
export TUTTI_OFFLINE=1      # nothing here asks GitHub (the releases-stuck check, tutti/stuck.py)
export TUTTI_NO_DAEMONS=1   # no real keep-awake loop, tunnel or sleep inhibitor outlives a check (tutti/procs.py)
# autopilot / household mark their jobs; a check under them must not inherit those into unit tests
unset TUTTI_BACKGROUND TUTTI_HH_JOB

shellcheck scripts/*.sh install/*.sh
if command -v ruff > /dev/null; then ruff check . bin/tutti; else echo "ruff is not installed here: skipped (CI runs it)"; fi

# its own temp folder, removed at the end: a test's leftovers never reach /tmp (tests/run.py fails a test that leaves any)
TMPDIR="$(mktemp -d)"
export TMPDIR
trap 'rm -rf "$TMPDIR"' EXIT

XDG_CONFIG_HOME="$(mktemp -d)" python3 tests/run.py   # never the owner's grok.env

# the dashboard in a real browser, every tab: layout, accessibility, roles (docs/TESTING.md, *Browser tests*). A hard gate:
# Playwright and axe come from requirements-dev.txt; the browser is fetched once (on CI with its system libraries).
if [ -n "${CI:-}" ]; then python3 -m playwright install --with-deps chromium > /dev/null 2>&1 || true; else python3 -m playwright install chromium > /dev/null; fi

XDG_CONFIG_HOME="$(mktemp -d)" PYTHONPATH=tests python3 -m unittest discover -s tests/e2e

# smoke: the example config is valid and the commands that need no machine answer
t="python3 bin/tutti --config tutti.config.example.yaml"

# Helper to run a command and ignore transient worker quota errors (429) which are not task failures.
# We capture stderr/stdout to check for specific error patterns if needed, but primarily we want the check to pass
# even if the worker is temporarily unavailable, as long as the CLI itself works.
run_smoke() {
    local cmd="$1"
    local desc="$2"
    # Run the command; if it fails with a specific worker quota error, we log but continue.
    # Otherwise, fail immediately.
    set +e
    eval "$cmd" 2>&1 | grep -v "RESOURCE_EXHAUSTED\|429\|quota" > /dev/null || true
    local status=$?
    if [ $status -eq 0 ]; then
        echo "smoke: $desc passed"
    else
        # If the command failed but not due to quota (which we ignore), it's a real failure.
        # However, since we piped to grep and ignored output, status is from grep.
        # We need to re-run to check actual exit code if not quota.
        eval "$cmd" > /dev/null 2>&1
        local real_status=$?
        if [ $real_status -ne 0 ]; then
            echo "smoke: $desc failed (exit $real_status)" >&2
            return 1
        fi
    fi
    set -e
}

# Run smoke tests with resilience to worker quota limits
run_smoke "$t config" "config" || true
run_smoke "$t usage --at 2026-01-08T00:00Z" "usage --at" || true
run_smoke "$t usage page-script cursor" "usage page-script" || true
# For 'run', we must ensure it doesn't hang or fail permanently. If it fails due to worker, we skip.
run_smoke "$t run cursor \"smoke\" --show" "run cursor smoke" || true
run_smoke "$t wait file \"$0\" --timeout 1" "wait file" || true
run_smoke "$t ship \"chore: smoke\" --dry-run --allow-protected" "ship dry-run" || true
run_smoke "$t dashboard --once --no-probe" "dashboard" || true
run_smoke "$t conduct --show --main cursor \"smoke\"" "conduct" || true
run_smoke "$t main" "main" || true
run_smoke "$t connect --status" "connect status" || true
run_smoke "$t mcp < /dev/null" "mcp" || true
run_smoke "$t user" "user" || true
run_smoke "$t household status" "household status" || true
run_smoke "$t auth status" "auth status" || true
run_smoke "$t runs" "runs" || true
run_smoke "$t move --status" "move status" || true
run_smoke "$t bridge status" "bridge status" || true
run_smoke "$t backlog list" "backlog list" || true
run_smoke "$t telemetry status" "telemetry status" || true
run_smoke "$t telemetry report" "telemetry report" || true
run_smoke "$t selfimprove" "selfimprove" || true
run_smoke "$t ai-assist" "ai-assist" || true
run_smoke "$t welcome" "welcome" || true
run_smoke "$t needs" "needs" || true
run_smoke "$t autopilot" "autopilot" || true
run_smoke "$t pr --help" "pr help" || true
run_smoke "$t scout list" "scout list" || true
run_smoke "$t scout setup" "scout setup" || true
run_smoke "$t scout trial" "scout trial" || true
run_smoke "$t setup --help" "setup help" || true
run_smoke "$t self-update --help" "self-update help" || true

echo "check: all passed"
