#!/bin/bash
set -u
t="python3 bin/tutti --config tutti.config.example.yaml"
commands=(
  "$t config"
  "$t usage --at 2026-01-08T00:00Z"
  "$t usage page-script cursor"
  "$t run cursor 'smoke' --show"
  "$t wait file scripts/check.sh --timeout 1"
  "$t ship 'chore: smoke' --dry-run --allow-protected"
  "$t dashboard --once --no-probe"
  "$t conduct --show --main cursor 'smoke'"
  "$t main"
  "$t connect --status"
  "$t mcp"
  "$t user"
  "$t household status"
  "$t auth status"
  "$t runs"
  "$t move --status"
  "$t bridge status"
  "$t backlog list"
  "$t telemetry status"
  "$t telemetry report"
  "$t selfimprove"
  "$t ai-assist"
  "$t welcome"
  "$t needs"
  "$t autopilot"
  "$t pr --help"
  "$t scout list"
  "$t scout setup"
  "$t scout trial"
  "$t setup --help"
  "$t self-update --help"
)

for cmd in "${commands[@]}"; do
  echo "Running: $cmd"
  eval "$cmd" > /dev/null
  if [ $? -ne 0 ]; then
    echo "FAILED: $cmd"
    exit 1
  fi
done
echo "All passed"
