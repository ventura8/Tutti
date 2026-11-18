#!/usr/bin/env bash
# check-browser-install.sh: install Chromium and use existing system libraries when a real launch succeeds.
set -euo pipefail
python3 -m playwright install --only-shell chromium
if [ -n "${CI:-}" ]; then
	# Keep the probe bounded and do not pass unrelated environment variables to the browser.
	# timeout signals the process group; detached descendants may require separate cleanup.
	if ! env -i PATH="$PATH" HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" \
		timeout --kill-after=5s 20s python3 -c \
		'from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch()
    browser.close()'; then
		echo "check: Chromium launch probe failed; installing Playwright system dependencies"
		python3 -m playwright install --with-deps --only-shell chromium
	fi
fi
