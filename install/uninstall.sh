#!/usr/bin/env bash
# Tutti uninstaller: curl -fsSL https://raw.githubusercontent.com/ventura8/Tutti/main/install/uninstall.sh | bash
# Removes the `tutti` command and its install folder. Your config (~/.config/tutti) is kept unless TUTTI_PURGE=1.
set -euo pipefail
home="${TUTTI_HOME:-$HOME/.local/share/tutti}"
bin="${TUTTI_BIN:-$HOME/.local/bin}"
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
config_dir="$config_home/tutti"
rm -f "$bin/tutti"
rm -rf "$home"
if [ "${TUTTI_PURGE:-0}" = "1" ]; then rm -rf "$config_dir"; echo "tutti: config removed"; fi
echo "tutti removed (config kept in $config_dir unless TUTTI_PURGE=1)"
