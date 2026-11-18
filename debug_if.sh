#!/usr/bin/env bash
if ! python3 -c 'import playwright, axe_playwright_python' 2> /dev/null || ! command -v shellcheck > /dev/null; then
    echo "inside if"
else
    echo "outside if"
fi
