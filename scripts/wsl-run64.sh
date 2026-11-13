#!/usr/bin/env bash
# wsl-run64.sh <base64 script>: decodes a bash script and runs it, stdin passed through. Install it INSIDE WSL on a Windows
# machine and name its path as machines.<name>.wsl_helper: Windows' ssh hands a line to its own shell and then to wsl.exe, so
# nothing survives quoting; base64 does.
[ $# -eq 1 ] || { echo "usage: wsl-run64.sh <base64 of a bash script>" >&2; exit 2; }
exec bash -c "$(printf '%s' "$1" | base64 -d)"
