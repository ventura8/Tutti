#!/usr/bin/env bash
# Tutti uninstaller: curl -fsSL https://raw.githubusercontent.com/ventura8/Tutti/main/install/uninstall.sh | bash
# Removes the `tutti` command and its install folder. Your config (~/.config/tutti) is kept unless TUTTI_PURGE=1.
set -euo pipefail
home="${TUTTI_HOME:-$HOME/.local/share/tutti}"
bin="${TUTTI_BIN:-$HOME/.local/bin}"
rm -f "$bin/tutti"
rm -rf "$home"
if [ "${TUTTI_PURGE:-0}" = "1" ]; then rm -rf "$HOME/.config/tutti"; echo "tutti: config removed"; fi
echo "tutti removed (config kept in ~/.config/tutti unless TUTTI_PURGE=1)"
