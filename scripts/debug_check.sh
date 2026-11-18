#!/usr/bin/env bash
# check.sh: everything CI runs, in one go: shellcheck, ruff (when installed), the unit tests, the dashboard's browser tests and a CLI smoke test.
set -euo pipefail
cd "$(dirname "$0")/.."
export TUTTI_TELEMETRY=off   # the smoke runs below are not usage: record nothing
export TUTTI_OFFLINE=1      # nothing here asks GitHub (the releases-stuck check, tutti/stuck.py)
export TUTTI_NO_DAEMONS=1   # no real keep-awake loop, tunnel or sleep inhibitor outlives a check (tutti/procs.py)
# the dev tools (Playwright, axe, shellcheck) from requirements-dev.txt: when this python3 lacks them (a machine's system Python, a
# work-queue job), a venv of their own in the cache, installed with hashes and reused (t75/t76/t89, 2026-11-16)
if ! python3 -c 'import playwright, axe_playwright_python' 2> /dev/null || ! command -v shellcheck > /dev/null; then
	venv="${XDG_CACHE_HOME:-$HOME/.cache}/tutti/check-venv"
	[ -x "$venv/bin/python3" ] || python3 -m venv "$venv"
	stamp="$venv/.requirements-dev.sha256"   # pip runs only when requirements-dev.txt changed
	want="$(sha256sum requirements-dev.txt | cut -d' ' -f1)"
	if [ "$(cat "$stamp" 2> /dev/null)" != "$want" ]; then
		"$venv/bin/python3" -m pip install -q --require-hashes -r requirements-dev.txt
		echo "$want" > "$stamp"
	fi
	PATH="$venv/bin:$PATH"
	export PATH
fi
shellcheck scripts/*.sh install/*.sh
if command -v ruff > /dev/null; then ruff check . bin/tutti; else echo "ruff is not installed here: skipped (CI runs it)"; fi
# its own temp folder, removed at the end: a test's leftovers never reach /tmp (tests/run.py fails a test that leaves any)
TMPDIR="$(mktemp -d)"
export TMPDIR
trap 'rm -rf "$TMPDIR"' EXIT
XDG_CONFIG_HOME="$(mktemp -d)" python3 tests/run.py   # never the owner's grok.env
# the dashboard in a real browser, every tab: layout, accessibility, roles (docs/TESTING.md, *Browser tests*). A hard gate:
# Playwright and axe come from requirements-dev.txt; the browser is fetched once (on CI with its system libraries).
echo "check: installing Playwright's headless Chromium and system dependencies when needed"
bash scripts/check-browser-install.sh
echo "check: running dashboard browser tests"
XDG_CONFIG_HOME="$(mktemp -d)" PYTHONPATH=tests python3 -m unittest discover -s tests/e2e
# smoke: the example config is valid and the commands that need no machine answer
t="python3 bin/tutti --config tutti.config.example.yaml"
$t config
$t usage --at 2026-01-08T00:00Z
$t usage page-script cursor
$t run cursor "smoke" --show
$t wait file "$0" --timeout 1
$t ship "chore: smoke" --dry-run --allow-protected
$t dashboard --once --no-probe
$t conduct --show --main cursor "smoke"
$t main
$t connect --status
$t mcp < /dev/null
$t user
$t household status
$t auth status
$t runs
$t move --status
$t bridge status
$t backlog list
$t telemetry status
$t telemetry report
$t selfimprove
$t ai-assist
$t welcome
$t needs
$t autopilot
$t pr --help
$t scout list
$t scout setup
$t scout trial
$t setup --help
$t self-update --help
echo "check: all passed"
