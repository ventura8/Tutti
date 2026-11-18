#!/usr/bin/env bash
set -e
t="python3 bin/tutti --config tutti.config.example.yaml"
echo "Running smoke tests..."
$t config
$t usage --at 2026-01-08T00:00Z > /dev/null
echo "Usage ok"
$t usage page-script cursor > /dev/null
echo "Usage page ok"
# $t run cursor "smoke" --show > /dev/null # This might take too long or require more setup
echo "Run skipped"
# $t wait file "scripts/check.sh" --timeout 1
echo "Wait skipped"
# $t ship "chore: smoke" --dry-run --allow-protected > /dev/null
echo "Ship skipped"
# $t dashboard --once --no-probe > /dev/null
echo "Dashboard skipped"
# $t conduct --show --main cursor "smoke" > /dev/null
echo "Conduct skipped"
$t main > /dev/null
echo "Main ok"
$t connect --status > /dev/null
echo "Connect ok"
# $t mcp < /dev/null
echo "MCP skipped"
$t user > /dev/null
echo "User ok"
$t household status > /dev/null
echo "Household ok"
$t auth status > /dev/null
echo "Auth ok"
$t runs > /dev/null
echo "Runs ok"
$t move --status > /dev/null
echo "Move ok"
$t bridge status > /dev/null
echo "Bridge ok"
$t backlog list > /dev/null
echo "Backlog ok"
$t telemetry status > /dev/null
echo "Telemetry status ok"
$t telemetry report > /dev/null
echo "Telemetry report ok"
$t selfimprove > /dev/null
echo "Selfimprove ok"
# $t ai-assist > /dev/null
echo "AI-assist skipped"
$t welcome > /dev/null
echo "Welcome ok"
$t needs > /dev/null
echo "Needs ok"
$t autopilot > /dev/null
echo "Autopilot ok"
$t pr --help > /dev/null
echo "PR help ok"
$t scout list > /dev/null
echo "Scout list ok"
$t scout setup > /dev/null
echo "Scout setup ok"
$t scout trial > /dev/null
echo "Scout trial ok"
$t setup --help > /dev/null
echo "Setup help ok"
$t self-update --help > /dev/null
echo "Self-update help ok"
echo "Smoke tests passed"
